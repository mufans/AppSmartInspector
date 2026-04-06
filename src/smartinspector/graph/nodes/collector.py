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


def _merge_block_events(
    sql_events: list[dict],
    ws_events: list[dict],
) -> list[dict]:
    """Merge Perfetto SQL and WS block events.

    SQL data has precise ts_ns timestamps; WS data has stack_traces.
    Merge by matching on msgClass + dur_ms from SQL raw_name.
    """
    # Index WS events by (msg_class, dur_ms), keeping the one with longest stack_trace
    ws_index: dict[tuple[str, float], dict] = {}
    for ev in ws_events:
        key = (ev["msg_class"], ev["dur_ms"])
        if key not in ws_index or len(ev.get("stack_trace", [])) > len(ws_index[key].get("stack_trace", [])):
            ws_index[key] = ev

    # Also index by dur_ms for fuzzy matching
    ws_by_dur: dict[float, list[dict]] = {}
    for ev in ws_events:
        ws_by_dur.setdefault(ev["dur_ms"], []).append(ev)

    merged = []
    matched_ws_keys: set[tuple[str, float]] = set()

    for sql_ev in sql_events:
        result = dict(sql_ev)

        # Extract msgClass and dur_ms from SQL raw_name
        # Format: SI$block#MsgClass#250ms or SI$block#com.example.Worker.run#250ms
        name = sql_ev.get("raw_name", "")
        parts = name.split("#")
        sql_msg_class = ""
        sql_dur_ms = sql_ev.get("dur_ms", 0)

        if len(parts) >= 3:
            sql_msg_class = parts[1]

        # Exact match
        key = (sql_msg_class, sql_dur_ms)
        ws_match = ws_index.get(key)
        if not ws_match and sql_dur_ms in ws_by_dur:
            # Fuzzy match: any unmatched event with same dur_ms
            for candidate in ws_by_dur[sql_dur_ms]:
                cand_key = (candidate["msg_class"], candidate["dur_ms"])
                if cand_key not in matched_ws_keys:
                    ws_match = candidate
                    break

        if ws_match:
            matched_ws_keys.add((ws_match["msg_class"], ws_match["dur_ms"]))
            # WS has more reliable stack_trace
            if ws_match.get("stack_trace"):
                result["stack_trace"] = ws_match["stack_trace"]

        merged.append(result)

    # Add unmatched WS events (preserved without precise ts_ns)
    for ev in ws_events:
        key = (ev["msg_class"], ev["dur_ms"])
        if key not in matched_ws_keys:
            merged.append({
                "raw_name": f"SI$block#{ev['msg_class']}#{ev['dur_ms']}ms",
                "ts_ns": 0,  # WS has no precise timestamp
                "dur_ms": ev["dur_ms"],
                "stack_trace": ev.get("stack_trace", []),
            })

    return merged


def collector_node(state: AgentState) -> dict:
    """Collect and analyze a Perfetto trace.

    Runs PerfettoCollector.pull_trace_from_device() + summarize().
    Priority: CLI args (from state) > WS server config cache > defaults.
    """
    from smartinspector.collector.perfetto import PerfettoCollector

    skip_wait = state.get("skip_wait", False)
    print("  [collector] Starting trace collection...", flush=True)

    # Notify app to ensure hooks are ready before collecting
    if skip_wait:
        print("  [collector] --no-wait: skipping app connection wait, starting trace immediately", flush=True)
    else:
        try:
            from smartinspector.ws.server import SIServer
            server = SIServer.get()
            if server.has_connections():
                print("  [collector] Sending start_trace, waiting for hook ACK...", flush=True)
                ack_ok = server.send_start_trace(timeout=5.0)
                if ack_ok:
                    print("  [collector] Hook ACK received, hooks ready", flush=True)
                else:
                    print("  [collector] Hook ACK timeout, proceeding anyway", flush=True)
            elif server.is_running():
                print("  [collector] No app connected, waiting for app to connect...", flush=True)
                connected = server.wait_for_connection(timeout=30.0)
                if connected:
                    print("  [collector] App connected, sending start_trace...", flush=True)
                    ack_ok = server.send_start_trace(timeout=5.0)
                    if ack_ok:
                        print("  [collector] Hook ACK received, hooks ready", flush=True)
                    else:
                        print("  [collector] Hook ACK timeout, proceeding anyway", flush=True)
                else:
                    print("  [collector] App connection timeout, proceeding without hook readiness check", flush=True)
            else:
                print("  [collector] WS server not running, proceeding without hook readiness check", flush=True)
        except Exception as e:
            print(f"  [collector] start_trace ACK failed: {e}", flush=True)

    try:
        # Read perfetto params: CLI args override WS config
        pc = _read_perfetto_config()
        duration_ms = state.get("trace_duration_ms") or int(pc.get("trace_duration_ms", 10000))
        buffer_size_kb = state.get("trace_buffer_size_kb") or int(pc.get("buffer_size_kb", 65536))
        target_process = state.get("trace_target_process") or pc.get("target_process", "") or None

        # Pass through full config from HookConfig
        cpu_sampling_interval_ms = int(pc.get("cpu_sampling_interval_ms", 1))

        categories_cfg = pc.get("categories")
        if isinstance(categories_cfg, str) and categories_cfg:
            categories = [c.strip() for c in categories_cfg.split(",") if c.strip()]
        elif isinstance(categories_cfg, list) and categories_cfg:
            categories = categories_cfg
        else:
            categories = None

        collect_cpu_callstacks = pc.get("collectCpuCallstacks", True)
        collect_java_heap = pc.get("collectJavaHeap", True)

        print(f"  [collector] Config: duration={duration_ms}ms, buffer={buffer_size_kb}KB", flush=True)

        trace_path = PerfettoCollector.pull_trace_from_device(
            duration_ms=duration_ms,
            target_process=target_process,
            buffer_size_kb=buffer_size_kb,
            categories=categories,
            cpu_sampling_interval_ms=cpu_sampling_interval_ms,
            collect_cpu_callstacks=collect_cpu_callstacks if target_process else False,
            collect_java_heap=collect_java_heap if target_process else False,
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
                    # Merge: SQL data as primary (has precise ts_ns), WS supplements stack_trace
                    sql_events = summary.block_events or []
                    ws_list = []
                    for ev in ws_events:
                        ws_list.append({
                            "msg_class": ev.get("msgClass", "Unknown"),
                            "dur_ms": ev.get("durationMs", 0),
                            "stack_trace": ev.get("stackTrace", []),
                        })

                    merged = _merge_block_events(sql_events, ws_list)
                    summary.block_events = merged
                    print(f"  [collector] Merged {len(sql_events)} SQL + {len(ws_list)} WS block events -> {len(merged)} total", flush=True)
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
        error_msg = (
            f"Trace collection failed: {e}\n\n"
            "Possible fixes:\n"
            "1. Ensure the Android device is connected via USB and adb is available\n"
            "2. Run `/trace` with a pre-existing trace file\n"
            "3. Use `/config` to check device connection status"
        )
        print(f"  [collector] ERROR: {error_msg}", flush=True)
        return {
            "messages": [AIMessage(content=error_msg)],
            "perf_summary": "",
            "perf_analysis": "",
            "attribution_data": "",
            "attribution_result": "",
            "_trace_path": "",
        }
