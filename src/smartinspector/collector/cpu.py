"""CPU collection: hotspots (flame graph) and per-process/thread CPU usage."""

from smartinspector.debug_log import debug_log


class CpuMixin:
    """Mixin for PerfettoCollector providing CPU-related collection."""

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
            debug_log("perfetto", f"CPU hotspot query failed: {e}")
            return []

        if not rows:
            return []

        # Preload callsite -> frame mapping and parent relationships
        callsite_map: dict[int, tuple[str, int | None]] = {}
        try:
            cs_rows = tp.query("""
                SELECT spc.id, spf.name, spc.parent_id
                FROM stack_profile_callsite spc
                JOIN stack_profile_frame spf ON spc.frame_id = spf.id
            """)
            for r in cs_rows:
                callsite_map[r.id] = (r.name, r.parent_id)
        except Exception as e:
            debug_log("perfetto", f"callsite_map query failed: {e}")

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
            debug_log("perfetto", f"Trace bounds query failed: {e}")
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
            debug_log("perfetto", f"CPU count query failed: {e}")
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
            debug_log("perfetto", f"CPU usage query failed: {e}")
            return {}

        # Total CPU wall-time available = trace_dur * num_cpus
        total_wall_ns = trace_dur_ns * num_cpus

        # Group by process — skip kernel threads (pid 0 / no process name)
        proc_map: dict[str, dict] = {}
        total_cpu_ns = 0
        for r in rows:
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
