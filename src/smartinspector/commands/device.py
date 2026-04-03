"""Device management commands: /devices, /connect, /status, /disconnect."""

import subprocess


def _adb_cmd(args: list[str], timeout: int = 5) -> str:
    """Run an adb command and return stdout."""
    try:
        result = subprocess.run(
            ["adb"] + args,
            capture_output=True, text=True, timeout=timeout,
        )
        return result.stdout.strip()
    except FileNotFoundError:
        return "ERROR: adb not found. Install Android platform tools."
    except subprocess.TimeoutExpired:
        return "ERROR: adb command timed out."


def cmd_devices(args: str, state: dict) -> dict:
    """List connected Android devices."""
    output = _adb_cmd(["devices", "-l"])
    if not output or "List of devices" not in output:
        print("No devices found or adb not available.")
        return state

    lines = output.splitlines()
    devices = [l for l in lines[1:] if l.strip() and not l.startswith("*")]
    if not devices:
        print("No devices connected.")
    else:
        print(f"Connected devices ({len(devices)}):")
        for line in devices:
            print(f"  {line}")

    return state


def cmd_connect(args: str, state: dict) -> dict:
    """Connect to a device (adb connect)."""
    if not args:
        print("Usage: /connect <host:port>")
        print("Example: /connect 192.168.1.100:5555")
        return state

    output = _adb_cmd(["connect", args])
    print(output)
    state["_device"] = args
    return state


def cmd_status(args: str, state: dict) -> dict:
    """Show current session status."""
    print("Session status:")
    print(f"  Messages: {len(state.get('messages', []))}")

    perf = state.get("perf_summary", "")
    if perf:
        print(f"  Perf summary: loaded ({len(perf)} chars)")
    else:
        print("  Perf summary: (none)")

    analysis = state.get("perf_analysis", "")
    if analysis:
        print(f"  Perf analysis: loaded ({len(analysis)} chars)")
    else:
        print("  Perf analysis: (none)")

    device = state.get("_device", "")
    if device:
        print(f"  Device: {device}")
    else:
        # Check adb default
        output = _adb_cmd(["get-state"])
        if "device" in output:
            print(f"  Device: connected via USB")
        else:
            print("  Device: not connected")

    return state


def cmd_disconnect(args: str, state: dict) -> dict:
    """Disconnect from current device."""
    device = state.get("_device", "")
    if device:
        output = _adb_cmd(["disconnect", device])
        print(output)
        state.pop("_device", None)
    else:
        print("No remote device to disconnect.")
    return state
