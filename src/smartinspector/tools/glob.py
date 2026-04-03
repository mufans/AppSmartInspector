"""Glob tool: find files by name patterns using ripgrep."""

import subprocess
from langchain_core.tools import tool

from smartinspector.tools.rg import find_rg
from smartinspector.config import get_source_dir


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
    rg = find_rg()
    if not rg:
        return "Error: ripgrep (rg) not found. Please install it first."
    args = [rg, "--files", "--hidden", "--no-messages", "--glob", pattern, path]

    try:
        result = subprocess.run(
            args, capture_output=True, text=True, timeout=30,
        )
    except FileNotFoundError:
        return "Error: ripgrep (rg) not found. Please install it first."
    except subprocess.TimeoutExpired:
        return "Error: search timed out after 30s."

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
