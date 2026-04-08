"""Find ripgrep binary path and run ripgrep with timeout/buffer controls."""

import os
import shutil
import signal
import subprocess

_RG_PATH: str | None = None

# common alternative paths
_CANDIDATES = [
    "/opt/homebrew/bin/rg",
    "/usr/local/bin/rg",
    "/usr/bin/rg",
]

# ripgrep execution constants
DEFAULT_RG_TIMEOUT = 20  # seconds
MAX_BUFFER_SIZE = 20 * 1024 * 1024  # 20MB
SIGTERM_WAIT = 5  # seconds before escalating to SIGKILL


def find_rg() -> str | None:
    """Return path to rg binary, or None if not found."""
    global _RG_PATH
    if _RG_PATH is not None:
        return _RG_PATH

    # 1. check PATH via shutil
    found = shutil.which("rg")
    if found:
        _RG_PATH = found
        return found

    # 2. check known locations
    for candidate in _CANDIDATES:
        if os.path.isfile(candidate) and os.access(candidate, os.X_OK):
            _RG_PATH = candidate
            return candidate

    return None


class RipgrepTimeoutError(Exception):
    """Raised when ripgrep times out with no partial output."""


def run_rg(
    args: list[str],
    timeout: int = DEFAULT_RG_TIMEOUT,
) -> subprocess.CompletedProcess:
    """Run ripgrep with timeout, buffer limit, and graceful kill escalation.

    Args:
        args: Full argument list (including rg binary path).
        timeout: Timeout in seconds. Defaults to 20.

    Returns:
        CompletedProcess with stdout/stderr captured.
        On timeout with partial output, stdout contains the partial result
        (last line dropped as it may be incomplete).

    Raises:
        RipgrepTimeoutError: If timeout occurs with no output at all.
    """
    try:
        proc = subprocess.Popen(
            args,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
        )
    except OSError as e:
        # Return a fake CompletedProcess for OS errors (e.g. binary not found)
        return subprocess.CompletedProcess(
            args=args, returncode=2, stdout="", stderr=str(e)
        )

    stdout_chunks: list[bytes] = []
    stderr_chunks: list[bytes] = []
    stdout_truncated = False
    total_stdout_len = 0

    try:
        # Read stdout in a streaming fashion with buffer limit
        import selectors

        sel = selectors.DefaultSelector()
        if proc.stdout:
            sel.register(proc.stdout, selectors.EVENT_READ)
        if proc.stderr:
            sel.register(proc.stderr, selectors.EVENT_READ)

        import time
        deadline = time.monotonic() + timeout

        while sel.get_map():
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                raise subprocess.TimeoutExpired(cmd=args, timeout=timeout)

            events = sel.select(timeout=min(remaining, 0.5))
            for key, _ in events:
                chunk = key.fileobj.read1(65536) if hasattr(key.fileobj, 'read1') else key.fileobj.read(65536)
                if not chunk:
                    sel.unregister(key.fileobj)
                    continue
                if key.fileobj == proc.stdout:
                    if not stdout_truncated:
                        stdout_chunks.append(chunk)
                        total_stdout_len += len(chunk)
                        if total_stdout_len > MAX_BUFFER_SIZE:
                            stdout_chunks = [b"".join(stdout_chunks)[:MAX_BUFFER_SIZE]]
                            stdout_truncated = True
                elif key.fileobj == proc.stderr:
                    stderr_chunks.append(chunk)

        proc.wait(timeout=max(remaining, 1) if remaining > 0 else 1)
    except subprocess.TimeoutExpired:
        # Graceful kill: SIGTERM → wait → SIGKILL
        _kill_process(proc)
        proc.wait(timeout=5)

        stdout_data = b"".join(stdout_chunks).decode("utf-8", errors="replace")
        stderr_data = b"".join(stderr_chunks).decode("utf-8", errors="replace")

        if stdout_data.strip():
            # Drop last line (may be incomplete)
            lines = stdout_data.rstrip("\n").rsplit("\n", 1)
            if len(lines) > 1 and not stdout_truncated:
                stdout_data = lines[0]
            return subprocess.CompletedProcess(
                args=args, returncode=0, stdout=stdout_data, stderr=stderr_data
            )
        # No output at all — signal to caller that search was incomplete
        raise RipgrepTimeoutError(
            f"ripgrep timed out after {timeout}s with no output"
        )
    finally:
        sel.close()

    stdout_data = b"".join(stdout_chunks).decode("utf-8", errors="replace")
    stderr_data = b"".join(stderr_chunks).decode("utf-8", errors="replace")

    return subprocess.CompletedProcess(
        args=args, returncode=proc.returncode, stdout=stdout_data, stderr=stderr_data
    )


def _kill_process(proc: subprocess.Popen) -> None:
    """Send SIGTERM, wait briefly, then SIGKILL if still alive."""
    if proc.poll() is not None:
        return

    try:
        proc.terminate()  # SIGTERM
    except OSError:
        return

    try:
        proc.wait(timeout=SIGTERM_WAIT)
    except subprocess.TimeoutExpired:
        try:
            proc.kill()  # SIGKILL
        except OSError:
            pass
