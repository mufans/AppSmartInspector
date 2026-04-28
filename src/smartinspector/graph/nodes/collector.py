"""Collector node: trace collection (first step of full pipeline)."""

import json
import logging
import os
import subprocess

from langchain_core.messages import AIMessage

from smartinspector.debug_log import debug_log
from smartinspector.graph.state import AgentState, RouteDecision

logger = logging.getLogger(__name__)


def _check_adb_available() -> bool:
    """Check if adb is available in PATH."""
    try:
        subprocess.run(
            ["adb", "version"],
            capture_output=True, text=True, timeout=3,
        )
        return True
    except (FileNotFoundError, subprocess.TimeoutExpired):
        return False


def _adb_force_stop(package: str) -> bool:
    """Force-stop an app via adb. Returns True on success."""
    try:
        result = subprocess.run(
            ["adb", "shell", "am", "force-stop", package],
            capture_output=True, text=True, timeout=10,
        )
        if result.returncode == 0:
            logger.info("adb force-stop %s succeeded", package)
            return True
        logger.warning("adb force-stop failed: %s", result.stderr.strip())
        return False
    except (FileNotFoundError, subprocess.TimeoutExpired) as e:
        logger.warning("adb force-stop unavailable: %s", e)
        return False


def _adb_launch_monkey(package: str) -> bool:
    """Launch an app via monkey command (fallback). Returns True on success."""
    try:
        result = subprocess.run(
            ["adb", "shell", "monkey", "-p", package, "-c",
             "android.intent.category.LAUNCHER", "1"],
            capture_output=True, text=True, timeout=10,
        )
        if result.returncode == 0:
            logger.info("adb monkey launch %s succeeded", package)
            return True
        logger.warning("adb monkey launch failed: %s", result.stderr.strip())
        return False
    except (FileNotFoundError, subprocess.TimeoutExpired) as e:
        logger.warning("adb monkey launch unavailable: %s", e)
        return False


