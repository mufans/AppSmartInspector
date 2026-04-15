"""Trace collection and analysis commands: /trace, /record, /analyze, /frame."""

import json

from smartinspector.collector.perfetto import PerfettoCollector
from smartinspector.ws.server import SIServer


def _get_perfetto_config() -> dict:
    """Read perfetto_collection params from WS server config cache.

    The app sends config_sync on first WS connection (SIClient.onOpen),
    so the server always has the latest config after app connects.
    If no config cached (app never connected), returns empty dict -> defaults.
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
    """Collect and analyze a Perfetto trace via the graph pipeline.

    Routes through collector -> analyzer (TRACE path, stops before attributor).

    Usage: /trace [duration_ms] [package_name]
    """
    # Parse CLI args to override defaults
    parts = args.split() if args else []
    duration_ms = None
    target_process = None

    if len(parts) >= 1:
        try:
            duration_ms = int(parts[0])
            if duration_ms < 100 or duration_ms > 60000:
                print(f"  Warning: duration {duration_ms}ms out of range [100, 60000], clamped.")
                duration_ms = max(100, min(60000, duration_ms))
        except ValueError:
            target_process = parts[0]

    if len(parts) >= 2:
        target_process = parts[1]

    # Store parsed args in state for collector_node to use
    if duration_ms is not None:
        state["trace_duration_ms"] = duration_ms
    if target_process:
        state["trace_target_process"] = target_process

    from smartinspector.graph import create_graph, _stream_run
    from smartinspector.graph.state import RouteDecision

    graph = create_graph()

    state["messages"] = state.get("messages", []) + [
        {"role": "user", "content": "请采集并分析性能trace"},
    ]
    state["_route"] = RouteDecision.TRACE

    return _stream_run(graph, state)


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
            if duration_ms < 100 or duration_ms > 60000:
                print(f"  Warning: duration {duration_ms}ms out of range [100, 60000], clamped.")
                duration_ms = max(100, min(60000, duration_ms))
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
    """Analyze a Perfetto trace file via the perf_analyzer_node.

    Usage: /analyze [path]
    If no path given, analyzes the last recorded trace.
    """
    from smartinspector.graph.nodes.analyzer import perf_analyzer_node

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

        state["perf_summary"] = perf_json
        state["_trace_path"] = trace_path

        # Reuse graph node for LLM analysis
        analysis_state = {
            "messages": state.get("messages", []),
            "perf_summary": perf_json,
            "perf_analysis": state.get("perf_analysis", ""),
            "attribution_data": state.get("attribution_data", ""),
            "attribution_result": state.get("attribution_result", ""),
            "_trace_path": trace_path,
        }
        result = perf_analyzer_node(analysis_state)

        if result.get("perf_analysis"):
            state["perf_analysis"] = result["perf_analysis"]
            print(result["perf_analysis"])
        else:
            print("  Analysis complete. Use /summary for details or ask AI to analyze.")

    except Exception as e:
        print(f"ERROR: {e}")

    return state


def _parse_ns(value: str) -> int | None:
    """Parse a time value that may be in ns, us, or ms."""
    value = value.strip()
    if not value:
        return None
    # Strip unit suffixes
    orig = value
    for suffix in ("ns", "us", "\u00b5s", "ms"):
        if value.endswith(suffix):
            value = value[: -len(suffix)]
            break
    try:
        num = float(value)
    except ValueError:
        return None
    # Convert to ns based on suffix
    if orig.endswith("ms"):
        return int(num * 1_000_000)
    if orig.endswith(("us", "\u00b5s")):
        return int(num * 1_000)
    if orig.endswith("ns"):
        return int(num)
    # No suffix: assume ns if > 1e9, else ms
    if num > 1_000_000_000:
        return int(num)
    return int(num * 1_000_000)


def cmd_frame(args: str, state: dict) -> dict:
    """Analyze a user-selected frame/slice from a Perfetto trace.

    Usage:
        /frame ts=<timestamp> dur=<duration>     (both in ns, us, or ms)
        /frame ts=1234567890 dur=5000000          (ns)
        /frame ts=1234.5ms dur=5ms                (ms)
        /frame ts=500000us dur=1000us             (us)

    Requires a trace to be already loaded (via /trace, /record, or /analyze).
    """
    trace_path = state.get("_trace_path", "")
    if not trace_path:
        print("No trace loaded. Use /trace, /record, or /analyze first.")
        return state

    # Parse ts= and dur= from args
    ts_ns = None
    dur_ns = None
    for part in args.split():
        if part.startswith("ts="):
            ts_ns = _parse_ns(part[3:])
        elif part.startswith("dur="):
            dur_ns = _parse_ns(part[4:])

    if ts_ns is None or dur_ns is None:
        print("Usage: /frame ts=<timestamp> dur=<duration>")
        print("  Units: ns (default), us/\u00b5s, ms \u2014 e.g. /frame ts=1234ms dur=5ms")
        print(f"  Current trace: {trace_path}")
        return state

    print(f"  [frame] Analyzing ts={ts_ns} dur={dur_ns} ({dur_ns / 1e6:.2f}ms)...", flush=True)

    try:
        from smartinspector.agents.frame_analyzer import analyze_frame
        existing_summary = state.get("perf_summary", "")
        analysis = analyze_frame(trace_path, ts_ns, dur_ns, existing_summary)
        print(analysis)
        # Store as the latest perf_analysis for /summary
        state["perf_analysis"] = analysis
    except FileNotFoundError as e:
        print(f"ERROR: {e}")
    except Exception as e:
        print(f"ERROR: {e}")

    return state


def cmd_open(args: str, state: dict) -> dict:
    """Open Perfetto UI with SI Agent bridge for interactive frame analysis.

    Starts:
      1. trace_processor_shell HTTP server (port 9001) for Perfetto UI
      2. Bridge server (port 9877) serving Perfetto UI + WebSocket
      3. Opens browser to the bridge page

    The Perfetto UI plugin (com.smartinspector.Bridge) will connect
    automatically and allow interactive frame analysis.

    Usage: /open [trace_path]
    If no path given, uses the last analyzed/recorded trace.
    """
    import os

    trace_path = args.strip() or state.get("_trace_path", "")
    if not trace_path:
        print("Usage: /open <trace.pb>")
        print("  Or use /analyze or /trace first to load a trace.")
        return state

    if not os.path.isfile(trace_path):
        print(f"File not found: {trace_path}")
        return state

    state["_trace_path"] = trace_path

    import os
    from smartinspector.ws.bridge_server import start_bridge, open_browser

    ui_dist = os.path.join(
        os.path.dirname(os.path.dirname(os.path.dirname(os.path.dirname(
            os.path.abspath(__file__))))),
        "perfetto-build", "ui", "out", "dist",
    )

    if not os.path.isdir(ui_dist):
        print("Perfetto UI not built yet. Building...")
        print("  Run: ./perfetto-plugin/build.sh")
        print("")
        print("  This requires Node.js and git. The build takes ~5 minutes.")
        print("  After building, run /open again.")
        return state

    perf_summary = state.get("perf_summary", "")
    attribution_result = state.get("attribution_result", "")
    bridge = start_bridge(trace_path, perf_summary=perf_summary, attribution_result=attribution_result)

    if bridge.is_running():
        # Perfetto UI supports ?url= to auto-load a trace from a URL.
        # The bridge server serves the trace at /trace.pb.
        url = f"http://127.0.0.1:{bridge.port}/#!/?url=http://127.0.0.1:{bridge.port}/trace.pb"
        print(f"  Opening Perfetto UI: {url}")
        print(f"  Trace: {trace_path}")
        print(f"  trace_processor_shell: http://127.0.0.1:9001")
        print("")
        print("  Trace will load automatically. Then:")
        print("    1. Drag to select a time range on the timeline")
        print("    2. Click 'SI Frame Analysis' tab in the details panel")
        print("    3. Click 'Analyze with SI Agent'")
        print("")
        print("  Use /close to stop the bridge server.")
        open_browser(url)
    else:
        print("  ERROR: Bridge server failed to start.")

    return state


def cmd_close(args: str, state: dict) -> dict:
    """Stop the Perfetto UI bridge server.

    Usage: /close
    """
    from smartinspector.ws.bridge_server import stop_bridge
    stop_bridge()
    print("  Bridge server stopped.")
    return state
