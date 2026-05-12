"""Collector package: platform-specific performance data collectors."""

from smartinspector.collector.perfetto import PerfettoCollector, PerfSummary
from smartinspector.collector.lock import LockMixin

__all__ = ["PerfettoCollector", "PerfSummary", "LockMixin"]
