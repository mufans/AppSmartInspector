"""Hook configuration commands: /config, /hooks, /hook.

Uses WS (via adb forward) for config sync with the app.
"""

import json
import subprocess

from smartinspector.ws.server import SIServer

WS_PORT = 9876


def _ensure_server(state: dict) -> SIServer:
    """Ensure WS server is running and adb forward is set up. Returns the server."""
    server = SIServer.get(port=WS_PORT)
    if not server.is_running():
        server.start()
        state["_ws_server"] = server
        try:
            subprocess.run(
                ["adb", "reverse", f"tcp:{WS_PORT}", f"tcp:{WS_PORT}"],
                capture_output=True, text=True, timeout=5,
            )
            print(f"  WS server started, adb forward tcp:{WS_PORT} → tcp:{WS_PORT}")
        except Exception as e:
            print(f"  Warning: adb forward failed: {e}")
    return server


def _wait_for_app(server: SIServer, timeout: float = 10.0) -> str:
    """Trigger app reconnect and wait for config_sync. Returns config JSON or ''."""
    print("  Waiting for app to connect...")
    # Trigger app WS reconnect via broadcast
    try:
        subprocess.run(
            ["adb", "shell", "am", "broadcast", "-a", "com.smartinspector.WS_RECONNECT"],
            capture_output=True, text=True, timeout=5,
        )
    except Exception:
        pass
    config = server.wait_for_config(timeout=timeout)
    return config


def cmd_config(args: str, state: dict) -> dict:
    """View or update hook configuration via WS.

    Usage:
        /config          — show current config (from app via WS)
        /config reset    — reset to defaults
        /config <json>   — push config JSON to app
        /config source_dir <path> — set source code search directory
    """
    # Handle source_dir subcommand
    if args.strip().startswith("source_dir"):
        from smartinspector.config import get_source_dir, set_source_dir

        parts = args.strip().split(maxsplit=1)
        if len(parts) < 2:
            print(f"Current source dir: {get_source_dir()}")
            return state

        new_dir = parts[1].strip()
        import os
        expanded = os.path.expanduser(new_dir)
        if not os.path.isdir(expanded):
            print(f"  Directory not found: {expanded}")
            return state

        set_source_dir(new_dir)
        print(f"  Source dir set to: {get_source_dir()}")
        return state

    server = _ensure_server(state)

    if not args:
        # Show current config
        if server.has_connections():
            # Already connected — config should be cached from last sync
            config = server.get_config()
            if config:
                print("Current config:")
                print(config)
            else:
                print("  App connected but no config received yet. Try again.")
        else:
            # Try to connect app
            config = _wait_for_app(server, timeout=10)
            if config:
                print("Current config:")
                print(config)
            else:
                print("  Timed out. Is the app running on device?")
        return state

    if args.strip() == "reset":
        defaults = {
            "activity_lifecycle": True,
            "fragment_lifecycle": True,
            "rv_pipeline": True,
            "rv_adapter": True,
            "layout_inflate": False,
            "view_traverse": False,
            "handler_dispatch": False,
            "extra_hooks": [],
        }
        config_json = json.dumps(defaults, indent=2)
        print("Resetting config to defaults...")
        if server.has_connections():
            server.send_config(config_json)
            print("  Pushed to app via WS.")
        else:
            print("  No app connected. Will sync on next connection.")
        return state

    # Try to parse as JSON and push to app
    try:
        config = json.loads(args)
        config_json = json.dumps(config, indent=2)
        print("Pushing config...")
        if server.has_connections():
            server.send_config(config_json)
            print("  Done.")
        else:
            config = _wait_for_app(server, timeout=5)
            if server.has_connections():
                server.send_config(config_json)
                print("  Done.")
            else:
                print("  No app connected. Config cached locally.")
        return state
    except json.JSONDecodeError:
        print("Invalid JSON. Usage: /config '{\"rv_adapter\": false}'")
        return state


