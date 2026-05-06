"""CollectorRegistry: factory for platform-specific collectors.

Provides a single entry point to obtain the correct *BaseCollector*
implementation for a given platform.  Built-in collectors are registered
at import time; custom collectors can be registered at runtime.
"""

from __future__ import annotations

import threading
from typing import Any

from smartinspector.collector.base import BaseCollector
from smartinspector.debug_log import info_log


class CollectorRegistry:
    """Thread-safe registry and factory for *BaseCollector* subclasses.

    Usage::

        # Get the default collector for a platform
        collector_cls = CollectorRegistry.get("android")

        # Create an instance
        collector = collector_cls(trace_path="/tmp/trace.pb")

        # Register a custom collector
        CollectorRegistry.register("harmonyos", MyHarmonyOSCollector)
    """

    _registry: dict[str, type[BaseCollector]] = {}
    _default_platform: str = "android"
    _lock = threading.Lock()

    # ── Public API ──────────────────────────────────────────────

    @classmethod
    def register(cls, platform: str, collector_cls: type[BaseCollector]) -> None:
        """Register *collector_cls* for the given *platform* identifier.

        Args:
            platform: Lower-case platform name (``"android"``, ``"harmonyos"``).
            collector_cls: A concrete *BaseCollector* subclass.

        Raises:
            TypeError: If *collector_cls* is not a *BaseCollector* subclass.
        """
        if not (isinstance(collector_cls, type) and issubclass(collector_cls, BaseCollector)):
            raise TypeError(
                f"{collector_cls!r} is not a BaseCollector subclass"
            )
        with cls._lock:
            cls._registry[platform.lower()] = collector_cls
            info_log("registry", f"Registered collector for platform '{platform}': {collector_cls.__name__}")

    @classmethod
    def get(cls, platform: str | None = None) -> type[BaseCollector]:
        """Return the collector class for *platform*.

        Falls back to the default platform (``"android"``) when *platform*
        is ``None``.

        Args:
            platform: Platform identifier or ``None`` for the default.

        Returns:
            The registered *BaseCollector* subclass.

        Raises:
            ValueError: If no collector is registered for the platform.
        """
        name = (platform or cls._default_platform).lower()
        with cls._lock:
            collector_cls = cls._registry.get(name)
        if collector_cls is None:
            available = ", ".join(sorted(cls._registry)) or "(none)"
            raise ValueError(
                f"No collector registered for platform '{name}'. "
                f"Available: {available}"
            )
        return collector_cls

    @classmethod
    def create(
        cls,
        trace_path: str,
        platform: str | None = None,
        target_process: str | None = None,
        **kwargs: Any,
    ) -> BaseCollector:
        """Create a collector instance for the given platform.

        This is the primary convenience method — it resolves the correct
        class and instantiates it.

        Args:
            trace_path: Path to the trace file.
            platform: Platform identifier (``None`` → default).
            target_process: Target process / package name.
            **kwargs: Additional keyword arguments forwarded to the collector
                      constructor.

        Returns:
            A ready-to-use *BaseCollector* instance.
        """
        collector_cls = cls.get(platform)
        return collector_cls(
            trace_path=trace_path,
            target_process=target_process,
            **kwargs,
        )

    @classmethod
    def set_default_platform(cls, platform: str) -> None:
        """Change the default platform used when *platform* is ``None``."""
        cls._default_platform = platform.lower()

    @classmethod
    def list_platforms(cls) -> list[str]:
        """Return sorted list of registered platform names."""
        with cls._lock:
            return sorted(cls._registry)

    @classmethod
    def reset(cls) -> None:
        """Clear all registrations (useful for testing)."""
        with cls._lock:
            cls._registry.clear()
            cls._default_platform = "android"


# ── Auto-register built-in collectors ───────────────────────────

def _register_builtins() -> None:
    """Register built-in platform collectors.

    Each import is guarded so that a missing optional dependency
    (e.g. the Perfetto Python package) does not crash the registry.
    """
    # Android / Perfetto
    try:
        from smartinspector.collector.perfetto import PerfettoCollector

        CollectorRegistry.register("android", PerfettoCollector)
    except ImportError:
        pass


_register_builtins()
