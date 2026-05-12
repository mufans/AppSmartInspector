"""Tests for PerfettoCollector stdlib module collection methods.

Each test mocks tp.query() to verify SQL uses correct INCLUDE PERFETTO MODULE
statements and the result parsing logic works correctly.
"""

import pytest
from unittest.mock import MagicMock, patch, PropertyMock


# Helper: create a mock row object with attribute access
def _row(**kwargs):
    """Create a mock row with attribute-based access."""
    row = MagicMock()
    for k, v in kwargs.items():
        setattr(row, k, v)
    return row


def _make_collector():
    """Create a PerfettoCollector with mocked _open and _resolve_target_process."""
    from smartinspector.collector.perfetto import PerfettoCollector
    collector = PerfettoCollector.__new__(PerfettoCollector)
    collector.trace_path = "/tmp/test.pb"
    collector.shell_path = "/tmp/tp_shell"
    collector._tp = None
    collector._target_process_cache = None
    collector._target_package = "com.example.app"
    return collector


# -----------------------------------------------------------------------
# P0-1: collect_lock_contention (android.monitor_contention)
# -----------------------------------------------------------------------

class TestCollectLockContention:

    def test_returns_empty_when_no_data(self):
        collector = _make_collector()
        mock_tp = MagicMock()
        mock_tp.query.return_value = iter([])

        with patch.object(collector, '_open', return_value=mock_tp), \
             patch.object(collector, '_resolve_target_process', return_value={"upid": 1}):
            result = collector.collect_lock_contention()

        assert result == {}

    def test_parses_contention_rows(self):
        collector = _make_collector()
        mock_tp = MagicMock()

        contention_rows = [
            _row(
                id=1, ts=1000, dur=5_000_000,
                blocked_method="com.example.Foo.bar(Foo.java:10)",
                blocking_method="com.example.Baz.qux(Baz.java:20)",
                short_blocked_method="com.example.Foo.bar",
                short_blocking_method="com.example.Baz.qux",
                blocked_src="Foo.java:10",
                blocking_src="Baz.java:20",
                waiter_count=0,
                blocked_thread_name="main",
                blocking_thread_name="worker-1",
                blocked_utid=1,
                blocking_utid=2,
                is_blocked_thread_main=1,
                is_blocking_thread_main=0,
                upid=10,
                process_name="com.example.app",
            ),
        ]

        def query_side_effect(sql):
            if "android_monitor_contention_chain_thread_state" in sql:
                return iter([])
            return iter(contention_rows)

        mock_tp.query.side_effect = query_side_effect

        with patch.object(collector, '_open', return_value=mock_tp), \
             patch.object(collector, '_resolve_target_process', return_value={"upid": 10}):
            result = collector.collect_lock_contention()

        assert result["total_count"] == 1
        assert result["main_thread_blocks"] == 1
        assert len(result["top_contentions"]) == 1
        assert result["top_contentions"][0]["dur_ms"] == 5.0
        assert result["top_contentions"][0]["is_main_thread_blocked"] is True

    def test_includes_thread_state_breakdown(self):
        collector = _make_collector()
        mock_tp = MagicMock()

        ts_rows = [
            _row(id=1, thread_state="Running", thread_state_dur=3_000_000, thread_state_count=1),
        ]
        contention_rows = [
            _row(id=1, ts=1000, dur=5_000_000,
                 blocked_method="Foo.bar", blocking_method="Baz.qux",
                 short_blocked_method="Foo.bar", short_blocking_method="Baz.qux",
                 blocked_src="Foo.java:10", blocking_src="Baz.java:20",
                 waiter_count=0, blocked_thread_name="main", blocking_thread_name="worker",
                 blocked_utid=1, blocking_utid=2,
                 is_blocked_thread_main=1, is_blocking_thread_main=0,
                 upid=10, process_name="com.example.app"),
        ]

        def query_side_effect(sql):
            if "android_monitor_contention_chain_thread_state" in sql:
                return iter(ts_rows)
            return iter(contention_rows)

        mock_tp.query.side_effect = query_side_effect

        with patch.object(collector, '_open', return_value=mock_tp), \
             patch.object(collector, '_resolve_target_process', return_value={"upid": 10}):
            result = collector.collect_lock_contention()

        assert "thread_state_breakdown" in result
        assert result["thread_state_breakdown"][0]["thread_state"] == "Running"

    def test_uses_upid_filter(self):
        collector = _make_collector()
        mock_tp = MagicMock()
        mock_tp.query.return_value = iter([])

        with patch.object(collector, '_open', return_value=mock_tp), \
             patch.object(collector, '_resolve_target_process', return_value={"upid": 42}):
            collector.collect_lock_contention()

        # First call should include the upid filter
        first_sql = mock_tp.query.call_args_list[0][0][0]
        assert "upid = 42" in first_sql
        assert "INCLUDE PERFETTO MODULE android.monitor_contention" in first_sql


