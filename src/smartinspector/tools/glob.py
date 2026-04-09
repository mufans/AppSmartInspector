"""Glob tool: find files by name patterns using ripgrep."""

import os
import subprocess
from langchain_core.tools import tool

from smartinspector.tools.rg import find_rg
from smartinspector.config import get_source_dir, get_tool_timeout
from smartinspector.tools.path_utils import validate_search_path

VCS_DIRS = [".git", ".svn", ".hg", ".bzr", ".jj", ".sl"]


def _to_relative_path(abs_path: str, search_root: str) -> str:
    """Convert absolute path to relative path w.r.t. search_root."""
    try:
        return os.path.relpath(abs_path, search_root)
    except ValueError:
        return abs_path


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
    search_root = validate_search_path(path)
    if search_root is None:
        return "Error: invalid path."
    rg = find_rg()
    if not rg:
        return "Error: ripgrep (rg) not found. Please install it first."

    args = [rg, "--files", "--hidden", "--no-messages",
            "--sort=modified",
            "--glob", pattern,
            search_root]
    # Exclude VCS directories
    for vcs in VCS_DIRS:
        args.extend(["--glob", f"!{vcs}"])

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

    # Convert to relative paths
    files = [_to_relative_path(f, search_root) for f in files]

    limit = 100
    truncated = len(files) > limit
    files = files[:limit]

    output = "\n".join(files)
    if truncated:
        output += f"\n\n(Showing first {limit} of {len(lines)} files. Use a more specific pattern.)"

    return output[:8000]
