"""Session management commands: /help, /clear, /summary, /tokens."""

import json


def cmd_help(args: str, state: dict) -> dict:
    """Show help for all slash commands."""
    help_text = """
SmartInspector Commands:

  Device:
    /devices              List connected Android devices
    /connect <host:port>  Connect to device via adb TCP
    /status               Show current session status
    /disconnect           Disconnect from remote device

  Trace:
    /trace [ms] [pkg]     Collect + analyze trace (default 10000ms)
    /record [ms] [pkg]    Record trace without analysis
    /analyze [path]       Analyze a trace file
    /frame ts=X dur=Y     Analyze a selected frame (from Perfetto UI)
    /open                 Open Perfetto UI with SI Agent bridge
    /close                Stop the Perfetto UI bridge server

  Hooks:
    /config [json|reset]  View/set hook configuration
    /config source_dir <path>
                          Set source code search directory
    /hooks                List available hooks
    /hook on/off <id>     Enable/disable a hook
    /hook add <c> <m>     Add extra hook (class + method)
    /hook rm <class>      Remove extra hook
    /debug                Open debug config UI on device

  Session:
    /clear                Clear session data (perf_summary, analysis)
    /summary              Show perf summary from last analysis
    /tokens               Show token usage statistics for current session
    /full                 Full flow: trace → analyze → attribute → report
    /report               Generate performance report

  General:
    /help                 Show this help
    quit / exit           Exit SmartInspector
"""
    print(help_text)
    return state


def cmd_clear(args: str, state: dict) -> dict:
    """Clear session data."""
    state["perf_summary"] = ""
    state["perf_analysis"] = ""
    state["attribution_data"] = ""
    state["attribution_result"] = ""
    state["_trace_path"] = ""
    state.pop("trace_duration_ms", None)
    state.pop("trace_target_process", None)
    state.pop("_route", None)
    state["messages"] = []
    print("Session cleared.")
    return state


def cmd_summary(args: str, state: dict) -> dict:
    """Show a summary of the last perf analysis."""
    perf_json = state.get("perf_summary", "")
    if not perf_json:
        print("No perf summary available. Use /trace to collect one.")
        return state

    try:
        data = json.loads(perf_json)
    except json.JSONDecodeError:
        print("Perf summary data is corrupted.")
        return state

    print("=== Performance Summary ===\n")

    # Frame timeline
    ft = data.get("frame_timeline", {})
    if ft.get("fps"):
        total = ft.get("total_frames", 0)
        jank = ft.get("jank_frames", 0)
        print(f"FPS: {ft['fps']}")
        print(f"  Total frames: {total}")
        print(f"  Jank frames: {jank}")

        # Jank detail with frame numbers — only show if actual jank
        if ft.get("jank_detail") and ft.get("jank_frames", 0) > 0:
            jank_strs = []
            for jf in ft["jank_detail"][:10]:
                jank_strs.append(f"#{jf['frame_index']}({jf['dur_ms']:.1f}ms)")
            print(f"  Jank frames: {', '.join(jank_strs)}")

    # CPU usage
    cpu_usage = data.get("cpu_usage", {})
    if cpu_usage.get("cpu_usage_pct") is not None:
        num_cpus = cpu_usage.get("num_cpus", 1)
        print(f"\nCPU: {cpu_usage['cpu_usage_pct']}% overall ({num_cpus} cores)")
        for proc in cpu_usage.get("top_processes", [])[:5]:
            print(f"  {proc['process']:40s} {proc['cpu_pct']:>5.1f}%")
            for t in proc.get("threads", [])[:3]:
                print(f"    {t['name']:38s} {t['cpu_pct']:>5.1f}%  switches={t.get('switches', 0)}")

    # Process memory
    pm = data.get("process_memory", {})
    procs = pm.get("processes", [])
    if procs:
        print(f"\nProcess memory (max RSS during trace):")
        for p in procs[:5]:
            if p.get("rss_kb") is not None:
                rss_mb = p["rss_kb"] / 1024
                anon_str = f" (anon {p['rss_anon_kb']/1024:.0f}MB)" if p.get("rss_anon_kb") else ""
                print(f"  {p['name']:40s} RSS {rss_mb:.0f}MB{anon_str}")
            elif p.get("rss_anon_kb") is not None:
                anon_mb = p["rss_anon_kb"] / 1024
                print(f"  {p['name']:40s} RSS anon {anon_mb:.0f}MB")

    # Scheduling
    sched = data.get("scheduling", {})
    if sched.get("hot_threads"):
        print(f"\nTop threads:")
        for t in sched["hot_threads"][:5]:
            print(f"  {t['comm']:30s} switches={t['switches']:>6d}  dur={t['total_dur_ms']:>8.1f}ms")

    # View slices
    vs = data.get("view_slices", {})
    if vs.get("rv_instances"):
        print(f"\nRecyclerView instances:")
        for inst in vs["rv_instances"]:
            print(f"  {inst['instance']:40s} total={inst['total_ms']:>8.1f}ms  count={inst['count']}")
            for method, stats in inst.get("methods", {}).items():
                print(f"    {method:38s} avg={stats['total_ms']/max(stats['count'],1):>6.2f}ms  max={stats['max_ms']:>6.2f}ms  x{stats['count']}")

    if vs.get("slowest_slices"):
        print(f"\nSlowest slices (top 10):")
        for s in vs["slowest_slices"][:10]:
            custom = " [custom]" if s.get("is_custom") else ""
            print(f"  {s['dur_ms']:>8.2f}ms  {s['name']}{custom}")

    # CPU hotspots
    cpu = data.get("cpu_hotspots", [])
    if cpu:
        print(f"\nCPU hotspots:")
        for h in cpu[:5]:
            print(f"  {h.get('function', '?'):40s} {h.get('pct', 0):>5.1f}%  samples={h.get('samples', 0)}")

    # Memory (heap graph)
    mem = data.get("memory", {})
    if mem.get("heap_graph_classes"):
        print(f"\nTop heap allocations:")
        for a in mem["heap_graph_classes"][:5]:
            print(f"  {a['class_name']:40s} {a['total_size_kb']:>8.1f}KB  x{a['obj_count']}")

    print("\n=== End Summary ===")
    return state


def cmd_tokens(args: str, state: dict) -> dict:
    """Show token usage statistics for the current session."""
    from smartinspector.token_tracker import get_tracker

    tracker = get_tracker()
    print("\n" + tracker.summary())
    return state
