"""Unified logging for SmartInspector pipeline.

All logging goes to ``reports/debug_{timestamp}.log`` — nothing is written
to the console.

Two levels:

- ``info_log(category, message)`` — always written (pipeline progress,
  warnings, errors).  Replaces ``logging.info/warning/error``.
- ``debug_log(category, message)`` — only written when ``SI_DEBUG=1``
  or ``--debug`` flag is set (detailed diagnostics, SQL queries, raw data).

Enable verbose mode: ``SI_DEBUG=1`` or ``--debug``.
"""

import datetime
import os
import pathlib
import threading

_REPORTS_DIR = pathlib.Path(__file__).resolve().parent.parent.parent / "reports"

_lock = threading.Lock()
_log_path: pathlib.Path | None = None


def is_debug_enabled() -> bool:
    """Return True if debug logging is active."""
    return os.environ.get("SI_DEBUG", "").strip() in ("1", "true", "yes")


def get_debug_log_path() -> pathlib.Path | None:
    """Return the current debug log file path, or None if not started."""
    return _log_path


def _ensure_log_file() -> None:
    """Create the log file and reports directory if needed."""
    global _log_path
    if _log_path is None:
        _REPORTS_DIR.mkdir(parents=True, exist_ok=True)
        ts = datetime.datetime.now().strftime("%Y%m%d_%H%M%S")
        _log_path = _REPORTS_DIR / f"debug_{ts}.log"


def _write(category: str, message: str) -> None:
    """Write a timestamped line to the log file (thread-safe)."""
    with _lock:
        _ensure_log_file()
        ts = datetime.datetime.now().strftime("%Y-%m-%d %H:%M:%S.%f")[:-3]
        line = f"[{ts}] [{category}] {message}\n"
        with _log_path.open("a", encoding="utf-8") as f:  # type: ignore[union-attr]
            f.write(line)


def info_log(category: str, message: str) -> None:
    """Log an info-level message (always written, regardless of SI_DEBUG).

    Args:
        category: Module identifier (collector, attributor, reporter, ws, etc.)
        message: Log message
    """
    _write(category, message)


def debug_log(category: str, message: str) -> None:
    """Log a debug-level message (only written when SI_DEBUG=1).

    Safe to call from any thread; writes are serialised.
    If debug mode is off this is a no-op.

    Args:
        category: Module identifier (collector, attributor, reporter, ws, etc.)
        message: Log message
    """
    if not is_debug_enabled():
        return
    _write(category, message)
