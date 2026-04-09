"""Read tool: read file contents with line numbers."""

import os
from functools import lru_cache
from langchain_core.tools import tool

from smartinspector.config import get_read_max_lines, get_read_max_line_length

MAX_LINES = get_read_max_lines()
MAX_LINE_LENGTH = get_read_max_line_length()
MAX_OUTPUT_TOKENS = 8000


def _estimate_tokens(text: str) -> int:
    """Rough token estimation: ~4 chars per token for English, ~1.5 chars for CJK."""
    cjk = sum(1 for c in text if '\u4e00' <= c <= '\u9fff')
    ascii_chars = len(text) - cjk
    return int(ascii_chars / 4 + cjk / 1.5)


def _file_mtime(file_path: str) -> float:
    """Get file modification time for cache invalidation."""
    try:
        return os.path.getmtime(file_path)
    except OSError:
        return 0.0


@lru_cache(maxsize=64)
def _read_file_content(file_path: str, offset: int, limit: int, _mtime: float) -> str:
    """Cached file reading. Includes mtime in cache key to prevent stale data."""
    if not os.path.exists(file_path):
        # try to suggest similar files
        parent = os.path.dirname(file_path)
        basename = os.path.basename(file_path)
        suggestions: list[str] = []
        if os.path.isdir(parent):
            for entry in os.listdir(parent):
                if basename.lower() in entry.lower() or entry.lower() in basename.lower():
                    suggestions.append(os.path.join(parent, entry))
        if suggestions:
            return f"File not found: {file_path}\nDid you mean: {suggestions[:3]}?"
        return f"File not found: {file_path}"

    if os.path.isdir(file_path):
        entries = sorted(os.listdir(file_path))
        dirs = [e + "/" for e in entries if os.path.isdir(os.path.join(file_path, e))]
        files = [e for e in entries if not os.path.isdir(os.path.join(file_path, e))]
        output = "\n".join(dirs + files)
        return f"{file_path}/\n{output}"

    # detect binary file
    try:
        with open(file_path, "rb") as f:
            chunk = f.read(4096)
        if b"\x00" in chunk:
            return f"Cannot read binary file: {file_path}"
    except OSError:
        pass

    raw: list[str] = []
    tokens_used = 0
    truncated = False
    total_lines = 0

    with open(file_path, encoding="utf-8", errors="replace") as f:
        for line_text in f:
            total_lines += 1
            if total_lines < offset:
                continue
            if len(raw) >= limit:
                truncated = True
                continue
            if len(line_text) > MAX_LINE_LENGTH:
                line_text = line_text[:MAX_LINE_LENGTH] + f"... (truncated to {MAX_LINE_LENGTH} chars)\n"
            line_tokens = _estimate_tokens(line_text)
            if tokens_used + line_tokens > MAX_OUTPUT_TOKENS:
                truncated = True
                break
            raw.append(line_text.rstrip("\n\r"))
            tokens_used += line_tokens

    if total_lines < offset and not (total_lines == 0 and offset == 1):
        return f"Offset {offset} exceeds file length ({total_lines} lines)."

    lines = [f"{i + offset}: {line}" for i, line in enumerate(raw)]
    output = "\n".join(lines)

    last_read = offset + len(raw) - 1
    if truncated:
        output += f"\n\n(Showing lines {offset}-{last_read} of {total_lines}. Use offset={last_read + 1} to continue.)"
    elif total_lines > last_read:
        output += f"\n\n(Showing lines {offset}-{last_read} of {total_lines}. Use offset={last_read + 1} to continue.)"
    else:
        output += f"\n\n(End of file - {total_lines} lines)"

    return output[:50000]


@tool
def read(file_path: str, offset: int = 1, limit: int = 2000) -> str:
    """Read file contents with line numbers.

    Supports reading text files with line-by-line output.
    For large files, use offset and limit to read specific sections.

    Args:
        file_path: Absolute or relative path to the file.
        offset: Line number to start reading from (1-indexed). Defaults to 1.
        limit: Maximum number of lines to read. Defaults to 2000.
    """
    mtime = _file_mtime(file_path)
    return _read_file_content(file_path, offset, limit, mtime)
