"""Collector package: platform-specific performance data collectors."""

from smartinspector.collector.base import BaseCollector, PerfSummary
from smartinspector.collector.perfetto import PerfettoCollector
from smartinspector.collector.registry import CollectorRegistry

__all__ = [
    "BaseCollector",
    "CollectorRegistry",
    "PerfettoCollector",
    "PerfSummary",
]
