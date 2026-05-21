"""Tests for AnalysisDimension registry and base infrastructure."""

from smartinspector.collector.dimensions import (
    DimensionRegistry,
    HintContext,
    register_dimension,
)
from smartinspector.collector.dimensions.base import AnalysisDimension


class StubDimension(AnalysisDimension):
    """测试用 stub 维度。"""

    @property
    def name(self) -> str:
        return "stub_test"

    @property
    def description(self) -> str:
        return "测试维度"

    @property
    def metric_triggers(self) -> list[str]:
        return ["测试", "stub"]

    def collect(self, tp) -> dict:
        return {"test": True}

    def compute_hint(self, data: dict, context: HintContext) -> str:
        return "[测试] hint"

    def format_section(self, data: dict) -> str:
        return "## 测试\n数据"


def test_register_and_get():
    DimensionRegistry.clear()
    dim = StubDimension()
    DimensionRegistry.register(dim)

    assert DimensionRegistry.get("stub_test") is dim
    assert len(DimensionRegistry.all()) == 1
    assert DimensionRegistry.all()[0].name == "stub_test"
    DimensionRegistry.clear()


def test_get_nonexistent():
    DimensionRegistry.clear()
    assert DimensionRegistry.get("nonexistent") is None


def test_metric_triggers():
    dim = StubDimension()
    assert "测试" in dim.metric_triggers
    assert dim.metric_keys == ["stub_test"]


def test_hint_context_defaults():
    ctx = HintContext()
    assert ctx.frame_budget_ms == 16.67
    assert ctx.target_process == ""
    assert ctx.trace_duration_ms == 0.0


def test_register_decorator():
    DimensionRegistry.clear()

    @register_dimension
    class DecoratedDim(AnalysisDimension):
        @property
        def name(self) -> str:
            return "decorated"

        def collect(self, tp) -> dict:
            return {}

    # 类装饰器会自动实例化并注册，验证 registry 中存在该维度
    registered = DimensionRegistry.get("decorated")
    assert registered is not None
    assert registered.name == "decorated"
    assert isinstance(registered, DecoratedDim)
    DimensionRegistry.clear()


# --- LockContentionDimension tests ---

from smartinspector.collector.dimensions.lock_contention import LockContentionDimension


def test_lock_contention_name_and_keys():
    dim = LockContentionDimension()
    assert dim.name == "lock_contention"
    assert "锁竞争" in dim.metric_triggers
    assert "futex" in dim.metric_triggers
    assert dim.skill_name == "lock-contention"


def test_lock_contention_hint_main_thread():
    dim = LockContentionDimension()
    data = {
        "threads": [
            {
                "thread_name": "main",
                "futex_wait_count": 35,
                "total_wait_ms": 250.0,
                "max_wait_ms": 45.0,
                "avg_wait_ms": 7.1,
            },
        ],
        "contention_hotspots": [
            {
                "blocked_function": "futex_wait_queue_me",
                "thread_name": "main",
                "occurrences": 20,
                "total_ms": 150.0,
            }
        ],
    }
    hint = dim.compute_hint(data, HintContext())
    assert "[锁竞争]" in hint
    assert "main" in hint


def test_lock_contention_hint_below_threshold():
    dim = LockContentionDimension()
    data = {
        "threads": [
            {
                "thread_name": "bg_thread",
                "futex_wait_count": 5,
                "total_wait_ms": 2.0,
                "max_wait_ms": 1.0,
                "avg_wait_ms": 0.4,
            },
        ]
    }
    assert dim.compute_hint(data, HintContext()) == ""


def test_lock_contention_hint_empty():
    dim = LockContentionDimension()
    assert dim.compute_hint({}, HintContext()) == ""
    assert dim.compute_hint({"threads": []}, HintContext()) == ""


def test_lock_contention_format_section():
    dim = LockContentionDimension()
    data = {
        "threads": [
            {"thread_name": "main", "futex_wait_count": 35, "total_wait_ms": 250.0, "max_wait_ms": 45.0, "avg_wait_ms": 7.1},
        ],
        "contention_hotspots": [
            {"blocked_function": "futex_wait_queue_me", "thread_name": "main", "occurrences": 20, "total_ms": 150.0},
        ],
    }
    section = dim.format_section(data)
    assert "锁竞争" in section
    assert "main" in section


# --- SchedLatencyDimension tests ---

from smartinspector.collector.dimensions.sched_latency import SchedLatencyDimension


def test_sched_latency_name_and_keys():
    dim = SchedLatencyDimension()
    assert dim.name == "sched_latency"
    assert dim.perf_summary_key == "sched_latency"
    assert "sched_latency" in dim.metric_keys
    assert "调度延迟" in dim.metric_triggers


