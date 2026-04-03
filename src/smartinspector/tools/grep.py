"""Grep tool: search file contents using ripgrep."""

import subprocess
from langchain_core.tools import tool

from smartinspector.tools.rg import find_rg
from smartinspector.config import get_source_dir


@tool
def grep(pattern: str, path: str = "", include: str | None = None) -> str:
    """Search file contents using regular expressions (powered by ripgrep).

    Use this to find code patterns, function definitions, API calls, or any text across files.

    Args:
        pattern: The regex pattern to search for, e.g. "LazyForEach", "class\\s+\\w+".
        path: The directory to search in. Defaults to source_dir from config.
        include: File glob to filter, e.g. "*.ets" or "*.{ts,tsx}".
    """
    if not path:
        path = get_source_dir()
    rg = find_rg()
    if not rg:
        return "Error: ripgrep (rg) not found."

    args = [
        rg, "-nH", "--hidden", "--no-messages",
        "--field-match-separator=|",
        "--regexp", pattern,
    ]
    if include:
        args.extend(["--glob", include])
    args.append(path)

    rg = find_rg()
    if not rg:
        return "Error: ripgrep (rg) not found. Please install it first."

    args = [
        rg, "-nH", "--hidden", "--no-messages",
        "--field-match-separator=|",
        "--regexp", pattern,
    ]
    if include:
        args.extend(["--glob", include])
    args.append(path)

    try:
        result = subprocess.run(
            args, capture_output=True, text=True, timeout=30,
        )
    except subprocess.TimeoutExpired:
        return "Error: search timed out after 30s."

    if result.returncode == 1:
        return "No matches found."
    if result.returncode == 2 and not result.stdout.strip():
        return "No matches found."

    lines = result.stdout.strip().split("\n")
    if not lines or lines == [""]:
        return "No matches found."

    matches: list[dict] = []
    for line in lines:
        if not line:
            continue
        parts = line.split("|", 2)
        if len(parts) < 2:
            continue
        file_path, text = parts[0], parts[1]
        # file_path contains "filepath:linenum"
        if len(text) > 200:
            text = text[:200] + "..."
        matches.append({"path": file_path, "text": text})

    limit = 100
    truncated = len(matches) > limit
    matches = matches[:limit]

    output_parts: list[str] = []
    current_file = ""
    for m in matches:
        if current_file != m["path"]:
            current_file = m["path"]
            output_parts.append(f"\n{current_file}")
        output_parts.append(f"  {m['text']}")

    header = f"Found {len(matches)} matches"
    if truncated:
        header += f" (showing first {limit})"
    output = header + "".join(output_parts)

    return output[:8000]
