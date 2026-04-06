"""Global debug logging for pipeline data inspection.

Enable via environment variable ``SI_DEBUG=1`` or CLI flag ``--debug``.
Logs are written to ``reports/debug_{timestamp}.log``.
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


def debug_log(category: str, message: str) -> None:
    """Append a timestamped debug entry to the log file.

    Safe to call from any thread; writes are serialised.
    If debug mode is off this is a no-op.
    """
    if not is_debug_enabled():
        return

    global _log_path

    with _lock:
        if _log_path is None:
            _REPORTS_DIR.mkdir(parents=True, exist_ok=True)
            ts = datetime.datetime.now().strftime("%Y%m%d_%H%M%S")
            _log_path = _REPORTS_DIR / f"debug_{ts}.log"

        ts = datetime.datetime.now().strftime("%Y-%m-%d %H:%M:%S.%f")[:-3]
        line = f"[{ts}] [{category}] {message}\n"
        with _log_path.open("a", encoding="utf-8") as f:
            f.write(line)
