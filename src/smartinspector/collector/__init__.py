"""Collector package: platform-specific performance data collectors."""

from smartinspector.collector.perfetto import PerfettoCollector, PerfSummary

__all__ = ["PerfettoCollector", "PerfSummary"]
