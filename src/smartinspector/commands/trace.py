"""Trace collection and analysis commands: /trace, /record, /analyze."""

import json

from smartinspector.collector.perfetto import PerfettoCollector
from smartinspector.ws.server import SIServer


def _get_perfetto_config() -> dict:
    """Read perfetto_collection params from WS server config cache.

    The app sends config_sync on first WS connection (SIClient.onOpen),
    so the server always has the latest config after app connects.
    If no config cached (app never connected), returns empty dict → defaults.
    """
    server = SIServer.get()
    config_str = server.get_config()

    if not config_str:
        return {}

    try:
        config = json.loads(config_str)
        return config.get("perfetto_collection", {})
    except (json.JSONDecodeError, AttributeError):
        return {}


def cmd_trace(args: str, state: dict) -> dict:
    """Collect and analyze a Perfetto trace from connected device.

    Usage: /trace [duration_ms] [package_name]
    """
    pc = _get_perfetto_config()

    parts = args.split() if args else []
    duration_ms = pc.get("trace_duration_ms", 10000)
    target_process = pc.get("target_process", "") or None
    buffer_size_kb = pc.get("buffer_size_kb", 65536)
    cpu_sampling_interval_ms = pc.get("cpu_sampling_interval_ms", 1)

    # CLI args override config
    if len(parts) >= 1:
        try:
            duration_ms = int(parts[0])
        except ValueError:
            target_process = parts[0]

    if len(parts) >= 2:
        target_process = parts[1]

    if pc:
        print(f"  [config] App settings: duration={duration_ms}ms, buffer={buffer_size_kb}KB")
    else:
        print(f"  [config] Using defaults: duration={duration_ms}ms")

    print(f"Collecting trace ({duration_ms}ms)...", flush=True)
    if target_process:
        print(f"  Target: {target_process}")
    else:
        print("  Target: auto-detect foreground app")

    try:
        trace_path = PerfettoCollector.pull_trace_from_device(
            duration_ms=duration_ms,
            target_process=target_process,
            buffer_size_kb=buffer_size_kb,
            cpu_sampling_interval_ms=cpu_sampling_interval_ms,
        )
        print(f"  Trace saved: {trace_path}")
    except FileNotFoundError:
        print("ERROR: adb not found. Install Android platform tools.")
        return state
    except Exception as e:
        print(f"ERROR collecting trace: {e}")
        return state

    print("Analyzing trace...", flush=True)
    try:
        collector = PerfettoCollector(trace_path)
        summary = collector.summarize()
        perf_json = summary.to_json()
        collector.close()

        # Pretty-print key metrics
        data = json.loads(perf_json)

        print("\n  === Overview ===")

        # Frame timeline
        ft = data.get("frame_timeline", {})
        if ft.get("fps"):
            total = ft.get("total_frames", 0)
            jank = ft.get("jank_frames", 0)
            fps = ft["fps"]
            print(f"  Frames: {total} total, {jank} jank ({jank/max(total,1)*100:.1f}%)")
            print(f"  FPS: {fps} avg")

        # CPU usage — show per-process breakdown, not just system total
        cpu = data.get("cpu_usage", {})
        if cpu.get("cpu_usage_pct") is not None:
            num_cpus = cpu.get("num_cpus", 1)
            top_procs = cpu.get("top_processes", [])
            if top_procs:
                parts = []
                for proc in top_procs[:3]:
                    parts.append(f"{proc['process'][:30]} {proc['cpu_pct']}%")
                print(f"  CPU ({num_cpus} cores): {', '.join(parts)}")
            else:
                print(f"  CPU: {cpu['cpu_usage_pct']}% ({num_cpus} cores)")

        # Process memory — show top 3 + target app if not in top 3
        pm = data.get("process_memory", {})
        procs = pm.get("processes", [])
        if procs:
            mem_parts = []
            shown_names = set()
            for p in procs[:3]:
                if p.get("rss_kb") is not None:
                    rss_mb = p["rss_kb"] / 1024
                    mem_parts.append(f"{p['name'][:25]} {rss_mb:.0f}MB")
                    shown_names.add(p["name"])
            # Find target app: first com.* process in cpu_usage with SI$ slices
            has_si_slices = any(
                s.get("name", "").startswith("SI$")
                for s in data.get("view_slices", {}).get("summary", [])
            )
            if has_si_slices:
                for cpu_p in data.get("cpu_usage", {}).get("top_processes", []):
                    pn = cpu_p.get("process", "")
                    if pn.startswith("com.") and pn not in shown_names:
                        for mem_p in procs:
                            if mem_p["name"] == pn and mem_p.get("rss_kb"):
                                rss_mb = mem_p["rss_kb"] / 1024
                                mem_parts.append(f"{pn[:25]} {rss_mb:.0f}MB")
                                break
                        break
            if mem_parts:
                print(f"  Memory: {', '.join(mem_parts)}")

        # Activity + RV instances
        vs = data.get("view_slices", {})
        slices_summary = vs.get("summary", [])

        # Extract Activity names from SI$Activity.* slices
        activities = set()
        for sl in slices_summary:
            name = sl.get("name", "")
            if name.startswith("SI$Activity"):
                # SI$Activity.onCreate → Activity, SI$MainActivity.onResume → MainActivity
                act_name = name.split("$", 1)[-1].split(".")[0]
                if act_name != "Activity":
                    activities.add(act_name)
        if activities:
            print(f"  Activity: {', '.join(sorted(activities))}")

        # Jank frame numbers — only show if there are actual jank
        if ft.get("jank_detail") and ft.get("jank_frames", 0) > 0:
            jank_strs = []
            for jf in ft["jank_detail"][:5]:
                jank_strs.append(f"#{jf['frame_index']}({jf['dur_ms']:.1f}ms)")
            print(f"\n  Jank frames: {', '.join(jank_strs)}")

        state["perf_summary"] = perf_json
        state["_trace_path"] = trace_path
        print(f"\n  Trace analyzed. Use /summary for details or ask AI to analyze.")
    except Exception as e:
        print(f"ERROR analyzing trace: {e}")

    return state


