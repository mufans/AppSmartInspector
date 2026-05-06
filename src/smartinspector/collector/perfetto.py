"""PerfettoCollector: adb collect -> SQL query -> unified JSON.

The collect_* methods are split into domain-specific mixin modules under
``collector/``.  This file retains the core class infrastructure, the
``summarize()`` orchestrator, ``pull_trace_from_device()``, and standalone
helper functions used across mixins.
"""

from __future__ import annotations

import bisect
import json
import os
import shutil
import subprocess
import tempfile
from pathlib import Path

from perfetto.trace_processor import TraceProcessor, TraceProcessorConfig

from smartinspector.collector.base import BaseCollector, PerfSummary
from smartinspector.perfetto_compat import patch
from smartinspector.debug_log import info_log, debug_log
from smartinspector.collector._helpers import _parse_siblock_msg, _map_state_label

# Import mixin modules
from smartinspector.collector.sched import SchedMixin
from smartinspector.collector.cpu import CpuMixin
from smartinspector.collector.frame import FrameMixin
from smartinspector.collector.io import IoMixin
from smartinspector.collector.block import BlockMixin
from smartinspector.collector.thread import ThreadMixin
from smartinspector.collector.sys import SysMixin

# Apply macOS IPv4 fix
patch()

# Default path to trace_processor_shell
_PROJECT_ROOT = Path(__file__).resolve().parent.parent.parent.parent
SHELL_BIN = _PROJECT_ROOT / "bin" / "trace_processor_shell"


# Re-export helpers for backward compatibility with external importers
# (e.g., frame_analyzer.py imports _parse_siblock_msg indirectly via query_frame_slices)


