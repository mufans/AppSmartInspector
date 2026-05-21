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
