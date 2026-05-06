"""Tests for BaseCollector, CollectorRegistry, and PerfSummary."""

import json
import pytest

from smartinspector.collector.base import BaseCollector, PerfSummary, DeviceInfo
from smartinspector.collector.registry import CollectorRegistry


# ---------------------------------------------------------------------------
# Helpers — concrete test collector
# ---------------------------------------------------------------------------


class StubCollector(BaseCollector):
    """Minimal concrete collector for testing."""

    _platform = "test"

    def __init__(self, trace_path: str = "/tmp/stub.pb",
                 target_process: str | None = None, **kwargs):
        super().__init__(trace_path=trace_path, target_process=target_process)
        self._closed = False

    def summarize(self) -> PerfSummary:
        summary = PerfSummary()
        summary.metadata["platform"] = "test"
        return summary

    def close(self) -> None:
        self._closed = True


# ---------------------------------------------------------------------------
# PerfSummary
# ---------------------------------------------------------------------------


class TestPerfSummary:
    def test_default_fields(self):
        s = PerfSummary()
        assert s.frame_timeline == {}
        assert s.cpu_hotspots == []
        assert s.memory is None
        assert s.thread_state == []

    def test_to_json_round_trip(self):
        s = PerfSummary()
        s.metadata["device"] = "pixel8"
        s.cpu_usage = {"cpu_usage_pct": 42}
        j = s.to_json()
        parsed = json.loads(j)
        assert parsed["metadata"]["device"] == "pixel8"
        assert parsed["cpu_usage"]["cpu_usage_pct"] == 42

    def test_non_ascii_preserved(self):
        s = PerfSummary()
        s.metadata["label"] = "帧率分析"
        j = s.to_json()
        assert "帧率分析" in j


# ---------------------------------------------------------------------------
# DeviceInfo
# ---------------------------------------------------------------------------


class TestDeviceInfo:
    def test_defaults(self):
        info = DeviceInfo(platform="android")
        assert info.platform == "android"
        assert info.model == ""
        assert info.extra == {}


# ---------------------------------------------------------------------------
# BaseCollector (abstract contract)
# ---------------------------------------------------------------------------


class TestBaseCollector:
    def test_cannot_instantiate_directly(self):
        with pytest.raises(TypeError):
            BaseCollector(trace_path="/tmp/x.pb")  # type: ignore[abstract]

    def test_concrete_subclass_works(self):
        c = StubCollector()
        assert c.trace_path == "/tmp/stub.pb"
        assert c.target_process is None
        assert c.platform == "test"

    def test_summarize_returns_perf_summary(self):
        c = StubCollector()
        s = c.summarize()
        assert isinstance(s, PerfSummary)
        assert s.metadata["platform"] == "test"

    def test_close(self):
        c = StubCollector()
        assert c._closed is False
        c.close()
        assert c._closed is True

    def test_context_manager(self):
        with StubCollector() as c:
            assert c._closed is False
        assert c._closed is True

    def test_get_device_info_default(self):
        c = StubCollector()
        info = c.get_device_info()
        assert isinstance(info, DeviceInfo)
        assert info.platform == "test"

    def test_is_available_default(self):
        assert StubCollector.is_available() is True

    def test_pull_trace_from_device_default_raises(self):
        with pytest.raises(NotImplementedError, match="does not support"):
            StubCollector.pull_trace_from_device("com.example")

    def test_target_process_forwarded(self):
        c = StubCollector(target_process="com.app")
        assert c.target_process == "com.app"


# ---------------------------------------------------------------------------
# CollectorRegistry
# ---------------------------------------------------------------------------


class TestCollectorRegistry:
    @pytest.fixture(autouse=True)
    def _reset_registry(self):
        """Ensure a clean registry for each test."""
        CollectorRegistry.reset()
        yield
        CollectorRegistry.reset()

    def test_register_and_get(self):
        CollectorRegistry.register("test", StubCollector)
        cls = CollectorRegistry.get("test")
        assert cls is StubCollector

    def test_get_case_insensitive(self):
        CollectorRegistry.register("Test", StubCollector)
        assert CollectorRegistry.get("test") is StubCollector
        assert CollectorRegistry.get("TEST") is StubCollector

    def test_get_default_platform(self):
        CollectorRegistry.register("android", StubCollector)
        CollectorRegistry.set_default_platform("android")
        assert CollectorRegistry.get() is StubCollector

    def test_get_unknown_raises(self):
        with pytest.raises(ValueError, match="No collector registered"):
            CollectorRegistry.get("nonexistent")

    def test_register_non_collector_raises(self):
        with pytest.raises(TypeError, match="not a BaseCollector"):
            CollectorRegistry.register("bad", dict)  # type: ignore[arg-type]

    def test_create_instance(self):
        CollectorRegistry.register("test", StubCollector)
        instance = CollectorRegistry.create("/tmp/x.pb", platform="test")
        assert isinstance(instance, StubCollector)
        assert instance.trace_path == "/tmp/x.pb"

    def test_create_with_target_process(self):
        CollectorRegistry.register("test", StubCollector)
        instance = CollectorRegistry.create(
            "/tmp/x.pb", platform="test", target_process="com.app",
        )
        assert instance.target_process == "com.app"

    def test_list_platforms(self):
        CollectorRegistry.register("ios", StubCollector)
        CollectorRegistry.register("android", StubCollector)
        assert CollectorRegistry.list_platforms() == ["android", "ios"]

    def test_set_default_platform(self):
        CollectorRegistry.register("test", StubCollector)
        CollectorRegistry.set_default_platform("test")
        assert CollectorRegistry.get() is StubCollector

    def test_reset_clears_all(self):
        CollectorRegistry.register("test", StubCollector)
        CollectorRegistry.reset()
        assert CollectorRegistry.list_platforms() == []
