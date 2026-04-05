"""WebSocket server for SmartInspector CLI.

Runs on the host machine (lazily started on first /config or /connect).
The Android app connects as a WS client and syncs hook config.

Protocol (JSON):
    App → Server:
        {"type": "config_sync", "payload": <HookConfig JSON>}
        {"type": "config_request", "payload": null}

    Server → App:
        {"type": "config_update", "payload": <HookConfig JSON>}
        {"type": "config_response", "payload": <HookConfig JSON>}
        {"type": "start_trace", "payload": {"duration_ms": 10000, "target_process": "..."}}
"""

import asyncio
import json
import logging
import pathlib
import threading
import uuid
from typing import Callable

from smartinspector.config import get_ws_ping_timeout

logger = logging.getLogger(__name__)

_CONFIG_PATH = pathlib.Path.home() / ".smartinspector_config.json"

try:
    import websockets
except ImportError:
    websockets = None


class SIServer:
    """Singleton WebSocket server for SmartInspector CLI."""

    _instance = None
    _lock = threading.Lock()

    def __init__(self, port: int = 9876):
        self.port = port
        self._server = None
        self._loop = None
        self._thread = None
        self._connections: set = set()
        self._latest_config: str = ""  # latest config from app
        self._config_event = threading.Event()  # signals config received
        self._ready_event = threading.Event()
        self._on_message_handler: Callable | None = None
        self._pending_acks: dict[str, threading.Event] = {}
        self._latest_config: str = self._load_cached_config()

    @classmethod
    def get(cls, port: int = 9876) -> "SIServer":
        with cls._lock:
            if cls._instance is None:
                cls._instance = cls(port=port)
        return cls._instance

    def is_running(self) -> bool:
        return self._thread is not None and self._thread.is_alive()

    def start(self) -> None:
        """Start the WS server in a background daemon thread."""
        if self.is_running():
            return
        if websockets is None:
            print("  websockets not installed. Run: uv add websockets")
            return

        self._ready_event.clear()
        self._thread = threading.Thread(target=self._run_loop, daemon=True)
        self._thread.start()
        # Wait for server to be ready (or thread to die)
        if self._ready_event.wait(timeout=2.0):
            print(f"  WS server started on port {self.port}")
        elif self._thread.is_alive():
            print(f"  WS server starting on port {self.port} (still initializing)")
        else:
            print(f"  WS server FAILED to start on port {self.port} (check port conflict)")

    def stop(self) -> None:
        """Stop the WS server."""
        if self._loop is not None:
            asyncio.run_coroutine_threadsafe(self._shutdown(), self._loop)
        if self._thread is not None:
            self._thread.join(timeout=3)
        self._thread = None
        self._server = None

    def wait_for_config(self, timeout: float = 10.0) -> str:
        """Block until a config_sync message is received, or timeout.

        Returns the config JSON string, or empty string on timeout.
        """
        self._config_event.clear()
        self._config_event.wait(timeout=timeout)
        return self._latest_config

    def send_config(self, config_json: str, timeout: float = 5.0) -> bool:
        """Send a config_update to all connected apps.

        Returns True if at least one app received it.
        With ACK: waits for app confirmation within timeout.
        """
        if not self._connections:
            return False

        msg_id = str(uuid.uuid4())
        msg = json.dumps({
            "type": "config_update",
            "msg_id": msg_id,
            "payload": json.loads(config_json),
        })

        ack_event = threading.Event()
        self._pending_acks[msg_id] = ack_event

        future = asyncio.run_coroutine_threadsafe(self._broadcast(msg), self._loop)
        try:
            future.result(timeout=3)
            # Wait for ACK from any app
            ack_event.wait(timeout=timeout)
            if ack_event.is_set():
                self._persist_config(config_json)
            return ack_event.is_set()
        except Exception:
            return False
        finally:
            self._pending_acks.pop(msg_id, None)

    def get_config(self) -> str:
        """Return the latest config received from app."""
        return self._latest_config

    def request_block_events(self, timeout: float = 5.0) -> list[dict]:
        """Request cached block events from the connected app via WS.

        Sends {"type": "get_block_events"} and waits for
        {"type": "block_events", "payload": [...]}.

        Returns:
            List of block event dicts, or empty list on timeout/error.
        """
        if not self._connections or not self._loop:
            return []

        self._block_events_response = None
        self._block_events_event = threading.Event()

        msg = json.dumps({"type": "get_block_events"})
        future = asyncio.run_coroutine_threadsafe(self._broadcast(msg), self._loop)
        try:
            future.result(timeout=3)
        except Exception:
            return []

        # Wait for response
        self._block_events_event.wait(timeout=timeout)
        return self._block_events_response or []

    def has_connections(self) -> bool:
        return len(self._connections) > 0

    def on_message(self, handler: Callable) -> None:
        self._on_message_handler = handler

    # ── Config persistence ─────────────────────────────────────

    def _persist_config(self, config_json: str) -> None:
        """Save config to local cache file."""
        try:
            _CONFIG_PATH.write_text(config_json)
        except Exception as e:
            logger.debug("Failed to persist config: %s", e)

    @staticmethod
    def _load_cached_config() -> str:
        """Load config from local cache file."""
        try:
            if _CONFIG_PATH.exists():
                return _CONFIG_PATH.read_text()
        except Exception as e:
            logger.debug("Failed to load cached config: %s", e)
        return ""

    # ── Internal async ─────────────────────────────────────────

    def _run_loop(self) -> None:
        self._loop = asyncio.new_event_loop()
        asyncio.set_event_loop(self._loop)

        async def _serve():
            self._server = await websockets.serve(
                self._handler,
                "0.0.0.0",
                self.port,
                ping_interval=20,
                ping_timeout=get_ws_ping_timeout(),
            )
            self._ready_event.set()
            await asyncio.Future()  # run forever

        try:
            self._loop.run_until_complete(_serve())
        except OSError as e:
            print(f"  [ws] Failed to start: {e}")
        except Exception as e:
            print(f"  [ws] Unexpected error: {e}")

    async def _handler(self, ws) -> None:
        self._connections.add(ws)
        remote = ws.remote_address if hasattr(ws, "remote_address") else "?"
        print(f"  [ws] App connected: {remote}")
        try:
            async for raw in ws:
                try:
                    msg = json.loads(raw)
                except json.JSONDecodeError:
                    continue
                await self._dispatch(ws, msg)
        except websockets.exceptions.ConnectionClosed:
            pass
        finally:
            self._connections.discard(ws)
            print(f"  [ws] App disconnected: {remote}")

    async def _dispatch(self, ws, msg: dict) -> None:
        msg_type = msg.get("type", "")
        payload = msg.get("payload")

        if msg_type == "ack":
            # Handle ACK from app
            msg_id = msg.get("msg_id", "")
            event = self._pending_acks.get(msg_id)
            if event:
                event.set()
            return

        if msg_type == "config_sync":
            # App pushed its current config
            self._latest_config = json.dumps(payload) if isinstance(payload, dict) else str(payload)
            self._persist_config(self._latest_config)
            self._config_event.set()
            # Also notify external handler if registered
            if self._on_message_handler:
                self._on_message_handler(msg_type, payload)

        elif msg_type == "config_request":
            # App asked for server's config — respond with what we have
            resp = {"type": "config_response", "payload": None}
            if self._latest_config:
                resp["payload"] = json.loads(self._latest_config)
            await ws.send(json.dumps(resp))

        elif msg_type == "block_events":
            # App responded with cached block events
            if isinstance(payload, list):
                self._block_events_response = payload
            elif isinstance(payload, str):
                try:
                    self._block_events_response = json.loads(payload)
                except json.JSONDecodeError:
                    self._block_events_response = []
            else:
                self._block_events_response = []
            if hasattr(self, "_block_events_event") and self._block_events_event:
                self._block_events_event.set()

    async def _broadcast(self, msg: str) -> None:
        dead = set()
        for ws in self._connections:
            try:
                await ws.send(msg)
            except Exception:
                dead.add(ws)
        self._connections -= dead

    async def _shutdown(self) -> None:
        if self._server:
            self._server.close()
            await self._server.wait_closed()
