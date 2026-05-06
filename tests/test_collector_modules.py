"""Unit tests for collector sub-modules (mixin classes).

Tests each mixin independently by mocking the TraceProcessor interface.
"""

import pytest
from unittest.mock import MagicMock, patch
from types import SimpleNamespace

from smartinspector.collector._helpers import _parse_siblock_msg, _map_state_label
from smartinspector.collector.sched import SchedMixin
from smartinspector.collector.cpu import CpuMixin
from smartinspector.collector.io import IoMixin
from smartinspector.collector.sys import SysMixin
from smartinspector.collector.block import BlockMixin
from smartinspector.collector.thread import ThreadMixin


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------

class MockCollector:
    """Minimal mock that provides _open() for mixin methods."""

    def __init__(self, tp_mock):
        self._tp = tp_mock
        self._target_process_cache = None
        self._target_package = None

    def _open(self):
        return self._tp


# ---------------------------------------------------------------------------
# _helpers tests
# ---------------------------------------------------------------------------

class TestParseSiblockMsg:
    def test_empty_message(self):
        assert _parse_siblock_msg("") == []

    def test_none_like_empty(self):
        assert _parse_siblock_msg(None) == []

    def test_full_message(self):
        msg = "CpuBurnWorker$1|250ms|at com.example.Foo.run(Foo.java:123)|at com.example.Bar.doX(Bar.java:45)"
        result = _parse_siblock_msg(msg)
        assert len(result) == 2
        assert result[0] == "at com.example.Foo.run(Foo.java:123)"
        assert result[1] == "at com.example.Bar.doX(Bar.java:45)"

    def test_no_stack_frames(self):
        msg = "CpuBurnWorker$1|250ms"
        assert _parse_siblock_msg(msg) == []


class TestMapStateLabel:
    def test_running_states(self):
        assert _map_state_label("R") == "Running"
        assert _map_state_label("R+") == "Running"
        assert _map_state_label("Running") == "Running"

    def test_sleeping_states(self):
        assert _map_state_label("S") == "Sleeping"
        assert _map_state_label("S+") == "Sleeping"

    def test_disk_sleep(self):
        assert _map_state_label("D") == "DiskSleep"
        assert _map_state_label("D+") == "DiskSleep"

    def test_unknown_passthrough(self):
        assert _map_state_label("UNKNOWN_STATE") == "UNKNOWN_STATE"


# ---------------------------------------------------------------------------
# SchedMixin tests
# ---------------------------------------------------------------------------

class TestSchedMixin:
    def test_collect_sched_basic(self):
        tp = MagicMock()
        tp.query.return_value = iter([
            SimpleNamespace(comm="main", tid=1, switches=100, total_dur_ns=1_000_000_000, dominant_state="S"),
        ])
        collector = MockCollector(tp)
        mixin = SchedMixin()
        mixin._open = collector._open

        result = mixin.collect_sched()
        assert "hot_threads" in result
        assert len(result["hot_threads"]) == 1
        assert result["hot_threads"][0]["comm"] == "main"
        assert result["hot_threads"][0]["total_dur_ms"] == 1000.0

    def test_collect_sched_empty(self):
        tp = MagicMock()
        tp.query.return_value = iter([])
        collector = MockCollector(tp)
        mixin = SchedMixin()
        mixin._open = collector._open

        result = mixin.collect_sched()
        assert "hot_threads" in result
        assert len(result["hot_threads"]) == 0


# ---------------------------------------------------------------------------
# CpuMixin tests
# ---------------------------------------------------------------------------

class TestCpuMixin:
    def test_collect_cpu_hotspots_empty(self):
        tp = MagicMock()
        tp.query.return_value = iter([])
        collector = MockCollector(tp)
        mixin = CpuMixin()
        mixin._open = collector._open

        result = mixin.collect_cpu_hotspots()
        assert result == []

    def test_collect_cpu_usage_empty_trace(self):
        tp = MagicMock()
        tp.query.return_value = iter([])  # no trace_bounds
        collector = MockCollector(tp)
        mixin = CpuMixin()
        mixin._open = collector._open

        result = mixin.collect_cpu_usage()
        assert result == {}


# ---------------------------------------------------------------------------
# IoMixin tests
# ---------------------------------------------------------------------------