def test_sched_latency_hint_over_budget():
    dim = SchedLatencyDimension()
    data = {
        "threads": [
            {"thread_name": "main", "runnable_count": 50, "avg_runnable_ms": 12.0, "max_runnable_ms": 45.0},
            {"thread_name": "worker", "runnable_count": 30, "avg_runnable_ms": 1.0, "max_runnable_ms": 3.0},
        ],
        "summary": {"total_threads": 2, "over_budget_count": 1, "worst_thread": "main"},
    }
    ctx = HintContext(frame_budget_ms=16.67)
    hint = dim.compute_hint(data, ctx)
    assert "[调度延迟]" in hint
    assert "main" in hint
    assert "worker" not in hint


def test_sched_latency_hint_no_data():
    dim = SchedLatencyDimension()
    assert dim.compute_hint({}, HintContext()) == ""
    assert dim.compute_hint({"threads": []}, HintContext()) == ""


def test_sched_latency_hint_all_below_threshold():
    dim = SchedLatencyDimension()
    data = {
        "threads": [
            {"thread_name": "bg", "runnable_count": 10, "avg_runnable_ms": 0.5, "max_runnable_ms": 1.0},
        ]
    }
    assert dim.compute_hint(data, HintContext(frame_budget_ms=16.67)) == ""


def test_sched_latency_format_section():
    dim = SchedLatencyDimension()
    data = {
        "threads": [
            {"thread_name": "main", "runnable_count": 50, "avg_runnable_ms": 12.0, "max_runnable_ms": 45.0},
        ]
    }
    section = dim.format_section(data)
    assert "调度延迟" in section
    assert "main" in section
    assert "| " in section


def test_sched_latency_format_empty():
    dim = SchedLatencyDimension()
    assert dim.format_section({}) == ""
    assert dim.format_section({"threads": []}) == ""


# --- FileIODimension tests ---

from smartinspector.collector.dimensions.file_io import FileIODimension


def test_file_io_name_and_keys():
    dim = FileIODimension()
    assert dim.name == "file_io"
    assert "文件io" in dim.metric_triggers
    assert dim.skill_name == "io-analysis"


def test_file_io_hint_main_thread_blocked():
    dim = FileIODimension()
    data = {
        "blocking_events": [
            {"blocked_function": "folio_wait_bit_common", "thread_name": "main", "occurrences": 8, "total_ms": 120.0, "max_ms": 45.0},
        ],
        "main_thread_total_ms": 120.0,
    }
    hint = dim.compute_hint(data, HintContext())
    assert "[主线程IO]" in hint
    assert "main" in hint


def test_file_io_hint_no_main_thread():
    dim = FileIODimension()
    data = {
        "blocking_events": [
            {"blocked_function": "folio_wait_bit_common", "thread_name": "bg_thread", "occurrences": 3, "total_ms": 5.0, "max_ms": 2.0},
        ],
        "main_thread_total_ms": 0.0,
    }
    assert dim.compute_hint(data, HintContext()) == ""


def test_file_io_hint_empty():
    dim = FileIODimension()
    assert dim.compute_hint({}, HintContext()) == ""
    assert dim.compute_hint({"blocking_events": []}, HintContext()) == ""


def test_file_io_format():
    dim = FileIODimension()
    data = {
        "blocking_events": [
            {"blocked_function": "folio_wait_bit_common", "thread_name": "main", "occurrences": 8, "total_ms": 120.0, "max_ms": 45.0},
        ],
        "main_thread_total_ms": 120.0,
    }
    section = dim.format_section(data)
    assert "IO" in section
    assert "folio_wait_bit_common" in section


# --- MemoryTrendDimension tests ---

from smartinspector.collector.dimensions.memory_trend import MemoryTrendDimension


def test_memory_trend_name_and_keys():
    dim = MemoryTrendDimension()
    assert dim.name == "memory_trend"
    assert "内存趋势" in dim.metric_triggers
    assert dim.skill_name == "memory-analysis"


def test_memory_trend_hint_growth():
    dim = MemoryTrendDimension()
    data = {
        "process_name": "com.example.app",
        "samples": 50,
        "start_rss_mb": 120.0,
        "end_rss_mb": 180.0,
        "delta_mb": 60.0,
        "delta_pct": 50.0,
        "trend_slope_mb_per_s": 6.0,
    }
    hint = dim.compute_hint(data, HintContext())
    assert "[内存趋势]" in hint
    assert "50.0%" in hint or "50%" in hint


def test_memory_trend_hint_stable():
    dim = MemoryTrendDimension()
    data = {
        "process_name": "com.example.app",
        "samples": 50,
        "start_rss_mb": 120.0,
        "end_rss_mb": 130.0,
        "delta_mb": 10.0,
        "delta_pct": 8.3,
        "trend_slope_mb_per_s": 1.0,
    }
    assert dim.compute_hint(data, HintContext()) == ""


def test_memory_trend_hint_empty():
    dim = MemoryTrendDimension()
    assert dim.compute_hint({}, HintContext()) == ""


def test_memory_trend_format():
    dim = MemoryTrendDimension()
    data = {
        "process_name": "com.example.app",
        "samples": 50,
        "start_rss_mb": 120.0,
        "end_rss_mb": 180.0,
        "delta_mb": 60.0,
        "delta_pct": 50.0,
        "trend_slope_mb_per_s": 6.0,
    }
    section = dim.format_section(data)
    assert "内存" in section
    assert "120" in section
    assert "180" in section


