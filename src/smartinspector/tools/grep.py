"""Grep tool: search file contents using ripgrep.

Supports three output modes (content / files_with_matches / count),
pagination via head_limit + offset, context lines, and token-efficient
output with relative paths and automatic VCS exclusion.
"""

import os
import tempfile
from langchain_core.tools import tool

from smartinspector.tools.rg import find_rg, run_rg, RipgrepTimeoutError
from smartinspector.config import get_source_dir
from smartinspector.tools.path_utils import validate_search_path

# ── Constants ────────────────────────────────────────────────────

DEFAULT_HEAD_LIMIT = 250
MAX_RESULT_SIZE = 20_000  # chars
PERSIST_THRESHOLD = 20_000  # persist to file above this size

VCS_DIRS = [".git", ".svn", ".hg", ".bzr", ".jj", ".sl"]


def _maybe_persist_result(output: str) -> str:
    """Persist large results to a temp file and return a reference."""
    if len(output) <= PERSIST_THRESHOLD:
        return output
    with tempfile.NamedTemporaryFile(
        mode="w", suffix=".txt", prefix="grep_results_", delete=False
    ) as f:
        f.write(output)
        return (
            f"[Results persisted to {f.name} ({len(output)} chars). "
            f"Use read tool to access the full output.]"
        )


def _apply_head_limit(
    items: list, limit: int, offset: int = 0
) -> tuple[list, int | None]:
    """Apply pagination: offset skip + head_limit cap.

    Returns (truncated_items, applied_limit).
    applied_limit is set only when results were actually truncated,
    so the caller knows to show pagination info.
    """
    if limit == 0:
        # "no limit" escape hatch
        return items[offset:], None if offset == 0 else limit

    total = len(items)
    sliced = items[offset : offset + limit]

    if offset + limit < total:
        return sliced, limit
    return sliced, None


def _to_relative_path(abs_path: str, search_root: str) -> str:
    """Convert absolute path to relative path w.r.t. search_root."""
    try:
        return os.path.relpath(abs_path, search_root)
    except ValueError:
        # e.g. different drives on Windows
        return abs_path


def _parse_content_lines(raw: str) -> list[dict]:
    """Parse ripgrep -nH output lines into structured dicts.

    Each line has format: filepath:linenum:content
    """
    results: list[dict] = []
    for line in raw.split("\n"):
        if not line:
            continue
        # Split on first two colons: path:linenum:text
        parts = line.split(":", 2)
        if len(parts) < 3:
            # May have only path:linenum with empty text
            if len(parts) == 2:
                results.append({"path": parts[0], "line": parts[1], "text": ""})
            continue
        results.append({"path": parts[0], "line": parts[1], "text": parts[2]})
    return results


def _get_file_mtime(filepath: str) -> float:
    """Get file modification time, returns 0 on error."""
    try:
        return os.path.getmtime(filepath)
    except OSError:
        return 0.0


def _sort_by_mtime(files: list[str]) -> list[str]:
    """Sort file paths by modification time descending (newest first)."""
    return sorted(files, key=_get_file_mtime, reverse=True)


