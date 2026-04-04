"""Trace collection and analysis commands: /trace, /record, /analyze."""

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
