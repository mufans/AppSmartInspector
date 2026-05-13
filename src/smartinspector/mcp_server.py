"""MCP Server for SmartInspector — exposes CLI commands as MCP tools."""

import io
import json
import sys
from contextlib import redirect_stdout
from typing import Any

from mcp.server.fastmcp import FastMCP

from smartinspector.debug_log import info_log

# ---------------------------------------------------------------------------
# Global session state — persists across tool calls within one MCP session
# ---------------------------------------------------------------------------
_session_state: dict[str, Any] = {
    "messages": [],
    "perf_summary": "",
    "perf_analysis": "",
    "attribution_data": "",
    "attribution_result": "",
    "_trace_path": "",
    "_device": "",
}


def _apply_source_dir(source_dir: str | None) -> None:
    """Set source code search directory if provided.

    Args:
        source_dir: Path to source code root, or None to skip.
    """
    if source_dir:
        from smartinspector.config import set_source_dir
        set_source_dir(source_dir)
        info_log("mcp", f"Source dir set to: {source_dir}")


def _run_command(handler, args: str) -> str:
    """Execute a slash command handler and capture its stdout output.

    Args:
        handler: Command handler function with signature (args: str, state: dict) -> dict.
        args: Argument string to pass to the handler.

    Returns:
        Captured stdout text from the command execution.
    """
    global _session_state  # noqa: PLW0603

    buf = io.StringIO()
    with redirect_stdout(buf):
        try:
            _session_state = handler(args, _session_state)
        except Exception as e:
            info_log("mcp", f"ERROR: command failed: {e}")
            return f"Error: {e}"

    output = buf.getvalue().strip()
    return output or "Done."


# ---------------------------------------------------------------------------
# MCP Server instance
# ---------------------------------------------------------------------------
mcp = FastMCP(
    name="SmartInspector",
    instructions=(
        "SmartInspector is an AI-powered Android/HarmonyOS performance analysis tool. "
        "Use these tools to collect Perfetto traces, analyze performance, attribute "
        "issues to source code, and generate reports."
    ),
)


# ---------------------------------------------------------------------------
# Trace & Analysis tools
# ---------------------------------------------------------------------------

@mcp.tool(title="Full Analysis")
async def si_full(
    duration_ms: int | None = None,
    package_name: str | None = None,
    source_dir: str | None = None,
    no_wait: bool = False,
    debug: bool = False,
) -> str:
    """Run the full analysis pipeline: collect trace -> analyze -> attribute -> report.

    This is the primary entry point for comprehensive performance analysis.

    Args:
        duration_ms: Trace duration in milliseconds (100-60000). Default: 10000.
        package_name: Target app package name (e.g. com.example.app).
        source_dir: Source code directory for attribution (maps code hotspots to source files).
        no_wait: Skip waiting for app connection, start trace immediately.
        debug: Enable debug logging to reports/debug_*.log.

    Returns:
        Full performance analysis report in markdown.
    """
    from smartinspector.commands.orchestrate import cmd_full

    _apply_source_dir(source_dir)

    parts = []
    if no_wait:
        parts.append("--no-wait")
    if debug:
        parts.append("--debug")
    if duration_ms is not None:
        parts.append(str(duration_ms))
    if package_name:
        parts.append(package_name)

    return _run_command(cmd_full, " ".join(parts))


@mcp.tool(title="Trace Collection + Analysis")
async def si_trace(
    duration_ms: int | None = None,
    package_name: str | None = None,
    source_dir: str | None = None,
) -> str:
    """Collect a Perfetto trace and analyze it (stops before attribution/report).

    Args:
        duration_ms: Trace duration in milliseconds (100-60000). Default: 10000.
        package_name: Target app package name.
        source_dir: Source code directory for attribution.

    Returns:
        Analysis summary of the collected trace.
    """
    from smartinspector.commands.trace import cmd_trace

    _apply_source_dir(source_dir)

    parts = []
    if duration_ms is not None:
        parts.append(str(duration_ms))
    if package_name:
        parts.append(package_name)

    return _run_command(cmd_trace, " ".join(parts))


