"""BaseCollector: abstract base class for platform-specific performance collectors.

Defines the unified interface that all platform collectors (Android, HarmonyOS,
iOS) must implement.  Downstream analyzer / attributor / reporter layers are
platform-agnostic — they consume *PerfSummary* regardless of which collector
produced it.
"""

from __future__ import annotations

import abc
from dataclasses import dataclass, field
from typing import Any


# ── Platform-agnostic summary ──────────────────────────────────


@dataclass
class PerfSummary:
    """Unified performance summary produced by every collector.

    This is the *platform-independent* intermediate representation that
    downstream agents consume.  Each collector populates the fields it can;
    fields left empty mean "no data for this metric".
    """

    frame_timeline: dict = field(default_factory=dict)
    cpu_usage: dict = field(default_factory=dict)
    process_memory: dict = field(default_factory=dict)
    cpu_hotspots: list[dict] = field(default_factory=list)
    memory: dict | None = None
    scheduling: dict | None = None
    view_slices: dict = field(default_factory=dict)
    io_slices: dict = field(default_factory=dict)
    metadata: dict = field(default_factory=dict)
    block_events: list[dict] = field(default_factory=list)
    input_events: list[dict] = field(default_factory=list)
    compose_slices: dict = field(default_factory=dict)
    sys_stats: dict = field(default_factory=dict)
    thread_state: list[dict] = field(default_factory=list)

    def to_json(self) -> str:
        import json

        return json.dumps(self.__dict__, indent=2, ensure_ascii=False)


# ── Device information ─────────────────────────────────────────


@dataclass
class DeviceInfo:
    """Platform-agnostic device description."""

    platform: str  # "android", "harmonyos", "ios", ...
    model: str = ""
    os_version: str = ""
    serial: str = ""
    extra: dict[str, Any] = field(default_factory=dict)


# ── Abstract collector ─────────────────────────────────────────


class BaseCollector(abc.ABC):
    """Abstract base for all platform collectors.

    Subclass this for each supported platform.  The minimum contract is
    :meth:`summarize` and :meth:`close`.
    """

    def __init__(self, trace_path: str, target_process: str | None = None) -> None:
        self.trace_path = trace_path
        self.target_process = target_process

    # ── Required overrides ──────────────────────────────────────

    @abc.abstractmethod
    def summarize(self) -> PerfSummary:
        """Run all analyses and return a unified *PerfSummary*."""
        ...

    @abc.abstractmethod
    def close(self) -> None:
        """Release resources (trace processor, file handles, etc.)."""
        ...

    # ── Optional overrides ──────────────────────────────────────

    def get_device_info(self) -> DeviceInfo:
        """Return device information for the current trace.

        Default returns an unknown-device stub; subclasses should override.
        """
        return DeviceInfo(platform=self.platform)

    @classmethod
    def is_available(cls) -> bool:
        """Check whether this collector's platform tools are installed.

        Returns ``True`` by default; subclasses should override to perform
        real checks (e.g. verify ``adb`` is on ``$PATH``).
        """
        return True

    @classmethod
    def pull_trace_from_device(
        cls,
        target_process: str,
        duration_ms: int = 10_000,
        **kwargs: Any,
    ) -> str:
        """Pull a trace from a connected device and return the file path.

        Not all collectors support live-device collection (some may only
        analyse pre-recorded traces).  The default implementation raises
        ``NotImplementedError``.
        """
        raise NotImplementedError(
            f"{cls.__name__} does not support live device trace collection"
        )

    # ── Context manager ─────────────────────────────────────────

    def __enter__(self) -> BaseCollector:
        return self

    def __exit__(self, exc_type: Any, exc_val: Any, exc_tb: Any) -> bool:
        self.close()
        return False

    # ── Convenience helpers ─────────────────────────────────────

    @property
    def platform(self) -> str:
        """Platform identifier (e.g. ``"android"``, ``"harmonyos"``)."""
        return getattr(self, "_platform", "unknown")
