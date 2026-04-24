"""Slash command registry for SmartInspector CLI."""

from smartinspector.commands.device import cmd_devices, cmd_connect, cmd_status, cmd_disconnect
from smartinspector.commands.trace import cmd_trace, cmd_record, cmd_analyze, cmd_frame, cmd_open, cmd_close
from smartinspector.commands.hook import cmd_config, cmd_hooks, cmd_hook, cmd_debug
from smartinspector.commands.session import cmd_help, cmd_clear, cmd_summary, cmd_tokens
from smartinspector.commands.orchestrate import cmd_full, cmd_report
from smartinspector.commands.compare import cmd_compare

# Command registry: name → handler function
SLASH_COMMANDS = {
    "/help": cmd_help,
    "/devices": cmd_devices,
    "/connect": cmd_connect,
    "/status": cmd_status,
    "/disconnect": cmd_disconnect,
    "/trace": cmd_trace,
    "/record": cmd_record,
    "/analyze": cmd_analyze,
    "/frame": cmd_frame,
    "/open": cmd_open,
    "/close": cmd_close,
    "/config": cmd_config,
    "/hooks": cmd_hooks,
    "/hook": cmd_hook,
    "/debug": cmd_debug,
    "/clear": cmd_clear,
    "/summary": cmd_summary,
    "/tokens": cmd_tokens,
    "/full": cmd_full,
    "/report": cmd_report,
    "/compare": cmd_compare,
}


def handle_slash_command(user_input: str, state: dict) -> dict:
    """Parse and execute a slash command.

    Args:
        user_input: Raw user input starting with '/'.
        state: Current session state dict (messages, perf_summary, etc.).

    Returns:
        Updated state dict.
    """
    parts = user_input.strip().split(maxsplit=1)
    cmd_name = parts[0].lower()
    cmd_args = parts[1] if len(parts) > 1 else ""

    handler = SLASH_COMMANDS.get(cmd_name)
    if handler is None:
        print(f"Unknown command: {cmd_name}")
        print(f"Available commands: {', '.join(sorted(SLASH_COMMANDS.keys()))}")
        return state

    return handler(cmd_args, state)