@mcp.tool(title="Record Trace")
async def si_record(
    duration_ms: int | None = None,
    package_name: str | None = None,
) -> str:
    """Record a Perfetto trace without analysis.

    Args:
        duration_ms: Trace duration in milliseconds (100-60000). Default: 10000.
        package_name: Target app package name.

    Returns:
        Path to the recorded trace file.
    """
    from smartinspector.commands.trace import cmd_record

    parts = []
    if duration_ms is not None:
        parts.append(str(duration_ms))
    if package_name:
        parts.append(package_name)

    return _run_command(cmd_record, " ".join(parts))


@mcp.tool(title="Analyze Trace")
async def si_analyze(
    trace_path: str | None = None,
    source_dir: str | None = None,
) -> str:
    """Analyze a Perfetto trace file.

    Uses the last recorded trace if no path is provided.

    Args:
        trace_path: Path to the .pb trace file. Uses last recorded trace if omitted.
        source_dir: Source code directory for attribution.

    Returns:
        Performance analysis results in markdown.
    """
    from smartinspector.commands.trace import cmd_analyze

    _apply_source_dir(source_dir)

    return _run_command(cmd_analyze, trace_path or "")


@mcp.tool(title="Frame Analysis")
async def si_frame(
    ts: str,
    dur: str,
    source_dir: str | None = None,
) -> str:
    """Analyze a specific frame/slice from a loaded Perfetto trace.

    Requires a trace to be already loaded (via si_trace, si_record, or si_analyze).

    Args:
        ts: Start timestamp (supports ns, us, ms suffixes, e.g. '1234ms', '500000us').
        dur: Duration of the frame (supports ns, us, ms suffixes).
        source_dir: Source code directory for attribution.

    Returns:
        Frame-level analysis results.
    """
    from smartinspector.commands.trace import cmd_frame

    _apply_source_dir(source_dir)

    args = f"ts={ts} dur={dur}"
    return _run_command(cmd_frame, args)


@mcp.tool(title="Cold Start Analysis")
async def si_startup(
    package_name: str,
    source_dir: str | None = None,
) -> str:
    """Analyze app cold start performance: force-stop -> trace -> launch -> analyze.

    Args:
        package_name: Target app package name (e.g. com.example.app).
        source_dir: Source code directory for attribution.

    Returns:
        Cold start analysis report with phase breakdown.
    """
    from smartinspector.commands.orchestrate import cmd_startup

    _apply_source_dir(source_dir)

    return _run_command(cmd_startup, package_name)


@mcp.tool(title="Quick Analysis")
async def si_quick(
    trace_path: str | None = None,
    source_dir: str | None = None,
) -> str:
    """Run fast deterministic analysis without LLM calls.

    Pure computation: collector -> deterministic hints -> fast-path attribution.
    No LLM API calls, suitable for quick feedback during development.

    Args:
        trace_path: Path to the .pb trace file. Uses last recorded trace if omitted.
        source_dir: Source code directory for attribution.

    Returns:
        Quick analysis report in markdown.
    """
    from smartinspector.commands.quick import cmd_quick

    _apply_source_dir(source_dir)

    return _run_command(cmd_quick, trace_path or "")


@mcp.tool(title="Generate Report")
async def si_report(output_path: str | None = None) -> str:
    """Generate a performance report from previously collected analysis data.

    Requires prior analysis via si_full, si_trace, or si_analyze.

    Args:
        output_path: Optional file path to save the report markdown.

    Returns:
        Generated performance report in markdown.
    """
    from smartinspector.commands.orchestrate import cmd_report

    return _run_command(cmd_report, output_path or "")


# ---------------------------------------------------------------------------
# Comparison tool
# ---------------------------------------------------------------------------

@mcp.tool(title="Compare Reports")
async def si_compare(
    report1: str | None = None,
    report2: str | None = None,
    mode: str = "latest",
) -> str:
    """Compare two performance analysis reports and show trends.

    Modes:
        - 'latest': Compare the two most recent reports.
        - 'list': List all saved reports.
        - 'files': Compare two specific report files (provide report1 and report2).

    Args:
        report1: Path to the first report file (for 'files' mode).
        report2: Path to the second report file (for 'files' mode).
        mode: Comparison mode: 'latest', 'list', or 'files'.

    Returns:
        Comparison report with metric deltas and trend indicators.
    """
    from smartinspector.commands.compare import cmd_compare

    if mode == "list":
        return _run_command(cmd_compare, "list")
    if mode == "files" and report1 and report2:
        return _run_command(cmd_compare, f"{report1} {report2}")
    # Default: latest
    return _run_command(cmd_compare, "latest")


