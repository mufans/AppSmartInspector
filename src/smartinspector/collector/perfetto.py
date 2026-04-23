"""PerfettoCollector: adb collect -> SQL query -> unified JSON."""

import bisect
import json
import os
import subprocess
import tempfile
from dataclasses import dataclass, field
from pathlib import Path

from perfetto.trace_processor import TraceProcessor, TraceProcessorConfig

from smartinspector.perfetto_compat import patch

import logging
logger = logging.getLogger(__name__)

# Apply macOS IPv4 fix
patch()

# Default path to trace_processor_shell
_PROJECT_ROOT = Path(__file__).resolve().parent.parent.parent.parent
SHELL_BIN = _PROJECT_ROOT / "bin" / "trace_processor_shell"


def _parse_siblock_msg(msg: str) -> list[str]:
    """Parse SIBlock logcat message into stack trace frames.

    Input format: "MsgClass|250ms|at com.example.Foo.run(Foo.java:123)|at com.example.Bar.doX(Bar.java:45)"
    Output: ["at com.example.Foo.run(Foo.java:123)", "at com.example.Bar.doX(Bar.java:45)"]
    """
    if not msg:
        return []

    parts = msg.split("|")
    # First two parts are class name and duration, rest are stack frames
    frames = []
    for part in parts[2:]:
        part = part.strip()
        if part and part.startswith("at "):
            frames.append(part)
    return frames


@dataclass
class PerfSummary:
    """Unified performance summary (~2KB JSON)."""
    frame_timeline: dict = field(default_factory=dict)
    cpu_usage: dict = field(default_factory=dict)
    process_memory: dict = field(default_factory=dict)
    cpu_hotspots: list[dict] = field(default_factory=list)
    memory: dict | None = None
    scheduling: dict | None = None
    view_slices: dict = field(default_factory=dict)
    io_slices: dict = field(default_factory=dict)
    metadata: dict = field(default_factory=dict)
    block_events: list[dict] = field(default_factory=list)
    input_events: list[dict] = field(default_factory=list)
    sys_stats: dict = field(default_factory=dict)
    thread_state: list[dict] = field(default_factory=list)

    def to_json(self) -> str:
        return json.dumps(self.__dict__, indent=2, ensure_ascii=False)


