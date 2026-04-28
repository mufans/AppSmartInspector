"""Bridge Server: connects self-hosted Perfetto UI to SI Agent.

Serves:
  - Static Perfetto UI files (from perfetto-build/ui/out/dist/)
  - WebSocket /bridge endpoint for the SI Bridge plugin

The Perfetto UI plugin (com.smartinspector.Bridge) connects to
ws://127.0.0.1:9877/bridge and sends frame_selected events.
This server forwards them to the frame_analyzer agent and returns results.
"""

import asyncio
import json
import logging
import os
import pathlib
import threading
from typing import Callable, Awaitable

logger = logging.getLogger(__name__)

_BRIDGE_PORT = 9877

# Perfetto UI static files directory (relative to project root)
_PROJECT_ROOT = pathlib.Path(__file__).resolve().parent.parent.parent.parent
_UI_DIST_DIR = _PROJECT_ROOT / "perfetto-build" / "ui" / "out" / "dist"

# MIME types for static file serving
_MIME_TYPES = {
    ".html": "text/html; charset=utf-8",
    ".js": "application/javascript; charset=utf-8",
    ".css": "text/css; charset=utf-8",
    ".json": "application/json; charset=utf-8",
    ".png": "image/png",
    ".svg": "image/svg+xml",
    ".ico": "image/x-icon",
    ".wasm": "application/wasm",
    ".map": "application/json",
}