# --- BinderIPCDimension tests ---

from smartinspector.collector.dimensions.binder_ipc import BinderIPCDimension


def test_binder_ipc_name_and_keys():
    dim = BinderIPCDimension()
    assert dim.name == "binder_ipc"
    assert "binder" in dim.metric_triggers
    assert dim.skill_name == "binder-ipc"


def test_binder_ipc_format():
    dim = BinderIPCDimension()
    data = {
        "threads": [
            {"thread_name": "main", "binder_waits": 12, "total_wait_ms": 180.0, "max_wait_ms": 35.0},
        ]
    }
    section = dim.format_section(data)
    assert "Binder" in section
    assert "main" in section


def test_binder_ipc_format_empty():
    dim = BinderIPCDimension()
    assert dim.format_section({}) == ""
    assert dim.format_section({"threads": []}) == ""


# --- CpuThrottlingDimension tests ---

from smartinspector.collector.dimensions.cpu_throttling import CpuThrottlingDimension


def test_cpu_throttling_name_and_keys():
    dim = CpuThrottlingDimension()
    assert dim.name == "cpu_throttling"
    assert "降频" in dim.metric_triggers
    assert dim.skill_name == "cpu-throttling"
    assert "sys_stats" in dim.metric_keys


def test_cpu_throttling_hint_throttled():
    dim = CpuThrottlingDimension()
    data = {
        "cpu_freq_by_core": {
            "0": {"min_mhz": 300, "max_mhz": 2841, "avg_mhz": 800, "samples": 100},
            "4": {"min_mhz": 300, "max_mhz": 2841, "avg_mhz": 400, "samples": 100},
        },
        "throttled_cores": [
            {"core": 4, "max_mhz": 2841, "avg_mhz": 400, "throttle_pct": 85.9},
        ],
    }
    hint = dim.compute_hint(data, HintContext())
    assert "[CPU降频]" in hint


def test_cpu_throttling_hint_normal():
    dim = CpuThrottlingDimension()
    data = {
        "cpu_freq_by_core": {
            "0": {"min_mhz": 300, "max_mhz": 2841, "avg_mhz": 2000, "samples": 100},
        },
        "throttled_cores": [],
    }
    assert dim.compute_hint(data, HintContext()) == ""


def test_cpu_throttling_hint_empty():
    dim = CpuThrottlingDimension()
    assert dim.compute_hint({}, HintContext()) == ""


# --- GcEventsDimension tests ---

from smartinspector.collector.dimensions.gc_events import GcEventsDimension


def test_gc_events_name_and_keys():
    dim = GcEventsDimension()
    assert dim.name == "gc_events"
    assert "gc" in dim.metric_triggers
    assert "垃圾回收" in dim.metric_triggers
    assert dim.skill_name == "gc-analysis"


def test_gc_events_hint_with_pause():
    dim = GcEventsDimension()
    data = {
        "total_count": 15,
        "total_pause_ms": 120.0,
        "max_pause_ms": 35.0,
        "main_thread_pause_ms": 80.0,
        "events": [
            {"name": "GC: Wait For Concurrent", "dur_ms": 35.0, "gc_reason": "Alloc", "gc_type": "Concurrent"},
            {"name": "GC: Alloc", "dur_ms": 8.0, "gc_reason": "Alloc", "gc_type": "Non-concurrent"},
        ],
    }
    hint = dim.compute_hint(data, HintContext(frame_budget_ms=16.67))
    assert "[GC分析]" in hint
    assert "35.0" in hint


def test_gc_events_hint_below_threshold():
    dim = GcEventsDimension()
    data = {
        "total_count": 2,
        "total_pause_ms": 3.0,
        "max_pause_ms": 2.0,
        "main_thread_pause_ms": 0.0,
        "events": [
            {"name": "GC: Background", "dur_ms": 2.0, "gc_reason": "Background", "gc_type": "Concurrent"},
        ],
    }
    assert dim.compute_hint(data, HintContext(frame_budget_ms=16.67)) == ""


def test_gc_events_hint_empty():
    dim = GcEventsDimension()
    assert dim.compute_hint({}, HintContext()) == ""
    assert dim.compute_hint({"events": []}, HintContext()) == ""


def test_gc_events_format_section():
    dim = GcEventsDimension()
    data = {
        "total_count": 5,
        "total_pause_ms": 50.0,
        "max_pause_ms": 20.0,
        "main_thread_pause_ms": 30.0,
        "events": [
            {"name": "GC: Alloc", "dur_ms": 20.0, "gc_reason": "Alloc", "gc_type": "Non-concurrent"},
        ],
    }
    section = dim.format_section(data)
    assert "GC" in section
    assert "20.0" in section


def test_gc_events_format_empty():
    dim = GcEventsDimension()
    assert dim.format_section({"events": []}) == ""