# -----------------------------------------------------------------------
# P0-2: collect_binder_txns (android.binder)
# -----------------------------------------------------------------------

class TestCollectBinderTxns:

    def test_returns_empty_when_no_data(self):
        collector = _make_collector()
        mock_tp = MagicMock()
        mock_tp.query.return_value = iter([])

        with patch.object(collector, '_open', return_value=mock_tp), \
             patch.object(collector, '_resolve_target_process', return_value={"upid": 1}):
            result = collector.collect_binder_txns()

        assert result == {}

    def test_parses_binder_rows(self):
        collector = _make_collector()
        mock_tp = MagicMock()

        txn_rows = [
            _row(
                binder_txn_id=1, binder_reply_id=2,
                aidl_name="IAccountManager.getAccountType",
                interface="android.accounts.IAccountManager",
                method_name="getAccountType",
                client_process="com.example.app", client_thread="main",
                client_upid=10, client_tid=1001, client_pid=1001,
                is_main_thread=True,
                client_ts=1000, client_dur=10_000_000,
                server_process="system_server", server_thread="binder:1000_3",
                server_upid=5, server_tid=503, server_pid=1000,
                server_ts=2000, server_dur=8_000_000,
                is_sync=True,
                client_oom_score=100, server_oom_score=0,
            ),
        ]

        def query_side_effect(sql):
            if "android_binder_metrics_by_process" in sql:
                return iter([])
            return iter(txn_rows)

        mock_tp.query.side_effect = query_side_effect

        with patch.object(collector, '_open', return_value=mock_tp), \
             patch.object(collector, '_resolve_target_process', return_value={"upid": 10}):
            result = collector.collect_binder_txns()

        assert result["total_count"] == 1
        assert result["sync_count"] == 1
        assert result["main_thread_count"] == 1
        assert result["top_by_duration"][0]["client_dur_ms"] == 10.0
        assert result["top_by_duration"][0]["server_dur_ms"] == 8.0

    def test_includes_module_include(self):
        collector = _make_collector()
        mock_tp = MagicMock()
        mock_tp.query.return_value = iter([])

        with patch.object(collector, '_open', return_value=mock_tp), \
             patch.object(collector, '_resolve_target_process', return_value={"upid": 1}):
            collector.collect_binder_txns()

        first_sql = mock_tp.query.call_args_list[0][0][0]
        assert "INCLUDE PERFETTO MODULE android.binder" in first_sql


# -----------------------------------------------------------------------
# P0-2b: collect_binder_breakdown (android.binder_breakdown)
# -----------------------------------------------------------------------

class TestCollectBinderBreakdown:

    def test_returns_empty_when_no_data(self):
        collector = _make_collector()
        mock_tp = MagicMock()
        mock_tp.query.return_value = iter([])

        with patch.object(collector, '_open', return_value=mock_tp), \
             patch.object(collector, '_resolve_target_process', return_value={}):
            result = collector.collect_binder_breakdown()

        assert result == {}

    def test_parses_both_breakdowns(self):
        collector = _make_collector()
        mock_tp = MagicMock()

        server_rows = [
            _row(binder_txn_id=1, binder_reply_id=2, ts=1000, dur=5_000_000, reason="cpu"),
        ]
        client_rows = [
            _row(binder_txn_id=1, binder_reply_id=2, ts=1000, dur=3_000_000, reason="waiting_for_reply"),
        ]

        def query_side_effect(sql):
            if "android_binder_server_breakdown" in sql:
                return iter(server_rows)
            if "android_binder_client_breakdown" in sql:
                return iter(client_rows)
            return iter([])

        mock_tp.query.side_effect = query_side_effect

        with patch.object(collector, '_open', return_value=mock_tp), \
             patch.object(collector, '_resolve_target_process', return_value={}):
            result = collector.collect_binder_breakdown()

        assert "server_breakdown" in result
        assert "client_breakdown" in result
        assert result["server_breakdown"][0]["reason"] == "cpu"
        assert result["client_breakdown"][0]["reason"] == "waiting_for_reply"
        assert result["server_breakdown"][0]["dur_ms"] == 5.0


