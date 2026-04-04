"""Collector node: trace collection (first step of full pipeline)."""

import json

from langchain_core.messages import AIMessage

from smartinspector.graph.state import AgentState


def _read_perfetto_config() -> dict:
    """Read perfetto_collection params from WS server config cache.

    The Android app sends config_sync on WS connect (SIClient.onOpen),
    which includes perfetto_collection.trace_duration_ms etc.
    If no config cached (app never connected), returns empty dict -> defaults.
    """
    from smartinspector.ws.server import SIServer

    server = SIServer.get()
    config_str = server.get_config()

    if not config_str:
        return {}

    try:
        config = json.loads(config_str)
        return config.get("perfetto_collection", {})
    except (json.JSONDecodeError, AttributeError):
        return {}


def collector_node(state: AgentState) -> dict:
    """Collect and analyze a Perfetto trace.

    Runs PerfettoCollector.pull_trace_from_device() + summarize().
    Reads perfetto params from WS server config cache (sent by app via config_sync).
    """
    from smartinspector.collector.perfetto import PerfettoCollector

    print("  [collector] Starting trace collection...", flush=True)

    try:
        # Read perfetto params from WS server config cache (app sends via config_sync)
        pc = _read_perfetto_config()
        duration_ms = int(pc.get("trace_duration_ms", 10000))
        buffer_size_kb = int(pc.get("buffer_size_kb", 65536))
        target_process = pc.get("target_process", "") or None

        print(f"  [collector] Config: duration={duration_ms}ms, buffer={buffer_size_kb}KB", flush=True)

        trace_path = PerfettoCollector.pull_trace_from_device(
            duration_ms=duration_ms,
            target_process=target_process,
            buffer_size_kb=buffer_size_kb,
        )
        print(f"  [collector] Trace saved to {trace_path}", flush=True)

        collector = PerfettoCollector(trace_path)
        summary = collector.summarize()

        # Request block events from app via WS (structured JSON, more reliable
        # than querying Perfetto's android_logs table which is often empty)
        try:
            from smartinspector.ws.server import SIServer
            server = SIServer.get()
            if server.has_connections():
                print("  [collector] Requesting block events from app...", flush=True)
                ws_events = server.request_block_events(timeout=5.0)
                if ws_events:
                    merged = []
                    for ev in ws_events:
                        raw_name = f"SI$block#{ev.get('msgClass', 'Unknown')}#{ev.get('durationMs', 0)}ms"
                        merged.append({
                            "raw_name": raw_name,
                            "ts_ns": 0,
                            "dur_ms": ev.get("durationMs", 0),
                            "stack_trace": ev.get("stackTrace", []),
                        })
                    summary.block_events = merged
                    print(f"  [collector] Got {len(merged)} block events from app", flush=True)
                else:
                    print("  [collector] No block events from app", flush=True)
        except Exception as e:
            print(f"  [collector] Block events request failed: {e}", flush=True)

        perf_json = summary.to_json()

        print(f"  [collector] Analysis complete ({len(perf_json)} bytes)", flush=True)

        return {
            "messages": [AIMessage(content="[trace collected and analyzed]")],
            "perf_summary": perf_json,
            "perf_analysis": state.get("perf_analysis", ""),
            "attribution_data": "",
            "attribution_result": "",
            "_trace_path": trace_path,
        }
    except Exception as e:
        error_msg = f"Trace collection failed: {e}"
        print(f"  [collector] ERROR: {error_msg}", flush=True)
        return {
            "messages": [AIMessage(content=error_msg)],
            "perf_summary": "",
            "perf_analysis": "",
            "attribution_data": "",
            "attribution_result": "",
            "_trace_path": "",
        }