# ---------------------------------------------------------------------------
# Device management tools
# ---------------------------------------------------------------------------

@mcp.tool(title="List Devices")
async def si_devices() -> str:
    """List connected Android devices via adb.

    Returns:
        List of connected devices with details.
    """
    from smartinspector.commands.device import cmd_devices

    return _run_command(cmd_devices, "")


@mcp.tool(title="Connect Device")
async def si_connect(host_port: str) -> str:
    """Connect to an Android device via ADB TCP.

    Args:
        host_port: Device address in host:port format (e.g. '192.168.1.100:5555').

    Returns:
        Connection result message.
    """
    from smartinspector.commands.device import cmd_connect

    return _run_command(cmd_connect, host_port)


@mcp.tool(title="Session Status")
async def si_status() -> str:
    """Show current session status: loaded trace, analysis, device connection.

    Returns:
        Session status summary.
    """
    from smartinspector.commands.device import cmd_status

    return _run_command(cmd_status, "")


@mcp.tool(title="Disconnect Device")
async def si_disconnect() -> str:
    """Disconnect from the current remote device.

    Returns:
        Disconnection result message.
    """
    from smartinspector.commands.device import cmd_disconnect

    return _run_command(cmd_disconnect, "")


# ---------------------------------------------------------------------------
# Hook configuration tools
# ---------------------------------------------------------------------------

@mcp.tool(title="Hook Configuration")
async def si_config(
    action: str = "show",
    json_config: str | None = None,
    source_dir: str | None = None,
) -> str:
    """View or update hook configuration for trace collection.

    Actions:
        - 'show': Display current configuration.
        - 'reset': Reset to default configuration.
        - 'set': Push JSON configuration to the app (provide json_config).
        - 'source_dir': Set source code search directory (provide source_dir).

    Args:
        action: Action to perform: 'show', 'reset', 'set', or 'source_dir'.
        json_config: JSON configuration string (for 'set' action).
        source_dir: Source code directory path (for 'source_dir' action).

    Returns:
        Configuration status or current settings.
    """
    from smartinspector.commands.hook import cmd_config

    if action == "source_dir" and source_dir:
        return _run_command(cmd_config, f"source_dir {source_dir}")
    if action == "reset":
        return _run_command(cmd_config, "reset")
    if action == "set" and json_config:
        return _run_command(cmd_config, json_config)
    # Default: show
    return _run_command(cmd_config, "")


@mcp.tool(title="List Hooks")
async def si_hooks() -> str:
    """List all available trace hooks and their current status.

    Returns:
        Table of hooks with their IDs, default status, and descriptions.
    """
    from smartinspector.commands.hook import cmd_hooks

    return _run_command(cmd_hooks, "")


@mcp.tool(title="Manage Hook")
async def si_hook(
    action: str,
    hook_id: str | None = None,
    class_name: str | None = None,
    method_name: str | None = None,
) -> str:
    """Enable, disable, add, or remove individual trace hooks.

    Actions:
        - 'on': Enable a built-in hook (provide hook_id).
        - 'off': Disable a built-in hook (provide hook_id).
        - 'add': Add an extra hook (provide class_name and method_name).
        - 'rm': Remove an extra hook (provide class_name).

    Args:
        action: Action to perform: 'on', 'off', 'add', or 'rm'.
        hook_id: Built-in hook ID (for 'on'/'off' actions).
        class_name: Fully qualified class name (for 'add'/'rm' actions).
        method_name: Method name (for 'add' action).

    Returns:
        Action result message.
    """
    from smartinspector.commands.hook import cmd_hook

    if action in ("on", "off") and hook_id:
        return _run_command(cmd_hook, f"{action} {hook_id}")
    if action == "add" and class_name and method_name:
        return _run_command(cmd_hook, f"add {class_name} {method_name}")
    if action == "rm" and class_name:
        return _run_command(cmd_hook, f"rm {class_name}")
    return "Usage: action must be 'on'/'off' with hook_id, 'add' with class_name+method_name, or 'rm' with class_name."


# ---------------------------------------------------------------------------
# Session management tools
# ---------------------------------------------------------------------------