@tool
def grep(
    pattern: str,
    path: str = "",
    include: str | None = None,
    type: str | None = None,
    output_mode: str = "content",
    head_limit: int = DEFAULT_HEAD_LIMIT,
    offset: int = 0,
    context: int = 0,
) -> str:
    """Search file contents using regular expressions (powered by ripgrep).

    Use this to find code patterns, function definitions, API calls, or any text across files.

    Args:
        pattern: The regex pattern to search for, e.g. "LazyForEach", "class\\s+\\w+".
        path: The directory to search in. Defaults to source_dir from config.
        include: File glob to filter, e.g. "*.ets" or "*.{ts,tsx}".
        type: File type to search, e.g. "js", "py", "rust", "java", "kotlin". Uses ripgrep's built-in type mappings.
        output_mode: "content" (default, show matched lines), "files_with_matches" (file list), or "count" (match counts per file).
        head_limit: Max number of results to return. Default 250. Use 0 for unlimited.
        offset: Skip first N results before applying head_limit. Default 0.
        context: Number of context lines before and after each match. Default 0.
    """
    # ── Validate inputs ──────────────────────────────────────
    if not path:
        path = get_source_dir()
    path = validate_search_path(path)
    if path is None:
        return "Error: invalid path."

    rg = find_rg()
    if not rg:
        return "Error: ripgrep (rg) not found."

    # ── Build ripgrep args ───────────────────────────────────
    args = [
        rg,
        "--no-messages",
        "--max-columns", "500",
    ]

    # VCS directory exclusion
    for vcs_dir in VCS_DIRS:
        args.extend(["--glob", f"!{vcs_dir}"])

    if output_mode == "files_with_matches":
        args.append("-l")  # list files only
    elif output_mode == "count":
        args.append("--count")
    else:  # content mode
        args.extend(["-n", "-H"])

    if include:
        args.extend(["--glob", include])

    if type:
        args.extend(["--type", type])

    if context > 0:
        args.extend(["-C", str(context)])

    args.extend(["--regexp", pattern])
    args.append(path)

    # ── Run ripgrep ──────────────────────────────────────────
    try:
        result = run_rg(args)
    except RipgrepTimeoutError:
        return f"Error: search timed out. Try a more specific pattern or narrower path."

    # EAGAIN / resource exhaustion retry: fall back to single-threaded
    if result.returncode == 2 and result.stderr and "EAGAIN" in result.stderr:
        try:
            result = run_rg(args + ["-j", "1"])
        except RipgrepTimeoutError:
            return f"Error: search timed out. Try a more specific pattern or narrower path."

    if result.returncode == 1:
        return "No matches found."
    if result.returncode == 2 and not result.stdout.strip():
        return "No matches found."

    raw = result.stdout.strip()
    if not raw:
        return "No matches found."

    # ── Process output by mode ───────────────────────────────
    search_root = os.path.realpath(path)

    if output_mode == "files_with_matches":
        files = [f for f in raw.split("\n") if f.strip()]
        files = _sort_by_mtime(files)

        sliced, applied_limit = _apply_head_limit(files, head_limit, offset)
        rel_paths = [_to_relative_path(f, search_root) for f in sliced]

        total = len(files)
        header = f"Found {total} files"
        if applied_limit is not None:
            header += f" limit: {applied_limit}"
        output = header + "\n" + "\n".join(rel_paths)

        if applied_limit is not None:
            output += f"\n\n[Showing results with pagination = limit: {applied_limit}, offset: {offset}]"

        return _maybe_persist_result(output)

    if output_mode == "count":
        lines = [l for l in raw.split("\n") if l.strip()]
        sliced, applied_limit = _apply_head_limit(lines, head_limit, offset)

        # Convert paths to relative
        rel_lines = []
        total_count = 0
        for line in sliced:
            parts = line.split(":", 1)
            if len(parts) == 2:
                rel_path = _to_relative_path(parts[0], search_root)
                rel_lines.append(f"{rel_path}:{parts[1]}")
                try:
                    total_count += int(parts[1])
                except ValueError:
                    pass
            else:
                rel_lines.append(line)

        output = "\n".join(rel_lines)
        footer = f"\n\nFound {total_count} total occurrences across {len(sliced)} files."
        if applied_limit is not None:
            footer += f" with pagination = limit: {applied_limit}"
        output += footer

        return _maybe_persist_result(output)

    # ── content mode (default) ───────────────────────────────
    matches = _parse_content_lines(raw)

    # Sort matches by file mtime descending
    file_mtimes: dict[str, float] = {}
    for m in matches:
        if m["path"] not in file_mtimes:
            file_mtimes[m["path"]] = _get_file_mtime(m["path"])

    matches.sort(key=lambda m: file_mtimes.get(m["path"], 0), reverse=True)

    # Apply head_limit BEFORE path conversion (truncate-first strategy)
    sliced, applied_limit = _apply_head_limit(matches, head_limit, offset)

    # Convert to output format
    output_parts: list[str] = []
    for m in sliced:
        rel_path = _to_relative_path(m["path"], search_root)
        line_num = m["line"]
        text = m["text"]
        if context > 0:
            # With context, just show raw text (ripgrep already added --/file headers)
            output_parts.append(f"{rel_path}-{line_num}:{text}")
        else:
            output_parts.append(f"{rel_path}:{line_num}:{text}")

    total_matches = len(matches)
    header = f"Found {total_matches} matches"
    if applied_limit is not None:
        header += f" (showing {len(sliced)})"
    output = header + "\n" + "\n".join(output_parts)

    if applied_limit is not None:
        output += f"\n\n[Showing results with pagination = limit: {applied_limit}, offset: {offset}]"

    return _maybe_persist_result(output)