class BridgeServer:
    """Async server that serves Perfetto UI + WebSocket bridge."""

    def __init__(
        self,
        port: int = _BRIDGE_PORT,
        ui_dir: str | pathlib.Path | None = None,
        on_frame_selected: Callable[[dict], Awaitable[dict]] | None = None,
        trace_path: str | None = None,
    ):
        self.port = port
        self.ui_dir = pathlib.Path(ui_dir) if ui_dir else _UI_DIST_DIR
        self.on_frame_selected = on_frame_selected
        self.trace_path = trace_path
        self._thread: threading.Thread | None = None
        self._loop: asyncio.AbstractEventLoop | None = None
        self._ws_clients: set = set()
        self._ready_event = threading.Event()
        self._server = None

    def is_running(self) -> bool:
        return self._thread is not None and self._thread.is_alive()

    def start(self) -> bool:
        """Start the bridge server in a background daemon thread."""
        if self.is_running():
            return True

        self._ready_event.clear()
        self._thread = threading.Thread(target=self._run_loop, daemon=True)
        self._thread.start()

        if self._ready_event.wait(timeout=5.0):
            print(f"  [bridge] Server ready on :{self.port}")
            return True
        else:
            print(f"  [bridge] Server failed to start on :{self.port}")
            return False

    def stop(self):
        """Stop the bridge server."""
        if self._loop and not self._loop.is_closed():
            asyncio.run_coroutine_threadsafe(self._shutdown(), self._loop)
        if self._thread:
            self._thread.join(timeout=3)
        self._thread = None

    # ── Internal async ─────────────────────────────────────────

    def _run_loop(self):
        self._loop = asyncio.new_event_loop()
        asyncio.set_event_loop(self._loop)
        try:
            self._loop.run_until_complete(self._serve())
        except OSError as e:
            logger.error("Bridge server failed to start: %s", e)
        except Exception as e:
            logger.error("Bridge server unexpected error: %s", e)

    async def _serve(self):
        import websockets

        self._server = await websockets.serve(
            self._ws_handler,
            "127.0.0.1",
            self.port,
            process_request=self._http_handler,
            ping_interval=20,
            ping_timeout=30,
        )
        self._ready_event.set()
        await asyncio.Future()  # run forever

    async def _shutdown(self):
        if self._server:
            self._server.close()
            await self._server.wait_closed()

    async def _ws_handler(self, ws):
        """Handle WebSocket connections from the Perfetto UI plugin."""
        self._ws_clients.add(ws)
        remote = ws.remote_address if hasattr(ws, "remote_address") else "?"
        logger.info("Plugin connected: %s", remote)
        try:
            async for raw in ws:
                try:
                    msg = json.loads(raw)
                except json.JSONDecodeError:
                    continue

                msg_type = msg.get("type", "")

                if msg_type == "frame_selected":
                    await self._handle_frame_selected(ws, msg.get("payload", {}))
                elif msg_type == "ping":
                    await ws.send(json.dumps({"type": "pong"}))
        except ConnectionError:
            pass
        finally:
            self._ws_clients.discard(ws)
            logger.info("Plugin disconnected: %s", remote)

    async def _handle_frame_selected(self, ws, payload: dict):
        """Forward frame selection to the agent and return results."""
        from smartinspector.debug_log import debug_log

        ts = payload.get("ts", 0)
        dur = payload.get("dur", 0)

        if not ts or not dur:
            await ws.send(json.dumps({
                "type": "analysis_error",
                "payload": {"error": "Missing ts or dur in payload"},
            }))
            return

        try:
            # Push progress to the frontend
            async def send_progress(step: str, detail: str = ""):
                debug_log("bridge", f"progress: {step} - {detail}")
                try:
                    await ws.send(json.dumps({
                        "type": "analysis_progress",
                        "payload": {"step": step, "detail": detail},
                    }))
                except Exception:
                    pass

            await send_progress("started", f"ts={ts} dur={dur}")
            debug_log("bridge", f"frame_selected: ts={ts} dur={dur} ({dur/1e6:.2f}ms)")

            if self.on_frame_selected:
                await send_progress("querying", "Querying trace data...")
                result = await self.on_frame_selected(payload, send_progress)
            else:
                result = await asyncio.get_event_loop().run_in_executor(
                    None, self._sync_analyze, ts, dur
                )

            await send_progress("done", "Analysis complete")
            await ws.send(json.dumps({
                "type": "analysis_result",
                "payload": result,
            }))
        except Exception as e:
            logger.exception("Frame analysis failed")
            debug_log("bridge", f"ERROR: {e}")
            await ws.send(json.dumps({
                "type": "analysis_error",
                "payload": {"error": str(e)},
            }))

    def _sync_analyze(self, ts: int, dur: int) -> dict:
        """Synchronous fallback for frame analysis."""
        from smartinspector.agents.frame_analyzer import analyze_frame
        from smartinspector.collector.perfetto import TraceServer

        # Get trace path from the running TraceServer or state
        trace_path = _get_active_trace_path()
        if not trace_path:
            return {"analysis": "No trace loaded. Use /trace first.", "error": True}

        analysis = analyze_frame(
            trace_path, ts, dur, _get_perf_summary(), _cached_attribution_result,
        )
        return {"analysis": analysis}

    # ── Static file serving ────────────────────────────────────

    def _http_handler(self, connection, request):
        """Serve static files for the Perfetto UI.

        In websockets >= 13, process_request receives (ServerConnection, Request).
        Returns a websockets.http11.Response or None to proceed with WS.
        """
        from websockets.http11 import Response as HTTPResponse
        from websockets.datastructures import Headers

        # Only intercept non-WebSocket (plain HTTP) requests
        upgrade = request.headers.get("Upgrade", "")
        if upgrade.lower() == "websocket":
            return None  # Let websockets handle WS upgrades

        status, headers_list, body = self._serve_static(request.path)

        return HTTPResponse(
            status_code=status,
            reason_phrase="OK" if status == 200 else "Error",
            headers=Headers(headers_list),
            body=body,
        )

    def _serve_static(self, raw_path: str):
        """Resolve a URL path to a static file and return (status, headers, body)."""
        import urllib.parse

        url_path = urllib.parse.unquote(raw_path.split("?")[0])

        # Route: /bridge is WS-only
        if url_path == "/bridge":
            return (400, [("Content-Type", "text/plain")],
                    b"This endpoint requires WebSocket")

        # Route: /trace.pb — serve the current trace file for auto-loading
        if url_path == "/trace.pb":
            if not self.trace_path or not pathlib.Path(self.trace_path).exists():
                return (404, [("Content-Type", "text/plain")],
                        b"No trace file available")
            try:
                body = pathlib.Path(self.trace_path).read_bytes()
            except OSError as e:
                return (500, [("Content-Type", "text/plain")],
                        f"Read error: {e}".encode())
            return (
                200,
                [
                    ("Content-Type", "application/octet-stream"),
                    ("Content-Length", str(len(body))),
                    ("Cache-Control", "no-cache"),
                    # Allow Perfetto UI JS to fetch this cross-origin
                    ("Access-Control-Allow-Origin", "*"),
                ],
                body,
            )

        # Map to file
        if url_path == "/" or url_path == "":
            url_path = "/index.html"

        file_path = self.ui_dir / url_path.lstrip("/")

        # Security: prevent path traversal
        try:
            file_path = file_path.resolve()
            ui_root = self.ui_dir.resolve()
            if not str(file_path).startswith(str(ui_root)):
                return (403, [("Content-Type", "text/plain")], b"Forbidden")
        except (ValueError, OSError):
            return (403, [("Content-Type", "text/plain")], b"Forbidden")

        if not file_path.exists():
            # SPA fallback: serve index.html for unknown routes
            file_path = self.ui_dir / "index.html"
            if not file_path.exists():
                return (
                    404,
                    [("Content-Type", "text/plain")],
                    f"Not found: {url_path}\n\nTo build the Perfetto UI, run:\n  ./perfetto-plugin/build.sh".encode(),
                )

        # Read and serve
        try:
            body = file_path.read_bytes()
        except OSError as e:
            return (500, [("Content-Type", "text/plain")], f"Read error: {e}".encode())

        ext = file_path.suffix.lower()
        content_type = _MIME_TYPES.get(ext, "application/octet-stream")

        return (
            200,
            [
                ("Content-Type", content_type),
                ("Content-Length", str(len(body))),
                ("Cache-Control", "no-cache"),
            ],
            body,
        )


