"""WebSocket communication layer for SmartInspector Agent ↔ App.

Architecture:
    CLI (WS server, lazy) ↔ App (WS client, started in Application.onCreate)
    JSON message protocol: {"type": "...", "payload": {...}}
"""