# -----------------------------------------------------------------------
# P0-3: collect_startup_metrics (android.startup)
# -----------------------------------------------------------------------

class TestCollectStartupMetrics:

    def test_returns_empty_when_no_startups(self):
        collector = _make_collector()
        mock_tp = MagicMock()
        mock_tp.query.return_value = iter([])

        with patch.object(collector, '_open', return_value=mock_tp), \
             patch.object(collector, '_resolve_target_process', return_value={"name": "com.example.app"}):
            result = collector.collect_startup_metrics()

        assert result == {}

    def test_parses_startup_with_ttd_and_breakdown(self):
        collector = _make_collector()
        mock_tp = MagicMock()

        startup_rows = [
            _row(startup_id=1, ts=1000, ts_end=5000, dur=4_000_000,
                 package="com.example.app", startup_type="cold"),
        ]
        ttd_rows = [
            _row(startup_id=1, time_to_initial_display=2_000_000,
                 time_to_full_display=4_000_000,
                 ttid_frame_id=10, ttfd_frame_id=20, upid=5),
        ]
        bd_rows = [
            _row(startup_id=1, slice_id=100, ts=1000, dur=1_500_000, reason="binder"),
        ]

        def query_side_effect(sql):
            if "android_startups" in sql and "time_to_display" not in sql:
                return iter(startup_rows)
            if "time_to_display" in sql:
                return iter(ttd_rows)
            if "startup_breakdowns" in sql or "opinionated_breakdown" in sql:
                return iter(bd_rows)
            return iter([])

        mock_tp.query.side_effect = query_side_effect

        with patch.object(collector, '_open', return_value=mock_tp), \
             patch.object(collector, '_resolve_target_process', return_value={"name": "com.example.app"}):
            result = collector.collect_startup_metrics()

        assert result["startups"][0]["dur_ms"] == 4.0
        assert result["startups"][0]["startup_type"] == "cold"
        assert result["time_to_display"][0]["ttid_ms"] == 2.0
        assert result["time_to_display"][0]["ttfd_ms"] == 4.0
        assert result["breakdowns"][0]["reason"] == "binder"

    def test_includes_module_statements(self):
        collector = _make_collector()
        mock_tp = MagicMock()
        mock_tp.query.return_value = iter([])

        with patch.object(collector, '_open', return_value=mock_tp), \
             patch.object(collector, '_resolve_target_process', return_value={}):
            collector.collect_startup_metrics()

        first_sql = mock_tp.query.call_args_list[0][0][0]
        assert "INCLUDE PERFETTO MODULE android.startup.startups" in first_sql


# -----------------------------------------------------------------------
# P0-4: collect_gc_events (android.garbage_collection)
# -----------------------------------------------------------------------