class PerfettoCollector(
    BaseCollector,
    SchedMixin,
    CpuMixin,
    FrameMixin,
    IoMixin,
    BlockMixin,
    ThreadMixin,
    SysMixin,
):
    """Collect and analyze Android Perfetto traces.

    Inherits from BaseCollector (platform-agnostic interface) and
    domain-specific mixin modules:
      - SchedMixin: collect_sched()
      - CpuMixin: collect_cpu_hotspots(), collect_cpu_usage()
      - FrameMixin: collect_frame_timeline(), collect_view_slices(), collect_compose_slices()
      - IoMixin: collect_io_slices(), collect_input_events()
      - BlockMixin: collect_block_events()
      - ThreadMixin: collect_thread_state()
      - SysMixin: collect_sys_stats(), collect_threads()
    """

    _platform: str = "android"

    def __init__(self, trace_path: str, shell_path: str | None = None,
                 target_process: str | None = None):
        super().__init__(trace_path=trace_path, target_process=target_process)
        self.shell_path = shell_path or str(SHELL_BIN)
        self._tp: TraceProcessor | None = None
        self._target_process_cache: dict | None = None  # cached resolve result

    def _open(self) -> TraceProcessor:
        if self._tp is not None:
            return self._tp
        config = TraceProcessorConfig(
            bin_path=self.shell_path,
            load_timeout=10,
        )
        self._tp = TraceProcessor(trace=self.trace_path, config=config)
        return self._tp

    def _resolve_target_process(self, package_name: str | None = None) -> dict:
        """Resolve target process info (upid, pid, uid) from package name.

        Tries ``process`` table first, falls back to ``package_list`` table
        for cold-start scenarios where the process table may be empty.

        Args:
            package_name: Android package name, e.g. "com.example.app".
                          Falls back to ``self.target_process`` if not provided.

        Returns:
            Dict with keys: upid, pid, uid, name, source ("process"|"package_list"|"")
        """
        package_name = package_name or self.target_process
        if not package_name:
            return {}
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
            debug_log("perfetto", f"process table lookup failed: {e}")

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
                        debug_log("perfetto", f"package_list fallback: found uid={uid} for {package_name} but no process entry")
            except Exception as e:
                debug_log("perfetto", f"package_list fallback failed: {e}")

        if result["source"]:
            debug_log("perfetto", f"resolved target process: {package_name} -> upid={result['upid']}, pid={result['pid']}, uid={result['uid']} (via {result['source']})")
        else:
            debug_log("perfetto", f"could not resolve target process: {package_name}")

        self._target_process_cache = result
        return result

    def close(self):
        if self._tp:
            self._tp.close()
            self._tp = None

    @classmethod
    def is_available(cls) -> bool:
        """Check if adb and trace_processor_shell are available."""
        return shutil.which("adb") is not None and SHELL_BIN.exists()

    def get_device_info(self):
        """Return Android device info from metadata."""
        from smartinspector.collector.base import DeviceInfo

        info = DeviceInfo(platform="android")
        try:
            tp = self._open()
            for r in tp.query("SELECT name, str_value FROM metadata"):
                if r.name == "device":
                    info.model = r.str_value or ""
                elif r.name == "fingerprint":
                    info.os_version = r.str_value or ""
        except Exception:
            pass
        return info

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
                debug_log("perfetto", f"Table {table} query failed: {e}")
                result[table] = -1  # table doesn't exist
        return result

    def collect_memory(self) -> dict:
        """Collect Java heap memory from android.java_hprof data."""
        from smartinspector.collector.memory import collect_heap_graph_analysis

        tp = self._open()

        # Resolve target upid for process-scoped queries
        target_upid = None
        if self._target_process_cache:
            target_upid = self._target_process_cache.get("upid")

        # Detailed heap graph analysis
        result = collect_heap_graph_analysis(tp, target_upid)

        # Fallback: if heap_graph_analysis returned nothing, try basic query
        if not result:
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
                allocs = []
                for r in rows:
                    allocs.append({
                        "class_name": r.class_name,
                        "obj_count": r.obj_count,
                        "total_size_kb": round(r.total_bytes / 1024, 1),
                    })
                if allocs:
                    result["heap_graph_classes"] = allocs
            except Exception as e:
                debug_log("perfetto", f"Basic heap graph query failed: {e}")

        return result

    def collect_process_memory(self) -> dict:
        """Collect process-level memory stats from process_counter_track."""
        tp = self._open()

        try:
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
            debug_log("perfetto", f"Process memory query failed: {e}")

        return {}

    def summarize(self) -> PerfSummary:
        """Run all analyses and return a unified summary."""
        summary = PerfSummary()

        # Metadata
        tp = self._open()
        try:
            meta = tp.query("SELECT name, str_value FROM metadata")
            for r in meta:
                val = r.str_value if hasattr(r, 'str_value') else ""
                summary.metadata[r.name] = val
        except Exception as e:
            debug_log("perfetto", f"Metadata query failed: {e}")

        # Table diagnosis — help understand why data may be missing
        try:
            diag = self._diagnose_tables()
            summary.metadata["table_stats"] = diag
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
            debug_log("perfetto", f"Table diagnosis failed: {e}")

        # P1-5: Resolve target process with package_list fallback for cold-start support
        if self.target_process:
            resolved = self._resolve_target_process(self.target_process)
            if resolved.get("source"):
                summary.metadata["target_process"] = resolved
                debug_log("perfetto", f"target process resolved via {resolved['source']}: {resolved}")

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

        # Compose slices (SI$compose# — recomposition tracking)
        try:
            summary.compose_slices = self.collect_compose_slices()
        except Exception as e:
            summary.compose_slices = {"error": str(e)}

        # System-level stats (CPU idle, frequency, fork rate)
        try:
            sys_stats = self.collect_sys_stats()
            if sys_stats:
                summary.sys_stats = sys_stats
        except Exception as e:
            debug_log("perfetto", f"sys_stats collection failed: {e}")

        # Thread state analysis (Running/S/D per SI$ slice)
        try:
            summary.thread_state = self.collect_thread_state()
            debug_log("perfetto", f"thread_state: collected {len(summary.thread_state)} entries")
            if not summary.thread_state:
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
                    debug_log("perfetto", f"thread_state diagnosis: total={ts_count}, main_thread={ts_main}")
                except Exception as e2:
                    debug_log("perfetto", f"thread_state diagnosis failed: {e2}")
        except Exception as e:
            debug_log("perfetto", f"thread_state collection failed: {e}")

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
        on_record_start: callable | None = None,
    ) -> str:
        """Pull a Perfetto trace from connected Android device via adb.

        Args:
            output_path: Local path to save the trace. Defaults to temp file.
            duration_ms: Trace duration in milliseconds.
            categories: Ftrace/atrace categories to enable.
            target_process: Target app package name for CPU/memory profiling.
            buffer_size_kb: Main buffer size in KB.
            cpu_sampling_interval_ms: CPU sampling interval in ms (1-10).
            collect_cpu_callstacks: Enable CPU callstack profiling (requires target_process).
            collect_java_heap: Enable Java heap profiling (requires target_process).
            on_record_start: Optional callback invoked after Perfetto recording starts.

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
            cpu_freq = max(1, 1000 // cpu_sampling_interval_ms)
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

        # --- Trace collection with SELinux fallback and auto-degradation ---
        timeout_sec = duration_ms // 1000 + 30
        collection_error = None

        # Strategy 1: Config mode via stdin pipe (preferred)
        try:
            if on_record_start:
                import threading
                import time

                proc = subprocess.Popen(
                    ["adb", "shell", f"perfetto -c - --txt -o {device_path}"],
                    stdin=subprocess.PIPE,
                    stdout=subprocess.PIPE,
                    stderr=subprocess.PIPE,
                    text=True,
                )
                try:
                    proc.stdin.write(config_text)
                    proc.stdin.flush()
                    proc.stdin.close()
                except (BrokenPipeError, OSError) as e:
                    info_log("perfetto", f"WARNING: Failed to write config to perfetto stdin: {e}")

                stdout_chunks: list[str] = []
                stderr_chunks: list[str] = []

                def _pipe_reader(stream, chunks: list[str]) -> None:
                    try:
                        chunks.append(stream.read())
                    except (ValueError, OSError):
                        chunks.append("")

                t_out = threading.Thread(
                    target=_pipe_reader, args=(proc.stdout, stdout_chunks), daemon=True,
                )
                t_err = threading.Thread(
                    target=_pipe_reader, args=(proc.stderr, stderr_chunks), daemon=True,
                )
                t_out.start()
                t_err.start()

                time.sleep(0.5)

                callback_error = None

                def _run_callback():
                    nonlocal callback_error
                    try:
                        on_record_start()
                    except Exception as exc:
                        callback_error = exc
                        info_log("perfetto", f"WARNING: on_record_start callback failed: {exc}")

                cb_thread = threading.Thread(target=_run_callback, daemon=True)
                cb_thread.start()
                cb_thread.join(timeout=10.0)
                if cb_thread.is_alive():
                    info_log("perfetto", "WARNING: on_record_start callback timed out after 10s")

                proc.wait(timeout=timeout_sec)
                t_out.join(timeout=5)
                t_err.join(timeout=5)

                stdout = stdout_chunks[0] if stdout_chunks else ""
                stderr = stderr_chunks[0] if stderr_chunks else ""
                if proc.returncode != 0:
                    raise subprocess.CalledProcessError(
                        proc.returncode, proc.args, stdout, stderr,
                    )
                if callback_error:
                    info_log("perfetto", f"WARNING: Trace collected but on_record_start had errors: {callback_error}")
            else:
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
            debug_log("perfetto", f"config mode (stdin pipe) failed: {err_msg}")
            collection_error = f"stdin-pipe: {err_msg}"

            # Strategy 2: SELinux fallback — push config file, use cat pipe
            try:
                config_device_path = "/data/local/tmp/si_perfetto_config.pbtx"
                subprocess.run(
                    ["adb", "push", "/dev/stdin", config_device_path],
                    input=config_text,
                    check=True, capture_output=True, text=True,
                    timeout=10,
                )
                subprocess.run(
                    ["adb", "shell", f"cat {config_device_path} | perfetto -c - --txt -o {device_path}"],
                    check=True, capture_output=True, text=True,
                    timeout=timeout_sec,
                )
                collection_error = None
                debug_log("perfetto", "SELinux fallback (cat pipe) succeeded")
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
                debug_log("perfetto", f"SELinux fallback (cat pipe) failed: {err_msg2}")
                collection_error = f"stdin-pipe + cat-pipe: {err_msg} / {err_msg2}"

                # Strategy 3: Auto-degradation — command-line mode
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
                    debug_log("perfetto", "auto-degradation to cmdline mode succeeded")
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
        """Start trace_processor_shell in HTTP mode."""
        import time
        import urllib.request
        import urllib.error

        if self.process is not None and self.process.poll() is None:
            return True

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
                info_log("perfetto", f"TraceServer ready on :{self.port}")
                return True
            except (urllib.error.URLError, OSError):
                if self.process.poll() is not None:
                    stderr = self.process.stderr.read().decode()
                    raise RuntimeError(f"trace_processor_shell exited: {stderr}")
                time.sleep(0.2)

        self.stop()
        return False

    def query(self, sql: str) -> list[dict]:
        """Execute a SQL query via the Python API connecting to HTTP server."""
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

        call_chains = []
        seen_ids: set[int] = set()
        for s in slices[:10]:
            if s["id"] in seen_ids:
                continue
            chain = _walk_call_chain(tp, s["id"], seen_ids)
            if chain:
                call_chains.append(chain)

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
    """Correlate SI$block# slices with SIBlock logcat entries for stack traces."""
    block_slices = [s for s in slices if s["name"].startswith("SI$block#")]
    if not block_slices:
        return

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
    MATCH_WINDOW_NS = 500_000_000

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
    for _ in range(20):
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
