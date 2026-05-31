"""Collector node: trace collection (first step of full pipeline)."""

import json
import os
import subprocess

# Module-level trace path for headless/CI mode.
# LangGraph's state merge may lose _trace_path between orchestrator → collector
# when using MemorySaver checkpoint (observed with LangGraph 1.1.3).
# The headless runner sets this before invoking the graph.
_headless_trace_path: str = ""

from langchain_core.messages import AIMessage

from smartinspector.debug_log import debug_log, info_log
from smartinspector.graph.state import AgentState, RouteDecision


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
            info_log("collector", f"adb force-stop {package} succeeded")
            return True
        info_log("collector", f"WARNING: adb force-stop failed: {result.stderr.strip()}")
        return False
    except (FileNotFoundError, subprocess.TimeoutExpired) as e:
        info_log("collector", f"WARNING: adb force-stop unavailable: {e}")
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
            info_log("collector", f"adb monkey launch {package} succeeded")
            return True
        info_log("collector", f"WARNING: adb monkey launch failed: {result.stderr.strip()}")
        return False
    except (FileNotFoundError, subprocess.TimeoutExpired) as e:
        info_log("collector", f"WARNING: adb monkey launch unavailable: {e}")
        return False


def _adb_resolve_launcher(package: str) -> str | None:
    """Resolve the launcher activity component name for a package.

    Uses ``cmd package resolve-activity`` to find the MAIN/LAUNCHER
    activity, which is more reliable than ``am start -p`` on many
    Android versions.

    Returns:
        Component string like ``com.example/.MainActivity``, or None.
    """
    try:
        result = subprocess.run(
            ["adb", "shell", "cmd", "package", "resolve-activity",
             "--brief", "-c", "android.intent.category.LAUNCHER", package],
            capture_output=True, text=True, timeout=10,
        )
        for line in result.stdout.strip().splitlines():
            line = line.strip()
            if "/" in line and package in line:
                return line
    except (FileNotFoundError, subprocess.TimeoutExpired):
        pass
    return None


def _adb_launch_app(package: str) -> bool:
    """Launch an app via adb. Returns True on success.

    Strategy:
        1. Resolve launcher activity via ``cmd package resolve-activity``.
        2. Launch via ``am start -n component`` (most reliable).
        3. Fallback to ``am start -a MAIN -c LAUNCHER -p`` (less reliable).
        4. Fallback to ``monkey`` command.
    """
    # Strategy 1: resolve component, then am start -n
    component = _adb_resolve_launcher(package)
    if component:
        try:
            result = subprocess.run(
                ["adb", "shell", "am", "start", "-n", component],
                capture_output=True, text=True, timeout=10,
            )
            if result.returncode == 0 and "Error" not in result.stdout:
                info_log("collector", f"adb am start -n {component} succeeded")
                return True
            info_log("collector", f"WARNING: adb am start -n failed: {result.stderr.strip() or result.stdout.strip()}")
        except (FileNotFoundError, subprocess.TimeoutExpired) as e:
            info_log("collector", f"WARNING: adb am start -n unavailable: {e}")

    # Strategy 2: am start with intent flags
    try:
        result = subprocess.run(
            ["adb", "shell", "am", "start",
             "-a", "android.intent.action.MAIN",
             "-c", "android.intent.category.LAUNCHER",
             "-p", package],
            capture_output=True, text=True, timeout=10,
        )
        if result.returncode == 0 and "Error" not in result.stdout:
            info_log("collector", f"adb am start (intent) {package} succeeded")
            return True
        info_log("collector", f"WARNING: adb am start (intent) failed: {result.stderr.strip() or result.stdout.strip()}")
    except (FileNotFoundError, subprocess.TimeoutExpired) as e:
        info_log("collector", f"WARNING: adb am start (intent) unavailable: {e}")

    # Strategy 3: monkey command
    return _adb_launch_monkey(package)


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