def cmd_hooks(args: str, state: dict) -> dict:
    """List all available hooks and their status.

    Usage: /hooks
    """
    hooks = [
        ("activity_lifecycle", "Activity lifecycle (onCreate, onResume, etc.)", True),
        ("fragment_lifecycle", "Fragment lifecycle (onCreate, onCreateView, etc.)", True),
        ("rv_pipeline", "RecyclerView pipeline (dispatchLayoutStep, onDraw)", True),
        ("rv_adapter", "RecyclerView adapter (onBindViewHolder, onCreateViewHolder)", True),
        ("layout_inflate", "LayoutInflater.inflate calls", False),
        ("view_traverse", "View.measure/layout/draw (non-RV)", False),
        ("handler_dispatch", "Handler.dispatchMessage (main thread)", False),
    ]

    server = SIServer.get(port=WS_PORT)
    app_config = None
    if server.get_config():
        try:
            app_config = json.loads(server.get_config())
        except Exception:
            pass

    print("Available hooks:")
    print(f"  {'Hook ID':<22} {'Default':<10} {'App':<10} Description")
    print(f"  {'-'*22} {'-'*10} {'-'*10} {'-'*40}")
    for hook_id, desc, default in hooks:
        default_str = "ON" if default else "OFF"
        app_str = ""
        if app_config:
            app_val = app_config.get(hook_id, default)
            app_str = "ON" if app_val else "OFF"
        print(f"  {hook_id:<22} {default_str:<10} {app_str:<10} {desc}")

    print()
    print("Use /config to change settings, /hook add/rm for extra hooks.")
    return state


def cmd_hook(args: str, state: dict) -> dict:
    """Manage individual hooks or extra hooks.

    Usage:
        /hook on <hook_id>     — enable a built-in hook
        /hook off <hook_id>    — disable a built-in hook
        /hook add <class> <method>  — add extra hook
        /hook rm <class>       — remove extra hook
    """
    parts = args.strip().split()
    if not parts:
        print("Usage:")
        print("  /hook on <hook_id>          — enable a built-in hook")
        print("  /hook off <hook_id>         — disable a built-in hook")
        print("  /hook add <class> <method>  — add extra hook")
        print("  /hook rm <class>            — remove extra hook")
        return state

    action = parts[0]
    server = _ensure_server(state)

    # Start from current config
    config = {}
    local = server.get_config()
    if local:
        try:
            config = json.loads(local)
        except Exception:
            pass

    if action in ("on", "off"):
        if len(parts) < 2:
            print(f"Usage: /hook {action} <hook_id>")
            return state
        hook_id = parts[1]
        config[hook_id] = (action == "on")
        config_json = json.dumps(config, indent=2)
        print(f"Setting {hook_id} = {action == 'on'}...")
        if server.has_connections():
            server.send_config(config_json)
            print("  Pushed to app via WS.")
        else:
            print("  No app connected. Will sync when app connects.")

    elif action == "add":
        if len(parts) < 3:
            print("Usage: /hook add <class_name> <method_name>")
            return state
        class_name = parts[1]
        method_name = parts[2]
        extra_hooks = config.get("extra_hooks", [])
        found = False
        for eh in extra_hooks:
            if eh.get("class_name") == class_name:
                if method_name not in eh.get("methods", []):
                    eh.setdefault("methods", []).append(method_name)
                found = True
                break
        if not found:
            extra_hooks.append({
                "class_name": class_name,
                "methods": [method_name],
                "enabled": True,
            })
        config["extra_hooks"] = extra_hooks
        config_json = json.dumps(config, indent=2)
        print(f"Adding extra hook: {class_name}.{method_name}...")
        if server.has_connections():
            server.send_config(config_json)
            print("  Pushed to app via WS.")
        else:
            print("  No app connected. Will sync when app connects.")

    elif action == "rm":
        if len(parts) < 2:
            print("Usage: /hook rm <class_name>")
            return state
        class_name = parts[1]
        extra_hooks = config.get("extra_hooks", [])
        config["extra_hooks"] = [eh for eh in extra_hooks if eh.get("class_name") != class_name]
        config_json = json.dumps(config, indent=2)
        print(f"Removing extra hook: {class_name}...")
        if server.has_connections():
            server.send_config(config_json)
            print("  Pushed to app via WS.")
        else:
            print("  No app connected. Will sync when app connects.")

    else:
        print(f"Unknown action: {action}")
        print("Use: on, off, add, rm")

    return state


def cmd_debug(args: str, state: dict) -> dict:
    """Open the debug HookConfigActivity on the connected device.

    Usage: /debug
    """
    try:
        result = subprocess.run(
            [
                "adb", "shell", "am", "start",
                "-n", "com.smartinspector.hook/com.smartinspector.tracelib.HookConfigActivity",
            ],
            capture_output=True, text=True, timeout=5,
        )
        if result.returncode == 0:
            print("  Debug config activity launched on device.")
        else:
            print(f"  Failed: {result.stderr.strip()}")
    except FileNotFoundError:
        print("  adb not found. Install Android platform tools.")
    except subprocess.TimeoutExpired:
        print("  adb timed out. Is a device connected?")
    except Exception as e:
        print(f"  Error: {e}")
    return state