class TestCollectGCEvents:

    def test_returns_empty_when_no_data(self):
        collector = _make_collector()
        mock_tp = MagicMock()
        mock_tp.query.return_value = iter([])

        with patch.object(collector, '_open', return_value=mock_tp), \
             patch.object(collector, '_resolve_target_process', return_value={"upid": 1}):
            result = collector.collect_gc_events()

        assert result == {}

    def test_parses_gc_events(self):
        collector = _make_collector()
        mock_tp = MagicMock()

        gc_rows = [
            _row(
                gc_id=1, gc_ts=1000, gc_dur=15_000_000,
                gc_running_dur=10_000_000, gc_runnable_dur=3_000_000,
                gc_unint_io_dur=1_000_000, gc_unint_non_io_dur=1_000_000,
                gc_type="partial", is_mark_compact=0,
                reclaimed_mb=4.5, min_heap_mb=8.0, max_heap_mb=32.0,
                tid=1001, thread_name="main", process_name="com.example.app", upid=10,
            ),
            _row(
                gc_id=2, gc_ts=5000, gc_dur=8_000_000,
                gc_running_dur=6_000_000, gc_runnable_dur=1_000_000,
                gc_unint_io_dur=0.5e6, gc_unint_non_io_dur=0.5e6,
                gc_type="full", is_mark_compact=1,
                reclaimed_mb=12.0, min_heap_mb=4.0, max_heap_mb=64.0,
                tid=1001, thread_name="main", process_name="com.example.app", upid=10,
            ),
        ]
        mock_tp.query.return_value = iter(gc_rows)

        with patch.object(collector, '_open', return_value=mock_tp), \
             patch.object(collector, '_resolve_target_process', return_value={"upid": 10}):
            result = collector.collect_gc_events()

        assert result["total_count"] == 2
        assert result["total_reclaimed_mb"] == 16.5
        assert result["events"][0]["dur_ms"] == 15.0
        assert result["events"][0]["running_ms"] == 10.0
        assert result["events"][0]["reclaimed_mb"] == 4.5
        assert result["events"][1]["is_mark_compact"] is True

    def test_includes_module(self):
        collector = _make_collector()
        mock_tp = MagicMock()
        mock_tp.query.return_value = iter([])

        with patch.object(collector, '_open', return_value=mock_tp), \
             patch.object(collector, '_resolve_target_process', return_value={}):
            collector.collect_gc_events()

        sql = mock_tp.query.call_args_list[0][0][0]
        assert "INCLUDE PERFETTO MODULE android.garbage_collection" in sql


# -----------------------------------------------------------------------
# P0-5: collect_anrs (android.anrs)
# -----------------------------------------------------------------------

class TestCollectANRs:

    def test_returns_empty_when_no_data(self):
        collector = _make_collector()
        mock_tp = MagicMock()
        mock_tp.query.return_value = iter([])

        with patch.object(collector, '_open', return_value=mock_tp), \
             patch.object(collector, '_resolve_target_process', return_value={"upid": 1}):
            result = collector.collect_anrs()

        assert result == {}

    def test_parses_anr_events(self):
        collector = _make_collector()
        mock_tp = MagicMock()

        anr_rows = [
            _row(
                process_name="com.example.app", pid=1001, upid=10,
                error_id="abc-123", ts=5000,
                subject="Input dispatching timed out",
                intent="com.example.app/.MainActivity",
                component="com.example.app/.MainActivity",
                timer_delay=5000,
                anr_type="INPUT_DISPATCHING",
                anr_dur_ms=10000,
                default_anr_dur_ms=5000,
            ),
        ]
        mock_tp.query.return_value = iter(anr_rows)

        with patch.object(collector, '_open', return_value=mock_tp), \
             patch.object(collector, '_resolve_target_process', return_value={"upid": 10}):
            result = collector.collect_anrs()

        assert result["total_count"] == 1
        assert result["anrs"][0]["anr_type"] == "INPUT_DISPATCHING"
        assert result["anrs"][0]["anr_dur_ms"] == 10000
        assert result["anrs"][0]["subject"] == "Input dispatching timed out"
        assert result["anrs"][0]["intent"] == "com.example.app/.MainActivity"

    def test_omits_null_fields(self):
        collector = _make_collector()
        mock_tp = MagicMock()

        anr_rows = [
            _row(
                process_name="com.example.app", pid=1001, upid=10,
                error_id="abc-456", ts=5000,
                subject="Broadcast timeout",
                intent=None, component=None,
                timer_delay=None,
                anr_type="BROADCAST",
                anr_dur_ms=None, default_anr_dur_ms=None,
            ),
        ]
        mock_tp.query.return_value = iter(anr_rows)

        with patch.object(collector, '_open', return_value=mock_tp), \
             patch.object(collector, '_resolve_target_process', return_value={"upid": 10}):
            result = collector.collect_anrs()

        anr = result["anrs"][0]
        assert "intent" not in anr
        assert "component" not in anr
        assert "anr_dur_ms" not in anr

    def test_uses_upid_filter(self):
        collector = _make_collector()
        mock_tp = MagicMock()
        mock_tp.query.return_value = iter([])

        with patch.object(collector, '_open', return_value=mock_tp), \
             patch.object(collector, '_resolve_target_process', return_value={"upid": 42}):
            collector.collect_anrs()

        sql = mock_tp.query.call_args_list[0][0][0]
        assert "upid = 42" in sql