def _print_instant_preview(perf_json: str) -> None:
    """Print a concise instant preview of key findings from perf data.

    Shows FPS, worst frame, main-thread blocking, CPU hotspots, and dimension
    severity immediately after SQL analysis — before waiting for LLM.
    Uses only lightweight field extraction, no compute_hints() to avoid
    redundant full computation (reporter will run that later).
    """
    try:
        data = json.loads(perf_json)
    except (json.JSONDecodeError, TypeError):
        return

    findings: list[str] = []

    # FPS & frame timeline
    ft = data.get("frame_timeline") or {}
    fps = ft.get("fps", 0)
    total = ft.get("total_frames", 0)
    jank = ft.get("jank_count", 0)
    if fps > 0:
        findings.append(f"帧率: {fps} FPS ({total} 帧, {jank} 卡顿)")

    # Worst frame
    slowest = ft.get("slowest_frames") or []
    if slowest:
        worst = slowest[0]
        dur = worst.get("dur_ms", 0)
        name = worst.get("name", "unknown")
        findings.append(f"最慢帧: {dur:.1f}ms ({name})")

    # Main-thread blocking
    blocks = data.get("block_events") or []
    if blocks:
        total_dur = sum(b.get("dur_ms", 0) for b in blocks)
        findings.append(f"主线程阻塞: {len(blocks)} 次 (共 {total_dur:.0f}ms)")

    # CPU hotspots
    hotspots = data.get("cpu_hotspots") or []
    cpu_list = [h for h in hotspots if isinstance(h, dict) and not h.get("error")]
    if cpu_list:
        top = cpu_list[0]
        fname = top.get("name", "?")
        pct = top.get("pct", 0)
        findings.append(f"CPU 热点: {fname} ({pct:.1f}%)")

    # Dimension severity hints (lightweight — first line only)
    dims = data.get("dimensions") or {}
    if dims:
        try:
            from smartinspector.collector.dimensions import DimensionRegistry, HintContext
            from smartinspector.agents.deterministic import _detect_frame_budget_ms
            DimensionRegistry.discover()
            fb = _detect_frame_budget_ms(data)
            ctx = HintContext(frame_budget_ms=fb)
            for dim in DimensionRegistry.all():
                dd = dims.get(dim.name)
                if not dd:
                    continue
                hint = dim.compute_hint(dd, ctx)
                if hint:
                    first_line = hint.split("\n")[0]
                    findings.append(first_line)
        except Exception:
            pass

    # P0 slices from view_slices (fast field extraction, no compute_hints)
    vs = (data.get("view_slices") or {}).get("slowest_slices") or []
    p0_slices = [s for s in vs if s.get("is_custom") and s.get("dur_ms", 0) > 16.67]
    for s in sorted(p0_slices, key=lambda x: -x.get("dur_ms", 0))[:3]:
        findings.append(f"P0: {s.get('name', '?')} ({s.get('dur_ms', 0):.1f}ms)")

    if findings:
        print("\n  ┌─ 即时快报 ─────────────────────────────", flush=True)  # noqa: LOG — user-facing progress
        for f in findings:
            print(f"  │ {f}", flush=True)  # noqa: LOG — user-facing progress
        print("  └─────────────────────────────────────────", flush=True)  # noqa: LOG — user-facing progress


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
    info_log("collector", f"Starting trace collection (route={route})...")

    # Check for pre-existing trace file early (skip WS wait + device collection)
    # Fallback to module-level var for headless mode (LangGraph state merge issue)
    preloaded_trace = state.get("_trace_path", "") or _headless_trace_path

    if not (preloaded_trace and os.path.isfile(preloaded_trace)):
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
                    info_log("collector", f"Cold start mode: force-stopping {cold_start_target}")
                    _adb_force_stop(cold_start_target)
                else:
                    info_log("collector",
                        "WARNING: adb not found in PATH, skipping cold start auto-launch. "
                        "Manually stop the app before tracing for best results."
                    )
                    cold_start_target = None  # Disable auto-launch
            else:
                info_log("collector", "WARNING: Cold start mode but no --target specified, skipping auto ADB launch")

        # Notify app to ensure hooks are ready before collecting
        if skip_wait:
            info_log("collector", "--no-wait: skipping app connection wait, starting trace immediately")
        else:
            try:
                from smartinspector.ws.server import SIServer
                server = SIServer.get()
                if server.has_connections():
                    info_log("collector", "Sending start_trace, waiting for hook ACK...")
                    ack_ok = server.send_start_trace(timeout=5.0)
                    if ack_ok:
                        info_log("collector", "Hook ACK received, hooks ready")
                    else:
                        info_log("collector", "WARNING: Hook ACK timeout, proceeding anyway")
                elif server.is_running():
                    info_log("collector", "No app connected, waiting for app to connect...")
                    connected = server.wait_for_connection(timeout=30.0)
                    if connected:
                        info_log("collector", "App connected, sending start_trace...")
                        ack_ok = server.send_start_trace(timeout=5.0)
                        if ack_ok:
                            info_log("collector", "Hook ACK received, hooks ready")
                        else:
                            info_log("collector", "WARNING: Hook ACK timeout, proceeding anyway")
                    else:
                        info_log("collector", "WARNING: App connection timeout, proceeding without hook readiness check")
                else:
                    info_log("collector", "WS server not running, proceeding without hook readiness check")
            except Exception as e:
                info_log("collector", f"WARNING: start_trace ACK failed: {e}")

    try:
        if preloaded_trace and os.path.isfile(preloaded_trace):
            info_log("collector", f"Pre-loaded trace file: {preloaded_trace} (skipping device collection)")
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

            collect_cpu_callstacks = pc.get("cpu_callstacks", True)
            collect_java_heap = pc.get("java_heap", True)

            info_log("collector", f"Config: duration={duration_ms}ms, buffer={buffer_size_kb}KB")

            # Auto-detect target_process from ADB if not set by user or app config.
            # This enables heap dump (android.java_hprof) and CPU callstack sampling
            # even when the user doesn't explicitly set --target.
            if not target_process:
                try:
                    # Use adb shell with pipe inside device shell
                    result = subprocess.run(
                        ["adb", "shell", "dumpsys activity activities 2>/dev/null | grep mResumedActivity | head -1"],
                        capture_output=True, text=True, timeout=5,
                    )
                    output = result.stdout.strip()
                    # Parse: "  mResumedActivity: ActivityRecord{... u0 com.example.app/.ActivityName ...}"
                    if "/" in output:
                        # Extract package before the "/"
                        before_slash = output.split("/")[0]
                        # Last whitespace-separated token is the package name
                        pkg = before_slash.strip().split()[-1]
                        if "." in pkg and not pkg.startswith("{"):
                            target_process = pkg
                            info_log("collector", f"Auto-detected target_process from foreground app: {target_process}")
                except Exception:
                    pass  # non-critical: proceed without target_process

            # Cold start: ensure target_process is set in state for downstream nodes
            if is_startup and cold_start_target and not target_process:
                target_process = cold_start_target

            # Build on_record_start callback for cold start: launch app while Perfetto records
            on_record_start = None
            if cold_start_target:
                _launch_target = cold_start_target
                def on_record_start():
                    info_log("collector", f"Cold start mode: launching {_launch_target} (during trace recording)")
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
            info_log("collector", f"Trace saved to {trace_path}")
            debug_log("collector", f"trace_path: {trace_path}")

        collector = PerfettoCollector(trace_path, target_process=target_process)
        summary = collector.summarize()

        # Request block events from app via WS (structured JSON, more reliable
        # than querying Perfetto's android_logs table which is often empty)
        try:
            from smartinspector.ws.server import SIServer
            server = SIServer.get()
            if server.has_connections():
                info_log("collector", "Requesting block events from app...")
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
                    info_log("collector", f"Merged {len(sql_events)} SQL + {len(ws_list)} WS block events -> {len(merged)} total")
                else:
                    info_log("collector", "No block events from app")
        except Exception as e:
            info_log("collector", f"WARNING: Block events request failed: {e}")

        perf_json = summary.to_json()

        info_log("collector", f"Analysis complete ({len(perf_json)} bytes)")

        # Instant preview: show key findings before LLM analysis begins
        _print_instant_preview(perf_json)

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
        info_log("collector", f"ERROR: {error_msg}")
        return {
            "messages": [AIMessage(content=error_msg)],
            "perf_summary": "",
            "perf_analysis": "",
            "attribution_data": "",
            "attribution_result": "",
            "_trace_path": "",
        }