# ── Global state helpers ──────────────────────────────────────

_active_bridge: BridgeServer | None = None
_active_trace_server = None  # TraceServer instance if running
_cached_perf_summary: str = ""
_cached_attribution_result: str = ""


def _get_active_trace_path() -> str:
    """Get the trace path from the active TraceServer."""
    if _active_trace_server:
        return _active_trace_server.trace_path
    return ""


def _get_perf_summary() -> str:
    """Get the current perf_summary from global state."""
    return _cached_perf_summary


def start_bridge(
    trace_path: str,
    port: int = _BRIDGE_PORT,
    perf_summary: str = "",
    attribution_result: str = "",
) -> BridgeServer:
    """Start the bridge server with frame analysis wired up.

    Args:
        trace_path: Path to the .pb trace file.
        port: Port to serve on (default 9877).
        perf_summary: Existing perf_summary JSON for context.

    Returns:
        The running BridgeServer instance.
    """
    global _active_bridge, _active_trace_server

    # Ensure debug logging is active for bridge sessions
    import os
    if not os.environ.get("SI_DEBUG"):
        os.environ["SI_DEBUG"] = "1"

    # Start TraceServer (trace_processor_shell HTTP mode)
    from smartinspector.collector.perfetto import TraceServer

    trace_server = TraceServer(trace_path, port=9001)
    print(f"  [bridge] Starting trace_processor_shell on :9001...", flush=True)
    if not trace_server.start():
        logger.warning("TraceServer failed to start, /frame SQL queries will use file mode")
    _active_trace_server = trace_server

    # Store perf_summary and attribution_result for analysis context
    _perf_summary_cache = perf_summary
    _attribution_result_cache = attribution_result

    # Also store at module level for _sync_analyze fallback
    global _cached_perf_summary, _cached_attribution_result
    _cached_perf_summary = perf_summary
    _cached_attribution_result = attribution_result

    async def on_frame_selected(payload: dict, send_progress=None) -> dict:
        from smartinspector.debug_log import debug_log

        ts = int(payload.get("ts", 0))
        dur = int(payload.get("dur", 0))

        loop = asyncio.get_event_loop()

        async def progress(step: str, detail: str = ""):
            debug_log("bridge", f"{step}: {detail}")
            if send_progress:
                await send_progress(step, detail)

        # Sync callback that bridges progress from thread pool to async WS
        def on_progress(msg: str):
            if not send_progress:
                return
            future = asyncio.run_coroutine_threadsafe(
                send_progress("progress", msg), loop,
            )
            # Log any errors from the scheduled coroutine
            def _log_result(fut):
                try:
                    fut.result()
                except Exception as exc:
                    debug_log("bridge", f"on_progress send failed: {exc}")
            future.add_done_callback(_log_result)

        await progress("querying", "Querying trace slices...")
        from smartinspector.agents.frame_analyzer import analyze_frame
        analysis = await loop.run_in_executor(
            None, analyze_frame, trace_path, ts, dur, _perf_summary_cache,
            _attribution_result_cache, on_progress,
        )
        return {"analysis": analysis}

    bridge = BridgeServer(
        port=port,
        on_frame_selected=on_frame_selected,
        trace_path=trace_path,
    )
    bridge.start()
    _active_bridge = bridge
    return bridge


def stop_bridge():
    """Stop the bridge server and trace server."""
    global _active_bridge, _active_trace_server
    if _active_bridge:
        _active_bridge.stop()
        _active_bridge = None
    if _active_trace_server:
        _active_trace_server.stop()
        _active_trace_server = None


def open_browser(url: str):
    """Open URL in the default browser."""
    import subprocess
    import sys
    if sys.platform == "darwin":
        subprocess.Popen(["open", url])
    elif sys.platform.startswith("linux"):
        subprocess.Popen(["xdg-open", url])
    else:
        subprocess.Popen(["cmd", "/c", "start", url])