def cmd_record(args: str, state: dict) -> dict:
    """Record a Perfetto trace without analysis.

    Usage: /record [duration_ms] [package_name]
    """
    pc = _get_perfetto_config()

    parts = args.split() if args else []
    duration_ms = pc.get("trace_duration_ms", 10000)
    target_process = pc.get("target_process", "") or None
    buffer_size_kb = pc.get("buffer_size_kb", 65536)
    cpu_sampling_interval_ms = pc.get("cpu_sampling_interval_ms", 1)

    # CLI args override config
    if len(parts) >= 1:
        try:
            duration_ms = int(parts[0])
        except ValueError:
            target_process = parts[0]

    if len(parts) >= 2:
        target_process = parts[1]

    if pc:
        print(f"  [config] App settings: duration={duration_ms}ms, buffer={buffer_size_kb}KB")
    else:
        print(f"  [config] Using defaults: duration={duration_ms}ms")

    print(f"Recording trace ({duration_ms}ms)...", flush=True)

    try:
        trace_path = PerfettoCollector.pull_trace_from_device(
            duration_ms=duration_ms,
            target_process=target_process,
            buffer_size_kb=buffer_size_kb,
            cpu_sampling_interval_ms=cpu_sampling_interval_ms,
        )
        print(f"  Trace saved: {trace_path}")
        print("  Use /analyze <path> to analyze it.")
        state["_trace_path"] = trace_path
    except FileNotFoundError:
        print("ERROR: adb not found.")
    except Exception as e:
        print(f"ERROR: {e}")

    return state


def cmd_analyze(args: str, state: dict) -> dict:
    """Analyze a Perfetto trace file.

    Usage: /analyze [path]
    If no path given, analyzes the last recorded trace.
    """
    trace_path = args.strip() or state.get("_trace_path", "")
    if not trace_path:
        print("Usage: /analyze <trace_path>")
        print("  Or use /trace to collect and analyze in one step.")
        return state

    print(f"Analyzing: {trace_path}", flush=True)

    try:
        collector = PerfettoCollector(trace_path)
        summary = collector.summarize()
        perf_json = summary.to_json()
        collector.close()

        data = json.loads(perf_json)

        if data.get("frame_timeline", {}).get("fps"):
            ft = data["frame_timeline"]
            total = ft.get("total_frames", 0)
            jank = ft.get("jank_frames", 0)
            print(f"  FPS: {ft['fps']} ({total} frames, {jank} jank)")
        if data.get("cpu_usage", {}).get("cpu_usage_pct") is not None:
            print(f"  CPU: {data['cpu_usage']['cpu_usage_pct']}%")
        if data.get("view_slices", {}).get("rv_instances"):
            rv = data["view_slices"]["rv_instances"]
            for inst in rv:
                print(f"  RV: {inst['instance']} ({inst['total_ms']:.1f}ms total)")

        state["perf_summary"] = perf_json
        print("\n  Use /summary for full details or ask AI to analyze.")
    except Exception as e:
        print(f"ERROR: {e}")

    return state
