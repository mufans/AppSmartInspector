"""Glob tool: find files by name patterns using ripgrep."""

import os
import subprocess
from langchain_core.tools import tool

from smartinspector.tools.rg import find_rg
from smartinspector.config import get_source_dir, get_tool_timeout


def _validate_search_path(path: str) -> str | None:
    """Validate and resolve search path. Returns resolved path or None if invalid."""
    # Block traversal: check raw path components before normalization
    parts = path.replace("\\", "/").split("/")
    if ".." in parts:
        return None
    return os.path.realpath(path)


@tool
def glob(pattern: str, path: str = "") -> str:
    """Find files by name pattern (powered by ripgrep).

    Returns file paths sorted by modification time (newest first).
    Use this when you need to locate files but don't know their exact path.

    Args:
        pattern: Glob pattern to match, e.g. "**/*.ets", "src/**/*.ts", "*.{json,yaml}".
        path: The directory to search in. Defaults to source_dir from config.
    """
    if not path:
        path = get_source_dir()
    path = _validate_search_path(path)
    if path is None:
        return "Error: invalid path."
    rg = find_rg()
    if not rg:
        return "Error: ripgrep (rg) not found. Please install it first."
    args = [rg, "--files", "--hidden", "--no-messages", "--glob", pattern, path]

    try:
        result = subprocess.run(
            args, capture_output=True, text=True, timeout=get_tool_timeout(),
        )
    except FileNotFoundError:
        return "Error: ripgrep (rg) not found. Please install it first."
    except subprocess.TimeoutExpired:
        return f"Error: search timed out after {get_tool_timeout()}s."

    if result.returncode != 0 and not result.stdout.strip():
        return "No files found."

    lines = result.stdout.strip().split("\n")
    files = [line for line in lines if line]

    if not files:
        return "No files found."

    limit = 100
    truncated = len(files) > limit
    files = files[:limit]

    output = "\n".join(files)
    if truncated:
        output += f"\n\n(Showing first {limit} of {len(lines)} files. Use a more specific pattern.)"

    return output[:8000]
