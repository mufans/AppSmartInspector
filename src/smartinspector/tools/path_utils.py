"""Shared path validation utilities for tools."""

import os


def validate_search_path(path: str) -> str | None:
    """Validate and resolve search path. Returns resolved path or None if invalid."""
    parts = path.replace("\\", "/").split("/")
    if ".." in parts:
        return None
    return os.path.realpath(path)