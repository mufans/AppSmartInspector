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