class PerfettoCollector:
    """Collect and analyze Android Perfetto traces."""

    def __init__(self, trace_path: str, shell_path: str | None = None,
                 target_process: str | None = None):
        self.trace_path = trace_path
        self.shell_path = shell_path or str(SHELL_BIN)
        self._tp: TraceProcessor | None = None
        self._target_process_cache: dict | None = None  # cached resolve result
        if target_process:
            # Pre-populate cache with package name so resolve can be triggered lazily
            self._target_process_cache = {
                "upid": None, "pid": None, "uid": None,
                "name": target_process, "source": "",
            }

    def _open(self) -> TraceProcessor:
        if self._tp is not None:
            return self._tp
        config = TraceProcessorConfig(
            bin_path=self.shell_path,
            load_timeout=10,
        )
        self._tp = TraceProcessor(trace=self.trace_path, config=config)
        return self._tp

    def _resolve_target_process(self, package_name: str) -> dict:
        """Resolve target process info (upid, pid, uid) from package name.

        Tries ``process`` table first, falls back to ``package_list`` table
        for cold-start scenarios where the process table may be empty.

        Args:
            package_name: Android package name, e.g. "com.example.app"

        Returns:
            Dict with keys: upid, pid, uid, name, source ("process"|"package_list"|"")
        """
        if self._target_process_cache is not None:
            return self._target_process_cache

        result = {"upid": None, "pid": None, "uid": None, "name": package_name, "source": ""}
        tp = self._open()

        # Strategy 1: direct lookup in process table
        try:
            rows = tp.query(f"""
                SELECT upid, pid, uid
                FROM process
                WHERE name = '{package_name}'
                LIMIT 1
            """)
            for r in rows:
                result["upid"] = r.upid
                result["pid"] = r.pid
                result["uid"] = r.uid
                result["source"] = "process"
                break
        except Exception as e:
            logger.debug("process table lookup failed: %s", e)

        # Strategy 2: fallback to package_list -> uid -> process
        if not result["upid"]:
            try:
                uid = None
                pl_rows = tp.query(f"""
                    SELECT uid
                    FROM package_list
                    WHERE package_name = '{package_name}'
                    LIMIT 1
                """)
                for r in pl_rows:
                    uid = r.uid
                    break

                if uid is not None:
                    result["uid"] = uid
                    # Find process by uid
                    proc_rows = tp.query(f"""
                        SELECT upid, pid, name
                        FROM process
                        WHERE uid = {uid}
                        LIMIT 1
                    """)
                    for r in proc_rows:
                        result["upid"] = r.upid
                        result["pid"] = r.pid
                        result["name"] = r.name
                        result["source"] = "package_list"
                        break

                    if not result["upid"]:
                        # package_list found UID but process not in process table yet
                        # (cold start: process hasn't started during trace)
                        result["source"] = "package_list_uid_only"
                        logger.debug("package_list fallback: found uid=%d for %s but no process entry",
                                     uid, package_name)
            except Exception as e:
                logger.debug("package_list fallback failed: %s", e)

        if result["source"]:
            logger.debug("resolved target process: %s -> upid=%s, pid=%s, uid=%s (via %s)",
                         package_name, result["upid"], result["pid"], result["uid"], result["source"])
        else:
            logger.debug("could not resolve target process: %s", package_name)

        self._target_process_cache = result
        return result

    def close(self):
        if self._tp:
            self._tp.close()
            self._tp = None

    def __enter__(self):
        return self

    def __exit__(self, exc_type, exc_val, exc_tb):
        self.close()
        return False

    def collect_sched(self) -> dict:
        """Analyze scheduling data with end_state and blocked reasons."""
        tp = self._open()
        rows = tp.query("""
            SELECT
              thread.name AS comm,
              thread.tid AS tid,
              COUNT(*) AS switches,
              SUM(sched.dur) AS total_dur_ns,
              MODE() WITHIN GROUP (ORDER BY sched.end_state) AS dominant_state
            FROM sched
            JOIN thread ON sched.utid = thread.utid
            GROUP BY thread.name, thread.tid
            ORDER BY switches DESC
            LIMIT 20
        """)
        hot_threads = []
        for r in rows:
            entry = {
                "comm": r.comm,
                "tid": r.tid,
                "switches": r.switches,
                "total_dur_ms": round(r.total_dur_ns / 1e6, 2),
                "dominant_state": r.dominant_state,
            }
            hot_threads.append(entry)

        # Blocked reasons from sched_blocked_reason table
        blocked_reasons: list[dict] = []
        try:
            br_rows = tp.query("""
                SELECT
                  t.name AS comm,
                  br.blocked_reason,
                  br.io_wait,
                  COUNT(*) AS occurrences
                FROM sched_blocked_reason br
                JOIN thread t ON br.utid = t.utid
                GROUP BY t.name, br.blocked_reason, br.io_wait
                ORDER BY occurrences DESC
                LIMIT 10
            """)
            for r in br_rows:
                blocked_reasons.append({
                    "comm": r.comm,
                    "reason": r.blocked_reason,
                    "io_wait": bool(r.io_wait),
                    "occurrences": r.occurrences,
                })
        except Exception as e:
            logger.debug("sched_blocked_reason query failed: %s", e)

        result = {"hot_threads": hot_threads}
        if blocked_reasons:
            result["blocked_reasons"] = blocked_reasons

        return result

    def collect_cpu_hotspots(self) -> list[dict]:
        """Find CPU hotspots with callchain reconstruction."""
        tp = self._open()
        try:
            rows = tp.query("""
                SELECT
                  spf.name AS function_name,
                  t.name AS thread_name,
                  ps.callsite_id,
                  COUNT(*) AS sample_count,
                  SUM(COUNT(*)) OVER () AS total_samples
                FROM perf_sample ps
                JOIN stack_profile_callsite spc ON ps.callsite_id = spc.id
                JOIN stack_profile_frame spf ON spc.frame_id = spf.id
                JOIN thread t ON ps.utid = t.utid
                WHERE ps.callsite_id IS NOT NULL
                GROUP BY spf.name, t.name, ps.callsite_id
                ORDER BY sample_count DESC
                LIMIT 20
            """)
        except Exception as e:
            logger.debug("CPU hotspot query failed: %s", e)
            return []

        if not rows:
            return []

        # Preload callsite -> frame mapping and parent relationships
        callsite_map: dict[int, tuple[str, int | None]] = {}  # id -> (frame_name, parent_id)
        try:
            cs_rows = tp.query("""
                SELECT spc.id, spf.name, spc.parent_id
                FROM stack_profile_callsite spc
                JOIN stack_profile_frame spf ON spc.frame_id = spf.id
            """)
            for r in cs_rows:
                callsite_map[r.id] = (r.name, r.parent_id)
        except Exception as e:
            logger.debug("callsite_map query failed: %s", e)

        hotspots = []
        for r in rows:
            pct = round(r.sample_count / r.total_samples * 100, 1) if r.total_samples else 0

            # Reconstruct callchain (leaf to root)
            callchain = []
            callsite_id = r.callsite_id
            visited = set()
            max_depth = 15
            for _ in range(max_depth):
                if callsite_id is None or callsite_id in visited:
                    break
                visited.add(callsite_id)
                entry = callsite_map.get(callsite_id)
                if entry is None:
                    break
                callchain.append(entry[0])  # frame name
                callsite_id = entry[1]  # parent_id

            hotspots.append({
                "function": r.function_name,
                "thread": r.thread_name,
                "samples": r.sample_count,
                "pct": pct,
                "callchain": callchain,  # [leaf, ..., root]
            })

        return hotspots

    def collect_frame_timeline(self) -> dict:
        """Analyze frame rendering timeline (jank detection).

        Queries actual_frame_timeline_slice grouped by display_frame_token.
        Only counts app surface frames (excludes SurfaceFlinger display frames).
        Uses jank_type from SurfaceFlinger (None = no jank).
        Adds frame_index (1-based) so jank frames can be identified by number.
        Includes expected_dur_ms from expected_frame_timeline_slice for comparison.
        """
        tp = self._open()

        # Build expected timeline lookup: display_frame_token -> expected dur
        expected_map: dict[int, float] = {}
        try:
            exp_rows = tp.query("""
                SELECT
                  display_frame_token,
                  MAX(dur) AS expected_dur_ns
                FROM expected_frame_timeline_slice
                GROUP BY display_frame_token
            """)
            for r in exp_rows:
                expected_map[r.display_frame_token] = round(r.expected_dur_ns / 1e6, 2)
        except Exception as e:
            logger.debug("Expected frame timeline query failed: %s", e)

        try:
            rows = tp.query("""
                SELECT
                  display_frame_token,
                  MIN(ts) AS frame_ts,
                  MAX(dur) AS frame_dur_ns,
                  GROUP_CONCAT(DISTINCT jank_type) AS jank_types,
                  GROUP_CONCAT(DISTINCT layer_name) AS layers
                FROM actual_frame_timeline_slice
                WHERE dur > 0
                  AND surface_frame_token > 0
                GROUP BY display_frame_token
                ORDER BY frame_ts ASC
            """)
        except Exception as e:
            logger.debug("Frame timeline query failed: %s", e)
            return {}

        # User-impacting jank types per Perfetto/SurfaceFlinger docs:
        #   App Deadline Missed       = app missed vsync deadline (real jank)
        #   Dropped Frame             = frame dropped entirely (real jank)
        #   SurfaceFlinger *Deadline  = SF/HAL jank
        #   Display HAL               = display HAL jank
        #   Unknown Jank              = unknown jank
        # NOT user-perceivable:
        #   Buffer Stuffing           = pipeline queued state
        #   Prediction Error          = scheduler drift
        #   None                      = no jank
        USER_JANK_TYPES = {
            "App Deadline Missed", "Dropped Frame",
            "SurfaceFlinger CPU Deadline Missed", "SurfaceFlinger GPU Deadline Missed",
            "SurfaceFlinger Scheduling Delay", "Display HAL",
            "Unknown Jank",
        }

        frames = []
        for r in rows:
            dur_ms = round(r.frame_dur_ns / 1e6, 2)
            all_jank = [j.strip() for j in (r.jank_types or "").split(",") if j.strip() and j.strip() != "None"]
            # Only count user-impacting jank (exclude BufferStuffing, PredictionError)
            jank_list = [j for j in all_jank if j in USER_JANK_TYPES]
            expected_dur = expected_map.get(r.display_frame_token, 0)
            frames.append({
                "ts_ns": r.frame_ts,
                "dur_ms": dur_ms,
                "expected_dur_ms": expected_dur,
                "jank_types": jank_list,
                "layers": (r.layers or ""),
                "is_jank": len(jank_list) > 0,
            })

        if not frames:
            return {"total_frames": 0}

        # Assign frame_index (1-based)
        for i, f in enumerate(frames):
            f["frame_index"] = i + 1

        # Jank = SurfaceFlinger flagged frames (jank_type != None)
        jank = [f for f in frames if f["is_jank"]]

        # FPS = total app frames / time span
        fps = 0.0
        if len(frames) > 1:
            total_s = (frames[-1]["ts_ns"] - frames[0]["ts_ns"]) / 1e9
            if total_s > 0:
                fps = round(len(frames) / total_s, 1)

        # Slowest frames and jank detail (both with frame_index)
        slowest = sorted(frames, key=lambda x: -x["dur_ms"])[:10]
        jank_detail = sorted(jank, key=lambda x: -x["dur_ms"])[:10]

        return {
            "fps": fps,
            "total_frames": len(frames),
            "jank_frames": len(jank),
            "jank_types": list(set(jt for f in jank for jt in f["jank_types"])),
            "slowest_frames": slowest,
            "jank_detail": jank_detail,
        }

    def collect_cpu_usage(self) -> dict:
        """Calculate CPU usage per thread/process from sched data.

        Returns overall CPU % (normalized by core count), per-process/thread
        breakdown, and the number of CPU cores detected.
        """
        tp = self._open()

        # Get trace time bounds from trace_bounds table
        try:
            bounds = tp.query("SELECT start_ts, end_ts FROM trace_bounds")
            for b in bounds:
                trace_start_ns = b.start_ts
                trace_end_ns = b.end_ts
                break
            else:
                return {}
        except Exception as e:
            logger.debug("Trace bounds query failed: %s", e)
            return {}

        trace_dur_ns = trace_end_ns - trace_start_ns
        if trace_dur_ns <= 0:
            return {}

        # Detect CPU core count from sched table
        try:
            cpu_rows = tp.query("SELECT COUNT(DISTINCT cpu) AS num_cpus FROM sched")
            num_cpus = 1
            for cr in cpu_rows:
                num_cpus = max(1, cr.num_cpus)
                break
        except Exception as e:
            logger.debug("CPU count query failed: %s", e)
            num_cpus = 1

        # Per-thread CPU usage from sched table
        try:
            rows = tp.query("""
                SELECT
                  process.name AS process_name,
                  process.pid,
                  thread.name AS thread_name,
                  thread.tid,
                  COUNT(*) AS switches,
                  SUM(sched.dur) AS total_dur_ns
                FROM sched
                JOIN thread ON sched.utid = thread.utid
                JOIN process ON thread.upid = process.upid
                GROUP BY process.name, process.pid, thread.name, thread.tid
                ORDER BY total_dur_ns DESC
                LIMIT 20
            """)
        except Exception as e:
            logger.debug("CPU usage query failed: %s", e)
            return {}

        # Total CPU wall-time available = trace_dur * num_cpus
        total_wall_ns = trace_dur_ns * num_cpus

        # Group by process — skip kernel threads (pid 0 / no process name)
        proc_map: dict[str, dict] = {}
        total_cpu_ns = 0
        for r in rows:
            # Skip kernel idle/swapper threads
            if not r.process_name or r.pid == 0:
                continue
            pname = r.process_name
            dur_ns = r.total_dur_ns or 0
            total_cpu_ns += dur_ns
            pct = round(dur_ns / total_wall_ns * 100, 1)

            if pname not in proc_map:
                proc_map[pname] = {
                    "process": pname,
                    "pid": r.pid,
                    "cpu_pct": 0.0,
                    "threads": [],
                    "_dur_ns": 0,
                }
            proc_map[pname]["_dur_ns"] += dur_ns
            proc_map[pname]["threads"].append({
                "name": r.thread_name or f"tid:{r.tid}",
                "cpu_pct": pct,
                "switches": r.switches,
            })

        # Finalize process-level pct
        top_processes = sorted(proc_map.values(), key=lambda x: -x["_dur_ns"])
        for p in top_processes:
            p["cpu_pct"] = round(p.pop("_dur_ns") / total_wall_ns * 100, 1)

        overall_pct = round(total_cpu_ns / total_wall_ns * 100, 1)

        return {
            "cpu_usage_pct": overall_pct,
            "num_cpus": num_cpus,
            "trace_dur_ms": round(trace_dur_ns / 1e6, 0),
            "top_processes": top_processes,
        }

    def collect_sys_stats(self) -> dict:
        """Collect system-level CPU stats from linux.sys_stats data source.

        Queries cpu_counter_track / counter tables for system-wide CPU usage
        and frequency data. This data is collected when linux.sys_stats is
        configured in pull_trace_from_device (stat_period_ms, cpufreq_period_ms).
        """
        tp = self._open()

        result: dict = {}

        # 1. System CPU idle time samples
        try:
            cpu_rows = tp.query("""
                SELECT
                  c.ts,
                  c.value AS cpu_util
                FROM counter c
                JOIN cpu_counter_track cct ON c.track_id = cct.id
                WHERE cct.name = 'cpuidle_time'
                ORDER BY c.ts ASC
            """)
            samples = [{"ts_ns": r.ts, "value": r.cpu_util} for r in cpu_rows]
            if samples:
                result["cpu_idle_samples"] = samples
        except Exception as e:
            logger.debug("CPU idle samples query failed: %s", e)

        # 2. CPU frequency per core
        try:
            freq_rows = tp.query("""
                SELECT
                  cct.cpu,
                  c.ts,
                  c.value AS freq_khz
                FROM counter c
                JOIN cpu_counter_track cct ON c.track_id = cct.id
                WHERE cct.name = 'cpufreq'
                ORDER BY cct.cpu, c.ts ASC
            """)
            freq_by_core: dict[int, list] = {}
            for r in freq_rows:
                freq_by_core.setdefault(r.cpu, []).append({
                    "ts_ns": r.ts,
                    "freq_khz": r.freq_khz,
                })
            if freq_by_core:
                result["cpu_freq_by_core"] = freq_by_core
        except Exception as e:
            logger.debug("CPU frequency query failed: %s", e)

        # 3. Fork rate
        try:
            fork_rows = tp.query("""
                SELECT
                  c.ts,
                  c.value AS fork_count
                FROM counter c
                JOIN cpu_counter_track cct ON c.track_id = cct.id
                WHERE cct.name = 'num_forks'
                ORDER BY c.ts ASC
            """)
            forks = [{"ts_ns": r.ts, "forks": r.fork_count} for r in fork_rows]
            if forks:
                result["fork_rate"] = forks
        except Exception as e:
            logger.debug("Fork rate query failed: %s", e)

        return result

    def collect_process_memory(self) -> dict:
        """Collect process-level memory stats from process_counter_track.

        Perfetto stores per-process memory (RSS, anon, etc.) as counter tracks
        when proc_stats_poll_ms is configured. Values are in KB.
        """
        tp = self._open()

        try:
            # Pivot: one row per process, columns for avg/max RSS and anon
            rows = tp.query("""
                SELECT
                  p.name,
                  p.pid,
                  AVG(CASE WHEN pct.name = 'mem.rss' THEN c.value END) AS avg_rss_kb,
                  MAX(CASE WHEN pct.name = 'mem.rss' THEN c.value END) AS max_rss_kb,
                  AVG(CASE WHEN pct.name = 'mem.rss.anon' THEN c.value END) AS avg_anon_kb,
                  MAX(CASE WHEN pct.name = 'mem.rss.anon' THEN c.value END) AS max_anon_kb
                FROM process_counter_track pct
                JOIN counter c ON c.track_id = pct.id
                JOIN process p ON pct.upid = p.upid
                WHERE pct.name IN ('mem.rss', 'mem.rss.anon')
                GROUP BY p.name, p.pid
                ORDER BY max_rss_kb DESC
                LIMIT 10
            """)
            processes = []
            for r in rows:
                entry = {"name": r.name, "pid": r.pid}
                if r.max_rss_kb is not None:
                    # Counter values are in bytes, convert to KB
                    entry["rss_kb"] = round(r.max_rss_kb / 1024)
                    entry["avg_rss_kb"] = round(r.avg_rss_kb / 1024)
                if r.max_anon_kb is not None:
                    entry["rss_anon_kb"] = round(r.max_anon_kb / 1024)
                    entry["avg_anon_kb"] = round(r.avg_anon_kb / 1024)
                if entry.get("rss_kb") or entry.get("rss_anon_kb"):
                    processes.append(entry)
            if processes:
                return {"processes": processes}
        except Exception as e:
            logger.debug("Process memory query failed: %s", e)

        return {}

    def collect_memory(self) -> dict:
        """Collect Java heap memory from android.java_hprof data."""
        tp = self._open()
        try:
            rows = tp.query("""
                SELECT
                  c.name AS class_name,
                  COUNT(*) AS obj_count,
                  SUM(o.self_size) AS total_bytes
                FROM heap_graph_object o
                JOIN heap_graph_class c ON o.type_id = c.id
                WHERE o.reachable = 1
                GROUP BY c.name
                ORDER BY total_bytes DESC
                LIMIT 15
            """)
        except Exception as e:
            # heap_graph tables may not exist if no Java heap dump
            logger.debug("Heap graph query failed: %s", e)
            return {}

        allocs = []
        for r in rows:
            allocs.append({
                "class_name": r.class_name,
                "obj_count": r.obj_count,
                "total_size_kb": round(r.total_bytes / 1024, 1),
            })
        return {"heap_graph_classes": allocs}

    def collect_threads(self) -> list[dict]:
        """Collect thread info."""
        tp = self._open()
        rows = tp.query("SELECT tid, name FROM thread ORDER BY tid")
        threads = []
        for r in rows:
            if r.name:
                threads.append({"tid": r.tid, "name": r.name})
        return threads

    def collect_view_slices(self) -> dict:
        """Collect View-level slice data: doFrame, measure, layout, draw, RV events.

        Captures both SI$ custom TraceHook tags AND system atrace tags:
        - SI$RV#[viewId]#[Adapter].[method]  (RecyclerView pipeline — custom)
        - SI$Activity.lifecycle methods       (custom)
        - SI$Fragment.lifecycle methods       (custom)
        - SI$inflate#[layout]#[parent]        (LayoutInflater — custom)
        - SI$view#[class].[method]            (View traverse — custom)
        - SI$handler#[msg_class]              (Handler dispatch — custom)
        - System tags: doFrame, Choreographer, etc. (not prefixed)
        """
        tp = self._open()
        try:
            # Step 1: Get all SI$ slices (excluding IO tags: net/db/img) + system slices
            rows = tp.query("""
                SELECT
                  id,
                  name,
                  ts,
                  dur,
                  depth,
                  parent_id,
                  track_id
                FROM slice
                WHERE (name LIKE 'SI$%'
                       AND name NOT LIKE 'SI$net#%'
                       AND name NOT LIKE 'SI$db#%'
                       AND name NOT LIKE 'SI$img#%'
                       AND name NOT LIKE 'SI$touch#%')
                   OR name LIKE '%doFrame%'
                   OR name LIKE '%Choreographer%'
                   OR name LIKE '%Traversal%'
                   OR name LIKE '%performDraw%'
                   OR name LIKE '%performMeasure%'
                   OR name LIKE '%performLayout%'
                ORDER BY ts ASC
            """)

            # Step 2: Collect parent_ids that are not in result set, fetch them
            rows = list(rows)  # materialize iterator
            slice_ids_in_set = {r.id for r in rows}
            missing_parent_ids = set()
            for r in rows:
                if r.parent_id and r.parent_id not in slice_ids_in_set:
                    missing_parent_ids.add(r.parent_id)

            if missing_parent_ids:
                id_list = ",".join(str(pid) for pid in missing_parent_ids)
                try:
                    parent_rows = tp.query(f"""
                        SELECT id, name, ts, dur, depth, parent_id, track_id
                        FROM slice
                        WHERE id IN ({id_list})
                    """)
                    # Merge into main results, but exclude touch# slices that leak via parent fetch
                    filtered_parents = [r for r in parent_rows if not r.name.startswith("SI$touch#")]
                    rows = list(rows) + filtered_parents
                    for r in filtered_parents:
                        slice_ids_in_set.add(r.id)
                        # Check if these parents also have missing parents (go up one more level)
                        if r.parent_id and r.parent_id not in slice_ids_in_set:
                            missing_parent_ids.add(r.parent_id)

                    # One more level up for grandparents
                    level2_parents = set()
                    for r in parent_rows:
                        if r.parent_id and r.parent_id not in slice_ids_in_set:
                            level2_parents.add(r.parent_id)
                    if level2_parents:
                        id_list2 = ",".join(str(pid) for pid in level2_parents)
                        try:
                            gp_rows = tp.query(f"""
                                SELECT id, name, ts, dur, depth, parent_id, track_id
                                FROM slice
                                WHERE id IN ({id_list2})
                            """)
                            rows = list(rows) + [r for r in gp_rows if not r.name.startswith("SI$touch#")]
                        except Exception as e:
                            logger.debug("Grandparent slice query failed: %s", e)
                except Exception as e:
                    logger.debug("Parent slice query failed: %s", e)
        except Exception as e:
            logger.debug("View slices query failed: %s", e)
            return {}

        slices = []
        slice_by_id: dict[int, dict] = {}
        for r in rows:
            dur_ms = round(r.dur / 1e6, 2) if r.dur else 0
            is_custom = r.name.startswith("SI$")
            s = {
                "id": r.id,
                "name": r.name,
                "ts_ns": r.ts,
                "dur_ms": dur_ms,
                "depth": r.depth,
                "parent_id": r.parent_id,
                "is_custom": is_custom,
            }
            slices.append(s)
            slice_by_id[r.id] = s

        if not slices:
            return {}

        # ---- Jank hotspot: slowest individual slices (max 30) ----
        # Only include SI$ custom slices — system slices (doFrame etc.) waste slots
        slowest = sorted(
            [s for s in slices if s["is_custom"]],
            key=lambda x: -x["dur_ms"],
        )[:30]

        # ---- Aggregate by slice name ----
        name_stats: dict[str, dict] = {}
        for s in slices:
            n = s["name"]
            if n not in name_stats:
                name_stats[n] = {"name": n, "count": 0, "total_ms": 0.0, "max_ms": 0.0, "is_custom": s["is_custom"]}
            name_stats[n]["count"] += 1
            name_stats[n]["total_ms"] += s["dur_ms"]
            name_stats[n]["max_ms"] = max(name_stats[n]["max_ms"], s["dur_ms"])

        # ---- RV instance grouping (SI$RV#[viewId]#[Adapter]) ----
        rv_instances: dict[str, dict] = {}
        for s in slices:
            n = s["name"]
            # Match both SI$RV#... and legacy RV#... tags
            rv_prefix = None
            if n.startswith("SI$RV#"):
                rv_prefix = "SI$"
            elif n.startswith("RV#"):
                rv_prefix = ""
            if rv_prefix is None:
                continue
            # Strip SI$ prefix for parsing
            tag_body = n[len(rv_prefix):]  # e.g. "RV#viewId#com.example.Adapter.method"
            # Parse: RV#viewId#Adapter.method — Adapter may contain dots (FQN)
            # Find the last dot after the last # to split adapter from method
            last_hash = tag_body.rfind("#")
            after_hash = tag_body[last_hash + 1:] if last_hash >= 0 else tag_body
            last_dot = after_hash.rfind(".")
            if last_dot >= 0:
                instance_key = tag_body[:last_hash + 1] + after_hash[:last_dot]
                method = after_hash[last_dot + 1:]
            else:
                instance_key = tag_body
                method = "unknown"
            if instance_key not in rv_instances:
                rv_instances[instance_key] = {
                    "instance": instance_key,
                    "total_ms": 0.0,
                    "count": 0,
                    "methods": {},
                    "max_dur_ms": 0.0,
                    "max_dur_method": "",
                }
            inst = rv_instances[instance_key]
            inst["total_ms"] += s["dur_ms"]
            inst["count"] += 1
            if s["dur_ms"] > inst["max_dur_ms"]:
                inst["max_dur_ms"] = s["dur_ms"]
                inst["max_dur_method"] = method
            if method not in inst["methods"]:
                inst["methods"][method] = {"count": 0, "total_ms": 0.0, "max_ms": 0.0}
            m = inst["methods"][method]
            m["count"] += 1
            m["total_ms"] += s["dur_ms"]
            m["max_ms"] = max(m["max_ms"], s["dur_ms"])

        rv_sorted = sorted(rv_instances.values(), key=lambda x: -x["total_ms"])

        # ---- Call chain: reconstruct parent chain + child breakdown ----

        # Build child lookup: parent_id -> list of children
        children_map: dict[int, list[dict]] = {}
        for s in slices:
            pid = s.get("parent_id")
            if pid:
                if pid not in children_map:
                    children_map[pid] = []
                children_map[pid].append(s)

        def _build_chain(slice_id: int) -> list[str]:
            """Walk up from a slice to its root ancestor via parent_id.
            Returns the chain bottom-up: [leaf, ..., root].
            """
            chain = []
            visited = set()
            current_id = slice_id
            while current_id and current_id not in visited:
                visited.add(current_id)
                current = slice_by_id.get(current_id)
                if not current:
                    break
                chain.append(f"{current['name']} [{current['dur_ms']:.2f}ms]")
                current_id = current.get("parent_id")
            return chain

        def _get_children_breakdown(parent_id: int) -> list[dict]:
            """Get direct children sorted by dur desc, with their own breakdown."""
            kids = children_map.get(parent_id, [])
            # Sort by dur descending, take top children
            kids_sorted = sorted(kids, key=lambda x: -x["dur_ms"])
            result = []
            seen_methods = set()
            for k in kids_sorted:
                name = k["name"]
                # Deduplicate by name — keep the slowest instance
                if name in seen_methods:
                    continue
                seen_methods.add(name)
                entry = {"name": name, "dur_ms": k["dur_ms"]}
                # Recurse one level for sub-children
                sub = _get_children_breakdown(k["id"])
                if sub:
                    entry["children"] = sub
                result.append(entry)
            return result

        # Build call chains for the top 10 slowest custom (SI$) slices
        slowest_custom = sorted(
            [s for s in slices if s["is_custom"] and s["dur_ms"] >= 1.0],
            key=lambda x: -x["dur_ms"],
        )[:10]

        call_chains = []
        for s in slowest_custom:
            raw_chain = _build_chain(s["id"])
            # Get child breakdown for the slice itself
            breakdown = _get_children_breakdown(s["id"])
            call_chains.append({
                "name": s["name"],
                "dur_ms": s["dur_ms"],
                "chain": list(reversed(raw_chain)),
                "breakdown": breakdown,
            })

        # ---- P1-5: Annotate slices with target process info ----
        target_process_info = {}
        # Extract target process from metadata if available
        target_pkg = self._target_process_cache.get("name", "") if self._target_process_cache else ""
        if target_pkg:
            target_process_info = self._resolve_target_process(target_pkg)
        elif self._target_process_cache is None:
            # Try to detect target process from slowest slices
            # SI$ slices contain class names that include the package name
            for s in slowest[:3]:
                name = s.get("name", "")
                if name.startswith("SI$"):
                    # e.g. SI$com.example.app.Class.method
                    body = name[3:]
                    # Extract potential package from class name
                    parts = body.split(".")
                    if len(parts) >= 3:
                        candidate_pkg = ".".join(parts[:3])
                        info = self._resolve_target_process(candidate_pkg)
                        if info.get("upid"):
                            target_process_info = info
                            break

        # Annotate slowest slices with their process name (via track_id)
        if target_process_info.get("upid"):
            target_upid = target_process_info["upid"]
            try:
                # Build track_id -> upid mapping for the slowest slices
                track_ids = set(s.get("track_id") for s in slowest if s.get("track_id"))
                if track_ids:
                    id_list = ",".join(str(tid) for tid in track_ids)
                    track_proc_map = {}
                    for r in tp.query(f"""
                        SELECT t.id AS track_id, p.upid, p.name AS process_name
                        FROM thread_track t
                        JOIN thread th ON t.utid = th.utid
                        JOIN process p ON th.upid = p.upid
                        WHERE t.id IN ({id_list})
                    """):
                        track_proc_map[r.track_id] = {"upid": r.upid, "name": r.process_name}

                    for s in slowest:
                        proc_info = track_proc_map.get(s.get("track_id"))
                        if proc_info:
                            s["process_name"] = proc_info["name"]
                            s["is_target"] = proc_info["upid"] == target_upid
            except Exception as e:
                logger.debug("track-process annotation failed: %s", e)

        result = {
            "summary": sorted(name_stats.values(), key=lambda x: -x["total_ms"]),
            "slowest_slices": slowest,
            "rv_instances": rv_sorted,
            "call_chains": call_chains,
        }

        # Include target process resolution info for downstream consumers
        if target_process_info:
            result["target_process"] = target_process_info

        return result

    def collect_io_slices(self) -> dict:
        """Collect IO slices (SI$net#/SI$db#/SI$img#) from all threads.

        These are NOT main-thread slices — they run on background/IO threads.
        Kept separate from view_slices to avoid polluting main-thread analysis.
        """
        tp = self._open()
        try:
            rows = tp.query("""
                SELECT name, ts, dur, depth, track_id
                FROM slice
                WHERE name LIKE 'SI$net#%'
                   OR name LIKE 'SI$db#%'
                   OR name LIKE 'SI$img#%'
                ORDER BY ts ASC
            """)
        except Exception as e:
            logger.debug("IO slices query failed: %s", e)
            return {}

        slices = []
        name_stats: dict[str, dict] = {}
        for r in rows:
            dur_ms = round(r.dur / 1e6, 2) if r.dur else 0
            name = r.name
            slices.append({
                "name": name,
                "ts_ns": r.ts,
                "dur_ms": dur_ms,
                "depth": r.depth,
            })
            # Determine IO type from prefix
            body = name[3:] if name.startswith("SI$") else name
            io_type = "unknown"
            if body.startswith("net#"):
                io_type = "network"
            elif body.startswith("db#"):
                io_type = "database"
            elif body.startswith("img#"):
                io_type = "image"

            if name not in name_stats:
                name_stats[name] = {
                    "name": name,
                    "io_type": io_type,
                    "count": 0,
                    "total_ms": 0.0,
                    "max_ms": 0.0,
                }
            name_stats[name]["count"] += 1
            name_stats[name]["total_ms"] += dur_ms
            name_stats[name]["max_ms"] = max(name_stats[name]["max_ms"], dur_ms)

        if not slices:
            return {}

        return {
            "total_count": len(slices),
            "summary": sorted(name_stats.values(), key=lambda x: -x["total_ms"]),
            "slowest": sorted(slices, key=lambda x: -x["dur_ms"])[:20],
        }

    def collect_input_events(self) -> list[dict]:
        """Collect touch input events from SI$touch# slices.

        Tag format: SI$touch#ActivityName#ACTION (e.g. SI$touch#MainActivity#DOWN)
        These are correlated with jank frames to establish input→jank causality.
        """
        tp = self._open()
        try:
            rows = tp.query("""
                SELECT name, ts, dur
                FROM slice
                WHERE name LIKE 'SI$touch#%'
                ORDER BY ts ASC
            """)
        except Exception as e:
            logger.debug("Input events query failed: %s", e)
            return []

        events = []
        for r in rows:
            name = r.name
            # Parse: SI$touch#ActivitySimpleName#ACTION
            body = name[len("SI$touch#"):]
            parts = body.split("#")
            activity = parts[0] if len(parts) >= 1 else "?"
            action = parts[1] if len(parts) >= 2 else "UNKNOWN"
            dur_ms = round(r.dur / 1e6, 2) if r.dur else 0
            events.append({
                "ts_ns": r.ts,
                "dur_ms": dur_ms,
                "activity": activity,
                "action": action,
                "raw_name": name,
            })

        return events

    def collect_block_events(self) -> list[dict]:
        """Collect block events from SI$block# slices + SIBlock logcat stacks.

        SI$block# slices come from BlockMonitor's Trace.beginSection (via atrace).
        The Perfetto 'dur' is ~0 because beginSection/endSection are called
        back-to-back as a marker.  The REAL duration is embedded in the slice
        name:  SI$block#MsgClass#250ms  →  250ms.

        SIBlock logcat entries come from BlockMonitor's Log.w (via android.log).
        These are correlated by timestamp to attach stack traces.
        """
        tp = self._open()

        # 1. Query SI$block# slices
        try:
            slice_rows = tp.query("""
                SELECT name, ts, dur
                FROM slice
                WHERE name LIKE 'SI$block#%'
                ORDER BY ts ASC
            """)
        except Exception as e:
            logger.debug("Block events query failed: %s", e)
            return []

        block_slices = []
        for r in slice_rows:
            name = r.name
            # Extract real duration from name suffix (#NNNms)
            # Name format: SI$block#com.example.Worker$1.run#250ms
            # May be truncated by atrace (~127 chars): SI$block#com.exampl....#25
            dur_ms = 0.0
            if "#" in name:
                last_hash = name.rfind("#")
                suffix = name[last_hash + 1:]
                # Try: "250ms", "250m" (truncated), "250" (very truncated)
                dur_str = suffix
                if suffix.endswith("ms"):
                    dur_str = suffix[:-2]
                elif suffix.endswith("m"):
                    dur_str = suffix[:-1]
                try:
                    dur_ms = float(dur_str)
                except ValueError:
                    pass
            # Fallback: use Perfetto dur if name parsing failed
            if dur_ms == 0 and r.dur:
                dur_ms = round(r.dur / 1e6, 2)

            block_slices.append({
                "raw_name": name,
                "ts_ns": r.ts,
                "dur_ms": dur_ms,
            })

        if not block_slices:
            return []

        # 2. Query SIBlock logcat entries for stack traces
        log_entries: list[dict] = []
        try:
            log_rows = tp.query("""
                SELECT ts, msg
                FROM android_logs
                WHERE tag = 'SIBlock'
                ORDER BY ts ASC
            """)
            for r in log_rows:
                log_entries.append({
                    "ts_ns": r.ts,
                    "msg": r.msg or "",
                })
        except Exception as e:
            logger.debug("SIBlock logcat query failed: %s", e)

        # 3. Correlate slices with log entries by timestamp (bisect, O(n log n + m log m))
        MATCH_WINDOW_NS = 500_000_000  # 500ms

        if log_entries:
            log_ts_list = sorted(
                [(log["ts_ns"], log) for log in log_entries],
                key=lambda x: x[0],
            )
            log_timestamps = [t for t, _ in log_ts_list]

            for block in block_slices:
                block_ts = block["ts_ns"]
                idx = bisect.bisect_left(log_timestamps, block_ts)
                best_match = None
                best_dist = MATCH_WINDOW_NS + 1

                # Check idx and idx-1 as candidates
                for candidate_idx in (idx - 1, idx):
                    if 0 <= candidate_idx < len(log_ts_list):
                        dist = abs(log_ts_list[candidate_idx][0] - block_ts)
                        if dist < best_dist:
                            best_dist = dist
                            best_match = log_ts_list[candidate_idx][1]

                if best_match and best_dist <= MATCH_WINDOW_NS:
                    block["stack_trace"] = _parse_siblock_msg(best_match["msg"])
                else:
                    block["stack_trace"] = []
        else:
            for block in block_slices:
                block["stack_trace"] = []

        return block_slices

    def collect_thread_state(self) -> list[dict]:
        """Analyze per-slice thread state distribution (Running/S/D).

        For each SI$ slow slice, queries the thread_state table to determine
        how much time the thread spent in each state (Running, S, D, etc.)
        during the slice's execution window. This helps distinguish "code is
        slow" (Running) from "thread is blocked/suspended" (S/D).

        Returns a list of dicts with:
          - slice_name: the SI$ slice name
          - dur_ms: total slice duration
          - state_distribution: {state: percentage} e.g. {"Running": 85.2, "S": 14.8}
          - dominant_state: the state with the highest percentage
        """
        tp = self._open()

        # First, get main thread utid
        try:
            main_thread_rows = tp.query("""
                SELECT utid FROM thread WHERE name = 'main' LIMIT 1
            """)
            main_utid = None
            for r in main_thread_rows:
                main_utid = r.utid
                break
            if main_utid is None:
                return []
        except Exception as e:
            logger.debug("thread_state: main thread query failed: %s", e)
            return []

        # Get SI$ slow slices (top 20 by duration)
        try:
            slice_rows = tp.query("""
                SELECT name, ts, dur
                FROM slice
                WHERE name LIKE 'SI$%'
                  AND name NOT LIKE 'SI$net#%'
                  AND name NOT LIKE 'SI$db#%'
                  AND name NOT LIKE 'SI$img#%'
                  AND name NOT LIKE 'SI$touch#%'
                  AND dur > 1000000
                ORDER BY dur DESC
                LIMIT 20
            """)
        except Exception as e:
            logger.debug("thread_state: slice query failed: %s", e)
            return []

        results = []
        for sr in slice_rows:
            slice_ts = sr.ts
            slice_dur = sr.dur
            slice_name = sr.name
            dur_ms = round(slice_dur / 1e6, 2)

            if dur_ms < 1.0:
                continue

            # Query thread_state overlapping the slice window on main thread.
            # Use overlap-based calculation: find all thread_state entries that
            # overlap with the slice and compute exact overlap duration per state.
            # This handles entries that straddle slice boundaries (very common for
            # long Running states during active execution).
            slice_end = slice_ts + slice_dur
            try:
                state_rows = tp.query(f"""
                    SELECT
                      state,
                      SUM(
                        MIN(
                          CASE WHEN dur < 0 THEN {slice_end} ELSE ts + dur END,
                          {slice_end}
                        ) -
                        MAX(ts, {slice_ts})
                      ) AS state_dur_ns
                    FROM thread_state
                    WHERE utid = {main_utid}
                      AND ts < {slice_end}
                      AND (dur < 0 OR ts + dur > {slice_ts})
                    GROUP BY state
                    ORDER BY state_dur_ns DESC
                """)

                state_dist = {}
                total_state_ns = 0
                for st in state_rows:
                    ns = st.state_dur_ns or 0
                    total_state_ns += ns
                    # Normalize state names
                    state_name = st.state
                    if state_name in ("R", "R+"):
                        state_name = "Running"
                    elif state_name in ("S", "S+"):
                        state_name = "Sleeping"
                    elif state_name in ("D", "D+"):
                        state_name = "DiskSleep"
                    state_dist[state_name] = state_dist.get(state_name, 0) + ns

                # Convert to percentages
                if total_state_ns > 0:
                    pct_dist = {
                        k: round(v / total_state_ns * 100, 1)
                        for k, v in state_dist.items()
                    }
                else:
                    pct_dist = state_dist

                dominant = max(pct_dist, key=pct_dist.get) if pct_dist else "unknown"

                results.append({
                    "slice_name": slice_name,
                    "dur_ms": dur_ms,
                    "state_distribution": pct_dist,
                    "dominant_state": dominant,
                })
            except Exception as e:
                logger.debug("thread_state: state query failed for %s: %s", slice_name, e)
                results.append({
                    "slice_name": slice_name,
                    "dur_ms": dur_ms,
                    "state_distribution": {},
                    "dominant_state": "unknown",
                })

        return results

    def _diagnose_tables(self) -> dict:
        """Check which key tables have data, for diagnosing empty results."""
        tp = self._open()
        checks = {
            "perf_sample": "SELECT COUNT(*) as c FROM perf_sample",
            "heap_graph_object": "SELECT COUNT(*) as c FROM heap_graph_object",
            "actual_frame_timeline_slice": "SELECT COUNT(*) as c FROM actual_frame_timeline_slice",
            "sched": "SELECT COUNT(*) as c FROM sched",
            "package_list": "SELECT COUNT(*) as c FROM package_list",
        }
        result = {}
        for table, sql in checks.items():
            try:
                rows = tp.query(sql)
                for r in rows:
                    result[table] = r.c
                    break
                else:
                    result[table] = 0
            except Exception as e:
                logger.debug("Table %s query failed: %s", table, e)
                result[table] = -1  # table doesn't exist
        return result

    def summarize(self) -> PerfSummary:
        """Run all analyses and return a unified summary."""
        summary = PerfSummary()

        # Metadata
        tp = self._open()
        try:
            meta = tp.query("SELECT key, str_value FROM metadata")
            for r in meta:
                summary.metadata[r.key] = r.str_value
        except Exception as e:
            logger.debug("Metadata query failed: %s", e)

        # Table diagnosis — help understand why data may be missing
        try:
            diag = self._diagnose_tables()
            summary.metadata["table_stats"] = diag
            # Build human-readable notes
            notes = []
            if diag.get("perf_sample", -1) <= 0:
                notes.append("CPU profiling (linux.perf): no data. Need target_process for callstack sampling.")
            if diag.get("heap_graph_object", -1) <= 0:
                notes.append("Java heap (android.java_hprof): no data. Need target_process for heap dump.")
            if diag.get("actual_frame_timeline_slice", -1) <= 0:
                notes.append("Frame timeline: no data. Device may not support SurfaceFlinger jank tracking.")
            if diag.get("package_list", -1) < 0:
                notes.append("package_list table not available. Cold-start process resolution disabled.")
            if notes:
                summary.metadata["diagnosis"] = notes
        except Exception as e:
            logger.debug("Table diagnosis failed: %s", e)

        # P1-5: Resolve target process with package_list fallback for cold-start support
        if self._target_process_cache and self._target_process_cache.get("name"):
            resolved = self._resolve_target_process(self._target_process_cache["name"])
            if resolved.get("source"):
                summary.metadata["target_process"] = resolved
                logger.debug("target process resolved via %s: %s", resolved["source"], resolved)

        # Scheduling
        try:
            summary.scheduling = self.collect_sched()
        except Exception as e:
            summary.scheduling = {"error": str(e)}

        # CPU hotspots
        try:
            summary.cpu_hotspots = self.collect_cpu_hotspots()
        except Exception as e:
            summary.cpu_hotspots = [{"error": str(e)}]

        # CPU usage (from sched)
        try:
            summary.cpu_usage = self.collect_cpu_usage()
        except Exception as e:
            summary.cpu_usage = {"error": str(e)}

        # Frame timeline
        try:
            summary.frame_timeline = self.collect_frame_timeline()
        except Exception as e:
            summary.frame_timeline = {"error": str(e)}

        # Process-level memory (RSS/PSS)
        try:
            summary.process_memory = self.collect_process_memory()
        except Exception as e:
            summary.process_memory = {"error": str(e)}

        # Heap graph memory (requires target_process)
        try:
            summary.memory = self.collect_memory()
        except Exception as e:
            summary.memory = {"error": str(e)}

        # View slices (doFrame, measure, layout, draw, RV events)
        try:
            summary.view_slices = self.collect_view_slices()
        except Exception as e:
            summary.view_slices = {"error": str(e)}

        # Block events (SI$block# slices + SIBlock logcat stacks)
        try:
            summary.block_events = self.collect_block_events()
        except Exception as e:
            summary.block_events = [{"error": str(e)}]

        # IO slices (SI$net#/SI$db#/SI$img# — all threads, not main-thread specific)
        try:
            summary.io_slices = self.collect_io_slices()
        except Exception as e:
            summary.io_slices = {"error": str(e)}

        # Input events (SI$touch# — touch event correlation with jank)
        try:
            summary.input_events = self.collect_input_events()
        except Exception as e:
            summary.input_events = [{"error": str(e)}]

        # System-level stats (CPU idle, frequency, fork rate)
        try:
            sys_stats = self.collect_sys_stats()
            if sys_stats:
                summary.sys_stats = sys_stats
        except Exception as e:
            logger.debug("sys_stats collection failed: %s", e)

        # Thread state analysis (Running/S/D per SI$ slice)
        try:
            summary.thread_state = self.collect_thread_state()
            logger.debug("thread_state: collected %d entries", len(summary.thread_state))
            if not summary.thread_state:
                # Diagnose why thread_state is empty
                try:
                    tp = self._open()
                    ts_count = 0
                    for r in tp.query("SELECT COUNT(*) as c FROM thread_state"):
                        ts_count = r.c
                        break
                    ts_main = 0
                    for r in tp.query("SELECT COUNT(*) as c FROM thread_state WHERE utid IN (SELECT utid FROM thread WHERE name = 'main')"):
                        ts_main = r.c
                        break
                    logger.debug("thread_state diagnosis: total=%d, main_thread=%d", ts_count, ts_main)
                except Exception as e2:
                    logger.debug("thread_state diagnosis failed: %s", e2)
        except Exception as e:
            logger.debug("thread_state collection failed: %s", e)

        return summary

    @staticmethod
    def pull_trace_from_device(
        output_path: str | None = None,
        duration_ms: int = 10000,
        categories: list[str] | None = None,
        target_process: str | None = None,
        buffer_size_kb: int = 65536,
        cpu_sampling_interval_ms: int = 1,
        collect_cpu_callstacks: bool = True,
        collect_java_heap: bool = True,
    ) -> str:
        """Pull a Perfetto trace from connected Android device via adb.

        Args:
            output_path: Local path to save the trace. Defaults to temp file.
            duration_ms: Trace duration in milliseconds.
            categories: Ftrace/atrace categories to enable.
            target_process: Target app package name for CPU/memory profiling,
                            e.g. "com.example.myapp". When set, enables CPU
                            callstack profiling and Java heap profiling.
            buffer_size_kb: Main buffer size in KB.
            cpu_sampling_interval_ms: CPU sampling interval in ms (1-10).
            collect_cpu_callstacks: Enable CPU callstack profiling (requires target_process).
            collect_java_heap: Enable Java heap profiling (requires target_process).

        Returns:
            Path to the downloaded trace file.
        """
        if output_path is None:
            fd, output_path = tempfile.mkstemp(suffix=".pb")
            os.close(fd)

        device_path = "/data/misc/perfetto-traces/smartinspector_trace.pb"

        default_categories = [
            "sched", "freq", "idle", "power", "memreclaim",
            "gfx", "view", "input", "dalvik", "am", "wm",
        ]
        cats = ",".join(categories or default_categories)

        # Build Perfetto textproto config
        config_lines = [
            f"duration_ms: {duration_ms}",
            f"buffers: {{ size_kb: {buffer_size_kb} fill_policy: DISCARD }}",
            "buffers: { size_kb: 4096 fill_policy: DISCARD }",
            "",
            "# Ftrace: scheduling + power + atrace",
            "data_sources: {",
            "  config {",
            '    name: "linux.ftrace"',
            "    ftrace_config {",
            '      ftrace_events: "sched/sched_process_exit"',
            '      ftrace_events: "sched/sched_process_free"',
            '      ftrace_events: "task/task_newtask"',
            '      ftrace_events: "task/task_rename"',
            '      ftrace_events: "sched/sched_switch"',
            '      ftrace_events: "power/suspend_resume"',
            '      ftrace_events: "sched/sched_blocked_reason"',
            '      ftrace_events: "sched/sched_wakeup"',
            '      ftrace_events: "sched/sched_wakeup_new"',
            '      ftrace_events: "sched/sched_waking"',
            '      ftrace_events: "power/cpu_frequency"',
            '      ftrace_events: "power/cpu_idle"',
            '      ftrace_events: "ftrace/print"',
            f'      atrace_categories: "{cats}"',
            '      atrace_apps: "*"',
            "      symbolize_ksyms: true",
            "      disable_generic_events: true",
            "    }",
            "  }",
            "}",
            "",
            "# Process stats for names, grouping, and memory (RSS/PSS)",
            "data_sources: {",
            "  config {",
            '    name: "linux.process_stats"',
            "    process_stats_config {",
            "      scan_all_processes_on_start: true",
            "      proc_stats_poll_ms: 2000",
            "    }",
            "  }",
            "}",
            "",
            "# System CPU/memory stats",
            "data_sources: {",
            "  config {",
            '    name: "linux.sys_stats"',
            "    sys_stats_config {",
            "      stat_period_ms: 1000",
            "      stat_counters: STAT_CPU_TIMES",
            "      stat_counters: STAT_FORK_COUNT",
            "      cpufreq_period_ms: 1000",
            "    }",
            "  }",
            "}",
            "",
            "# Android logcat events",
            "data_sources: {",
            "  config {",
            '    name: "android.log"',
            "  }",
            "}",
            "",
            "# Frame timeline from SurfaceFlinger",
            "data_sources: {",
            "  config {",
            '    name: "android.surfaceflinger.frametimeline"',
            "  }",
            "}",
        ]

        # CPU callstack profiling (requires target_process)
        if target_process and collect_cpu_callstacks:
            cpu_freq = max(1, 1000 // cpu_sampling_interval_ms)  # ms → Hz
            config_lines += [
                "",
                "# CPU callstack profiling",
                "data_sources: {",
                "  config {",
                '    name: "linux.perf"',
                "    perf_event_config {",
                "      timebase {",
                f"        frequency: {cpu_freq}",
                "        timestamp_clock: PERF_CLOCK_MONOTONIC",
                "      }",
                "      callstack_sampling {",
                "        scope {",
                f'          target_cmdline: "{target_process}"',
                "        }",
                "        kernel_frames: true",
                "      }",
                "    }",
                "  }",
                "}",
            ]

        # Java heap profiling (requires target_process)
        if target_process and collect_java_heap:
            config_lines += [
                "",
                "# Java heap profiling",
                "data_sources: {",
                "  config {",
                '    name: "android.java_hprof"',
                "    java_hprof_config {",
                f'      process_cmdline: "{target_process}"',
                "      dump_smaps: true",
                "    }",
                "  }",
                "}",
            ]

        config_text = "\n".join(config_lines)

        # --- P1-6 + P1-7: Trace collection with SELinux fallback and auto-degradation ---
        timeout_sec = duration_ms // 1000 + 30
        collection_error = None

        # Strategy 1: Config mode via stdin pipe (preferred)
        try:
            subprocess.run(
                ["adb", "shell", f"perfetto -c - --txt -o {device_path}"],
                input=config_text,
                check=True, capture_output=True, text=True,
                timeout=timeout_sec,
            )
            collection_error = None
        except (subprocess.CalledProcessError, subprocess.TimeoutExpired) as e:
            err_msg = ""
            if isinstance(e, subprocess.CalledProcessError):
                err_msg = e.stderr.strip() or e.stdout.strip() or f"exit {e.returncode}"
            else:
                err_msg = "timeout"
            logger.debug("config mode (stdin pipe) failed: %s", err_msg)
            collection_error = f"stdin-pipe: {err_msg}"

            # Strategy 2: P1-6 SELinux fallback — push config file, use cat pipe
            try:
                config_device_path = "/data/local/tmp/si_perfetto_config.pbtx"
                # Push config text to device
                subprocess.run(
                    ["adb", "push", "/dev/stdin", config_device_path],
                    input=config_text,
                    check=True, capture_output=True, text=True,
                    timeout=10,
                )
                # Use cat pipe to bypass SELinux restrictions
                subprocess.run(
                    ["adb", "shell", f"cat {config_device_path} | perfetto -c - --txt -o {device_path}"],
                    check=True, capture_output=True, text=True,
                    timeout=timeout_sec,
                )
                collection_error = None
                logger.debug("SELinux fallback (cat pipe) succeeded")
                # Cleanup config file
                subprocess.run(
                    ["adb", "shell", "rm", config_device_path],
                    capture_output=True, text=True,
                )
            except (subprocess.CalledProcessError, subprocess.TimeoutExpired, FileNotFoundError) as e2:
                err_msg2 = ""
                if isinstance(e2, subprocess.CalledProcessError):
                    err_msg2 = e2.stderr.strip() or e2.stdout.strip() or f"exit {e2.returncode}"
                else:
                    err_msg2 = str(e2)
                logger.debug("SELinux fallback (cat pipe) failed: %s", err_msg2)
                collection_error = f"stdin-pipe + cat-pipe: {err_msg} / {err_msg2}"

                # Strategy 3: P1-7 Auto-degradation — command-line mode
                # Simpler perfetto invocation without config file,
                # using inline -t and atrace categories only
                try:
                    duration_sec = duration_ms // 1000
                    cmdline = (
                        f"perfetto -o {device_path} -t {duration_sec}s "
                        f"--atrace-categories={cats}"
                    )
                    if target_process:
                        cmdline += f" --target-cmdline={target_process}"
                    subprocess.run(
                        ["adb", "shell", cmdline],
                        check=True, capture_output=True, text=True,
                        timeout=timeout_sec,
                    )
                    collection_error = None
                    logger.debug("auto-degradation to cmdline mode succeeded")
                    print("  [collector] Degraded to cmdline mode (no config)", flush=True)
                except (subprocess.CalledProcessError, subprocess.TimeoutExpired) as e3:
                    err_msg3 = ""
                    if isinstance(e3, subprocess.CalledProcessError):
                        err_msg3 = e3.stderr.strip() or e3.stdout.strip() or f"exit {e3.returncode}"
                    else:
                        err_msg3 = str(e3)
                    collection_error = f"all modes failed: stdin({err_msg}) / cat-pipe({err_msg2}) / cmdline({err_msg3})"

        if collection_error:
            raise RuntimeError(f"perfetto collection failed: {collection_error}")

        # Pull trace from device
        subprocess.run(
            ["adb", "pull", device_path, output_path],
            check=True, capture_output=True, text=True,
        )

        # Cleanup device
        subprocess.run(
            ["adb", "shell", "rm", device_path],
            capture_output=True, text=True,
        )

        return output_path


class TraceServer:
    """Manage trace_processor_shell HTTP server for on-demand querying.

    Starts ``trace_processor_shell -D <trace> --http-port <port>``
    so that both Perfetto UI (native acceleration) and Python code can
    query the trace via HTTP without loading it into memory repeatedly.
    """

    def __init__(self, trace_path: str, port: int = 9001):
        self.trace_path = trace_path
        self.port = port
        self.process: subprocess.Popen | None = None

    def start(self, timeout: float = 10.0) -> bool:
        """Start trace_processor_shell in HTTP mode.

        Returns True if the server becomes ready within *timeout* seconds.
        """
        import time
        import urllib.request
        import urllib.error

        if self.process is not None and self.process.poll() is None:
            return True  # already running

        shell = str(SHELL_BIN)
        if not Path(shell).exists():
            raise FileNotFoundError(f"trace_processor_shell not found: {shell}")

        self.process = subprocess.Popen(
            [shell, "-D", self.trace_path,
             "--http-port", str(self.port)],
            stdout=subprocess.PIPE, stderr=subprocess.PIPE,
        )

        deadline = time.monotonic() + timeout
        while time.monotonic() < deadline:
            try:
                urllib.request.urlopen(f"http://127.0.0.1:{self.port}/status", timeout=1)
                logger.info("TraceServer ready on :%d", self.port)
                return True
            except (urllib.error.URLError, OSError):
                if self.process.poll() is not None:
                    stderr = self.process.stderr.read().decode()
                    raise RuntimeError(f"trace_processor_shell exited: {stderr}")
                time.sleep(0.2)

        self.stop()
        return False

    def query(self, sql: str) -> list[dict]:
        """Execute a SQL query via the Python API connecting to HTTP server.

        Returns list of row dicts.
        """
        tp = TraceProcessor(addr=f"127.0.0.1:{self.port}",
                            config=TraceProcessorConfig(bin_path=str(SHELL_BIN)))
        try:
            result = tp.query(sql)
            return _rows_to_dicts(result)
        finally:
            tp.close()

    def stop(self):
        """Terminate the trace_processor_shell process."""
        if self.process and self.process.poll() is None:
            self.process.terminate()
            try:
                self.process.wait(timeout=5)
            except subprocess.TimeoutExpired:
                self.process.kill()
        self.process = None


def _rows_to_dicts(query_result) -> list[dict]:
    """Convert a perfetto QueryResult iterator to list of dicts."""
    rows = []
    for r in query_result:
        row = {}
        for desc in query_result.describe():
            col_name = desc.name
            row[col_name] = getattr(r, col_name, None)
        rows.append(row)
    return rows


def query_frame_slices(trace_path: str, ts_ns: int, dur_ns: int,
                       shell_path: str | None = None) -> dict:
    """Query trace data overlapping a user-selected time range.

    Opens a short-lived TraceProcessor, queries:
      1. All slices overlapping [ts_ns, ts_ns+dur_ns]
      2. Frame timeline entries overlapping the range
      3. Build call chain from parent_slice join

    Returns a dict with 'slices', 'frames', 'call_chains'.
    """
    config = TraceProcessorConfig(
        bin_path=shell_path or str(SHELL_BIN),
        load_timeout=10,
    )
    tp = TraceProcessor(trace=trace_path, config=config)
    try:
        # Slices overlapping the selected time range.
        # SI$block# slices have dur≈0 (beginSection+endSection are adjacent),
        # so a single ORDER BY dur DESC would squeeze them out.  Query in two
        # batches: all SI$ slices first, then top non-SI$ slices by duration.
        si_rows = tp.query(f"""
            SELECT id, name, ts, dur, depth, track_id, cat, parent_id
            FROM slice
            WHERE ts <= {ts_ns + dur_ns} AND ts + dur >= {ts_ns}
              AND name LIKE 'SI$%%'
        """)
        si_ids = set()
        si_slices_raw = []
        for r in si_rows:
            si_ids.add(r.id)
            si_slices_raw.append(r)

        remaining = 50 - len(si_slices_raw)
        other_rows = tp.query(f"""
            SELECT id, name, ts, dur, depth, track_id, cat, parent_id
            FROM slice
            WHERE ts <= {ts_ns + dur_ns} AND ts + dur >= {ts_ns}
              AND name NOT LIKE 'SI$%%'
            ORDER BY dur DESC
            LIMIT {max(remaining, 0)}
        """)
        slice_rows = list(si_slices_raw) + list(other_rows)
        slices = []
        for r in slice_rows:
            slices.append({
                "id": r.id,
                "name": r.name,
                "ts_ns": r.ts,
                "dur_ns": r.dur,
                "dur_ms": round(r.dur / 1e6, 2),
                "depth": r.depth,
                "track_id": r.track_id,
                "cat": r.cat,
                "parent_id": r.parent_id,
            })

        # Frame timeline overlapping the range
        frames = []
        try:
            frame_rows = tp.query(f"""
                SELECT display_frame_token, MIN(ts) AS frame_ts,
                       MAX(dur) AS frame_dur_ns,
                       GROUP_CONCAT(DISTINCT jank_type) AS jank_types
                FROM actual_frame_timeline_slice
                WHERE dur > 0 AND surface_frame_token > 0
                  AND ts <= {ts_ns + dur_ns} AND ts + dur >= {ts_ns}
                GROUP BY display_frame_token
                ORDER BY frame_ts
            """)
            for r in frame_rows:
                jank_list = [j.strip() for j in (r.jank_types or "").split(",")
                             if j.strip() and j.strip() != "None"]
                frames.append({
                    "ts_ns": r.frame_ts,
                    "dur_ms": round(r.frame_dur_ns / 1e6, 2),
                    "jank_types": jank_list,
                    "is_jank": len(jank_list) > 0,
                })
        except Exception:
            pass

        # Build call chains for top slices (parent -> child walk)
        call_chains = []
        seen_ids: set[int] = set()
        for s in slices[:10]:
            if s["id"] in seen_ids:
                continue
            chain = _walk_call_chain(tp, s["id"], seen_ids)
            if chain:
                call_chains.append(chain)

        # Correlate SI$block# slices with SIBlock logcat entries for stack traces.
        # This mirrors the bisect-based correlation in collect_block_events().
        _correlate_block_stacks_from_logcat(tp, slices, ts_ns, ts_ns + dur_ns)

        return {
            "ts_ns": ts_ns,
            "dur_ns": dur_ns,
            "dur_ms": round(dur_ns / 1e6, 2),
            "slices": slices,
            "frames": frames,
            "call_chains": call_chains,
        }
    finally:
        tp.close()


def _correlate_block_stacks_from_logcat(tp, slices: list[dict],
                                          range_start_ns: int, range_end_ns: int):
    """Correlate SI$block# slices with SIBlock logcat entries for stack traces.

    Modifies slices in-place, adding 'stack_trace' field to block slices.
    This mirrors the bisect-based correlation in collect_block_events().
    """
    import bisect

    block_slices = [s for s in slices if s["name"].startswith("SI$block#")]
    if not block_slices:
        return

    # Query android.log for SIBlock entries within the expanded time range
    try:
        log_rows = tp.query(f"""
            SELECT ts, msg
            FROM android.log
            WHERE msg LIKE 'SIBlock|%|%'
              AND ts >= {range_start_ns - 500_000_000}
              AND ts <= {range_end_ns + 500_000_000}
            ORDER BY ts ASC
        """)
        log_entries = []
        for r in log_rows:
            log_entries.append({"ts_ns": r.ts, "msg": r.msg or ""})
    except Exception:
        for s in block_slices:
            s["stack_trace"] = []
        return

    if not log_entries:
        for s in block_slices:
            s["stack_trace"] = []
        return

    log_ts_list = sorted(
        [(log["ts_ns"], log) for log in log_entries],
        key=lambda x: x[0],
    )
    log_timestamps = [t for t, _ in log_ts_list]
    MATCH_WINDOW_NS = 500_000_000  # 500ms

    for block in block_slices:
        block_ts = block["ts_ns"]
        idx = bisect.bisect_left(log_timestamps, block_ts)
        best_match = None
        best_dist = MATCH_WINDOW_NS + 1

        for candidate_idx in (idx - 1, idx):
            if 0 <= candidate_idx < len(log_ts_list):
                dist = abs(log_ts_list[candidate_idx][0] - block_ts)
                if dist < best_dist:
                    best_dist = dist
                    best_match = log_ts_list[candidate_idx][1]

        if best_match and best_dist <= MATCH_WINDOW_NS:
            block["stack_trace"] = _parse_siblock_msg(best_match["msg"])
        else:
            block["stack_trace"] = []


def _walk_call_chain(tp, slice_id: int, seen: set[int]) -> dict:
    """Walk from a slice up through parents to build a call chain."""
    chain_items = []
    current_id = slice_id
    for _ in range(20):  # max depth safety
        try:
            rows = list(tp.query(f"""
                SELECT id, name, ts, dur, depth, parent_id
                FROM slice WHERE id = {current_id}
            """))
        except Exception:
            break
        if not rows:
            break
        r = rows[0]
        seen.add(r.id)
        chain_items.append({
            "name": r.name,
            "dur_ms": round(r.dur / 1e6, 2),
            "depth": r.depth,
        })
        if r.parent_id is None or r.parent_id == 0:
            break
        current_id = r.parent_id

    # Reverse so parent is first
    chain_items.reverse()
    top = chain_items[0] if chain_items else {}
    top["children"] = chain_items[1:] if len(chain_items) > 1 else []
    return top