def _adb_launch_app(package: str) -> bool:
    """Launch an app via adb using LAUNCHER intent. Returns True on success.

    Uses ``am start`` with the MAIN/LAUNCHER intent and package filter,
    which is more portable than resolving a specific activity name.

    Strategy:
        1. Try ``am start -a MAIN -c LAUNCHER -p {package}``.
        2. Fallback to ``monkey`` command if start fails.
    """
    try:
        result = subprocess.run(
            ["adb", "shell", "am", "start",
             "-a", "android.intent.action.MAIN",
             "-c", "android.intent.category.LAUNCHER",
             "-p", package],
            capture_output=True, text=True, timeout=10,
        )
        if result.returncode == 0:
            logger.info("adb am start (intent) %s succeeded", package)
            return True
        logger.warning("adb am start failed: %s", result.stderr.strip())

        # Fallback: monkey command
        return _adb_launch_monkey(package)
    except (FileNotFoundError, subprocess.TimeoutExpired) as e:
        logger.warning("adb launch unavailable: %s", e)
        return False


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
        debug_log("collector", f"config_sync raw JSON: {config_str}")
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

    # Clear stale trace data to force re-collection on full_analysis/startup routes.
    # Without this, a second /full would reuse the old _trace_path and skip device collection.
    route = state.get("_route", "")
    is_startup = route in (RouteDecision.STARTUP, RouteDecision.STARTUP.value)
    is_full = route in (RouteDecision.FULL_ANALYSIS, RouteDecision.FULL_ANALYSIS.value)
    if is_full or is_startup:
        state = {**state, "_trace_path": ""}

    skip_wait = state.get("skip_wait", False)
    logger.info("Starting trace collection (route=%s)...", route)

    # Cold start auto ADB launch: force-stop before trace, launch after
    cold_start_target = None
    if is_startup:
        pc_pre = _read_perfetto_config()
        cold_start_target = (
            state.get("trace_target_process")
            or pc_pre.get("target_process", "")
            or None
        )
        if cold_start_target:
            if _check_adb_available():
                logger.info("Cold start mode: force-stopping %s", cold_start_target)
                _adb_force_stop(cold_start_target)
            else:
                logger.warning(
                    "adb not found in PATH, skipping cold start auto-launch. "
                    "Manually stop the app before tracing for best results."
                )
                cold_start_target = None  # Disable auto-launch
        else:
            logger.warning("Cold start mode but no --target specified, skipping auto ADB launch")

    # Notify app to ensure hooks are ready before collecting
    if skip_wait:
        logger.info("--no-wait: skipping app connection wait, starting trace immediately")
    else:
        try:
            from smartinspector.ws.server import SIServer
            server = SIServer.get()
            if server.has_connections():
                logger.info("Sending start_trace, waiting for hook ACK...")
                ack_ok = server.send_start_trace(timeout=5.0)
                if ack_ok:
                    logger.info("Hook ACK received, hooks ready")
                else:
                    logger.warning("Hook ACK timeout, proceeding anyway")
            elif server.is_running():
                logger.info("No app connected, waiting for app to connect...")
                connected = server.wait_for_connection(timeout=30.0)
                if connected:
                    logger.info("App connected, sending start_trace...")
                    ack_ok = server.send_start_trace(timeout=5.0)
                    if ack_ok:
                        logger.info("Hook ACK received, hooks ready")
                    else:
                        logger.warning("Hook ACK timeout, proceeding anyway")
                else:
                    logger.warning("App connection timeout, proceeding without hook readiness check")
            else:
                logger.info("WS server not running, proceeding without hook readiness check")
        except Exception as e:
            logger.warning("start_trace ACK failed: %s", e)

    try:
        # Check for pre-existing trace file (skip device collection)
        preloaded_trace = state.get("_trace_path", "")
        if preloaded_trace and os.path.isfile(preloaded_trace):
            logger.info("Pre-loaded trace file: %s (skipping device collection)", preloaded_trace)
            trace_path = preloaded_trace
            target_process = state.get("trace_target_process") or None
        else:
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

            logger.info("Config: duration=%dms, buffer=%dKB", duration_ms, buffer_size_kb)

            # Cold start: ensure target_process is set in state for downstream nodes
            if is_startup and cold_start_target and not target_process:
                target_process = cold_start_target

            # Build on_record_start callback for cold start: launch app while Perfetto records
            on_record_start = None
            if cold_start_target:
                _launch_target = cold_start_target
                def on_record_start():
                    logger.info("Cold start mode: launching %s (during trace recording)", _launch_target)
                    _adb_launch_app(_launch_target)

            trace_path = PerfettoCollector.pull_trace_from_device(
                duration_ms=duration_ms,
                target_process=target_process,
                buffer_size_kb=buffer_size_kb,
                categories=categories,
                cpu_sampling_interval_ms=cpu_sampling_interval_ms,
                collect_cpu_callstacks=collect_cpu_callstacks if target_process else False,
                collect_java_heap=collect_java_heap if target_process else False,
                on_record_start=on_record_start,
            )
            logger.info("Trace saved to %s", trace_path)
            debug_log("collector", f"trace_path: {trace_path}")

        collector = PerfettoCollector(trace_path, target_process=target_process)
        summary = collector.summarize()

        # Request block events from app via WS (structured JSON, more reliable
        # than querying Perfetto's android_logs table which is often empty)
        try:
            from smartinspector.ws.server import SIServer
            server = SIServer.get()
            if server.has_connections():
                logger.info("Requesting block events from app...")
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
                    logger.info("Merged %d SQL + %d WS block events -> %d total", len(sql_events), len(ws_list), len(merged))
                else:
                    logger.info("No block events from app")
        except Exception as e:
            logger.warning("Block events request failed: %s", e)

        perf_json = summary.to_json()

        logger.info("Analysis complete (%d bytes)", len(perf_json))

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
        logger.error(error_msg)
        return {
            "messages": [AIMessage(content=error_msg)],
            "perf_summary": "",
            "perf_analysis": "",
            "attribution_data": "",
            "attribution_result": "",
            "_trace_path": "",
        }