class TestIoMixin:
    def test_collect_io_slices_empty(self):
        tp = MagicMock()
        tp.query.return_value = iter([])
        collector = MockCollector(tp)
        mixin = IoMixin()
        mixin._open = collector._open

        result = mixin.collect_io_slices()
        assert result == {}

    def test_collect_io_slices_with_data(self):
        tp = MagicMock()
        tp.query.return_value = iter([
            SimpleNamespace(name="SI$net#get", ts=1000, dur=5_000_000, depth=2, track_id=1),
            SimpleNamespace(name="SI$db#query", ts=2000, dur=10_000_000, depth=2, track_id=1),
        ])
        collector = MockCollector(tp)
        mixin = IoMixin()
        mixin._open = collector._open

        result = mixin.collect_io_slices()
        assert result["total_count"] == 2
        assert len(result["summary"]) == 2
        assert result["slowest"][0]["name"] == "SI$db#query"

    def test_collect_input_events_empty(self):
        tp = MagicMock()
        tp.query.return_value = iter([])
        collector = MockCollector(tp)
        mixin = IoMixin()
        mixin._open = collector._open

        result = mixin.collect_input_events()
        assert result == []

    def test_collect_input_events_with_data(self):
        tp = MagicMock()
        tp.query.return_value = iter([
            SimpleNamespace(name="SI$touch#MainActivity#DOWN", ts=1000, dur=1_000_000),
        ])
        collector = MockCollector(tp)
        mixin = IoMixin()
        mixin._open = collector._open

        result = mixin.collect_input_events()
        assert len(result) == 1
        assert result[0]["activity"] == "MainActivity"
        assert result[0]["action"] == "DOWN"


# ---------------------------------------------------------------------------
# SysMixin tests
# ---------------------------------------------------------------------------

class TestSysMixin:
    def test_collect_sys_stats_empty(self):
        tp = MagicMock()
        tp.query.return_value = iter([])
        collector = MockCollector(tp)
        mixin = SysMixin()
        mixin._open = collector._open

        result = mixin.collect_sys_stats()
        assert result == {}

    def test_collect_threads(self):
        tp = MagicMock()
        tp.query.return_value = iter([
            SimpleNamespace(tid=1, name="main"),
            SimpleNamespace(tid=2, name="binder:1_1"),
            SimpleNamespace(tid=3, name=None),
        ])
        collector = MockCollector(tp)
        mixin = SysMixin()
        mixin._open = collector._open

        result = mixin.collect_threads()
        assert len(result) == 2  # tid=3 with name=None should be filtered
        assert result[0]["tid"] == 1
        assert result[0]["name"] == "main"


# ---------------------------------------------------------------------------
# BlockMixin tests
# ---------------------------------------------------------------------------

class TestBlockMixin:
    def test_collect_block_events_empty(self):
        tp = MagicMock()
        tp.query.return_value = iter([])
        collector = MockCollector(tp)
        mixin = BlockMixin()
        mixin._open = collector._open

        result = mixin.collect_block_events()
        assert result == []

    def test_collect_block_events_with_slices(self):
        tp = MagicMock()
        # First query returns block slices, second returns empty log entries
        tp.query.side_effect = [
            iter([SimpleNamespace(name="SI$block#Worker.run#100ms", ts=1000, dur=0)]),
            iter([]),  # no logcat entries
        ]
        collector = MockCollector(tp)
        mixin = BlockMixin()
        mixin._open = collector._open

        result = mixin.collect_block_events()
        assert len(result) == 1
        assert result[0]["dur_ms"] == 100.0
        assert result[0]["stack_trace"] == []


# ---------------------------------------------------------------------------
# ThreadMixin tests
# ---------------------------------------------------------------------------

class TestThreadMixin:
    def test_collect_thread_state_no_main_utid(self):
        tp = MagicMock()
        tp.query.return_value = iter([])  # no main thread found
        collector = MockCollector(tp)
        collector._target_package = None
        collector._target_process_cache = None
        mixin = ThreadMixin()
        mixin._open = collector._open
        mixin._target_package = collector._target_package
        mixin._resolve_target_process = lambda *a, **kw: {}

        result = mixin.collect_thread_state()
        assert result == []

    def test_check_intrinsic_thread_state_available(self):
        tp = MagicMock()
        tp.query.return_value = iter([SimpleNamespace(_1=1)])
        collector = MockCollector(tp)
        mixin = ThreadMixin()
        mixin._open = collector._open

        result = mixin._check_intrinsic_thread_state(tp)
        assert result is True

    def test_check_intrinsic_thread_state_unavailable(self):
        tp = MagicMock()
        tp.query.side_effect = Exception("table not found")
        collector = MockCollector(tp)
        mixin = ThreadMixin()
        mixin._open = collector._open

        result = mixin._check_intrinsic_thread_state(tp)
        assert result is False
