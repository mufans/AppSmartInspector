"""Find ripgrep binary path."""

import shutil
import os

_RG_PATH: str | None = None

# common alternative paths
_CANDIDATES = [
    "/opt/homebrew/bin/rg",
    "/usr/local/bin/rg",
    "/usr/bin/rg",
]


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