@mcp.tool(title="Performance Summary")
async def si_summary() -> str:
    """Show a summary of the last performance analysis.

    Requires prior analysis via si_full, si_trace, si_analyze, or si_quick.

    Returns:
        Formatted performance summary with FPS, CPU, memory, and hotspot data.
    """
    from smartinspector.commands.session import cmd_summary

    return _run_command(cmd_summary, "")


@mcp.tool(title="Clear Session")
async def si_clear() -> str:
    """Clear all session data (perf_summary, analysis, attribution).

    Returns:
        Confirmation message.
    """
    from smartinspector.commands.session import cmd_clear

    return _run_command(cmd_clear, "")


@mcp.tool(title="Token Usage")
async def si_tokens() -> str:
    """Show token usage statistics for the current session.

    Returns:
        Token usage summary with cost breakdown by agent role.
    """
    from smartinspector.commands.session import cmd_tokens

    return _run_command(cmd_tokens, "")


# ---------------------------------------------------------------------------
# Perfetto UI tools
# ---------------------------------------------------------------------------

@mcp.tool(title="Open Perfetto UI")
async def si_open(trace_path: str | None = None) -> str:
    """Open Perfetto UI with the SI Agent bridge for interactive frame analysis.

    Starts the bridge server and opens the browser. Requires Perfetto UI to be
    built (run ./perfetto-plugin/build.sh first).

    Args:
        trace_path: Path to the trace file. Uses last recorded trace if omitted.

    Returns:
        Bridge server status and URL.
    """
    from smartinspector.commands.trace import cmd_open

    return _run_command(cmd_open, trace_path or "")


@mcp.tool(title="Close Perfetto UI")
async def si_close() -> str:
    """Stop the Perfetto UI bridge server.

    Returns:
        Confirmation message.
    """
    from smartinspector.commands.trace import cmd_close

    return _run_command(cmd_close, "")


# ---------------------------------------------------------------------------
# Help tool
# ---------------------------------------------------------------------------

@mcp.tool(title="Help")
async def si_help() -> str:
    """Show help for all SmartInspector commands and tools.

    Returns:
        Help text describing all available commands.
    """
    from smartinspector.commands.session import cmd_help

    return _run_command(cmd_help, "")


# ---------------------------------------------------------------------------
# Headless/CI analysis tool
# ---------------------------------------------------------------------------

@mcp.tool(title="CI Analysis")
async def si_ci_analyze(
    trace_path: str,
    cmd: str = "full_analysis",
    target: str | None = None,
    duration: int = 10000,
    source_dir: str = ".",
    output_format: str = "markdown",
) -> str:
    """Run a non-interactive analysis pipeline for CI/automation use.

    Executes the full LangGraph pipeline in headless mode. Supports all
    pipeline routes: full_analysis, startup, analyze, trace.

    Args:
        trace_path: Path to the Perfetto trace file (.pb).
        cmd: Pipeline command: 'full_analysis', 'startup', 'analyze', or 'trace'.
        target: Target process package name.
        duration: Trace duration in milliseconds (default 10000).
        source_dir: Source code search directory.
        output_format: Output format: 'markdown' or 'json'.

    Returns:
        Analysis report in the requested format.
    """
    from smartinspector.headless import HeadlessRunner

    info_log("mcp", f"CI analysis: cmd={cmd}, trace={trace_path}, target={target}")

    runner = HeadlessRunner(
        source_dir=source_dir,
        target=target,
        trace_path=trace_path,
        fmt=output_format,
        duration=duration,
        cmd=cmd,
    )

    buf = io.StringIO()
    with redirect_stdout(buf):
        try:
            result = runner.run()
        except Exception as e:
            info_log("mcp", f"ERROR: CI analysis failed: {e}")
            return f"Error: {e}"

    # The runner writes output directly; return it
    return result


# ---------------------------------------------------------------------------
# Entry point
# ---------------------------------------------------------------------------

def main() -> None:
    """Start the SmartInspector MCP Server (stdio transport)."""
    print("SmartInspector MCP Server starting...", file=sys.stderr)
    print(f"  Tools: {len(mcp._tool_manager._tools)} registered", file=sys.stderr)
    print("  Transport: stdio", file=sys.stderr)
    info_log("mcp", "MCP Server starting (stdio transport)")
    mcp.run(transport="stdio")


if __name__ == "__main__":
    main()
