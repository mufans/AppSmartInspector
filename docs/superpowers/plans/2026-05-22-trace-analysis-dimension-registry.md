# Trace 分析维度注册表 Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** 建立 AnalysisDimension 维度注册表架构，新增 7 个分析维度（3 P0 + 4 P1），并完成 Prompt Skill 按维度化改造。

**Architecture:** 每个 AnalysisDimension 是自包含类（collect + hint + format + metric），通过 DimensionRegistry 自动发现和注册。新维度存放在 `collector/dimensions/` 包，数据存入 PerfSummary.dimensions dict。Pipeline 的 summarize/compute_hints/format_perf_sections/metric_qa 四个环节统一从 Registry 驱动。Prompt Skill 按维度组织知识文件，Agent 动态加载有数据的维度。

**Tech Stack:** Python 3.12+, Perfetto TraceProcessor SQL, pytest

**Spec:** `docs/superpowers/specs/2026-05-22-trace-analysis-dimension-registry.md`

---

## File Structure

### New Files

| File | Responsibility |
|------|---------------|
| `src/smartinspector/collector/dimensions/__init__.py` | DimensionRegistry + discover + _register 装饰器 |
| `src/smartinspector/collector/dimensions/base.py` | AnalysisDimension ABC + HintContext |
| `src/smartinspector/collector/dimensions/sched_latency.py` | CPU 调度延迟维度 |
| `src/smartinspector/collector/dimensions/lock_contention.py` | 锁竞争维度 |
| `src/smartinspector/collector/dimensions/gc_events.py` | GC 事件维度 |
| `src/smartinspector/collector/dimensions/file_io.py` | 文件 IO 延迟维度 |
| `src/smartinspector/collector/dimensions/memory_trend.py` | 内存增长趋势维度 |
| `src/smartinspector/collector/dimensions/binder_ipc.py` | Binder IPC 维度 |
| `src/smartinspector/collector/dimensions/cpu_throttling.py` | CPU 降频检测维度 |
| `prompts/skills/SKILL.md` | 维度 skill 索引文件 |
| `prompts/skills/dimensions/gc-analysis.md` | GC 分析知识 |
| `prompts/skills/dimensions/lock-contention.md` | 锁竞争知识 |
| `prompts/skills/dimensions/cpu-scheduling.md` | CPU 调度知识 |
| `prompts/skills/dimensions/io-analysis.md` | IO 分析知识 |
| `prompts/skills/dimensions/memory-analysis.md` | 内存分析知识 |
| `prompts/skills/dimensions/binder-ipc.md` | Binder IPC 知识 |
| `prompts/skills/dimensions/cpu-throttling.md` | CPU 降频知识 |
| `prompts/skills/dimensions/ui-jank.md` | UI/帧率知识 |
| `prompts/skills/dimensions/startup.md` | 冷启动知识 |
| `prompts/skills/shared/si-tag-system.md` | SI$ 标签格式共享知识 |
| `prompts/skills/shared/search-strategy.md` | 搜索策略共享知识 |
| `tests/test_dimensions.py` | 维度注册表 + 各维度单元测试 |

### Modified Files

| File | Change |
|------|--------|
| `src/smartinspector/collector/perfetto.py:64-80` | PerfSummary 新增 `dimensions` 字段 |
| `src/smartinspector/collector/perfetto.py:1590-1726` | summarize() 新增 Registry 调用 |
| `src/smartinspector/agents/deterministic.py:255-281` | compute_hints() 新增 Registry 调用 |
| `src/smartinspector/graph/nodes/reporter/formatter.py:6-174` | format_perf_sections() 新增 Registry 调用 |
| `src/smartinspector/graph/nodes/metric_qa.py:56-77` | METRIC_DATA_MAP 从 Registry 动态扩展 |
| `src/smartinspector/prompts.py` | 新增 load_skill() + load_prompt_with_skills() |
| Agent 文件 (4个) | 适配 load_prompt_with_skills() |
| `CLAUDE.md` | 更新文档 |

---

## Task 1: 维度注册表基础设施

**Files:**
- Create: `src/smartinspector/collector/dimensions/__init__.py`
- Create: `src/smartinspector/collector/dimensions/base.py`
- Create: `tests/test_dimensions.py`
- Modify: `src/smartinspector/collector/perfetto.py:64-80`

- [ ] **Step 1: Create base.py — AnalysisDimension ABC + HintContext**

```python
# src/smartinspector/collector/dimensions/base.py

"""Analysis dimension base class and hint context."""

from abc import ABC, abstractmethod
from dataclasses import dataclass


@dataclass
class HintContext:
    """Deterministic hint 计算上下文。"""

    frame_budget_ms: float = 16.67
    target_process: str = ""
    trace_duration_ms: float = 0.0


class AnalysisDimension(ABC):
    """分析维度基类。每个维度自包含 collect + hint + format + metric 逻辑。"""

    @property
    @abstractmethod
    def name(self) -> str:
        """维度唯一标识，如 'sched_latency'。"""

    @property
    def description(self) -> str:
        """维度中文描述。"""
        return self.name

    @property
    def perf_summary_key(self) -> str:
        """在 PerfSummary JSON 中的 key，默认等于 name。"""
        return self.name

    @property
    def metric_triggers(self) -> list[str]:
        """Metric QA 自然语言触发词列表。"""
        return []

    @property
    def metric_keys(self) -> list[str]:
        """Metric QA 对应的 perf_summary JSON keys。"""
        return [self.perf_summary_key]

    @property
    def skill_name(self) -> str:
        """对应的 dimension skill 文件名（不含 .md）。"""
        return self.name

    @abstractmethod
    def collect(self, tp) -> dict:
        """执行 SQL 查询，返回结构化数据。

        Args:
            tp: TraceProcessor 实例（已打开 trace 文件）。

        Returns:
            结构化 dict，序列化到 PerfSummary JSON。
        """

    def compute_hint(self, data: dict, context: HintContext) -> str:
        """计算 deterministic hint。返回空字符串表示无数据。

        Args:
            data: collect() 返回的数据。
            context: 全局分析上下文（帧预算、目标进程等）。

        Returns:
            中文文本 hint，格式为 "[标签] 具体结论"。
        """
        return ""

    def format_section(self, data: dict) -> str:
        """格式化为 LLM prompt 中的 markdown section。

        Args:
            data: collect() 返回的数据。

        Returns:
            Markdown 格式字符串，空字符串表示不输出。
        """
        return ""

    def metric_filter(self, data: dict) -> dict:
        """Metric QA 数据过滤。默认直接透传。"""
        return data
```

- [ ] **Step 2: Create __init__.py — DimensionRegistry**

```python
# src/smartinspector/collector/dimensions/__init__.py

"""Analysis dimension registry with auto-discovery."""

import importlib
import pkgutil

from smartinspector.collector.dimensions.base import AnalysisDimension, HintContext


class DimensionRegistry:
    """分析维度注册表。支持自动发现和按名获取。"""

    _dimensions: dict[str, AnalysisDimension] = {}

    @classmethod
    def register(cls, dim: AnalysisDimension) -> None:
        cls._dimensions[dim.name] = dim

    @classmethod
    def all(cls) -> list[AnalysisDimension]:
        return list(cls._dimensions.values())

    @classmethod
    def get(cls, name: str) -> AnalysisDimension | None:
        return cls._dimensions.get(name)

    @classmethod
    def discover(cls) -> None:
        """自动发现 dimensions/ 包下的所有维度模块。"""
        from smartinspector.collector import dimensions as pkg

        for _, module_name, _ in pkgutil.iter_modules(pkg.__path__):
            importlib.import_module(
                f"smartinspector.collector.dimensions.{module_name}"
            )

    @classmethod
    def clear(cls) -> None:
        """清空注册表（仅用于测试）。"""
        cls._dimensions.clear()


def register_dimension(dim: AnalysisDimension) -> AnalysisDimension:
    """类装饰器：注册维度实例到 Registry。"""
    DimensionRegistry.register(dim)
    return dim
```

- [ ] **Step 3: Write test for Registry basics**

```python
# tests/test_dimensions.py

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

    dim = DecoratedDim()
    assert DimensionRegistry.get("decorated") is dim
    DimensionRegistry.clear()
```

- [ ] **Step 4: Run tests to verify**

Run: `cd /Users/liujun/langchainProjects/AppSmartInspector && uv run pytest tests/test_dimensions.py -v`
Expected: 5 passed

- [ ] **Step 5: Add `dimensions` field to PerfSummary**

In `src/smartinspector/collector/perfetto.py`, after line 79 (`thread_state: list[dict]`), add:

```python
    dimensions: dict = field(default_factory=dict)  # Registry 维度数据
```

- [ ] **Step 6: Verify PerfSummary serialization**

Run: `cd /Users/liujun/langchainProjects/AppSmartInspector && uv run python -c "from smartinspector.collector.perfetto import PerfSummary; s = PerfSummary(); print(s.to_json())" | python -m json.tool | grep dimensions`
Expected: `"dimensions": {}`

- [ ] **Step 7: Commit**

```bash
cd /Users/liujun/langchainProjects/AppSmartInspector && git add src/smartinspector/collector/dimensions/ tests/test_dimensions.py src/smartinspector/collector/perfetto.py && git commit -m "feat(collector): add AnalysisDimension registry infrastructure and PerfSummary.dimensions field"
```

---

## Task 2: SchedLatencyDimension (P0)

**Files:**
- Create: `src/smartinspector/collector/dimensions/sched_latency.py`
- Modify: `tests/test_dimensions.py`

- [ ] **Step 1: Write failing tests for SchedLatencyDimension**

Append to `tests/test_dimensions.py`:

```python
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
    assert "12.0ms" in hint or "12.0" in hint
    # worker below threshold should not appear
    assert "worker" not in hint


def test_sched_latency_hint_no_data():
    dim = SchedLatencyDimension()
    hint = dim.compute_hint({}, HintContext())
    assert hint == ""
    hint = dim.compute_hint({"threads": []}, HintContext())
    assert hint == ""


def test_sched_latency_hint_all_below_threshold():
    dim = SchedLatencyDimension()
    data = {
        "threads": [
            {"thread_name": "bg", "runnable_count": 10, "avg_runnable_ms": 0.5, "max_runnable_ms": 1.0},
        ]
    }
    hint = dim.compute_hint(data, HintContext(frame_budget_ms=16.67))
    assert hint == ""


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
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `cd /Users/liujun/langchainProjects/AppSmartInspector && uv run pytest tests/test_dimensions.py::test_sched_latency_name_and_keys -v`
Expected: FAIL (ImportError: cannot import name 'SchedLatencyDimension')

- [ ] **Step 3: Implement SchedLatencyDimension**

```python
# src/smartinspector/collector/dimensions/sched_latency.py

"""CPU scheduling latency analysis dimension."""

from smartinspector.collector.dimensions import HintContext, register_dimension
from smartinspector.collector.dimensions.base import AnalysisDimension
from smartinspector.debug_log import debug_log


@register_dimension
class SchedLatencyDimension(AnalysisDimension):
    """分析线程从 runnable 到 running 的调度延迟。"""

    @property
    def name(self) -> str:
        return "sched_latency"

    @property
    def description(self) -> str:
        return "CPU 调度延迟分析"

    @property
    def skill_name(self) -> str:
        return "cpu-scheduling"

    @property
    def metric_triggers(self) -> list[str]:
        return ["调度延迟", "sched_latency", "runnable"]

    def collect(self, tp) -> dict:
        """使用 perfetto 标准库 sched.runnable 模块。"""
        rows = tp.query("""
            INCLUDE PERFETTO MODULE sched.runnable;

            SELECT
              thread_name,
              COUNT(*) AS runnable_count,
              AVG(runnable_dur) / 1e6 AS avg_runnable_ms,
              MAX(runnable_dur) / 1e6 AS max_runnable_ms
            FROM sched_runnable
            GROUP BY thread_name
            HAVING runnable_count > 5
            ORDER BY avg_runnable_ms DESC
            LIMIT 15
        """)

        threads = []
        for r in rows:
            threads.append({
                "thread_name": r.thread_name,
                "runnable_count": r.runnable_count,
                "avg_runnable_ms": round(r.avg_runnable_ms, 2),
                "max_runnable_ms": round(r.max_runnable_ms, 2),
            })

        result: dict = {"threads": threads}

        if threads:
            result["summary"] = {
                "total_threads": len(threads),
                "worst_thread": max(
                    threads, key=lambda t: t["avg_runnable_ms"]
                )["thread_name"],
            }

        debug_log("collector", f"sched_latency: {len(threads)} threads")
        return result

    def compute_hint(self, data: dict, context: HintContext) -> str:
        threads = data.get("threads", [])
        if not threads:
            return ""

        budget_threshold = context.frame_budget_ms * 0.5
        over = [t for t in threads if t["avg_runnable_ms"] > budget_threshold]

        if not over:
            return ""

        lines = [f"[调度延迟] (帧预算: {context.frame_budget_ms:.2f}ms)"]
        for t in sorted(over, key=lambda x: -x["avg_runnable_ms"]):
            lines.append(
                f"  {t['thread_name']}: avg={t['avg_runnable_ms']:.1f}ms, "
                f"max={t['max_runnable_ms']:.1f}ms, "
                f"runnable次数={t['runnable_count']}"
            )
        return "\n".join(lines)

    def format_section(self, data: dict) -> str:
        threads = data.get("threads", [])
        if not threads:
            return ""

        lines = ["## 调度延迟分析\n"]
        lines.append("| 线程 | 平均延迟 | 最大延迟 | runnable 次数 |")
        lines.append("|------|---------|---------|-------------|")
        for t in threads[:10]:
            lines.append(
                f"| {t['thread_name']} | {t['avg_runnable_ms']:.1f}ms | "
                f"{t['max_runnable_ms']:.1f}ms | {t['runnable_count']} |"
            )
        return "\n".join(lines)
```

- [ ] **Step 4: Run tests to verify they pass**

Run: `cd /Users/liujun/langchainProjects/AppSmartInspector && uv run pytest tests/test_dimensions.py -v -k sched_latency`
Expected: 6 passed

- [ ] **Step 5: Commit**

```bash
cd /Users/liujun/langchainProjects/AppSmartInspector && git add src/smartinspector/collector/dimensions/sched_latency.py tests/test_dimensions.py && git commit -m "feat(collector): add SchedLatencyDimension with sched.runnable module"
```

---

## Task 3: LockContentionDimension (P0)

**Files:**
- Create: `src/smartinspector/collector/dimensions/lock_contention.py`
- Modify: `tests/test_dimensions.py`

- [ ] **Step 1: Write failing tests for LockContentionDimension**

Append to `tests/test_dimensions.py`:

```python
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
    assert "45.0ms" in hint or "45.0" in hint


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
    hint = dim.compute_hint(data, HintContext())
    assert hint == ""


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
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `cd /Users/liujun/langchainProjects/AppSmartInspector && uv run pytest tests/test_dimensions.py::test_lock_contention_name_and_keys -v`
Expected: FAIL (ImportError)

- [ ] **Step 3: Implement LockContentionDimension**

```python
# src/smartinspector/collector/dimensions/lock_contention.py

"""Lock contention analysis dimension."""

from smartinspector.collector.dimensions import HintContext, register_dimension
from smartinspector.collector.dimensions.base import AnalysisDimension
from smartinspector.debug_log import debug_log


@register_dimension
class LockContentionDimension(AnalysisDimension):
    """分析 futex 等待导致的锁竞争。"""

    @property
    def name(self) -> str:
        return "lock_contention"

    @property
    def description(self) -> str:
        return "锁竞争分析"

    @property
    def skill_name(self) -> str:
        return "lock-contention"

    @property
    def metric_triggers(self) -> list[str]:
        return ["锁竞争", "lock", "futex", "锁"]

    def collect(self, tp) -> dict:
        """使用 __intrinsic_thread_state 分析 futex 等待。"""
        rows = tp.query("""
            SELECT
              t.name AS thread_name,
              COUNT(*) AS futex_wait_count,
              SUM(its.dur) / 1e6 AS total_wait_ms,
              MAX(its.dur) / 1e6 AS max_wait_ms,
              AVG(its.dur) / 1e6 AS avg_wait_ms
            FROM __intrinsic_thread_state its
            JOIN thread t ON its.utid = t.utid
            WHERE its.blocked_function GLOB '*futex*'
              AND its.dur > 100000
            GROUP BY t.name
            ORDER BY total_wait_ms DESC
            LIMIT 15
        """)

        threads = []
        for r in rows:
            threads.append({
                "thread_name": r.thread_name,
                "futex_wait_count": r.futex_wait_count,
                "total_wait_ms": round(r.total_wait_ms, 2),
                "max_wait_ms": round(r.max_wait_ms, 2),
                "avg_wait_ms": round(r.avg_wait_ms, 2),
            })

        # 收集 blocked_function 维度的热点
        hotspots = []
        try:
            hs_rows = tp.query("""
                SELECT
                  t.name AS thread_name,
                  its.blocked_function,
                  COUNT(*) AS occurrences,
                  SUM(its.dur) / 1e6 AS total_ms
                FROM __intrinsic_thread_state its
                JOIN thread t ON its.utid = t.utid
                WHERE its.blocked_function GLOB '*futex*'
                  AND its.dur > 100000
                GROUP BY t.name, its.blocked_function
                ORDER BY total_ms DESC
                LIMIT 10
            """)
            for r in hs_rows:
                hotspots.append({
                    "blocked_function": r.blocked_function,
                    "thread_name": r.thread_name,
                    "occurrences": r.occurrences,
                    "total_ms": round(r.total_ms, 2),
                })
        except Exception as e:
            debug_log("collector", f"lock_contention hotspots query failed: {e}")

        result: dict = {"threads": threads}
        if hotspots:
            result["contention_hotspots"] = hotspots

        debug_log("collector", f"lock_contention: {len(threads)} threads, {len(hotspots)} hotspots")
        return result

    def compute_hint(self, data: dict, context: HintContext) -> str:
        threads = data.get("threads", [])
        if not threads:
            return ""

        # 主线程阈值 5ms，其他线程 10ms
        main_threshold = 5.0
        other_threshold = 10.0

        flagged = []
        for t in threads:
            name = t["thread_name"]
            threshold = main_threshold if name == "main" else other_threshold
            if t["max_wait_ms"] > threshold:
                flagged.append(t)

        if not flagged:
            return ""

        lines = ["[锁竞争]"]
        for t in sorted(flagged, key=lambda x: -x["total_wait_ms"]):
            lines.append(
                f"  {t['thread_name']}: futex等待{t['futex_wait_count']}次, "
                f"total={t['total_wait_ms']:.1f}ms, "
                f"max={t['max_wait_ms']:.1f}ms, "
                f"avg={t['avg_wait_ms']:.1f}ms"
            )

        hotspots = data.get("contention_hotspots", [])
        if hotspots:
            lines.append("  热点函数:")
            for hs in hotspots[:3]:
                lines.append(
                    f"    {hs['blocked_function']} ({hs['thread_name']}): "
                    f"{hs['occurrences']}次, total={hs['total_ms']:.1f}ms"
                )

        return "\n".join(lines)

    def format_section(self, data: dict) -> str:
        threads = data.get("threads", [])
        if not threads:
            return ""

        lines = ["## 锁竞争分析\n"]
        lines.append("| 线程 | 等待次数 | 总等待 | 最大等待 | 平均等待 |")
        lines.append("|------|---------|--------|---------|---------|")
        for t in threads[:10]:
            lines.append(
                f"| {t['thread_name']} | {t['futex_wait_count']} | "
                f"{t['total_wait_ms']:.1f}ms | {t['max_wait_ms']:.1f}ms | "
                f"{t['avg_wait_ms']:.1f}ms |"
            )

        hotspots = data.get("contention_hotspots", [])
        if hotspots:
            lines.append("\n**热点函数:**\n")
            for hs in hotspots[:5]:
                lines.append(
                    f"- `{hs['blocked_function']}` ({hs['thread_name']}): "
                    f"{hs['occurrences']}次, {hs['total_ms']:.1f}ms"
                )

        return "\n".join(lines)
```

- [ ] **Step 4: Run tests to verify they pass**

Run: `cd /Users/liujun/langchainProjects/AppSmartInspector && uv run pytest tests/test_dimensions.py -v -k lock_contention`
Expected: 5 passed

- [ ] **Step 5: Commit**

```bash
cd /Users/liujun/langchainProjects/AppSmartInspector && git add src/smartinspector/collector/dimensions/lock_contention.py tests/test_dimensions.py && git commit -m "feat(collector): add LockContentionDimension with futex wait analysis"
```

---

## Task 4: GcEventsDimension (P0)

**Files:**
- Create: `src/smartinspector/collector/dimensions/gc_events.py`
- Modify: `tests/test_dimensions.py`

- [ ] **Step 1: Write failing tests for GcEventsDimension**

Append to `tests/test_dimensions.py`:

```python
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
    assert "35.0ms" in hint or "35" in hint


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
    hint = dim.compute_hint(data, HintContext(frame_budget_ms=16.67))
    assert hint == ""


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
    assert "20.0ms" in section or "20.0" in section


def test_gc_events_format_empty():
    dim = GcEventsDimension()
    assert dim.format_section({"events": []}) == ""
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `cd /Users/liujun/langchainProjects/AppSmartInspector && uv run pytest tests/test_dimensions.py::test_gc_events_name_and_keys -v`
Expected: FAIL (ImportError)

- [ ] **Step 3: Implement GcEventsDimension**

```python
# src/smartinspector/collector/dimensions/gc_events.py

"""GC event analysis dimension."""

from smartinspector.collector.dimensions import HintContext, register_dimension
from smartinspector.collector.dimensions.base import AnalysisDimension
from smartinspector.debug_log import debug_log


@register_dimension
class GcEventsDimension(AnalysisDimension):
    """分析 GC 事件对主线程和帧率的影响。"""

    @property
    def name(self) -> str:
        return "gc_events"

    @property
    def description(self) -> str:
        return "GC 事件分析"

    @property
    def skill_name(self) -> str:
        return "gc-analysis"

    @property
    def metric_triggers(self) -> list[str]:
        return ["gc", "GC", "垃圾回收", "垃圾回收器"]

    def collect(self, tp) -> dict:
        """从 slice 表查询 GC 事件。"""
        rows = tp.query("""
            SELECT
              name,
              ts,
              IIF(dur = -1, 0, dur) AS dur,
              EXTRACT_ARG(arg_set_id, 'reason') AS gc_reason,
              EXTRACT_ARG(arg_set_id, 'gc_type') AS gc_type,
              track_id
            FROM slice
            WHERE name GLOB '*GC*'
               OR name GLOB '*gc*'
               OR name GLOB '*GarbageCollector*'
               OR name GLOB '*ConcurrentGC*'
            ORDER BY dur DESC
            LIMIT 20
        """)

        events = []
        total_pause_ms = 0.0
        main_thread_pause_ms = 0.0

        for r in rows:
            dur_ms = round(r.dur / 1e6, 2)
            events.append({
                "name": r.name,
                "ts_ns": r.ts,
                "dur_ms": dur_ms,
                "gc_reason": r.gc_reason or "",
                "gc_type": r.gc_type or "",
            })
            total_pause_ms += dur_ms
            # 主线程 GC: name 含 "Wait For Concurrent" 或 "Alloc" 且 dur > 1ms
            if dur_ms > 1.0 and (
                "Wait For Concurrent" in r.name or r.name == "GC: Alloc"
            ):
                main_thread_pause_ms += dur_ms

        result = {
            "total_count": len(events),
            "total_pause_ms": round(total_pause_ms, 2),
            "max_pause_ms": events[0]["dur_ms"] if events else 0.0,
            "main_thread_pause_ms": round(main_thread_pause_ms, 2),
            "events": events,
        }

        debug_log("collector", f"gc_events: {len(events)} events, total={total_pause_ms:.1f}ms")
        return result

    def compute_hint(self, data: dict, context: HintContext) -> str:
        events = data.get("events", [])
        if not events:
            return ""

        max_pause = data.get("max_pause_ms", 0.0)
        main_pause = data.get("main_thread_pause_ms", 0.0)

        # P1 阈值: 任何 GC pause > 10ms
        if max_pause < 10.0:
            return ""

        lines = ["[GC分析]"]
        lines.append(
            f"  总暂停: {data.get('total_pause_ms', 0):.1f}ms, "
            f"最大: {max_pause:.1f}ms, "
            f"主线程影响: {main_pause:.1f}ms"
        )

        # 列出 >10ms 的事件
        over_10ms = [e for e in events if e["dur_ms"] > 10.0]
        if over_10ms:
            lines.append("  超过10ms的GC事件:")
            for e in over_10ms[:5]:
                lines.append(
                    f"    {e['name']}: {e['dur_ms']:.1f}ms "
                    f"(reason={e['gc_reason']}, type={e['gc_type']})"
                )

        # P0: 超过帧预算
        if max_pause > context.frame_budget_ms:
            lines.append(
                f"  ⚠ GC暂停超过帧预算({context.frame_budget_ms:.1f}ms)，可能导致jank"
            )

        return "\n".join(lines)

    def format_section(self, data: dict) -> str:
        events = data.get("events", [])
        if not events:
            return ""

        lines = ["## GC 事件分析\n"]
        lines.append(
            f"总次数: {data.get('total_count', 0)}, "
            f"总暂停: {data.get('total_pause_ms', 0):.1f}ms, "
            f"最大: {data.get('max_pause_ms', 0):.1f}ms, "
            f"主线程影响: {data.get('main_thread_pause_ms', 0):.1f}ms\n"
        )

        lines.append("| 事件 | 耗时 | 原因 | 类型 |")
        lines.append("|------|------|------|------|")
        for e in events[:10]:
            lines.append(
                f"| {e['name']} | {e['dur_ms']:.1f}ms | "
                f"{e['gc_reason']} | {e['gc_type']} |"
            )

        return "\n".join(lines)
```

- [ ] **Step 4: Run tests to verify they pass**

Run: `cd /Users/liujun/langchainProjects/AppSmartInspector && uv run pytest tests/test_dimensions.py -v -k gc_events`
Expected: 6 passed

- [ ] **Step 5: Commit**

```bash
cd /Users/liujun/langchainProjects/AppSmartInspector && git add src/smartinspector/collector/dimensions/gc_events.py tests/test_dimensions.py && git commit -m "feat(collector): add GcEventsDimension with GC pause analysis"
```

---

## Task 5: P1 Dimensions (FileIO, MemoryTrend, BinderIPC, CpuThrottling)

**Files:**
- Create: `src/smartinspector/collector/dimensions/file_io.py`
- Create: `src/smartinspector/collector/dimensions/memory_trend.py`
- Create: `src/smartinspector/collector/dimensions/binder_ipc.py`
- Create: `src/smartinspector/collector/dimensions/cpu_throttling.py`
- Modify: `tests/test_dimensions.py`

- [ ] **Step 1: Write failing tests for all 4 P1 dimensions**

Append to `tests/test_dimensions.py`:

```python
from smartinspector.collector.dimensions.file_io import FileIODimension
from smartinspector.collector.dimensions.memory_trend import MemoryTrendDimension
from smartinspector.collector.dimensions.binder_ipc import BinderIPCDimension
from smartinspector.collector.dimensions.cpu_throttling import CpuThrottlingDimension


# --- FileIODimension ---

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
    hint = dim.compute_hint(data, HintContext())
    assert hint == ""


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


# --- MemoryTrendDimension ---

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
    hint = dim.compute_hint(data, HintContext())
    assert hint == ""


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


# --- BinderIPCDimension ---

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


# --- CpuThrottlingDimension ---

def test_cpu_throttling_name_and_keys():
    dim = CpuThrottlingDimension()
    assert dim.name == "cpu_throttling"
    assert "降频" in dim.metric_triggers
    assert dim.skill_name == "cpu-throttling"
    # CpuThrottling 复用 sys_stats 数据
    assert "sys_stats" in dim.metric_keys


def test_cpu_throttling_hint_throttled():
    dim = CpuThrottlingDimension()
    # 模拟 sys_stats 格式的 cpu_freq_by_core 数据
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
    hint = dim.compute_hint(data, HintContext())
    assert hint == ""


def test_cpu_throttling_hint_empty():
    dim = CpuThrottlingDimension()
    assert dim.compute_hint({}, HintContext()) == ""
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `cd /Users/liujun/langchainProjects/AppSmartInspector && uv run pytest tests/test_dimensions.py -v -k "file_io or memory_trend or binder_ipc or cpu_throttling" -x`
Expected: FAIL (ImportError)

- [ ] **Step 3: Implement FileIODimension**

```python
# src/smartinspector/collector/dimensions/file_io.py

"""File I/O latency analysis dimension."""

from smartinspector.collector.dimensions import HintContext, register_dimension
from smartinspector.collector.dimensions.base import AnalysisDimension
from smartinspector.debug_log import debug_log


@register_dimension
class FileIODimension(AnalysisDimension):
    """分析主线程文件 I/O 阻塞。"""

    @property
    def name(self) -> str:
        return "file_io"

    @property
    def description(self) -> str:
        return "文件 IO 延迟分析"

    @property
    def skill_name(self) -> str:
        return "io-analysis"

    @property
    def metric_triggers(self) -> list[str]:
        return ["文件io", "file_io", "磁盘", "disk"]

    def collect(self, tp) -> dict:
        """使用 __intrinsic_thread_state io_wait 字段。"""
        rows = tp.query("""
            SELECT
              t.name AS thread_name,
              its.blocked_function,
              COUNT(*) AS occurrences,
              SUM(its.dur) / 1e6 AS total_ms,
              MAX(its.dur) / 1e6 AS max_ms
            FROM __intrinsic_thread_state its
            JOIN thread t ON its.utid = t.utid
            WHERE its.io_wait = 1
              AND its.dur > 100000
            GROUP BY t.name, its.blocked_function
            ORDER BY total_ms DESC
            LIMIT 15
        """)

        events = []
        main_total_ms = 0.0
        for r in rows:
            evt = {
                "blocked_function": r.blocked_function,
                "thread_name": r.thread_name,
                "occurrences": r.occurrences,
                "total_ms": round(r.total_ms, 2),
                "max_ms": round(r.max_ms, 2),
            }
            events.append(evt)
            if r.thread_name == "main":
                main_total_ms += r.total_ms

        result = {
            "blocking_events": events,
            "main_thread_total_ms": round(main_total_ms, 2),
        }
        debug_log("collector", f"file_io: {len(events)} events, main={main_total_ms:.1f}ms")
        return result

    def compute_hint(self, data: dict, context: HintContext) -> str:
        events = data.get("blocking_events", [])
        if not events:
            return ""

        main_events = [e for e in events if e["thread_name"] == "main" and e["total_ms"] > 5.0]
        if not main_events:
            return ""

        lines = ["[主线程IO]"]
        for e in main_events:
            lines.append(
                f"  {e['blocked_function']}: "
                f"{e['occurrences']}次, total={e['total_ms']:.1f}ms, "
                f"max={e['max_ms']:.1f}ms"
            )
        return "\n".join(lines)

    def format_section(self, data: dict) -> str:
        events = data.get("blocking_events", [])
        if not events:
            return ""

        lines = ["## 文件 IO 阻塞分析\n"]
        main_total = data.get("main_thread_total_ms", 0.0)
        if main_total > 0:
            lines.append(f"**主线程 IO 阻塞: {main_total:.1f}ms**\n")

        lines.append("| 线程 | 阻塞函数 | 次数 | 总耗时 | 最大耗时 |")
        lines.append("|------|---------|------|--------|---------|")
        for e in events[:10]:
            lines.append(
                f"| {e['thread_name']} | {e['blocked_function']} | "
                f"{e['occurrences']} | {e['total_ms']:.1f}ms | {e['max_ms']:.1f}ms |"
            )
        return "\n".join(lines)
```

- [ ] **Step 4: Implement MemoryTrendDimension**

```python
# src/smartinspector/collector/dimensions/memory_trend.py

"""Memory growth trend analysis dimension."""

from smartinspector.collector.dimensions import HintContext, register_dimension
from smartinspector.collector.dimensions.base import AnalysisDimension
from smartinspector.debug_log import debug_log


@register_dimension
class MemoryTrendDimension(AnalysisDimension):
    """分析内存 RSS 增长趋势，检测潜在泄漏。"""

    @property
    def name(self) -> str:
        return "memory_trend"

    @property
    def description(self) -> str:
        return "内存增长趋势分析"

    @property
    def skill_name(self) -> str:
        return "memory-analysis"

    @property
    def metric_triggers(self) -> list[str]:
        return ["内存趋势", "内存泄漏", "memory_trend"]

    def collect(self, tp) -> dict:
        """查询目标进程 RSS 时序数据并计算趋势。"""
        target = self._resolve_target(tp)
        if not target:
            return {}

        rows = tp.query(f"""
            SELECT
              c.ts,
              c.value AS rss_kb
            FROM process_counter_track pct
            JOIN counter c ON c.track_id = pct.id
            JOIN process p ON pct.upid = p.upid
            WHERE pct.name = 'mem.rss'
              AND p.name = '{target}'
            ORDER BY c.ts ASC
        """)

        samples = [(r.ts, r.rss_kb) for r in rows]
        if len(samples) < 2:
            return {"process_name": target, "samples": len(samples)}

        start_rss_mb = samples[0][1] / 1024
        end_rss_mb = samples[-1][1] / 1024
        delta_mb = end_rss_mb - start_rss_mb
        delta_pct = (delta_mb / start_rss_mb * 100) if start_rss_mb > 0 else 0

        # 计算趋势斜率 (MB/s)
        duration_ns = samples[-1][0] - samples[0][0]
        duration_s = duration_ns / 1e9 if duration_ns > 0 else 1
        slope = delta_mb / duration_s

        # 检测阶段性跳跃（相邻样本增长 > 10MB）
        jumps = []
        for i in range(1, len(samples)):
            prev_mb = samples[i - 1][1] / 1024
            curr_mb = samples[i][1] / 1024
            jump = curr_mb - prev_mb
            if jump > 10:
                jumps.append({
                    "ts_ns": samples[i][0],
                    "rss_mb": round(curr_mb, 1),
                    "delta_mb": round(jump, 1),
                })

        result = {
            "process_name": target,
            "samples": len(samples),
            "start_rss_mb": round(start_rss_mb, 1),
            "end_rss_mb": round(end_rss_mb, 1),
            "delta_mb": round(delta_mb, 1),
            "delta_pct": round(delta_pct, 1),
            "trend_slope_mb_per_s": round(slope, 2),
        }
        if jumps:
            result["jumps"] = jumps

        debug_log("collector", f"memory_trend: {delta_mb:.1f}MB ({delta_pct:.1f}%), {len(jumps)} jumps")
        return result

    def _resolve_target(self, tp) -> str:
        """从 metadata 解析目标进程名。"""
        try:
            meta = tp.query("SELECT str_value FROM metadata WHERE name = 'target_package'")
            for r in meta:
                if r.str_value:
                    return r.str_value
        except Exception:
            pass
        return ""

    def compute_hint(self, data: dict, context: HintContext) -> str:
        samples = data.get("samples", 0)
        if samples < 2:
            return ""

        delta_pct = data.get("delta_pct", 0.0)
        if delta_pct < 20.0:
            return ""

        lines = ["[内存趋势]"]
        lines.append(
            f"  RSS: {data.get('start_rss_mb', 0):.1f}MB → "
            f"{data.get('end_rss_mb', 0):.1f}MB "
            f"(+{data.get('delta_mb', 0):.1f}MB, +{delta_pct:.1f}%)"
        )

        jumps = data.get("jumps", [])
        if jumps:
            lines.append(f"  检测到 {len(jumps)} 次内存跳跃（>10MB）:")
            for j in jumps[:3]:
                lines.append(f"    +{j['delta_mb']:.1f}MB → {j['rss_mb']:.1f}MB")

        if delta_pct > 50:
            lines.append("  ⚠ 内存持续大幅增长，疑似泄漏")
        elif delta_pct > 20:
            lines.append("  ⚠ 内存增长较大，建议关注")

        return "\n".join(lines)

    def format_section(self, data: dict) -> str:
        samples = data.get("samples", 0)
        if samples < 2:
            return ""

        lines = ["## 内存增长趋势\n"]
        lines.append(
            f"进程: `{data.get('process_name', '?')}`\n"
            f"样本数: {samples}\n"
            f"起始: {data.get('start_rss_mb', 0):.1f}MB → "
            f"结束: {data.get('end_rss_mb', 0):.1f}MB "
            f"(+{data.get('delta_mb', 0):.1f}MB, +{data.get('delta_pct', 0):.1f}%)\n"
            f"增长斜率: {data.get('trend_slope_mb_per_s', 0):.2f} MB/s"
        )

        jumps = data.get("jumps", [])
        if jumps:
            lines.append(f"\n**内存跳跃事件（>10MB）:**")
            for j in jumps[:5]:
                lines.append(f"- +{j['delta_mb']:.1f}MB → {j['rss_mb']:.1f}MB")

        return "\n".join(lines)
```

- [ ] **Step 5: Implement BinderIPCDimension**

```python
# src/smartinspector/collector/dimensions/binder_ipc.py

"""Binder IPC latency analysis dimension."""

from smartinspector.collector.dimensions import HintContext, register_dimension
from smartinspector.collector.dimensions.base import AnalysisDimension
from smartinspector.debug_log import debug_log


@register_dimension
class BinderIPCDimension(AnalysisDimension):
    """分析 Binder 跨进程调用延迟。"""

    @property
    def name(self) -> str:
        return "binder_ipc"

    @property
    def description(self) -> str:
        return "Binder IPC 分析"

    @property
    def skill_name(self) -> str:
        return "binder-ipc"

    @property
    def metric_triggers(self) -> list[str]:
        return ["binder", "ipc", "跨进程"]

    def collect(self, tp) -> dict:
        """查询 binder_thread_read 阻塞。"""
        rows = tp.query("""
            SELECT
              t.name AS thread_name,
              COUNT(*) AS binder_waits,
              SUM(its.dur) / 1e6 AS total_wait_ms,
              MAX(its.dur) / 1e6 AS max_wait_ms,
              AVG(its.dur) / 1e6 AS avg_wait_ms
            FROM __intrinsic_thread_state its
            JOIN thread t ON its.utid = t.utid
            WHERE its.blocked_function = 'binder_thread_read'
              AND its.dur > 100000
            GROUP BY t.name
            ORDER BY total_wait_ms DESC
            LIMIT 10
        """)

        threads = []
        for r in rows:
            threads.append({
                "thread_name": r.thread_name,
                "binder_waits": r.binder_waits,
                "total_wait_ms": round(r.total_wait_ms, 2),
                "max_wait_ms": round(r.max_wait_ms, 2),
                "avg_wait_ms": round(r.avg_wait_ms, 2),
            })

        debug_log("collector", f"binder_ipc: {len(threads)} threads")
        return {"threads": threads}

    def compute_hint(self, data: dict, context: HintContext) -> str:
        threads = data.get("threads", [])
        if not threads:
            return ""

        # 标记主线程 binder 等待 >10ms 的事件
        flagged = [t for t in threads if t["thread_name"] == "main" and t["max_wait_ms"] > 10.0]
        if not flagged:
            return ""

        lines = ["[Binder IPC]"]
        for t in flagged:
            lines.append(
                f"  main线程 binder等待: {t['binder_waits']}次, "
                f"total={t['total_wait_ms']:.1f}ms, max={t['max_wait_ms']:.1f}ms"
            )
        return "\n".join(lines)

    def format_section(self, data: dict) -> str:
        threads = data.get("threads", [])
        if not threads:
            return ""

        lines = ["## Binder IPC 分析\n"]
        lines.append("| 线程 | 等待次数 | 总等待 | 最大等待 | 平均等待 |")
        lines.append("|------|---------|--------|---------|---------|")
        for t in threads[:10]:
            lines.append(
                f"| {t['thread_name']} | {t['binder_waits']} | "
                f"{t['total_wait_ms']:.1f}ms | {t['max_wait_ms']:.1f}ms | "
                f"{t['avg_wait_ms']:.1f}ms |"
            )
        return "\n".join(lines)
```

- [ ] **Step 6: Implement CpuThrottlingDimension**

```python
# src/smartinspector/collector/dimensions/cpu_throttling.py

"""CPU thermal throttling detection dimension."""

from smartinspector.collector.dimensions import HintContext, register_dimension
from smartinspector.collector.dimensions.base import AnalysisDimension
from smartinspector.debug_log import debug_log


@register_dimension
class CpuThrottlingDimension(AnalysisDimension):
    """检测 CPU 频率降频（thermal throttling）。"""

    @property
    def name(self) -> str:
        return "cpu_throttling"

    @property
    def description(self) -> str:
        return "CPU 降频检测"

    @property
    def skill_name(self) -> str:
        return "cpu-throttling"

    @property
    def metric_triggers(self) -> list[str]:
        return ["降频", "throttling", "cpu频率"]

    @property
    def metric_keys(self) -> list[str]:
        """复用 sys_stats 数据。"""
        return ["sys_stats"]

    @property
    def perf_summary_key(self) -> str:
        """存入 dimensions dict 的 key。"""
        return "cpu_throttling"

    def collect(self, tp) -> dict:
        """查询 CPU 频率信息，检测降频。"""
        rows = tp.query("""
            SELECT
              cpu,
              MIN(value) / 1e3 AS min_mhz,
              MAX(value) / 1e3 AS max_mhz,
              AVG(value) / 1e3 AS avg_mhz,
              COUNT(*) AS samples
            FROM cpu_counter_track
            JOIN counter ON counter.track_id = cpu_counter_track.id
            GROUP BY cpu
            ORDER BY cpu
        """)

        freq_by_core = {}
        throttled_cores = []

        for r in rows:
            core_id = str(r.cpu)
            freq = {
                "min_mhz": round(r.min_mhz, 0),
                "max_mhz": round(r.max_mhz, 0),
                "avg_mhz": round(r.avg_mhz, 0),
                "samples": r.samples,
            }
            freq_by_core[core_id] = freq

            # 降频检测: 平均频率 < 最高频率的 50%
            if freq["max_mhz"] > 0:
                throttle_pct = (1 - freq["avg_mhz"] / freq["max_mhz"]) * 100
                if throttle_pct > 50:
                    throttled_cores.append({
                        "core": r.cpu,
                        "max_mhz": freq["max_mhz"],
                        "avg_mhz": freq["avg_mhz"],
                        "throttle_pct": round(throttle_pct, 1),
                    })

        result = {
            "cpu_freq_by_core": freq_by_core,
            "throttled_cores": throttled_cores,
        }
        debug_log("collector", f"cpu_throttling: {len(freq_by_core)} cores, {len(throttled_cores)} throttled")
        return result

    def compute_hint(self, data: dict, context: HintContext) -> str:
        throttled = data.get("throttled_cores", [])
        if not throttled:
            return ""

        lines = ["[CPU降频]"]
        for c in throttled:
            lines.append(
                f"  Core {c['core']}: avg={c['avg_mhz']:.0f}MHz / "
                f"max={c['max_mhz']:.0f}MHz "
                f"(降频{c['throttle_pct']:.0f}%)"
            )
        lines.append("  可能原因: thermal throttling、功耗限制")
        return "\n".join(lines)

    def format_section(self, data: dict) -> str:
        throttled = data.get("throttled_cores", [])
        if not throttled:
            return ""

        lines = ["## CPU 降频检测\n"]
        lines.append("| 核心 | 最高频率 | 平均频率 | 降频比例 |")
        lines.append("|------|---------|---------|---------|")
        for c in throttled:
            lines.append(
                f"| Core {c['core']} | {c['max_mhz']:.0f}MHz | "
                f"{c['avg_mhz']:.0f}MHz | {c['throttle_pct']:.0f}% |"
            )
        return "\n".join(lines)
```

- [ ] **Step 7: Run all P1 dimension tests**

Run: `cd /Users/liujun/langchainProjects/AppSmartInspector && uv run pytest tests/test_dimensions.py -v -k "file_io or memory_trend or binder_ipc or cpu_throttling"`
Expected: 14 passed

- [ ] **Step 8: Run all dimension tests together**

Run: `cd /Users/liujun/langchainProjects/AppSmartInspector && uv run pytest tests/test_dimensions.py -v`
Expected: All passed (registry 5 + sched_latency 6 + lock_contention 5 + gc_events 6 + P1 14 = 36)

- [ ] **Step 9: Commit**

```bash
cd /Users/liujun/langchainProjects/AppSmartInspector && git add src/smartinspector/collector/dimensions/file_io.py src/smartinspector/collector/dimensions/memory_trend.py src/smartinspector/collector/dimensions/binder_ipc.py src/smartinspector/collector/dimensions/cpu_throttling.py tests/test_dimensions.py && git commit -m "feat(collector): add P1 dimensions (file_io, memory_trend, binder_ipc, cpu_throttling)"
```

---

## Task 6: Pipeline 集成

**Files:**
- Modify: `src/smartinspector/collector/perfetto.py:1590-1726` (summarize)
- Modify: `src/smartinspector/agents/deterministic.py:255-281` (compute_hints)
- Modify: `src/smartinspector/graph/nodes/reporter/formatter.py` (format_perf_sections)
- Modify: `src/smartinspector/graph/nodes/metric_qa.py:56-77` (METRIC_DATA_MAP)

- [ ] **Step 1: Add Registry call to summarize()**

In `src/smartinspector/collector/perfetto.py`, at the end of the `summarize()` method (before `return summary`), add:

```python
        # 维度注册表分析
        try:
            from smartinspector.collector.dimensions import DimensionRegistry
            DimensionRegistry.discover()
            for dim in DimensionRegistry.all():
                try:
                    dim_data = dim.collect(tp)
                    summary.dimensions[dim.perf_summary_key] = dim_data
                    info_log("collector", f"Dimension {dim.name}: collected")
                except Exception as e:
                    info_log("collector", f"Dimension {dim.name} collect failed: {e}")
                    summary.dimensions[dim.perf_summary_key] = {"error": str(e)}
        except Exception as e:
            info_log("collector", f"DimensionRegistry discover failed: {e}")
```

Also add the import at top of file if not present:
```python
from smartinspector.debug_log import info_log, debug_log
```

- [ ] **Step 2: Add Registry call to compute_hints()**

In `src/smartinspector/agents/deterministic.py`, after the existing `sections` list (around line 279, before `return`), add:

```python
    # 维度注册表 hints
    dimensions_data = data.get("dimensions", {})
    if dimensions_data:
        try:
            from smartinspector.collector.dimensions import DimensionRegistry, HintContext
            DimensionRegistry.discover()
            ctx = HintContext(
                frame_budget_ms=frame_budget_ms,
                target_process=data.get("metadata", {}).get("target_process", {}).get("package", ""),
                trace_duration_ms=float(data.get("metadata", {}).get("trace_duration_ns", 0)) / 1e6,
            )
            for dim in DimensionRegistry.all():
                dim_data = dimensions_data.get(dim.perf_summary_key)
                if dim_data and not dim_data.get("error"):
                    hint = dim.compute_hint(dim_data, ctx)
                    if hint:
                        sections.append(hint)
        except Exception as e:
            import sys
            print(f"DimensionRegistry hints failed: {e}", file=sys.stderr)
```

- [ ] **Step 3: Add Registry call to format_perf_sections()**

In `src/smartinspector/graph/nodes/reporter/formatter.py`, at the end of `format_perf_sections()` (before `return user_parts`), add:

```python
    # 维度注册表 sections
    dimensions_data = perf_data.get("dimensions", {})
    if dimensions_data:
        try:
            from smartinspector.collector.dimensions import DimensionRegistry
            DimensionRegistry.discover()
            for dim in DimensionRegistry.all():
                dim_data = dimensions_data.get(dim.perf_summary_key)
                if dim_data and not dim_data.get("error"):
                    section = dim.format_section(dim_data)
                    if section:
                        user_parts.append(section)
        except Exception:
            pass
```

- [ ] **Step 4: Extend METRIC_DATA_MAP from Registry**

In `src/smartinspector/graph/nodes/metric_qa.py`, after the existing `METRIC_DATA_MAP` dict (after line 77), add a function to extend it:

```python
def _extend_metric_map_from_registry() -> None:
    """从 DimensionRegistry 扩展 METRIC_DATA_MAP。"""
    try:
        from smartinspector.collector.dimensions import DimensionRegistry
        DimensionRegistry.discover()
        for dim in DimensionRegistry.all():
            for trigger in dim.metric_triggers:
                METRIC_DATA_MAP[trigger] = dim.metric_keys
    except Exception:
        pass


_extend_metric_map_from_registry()
```

- [ ] **Step 5: Verify CLI still works**

Run: `cd /Users/liujun/langchainProjects/AppSmartInspector && uv run smartinspector --help`
Expected: CLI help output, no import errors

- [ ] **Step 6: Run all tests**

Run: `cd /Users/liujun/langchainProjects/AppSmartInspector && uv run pytest tests/ -v`
Expected: All tests pass

- [ ] **Step 7: Commit**

```bash
cd /Users/liujun/langchainProjects/AppSmartInspector && git add src/smartinspector/collector/perfetto.py src/smartinspector/agents/deterministic.py src/smartinspector/graph/nodes/reporter/formatter.py src/smartinspector/graph/nodes/metric_qa.py && git commit -m "feat: integrate DimensionRegistry into pipeline (summarize/hints/formatter/metric_qa)"
```

---

## Task 7: Prompt Skill 化改造

**Files:**
- Modify: `src/smartinspector/prompts.py`
- Create: `prompts/skills/SKILL.md`
- Create: `prompts/skills/dimensions/*.md` (9 files)
- Create: `prompts/skills/shared/*.md` (2 files)
- Modify: Agent files (4 files)
- Modify: 5 prompt .txt files (精简)

- [ ] **Step 1: Rewrite prompts.py with skill loading**

```python
# src/smartinspector/prompts.py

"""Load prompt instructions and dimension skill knowledge on demand."""

from pathlib import Path

_PROMPTS_DIR = Path(__file__).resolve().parent.parent.parent / "prompts"
_SKILLS_DIR = _PROMPTS_DIR / "skills"
_DIMENSIONS_DIR = _SKILLS_DIR / "dimensions"
_SHARED_DIR = _SKILLS_DIR / "shared"

_skill_cache: dict[str, str] = {}


def load_prompt(name: str) -> str:
    """Load a prompt file by name (without .txt extension).

    Args:
        name: Prompt file name, e.g. "attributor", "report-generator".

    Returns:
        The prompt text content.
    """
    path = _PROMPTS_DIR / f"{name}.txt"
    return path.read_text(encoding="utf-8")


def load_skill(name: str, category: str = "dimensions") -> str:
    """Load a skill knowledge file.

    Args:
        name: Skill file name (without .md), e.g. "gc-analysis".
        category: Subdirectory - "dimensions" (default) or "shared".

    Returns:
        Skill knowledge content. Cached after first load.
    """
    cache_key = f"{category}/{name}"
    if cache_key in _skill_cache:
        return _skill_cache[cache_key]

    base = _DIMENSIONS_DIR if category == "dimensions" else _SHARED_DIR
    path = base / f"{name}.md"
    if not path.exists():
        return ""

    content = path.read_text(encoding="utf-8")
    _skill_cache[cache_key] = content
    return content


def load_prompt_with_skills(name: str, *skill_names: str) -> str:
    """Load a prompt file and append dimension skill knowledge.

    Args:
        name: Prompt instruction file name (without .txt).
        *skill_names: Skill names to append.
            - "gc-analysis" → dimensions/gc-analysis.md
            - "shared:si-tag-system" → shared/si-tag-system.md

    Returns:
        Combined prompt text with instructions + knowledge sections.
    """
    parts = [load_prompt(name)]

    for skill_name in skill_names:
        if skill_name.startswith("shared:"):
            ref = load_skill(skill_name[7:], category="shared")
        else:
            ref = load_skill(skill_name, category="dimensions")
        if ref:
            parts.append(f"\n\n# Knowledge: {skill_name}\n\n{ref}")

    return "\n".join(parts)
```

- [ ] **Step 2: Create dimension skill files**

Create `prompts/skills/dimensions/gc-analysis.md`:
```markdown
# GC 事件分析

## 数据源
- SQL 表: `slice` (name GLOB '*GC*' OR name GLOB '*GarbageCollector*')
- 参数提取: `EXTRACT_ARG(arg_set_id, 'reason')` → gc_reason
- 参数提取: `EXTRACT_ARG(arg_set_id, 'gc_type')` → gc_type

## 领域知识
- GC 类型:
  - Concurrent: 后台并发回收，通常不阻塞主线程
  - Non-concurrent (Stop-the-World): 暂停所有线程，直接影响帧率
- 常见触发原因:
  - Alloc: 对象分配触发（堆空间不足）
  - Explicit: System.gc() 调用
  - NativeAlloc: Native 内存分配触发
- 影响主线程的 GC: "GC: Wait For Concurrent" 和 "GC: Alloc"

## 严重度标准
- P0: GC pause > 帧预算（16ms@60Hz, 8ms@120Hz）且影响主线程
- P1: GC pause > 10ms
- P2: GC pause > 1ms

## Metric 字段
- `gc_events.total_count`: GC 总次数
- `gc_events.total_pause_ms`: 总暂停时间（ms）
- `gc_events.max_pause_ms`: 最长单次暂停时间
- `gc_events.main_thread_pause_ms`: 影响主线程的 GC 总暂停时间
- `gc_events.events[].name`: GC 事件名称
- `gc_events.events[].dur_ms`: 单次持续时间
- `gc_events.events[].gc_reason`: 触发原因
- `gc_events.events[].gc_type`: GC 类型

## 与其他维度的关联
- GC ↔ 帧时间线: GC pause 可能导致 jank 帧
- GC ↔ 内存趋势: 频繁 GC 说明内存压力大

## 优化方向
- 减少 GC 频率: 避免短生命周期对象、使用对象池
- 避免 Concurrent GC pause: 减少堆大小波动
- 检查 NativeAlloc 泄漏
- 避免在主线程分配大对象
```

Create `prompts/skills/dimensions/lock-contention.md`:
```markdown
# 锁竞争分析

## 数据源
- SQL 表: `__intrinsic_thread_state` (blocked_function GLOB '*futex*')
- 需要 `sched_switch` ftrace 事件支持

## 领域知识
- futex (Fast Userspace Mutex): Linux 用户空间互斥锁
- blocked_function 常见值:
  - `futex_wait_queue_me`: 等待锁释放
  - `futex_wait`: 通用 futex 等待
  - `do_futex`: futex 系统调用处理
- 主线程锁竞争: 直接导致 ANR 和 jank

## 严重度标准
- P0: 主线程 futex 等待 max > 帧预算
- P1: 主线程 futex 等待 max > 5ms
- P2: 其他线程 futex 等待 max > 10ms

## Metric 字段
- `lock_contention.threads[].thread_name`: 线程名
- `lock_contention.threads[].futex_wait_count`: futex 等待次数
- `lock_contention.threads[].total_wait_ms`: 总等待时间
- `lock_contention.threads[].max_wait_ms`: 最大单次等待
- `lock_contention.contention_hotspots[].blocked_function`: 阻塞函数名

## 与其他维度的关联
- 锁竞争 ↔ 线程状态: futex 等待是 Sleeping 状态的子集
- 锁竞争 ↔ 帧时间线: 主线程锁等待导致 jank

## 优化方向
- 减小锁粒度: 使用细粒度锁替代全局锁
- 避免主线程持锁: 将耗时操作移到子线程
- 使用无锁数据结构: ConcurrentHashMap、原子操作
```

Create `prompts/skills/dimensions/cpu-scheduling.md`:
```markdown
# CPU 调度延迟分析

## 数据源
- Perfetto 标准库模块: `INCLUDE PERFETTO MODULE sched.runnable`
- 表: `sched_runnable` (线程从 runnable 到 running 的等待时间)

## 领域知识
- 调度延迟 (Scheduling Latency): 线程变为 runnable 到实际获得 CPU 的时间差
- 高调度延迟意味着: CPU 被其他线程抢占、核数不足、调度器负载高
- 对主线程影响: 调度延迟直接增加帧处理时间

## 严重度标准
- P0: 平均调度延迟 > 帧预算的 50%
- P1: 平均调度延迟 > 4ms
- P2: 最大调度延迟 > 8ms

## Metric 字段
- `sched_latency.threads[].thread_name`: 线程名
- `sched_latency.threads[].runnable_count`: runnable 次数
- `sched_latency.threads[].avg_runnable_ms`: 平均调度延迟
- `sched_latency.threads[].max_runnable_ms`: 最大调度延迟

## 与其他维度的关联
- 调度延迟 ↔ CPU 使用率: 高 CPU 占用可能导致调度延迟
- 调度延迟 ↔ 帧时间线: 主线程调度延迟直接增加帧耗时

## 优化方向
- 减少线程数: 降低调度器负载
- 绑核: 将关键线程绑定到大核
- 降低后台 CPU 占用: 使用 WorkManager 替代常驻线程
```

Create `prompts/skills/dimensions/io-analysis.md`:
```markdown
# 文件 IO 分析

## 数据源
- SQL 表: `__intrinsic_thread_state` (io_wait = 1)
- 字段: `blocked_function` 指示具体阻塞函数

## 领域知识
- 常见阻塞函数:
  - `folio_wait_bit_common`: 等待文件页缓存
  - `wait_on_page_bit`: 等待页面写入
  - `vfs_read/vfs_write`: 虚拟文件系统读写
- SharedPreferences: `commit()` 同步写入磁盘，应在子线程使用 `apply()`

## 严重度标准
- P0: 主线程 IO 阻塞 > 帧预算
- P1: 主线程 IO 阻塞 > 5ms
- P2: 任何线程 IO 阻塞 > 10ms

## Metric 字段
- `file_io.blocking_events[].blocked_function`: 阻塞函数
- `file_io.blocking_events[].thread_name`: 线程名
- `file_io.blocking_events[].occurrences`: 发生次数
- `file_io.blocking_events[].total_ms`: 总阻塞时间
- `file_io.main_thread_total_ms`: 主线程总 IO 阻塞

## 优化方向
- 主线程禁止同步 IO: 使用协程或子线程
- SharedPreferences: 使用 `apply()` 替代 `commit()`
- 使用 mmap: 内存映射文件减少系统调用
```

Create `prompts/skills/dimensions/memory-analysis.md`:
```markdown
# 内存分析

## 数据源
- SQL 表: `process_counter_track` + `counter` (mem.rss 时序)
- `heap_graph_*` 表: 堆对象图

## 领域知识
- RSS (Resident Set Size): 进程实际使用的物理内存
- 内存增长模式:
  - 线性增长: 可能泄漏
  - 阶梯增长: 大对象分配或 Activity 未释放
  - 锯齿形: GC 回收正常模式

## 严重度标准
- P0: RSS 增长 > 50%
- P1: RSS 增长 > 20%
- P2: RSS 增长 > 10%

## Metric 字段
- `memory_trend.process_name`: 进程名
- `memory_trend.start_rss_mb`: 起始 RSS
- `memory_trend.end_rss_mb`: 结束 RSS
- `memory_trend.delta_mb`: 增量 (MB)
- `memory_trend.delta_pct`: 增量百分比
- `memory_trend.trend_slope_mb_per_s`: 增长斜率
- `memory_trend.jumps[]`: 阶段性跳跃事件

## 优化方向
- 检测泄漏: 使用 LeakCanary 或 Android Studio Profiler
- 减少大对象: Bitmap 缓存、对象池
- 优化缓存策略: LRU 缓存限制大小
```

Create `prompts/skills/dimensions/binder-ipc.md`:
```markdown
# Binder IPC 分析

## 数据源
- SQL 表: `__intrinsic_thread_state` (blocked_function = 'binder_thread_read')

## 领域知识
- Binder: Android 跨进程通信机制
- `binder_thread_read`: 线程等待 Binder 回复
- 高延迟意味着: 服务端处理慢、序列化数据大、系统服务繁忙

## 严重度标准
- P0: 主线程 binder 等待 max > 帧预算
- P1: 主线程 binder 等待 max > 10ms
- P2: 其他线程 binder 等待 max > 50ms

## Metric 字段
- `binder_ipc.threads[].thread_name`: 线程名
- `binder_ipc.threads[].binder_waits`: Binder 等待次数
- `binder_ipc.threads[].total_wait_ms`: 总等待时间
- `binder_ipc.threads[].max_wait_ms`: 最大单次等待

## 优化方向
- 减少 IPC 调用: 批量接口、缓存结果
- 避免主线程 IPC: 使用异步 Binder
- 优化序列化: 减少 Parcelable 数据量
```

Create `prompts/skills/dimensions/cpu-throttling.md`:
```markdown
# CPU 降频检测

## 数据源
- SQL 表: `cpu_counter_track` + `counter` (CPU 频率计数器)

## 领域知识
- Thermal Throttling: CPU 过热自动降频保护
- 降频影响: 所有线程执行变慢，帧预算更难满足
- 大小核架构: 小核降频影响更明显

## 严重度标准
- P0: 平均频率 < 最高频率的 30%
- P1: 平均频率 < 最高频率的 50%
- P2: 平均频率 < 最高频率的 70%

## Metric 字段
- `cpu_throttling.cpu_freq_by_core`: 各核心频率统计
- `cpu_throttling.throttled_cores[]`: 降频核心详情

## 优化方向
- 减少 CPU 密集操作: 降低持续 CPU 负载
- 优化算法: 降低时间复杂度
- 分散计算: 将密集计算分散到多帧
```

Create `prompts/skills/dimensions/ui-jank.md`:
```markdown
# UI / 帧率分析

## 数据源
- SQL 表: `actual_frame_timeline_slice` + `expected_frame_timeline_slice`
- SI$ 标签: `SI$RV#`, `SI$view#`, `SI$inflate#`, `SI$compose#`

## 领域知识
- 帧预算: 60Hz = 16.67ms, 120Hz = 8.33ms
- Jank: 实际帧耗时超过预期帧耗时
- RecyclerView 瓶颈: `onBindViewHolder` / `onCreateViewHolder` 耗时
- Compose 重组: 非首次组合过多 = 不必要重组

## 严重度标准
- P0: 帧耗时 > 3x 帧预算 (>50ms@60Hz)
- P1: 帧耗时 > 帧预算 (>16.67ms@60Hz)
- P2: 帧耗时 > 50% 帧预算

## 与其他维度的关联
- UI ↔ 调度延迟: 线程调度延迟增加帧耗时
- UI ↔ 锁竞争: 主线程等锁导致 jank
- UI ↔ GC: GC pause 导致 jank
- UI ↔ IO: 主线程 IO 阻塞导致 jank

## 优化方向
- RecyclerView: 使用 DiffUtil、ViewHolder 缓存、预加载
- 布局优化: 减少层级、使用 ViewStub、Merge
- Compose: 避免不必要重组、remember、derivedStateOf
```

Create `prompts/skills/dimensions/startup.md`:
```markdown
# 冷启动分析

## 数据源
- SQL 表: `android_thread_slices_for_all_startups` + `slice`

## 领域知识
- 冷启动阶段:
  - pre-main: 进程创建 → Application.onCreate
  - init: Application.onCreate → Activity.onCreate
  - first_frame: Activity.onCreate → 首帧渲染
- 热启动: Activity 从后台恢复，无进程创建

## 严重度标准
- P0: 冷启动 > 5s
- P1: 冷启动 > 2s
- P2: 冷启动 > 1s

## 优化方向
- 延迟初始化: 非必要组件延迟到首帧后
- 减少 Application.onCreate 耗时
- 使用 App Startup 库
- 避免主线程 IO 和锁等待
```

- [ ] **Step 3: Create shared skill files**

Create `prompts/skills/shared/si-tag-system.md` (extract from existing prompts — content TBD based on actual prompt file content, will be filled during implementation by reading attributor.txt, perf-analyzer.txt, frame-analyzer.txt).

Create `prompts/skills/shared/search-strategy.md` (extract from attributor.txt, code-explorer.txt — content TBD, will be filled during implementation).

- [ ] **Step 4: Create SKILL.md index**

```markdown
# SmartInspector Knowledge Base

## 维度 Skills (prompts/skills/dimensions/)

| Skill | 维度 | 触发词 |
|-------|------|--------|
| cpu-scheduling | CPU 调度延迟 | 调度/runnable/sched_latency |
| lock-contention | 锁竞争 | 锁/futex/lock_contention |
| gc-analysis | GC 事件 | gc/垃圾回收/gc_events |
| io-analysis | 文件 IO | io/磁盘/文件/file_io |
| memory-analysis | 内存趋势 | 内存/泄漏/memory_trend |
| binder-ipc | Binder IPC | binder/ipc/跨进程 |
| cpu-throttling | CPU 降频 | 降频/throttling/cpu频率 |
| ui-jank | UI/帧率 | 帧率/jank/卡顿/rv/compose |
| startup | 冷启动 | 启动/cold start |

## 共享 Skills (prompts/skills/shared/)

| Skill | 描述 |
|-------|------|
| si-tag-system | SI$ 标签格式定义和解析规则 |
| search-strategy | Java/Kotlin/XML 源码搜索策略 |
```

- [ ] **Step 5: Adapt agent files to load_prompt_with_skills**

Modify `src/smartinspector/agents/attributor.py`:
- Replace `load_prompt("attributor")` with `load_prompt_with_skills("attributor", "shared:si-tag-system", "shared:search-strategy")`
- Update import from `smartinspector.prompts` to include `load_prompt_with_skills`

Modify `src/smartinspector/graph/nodes/reporter/__init__.py`:
- Replace `load_prompt("report-generator")` with `load_prompt_with_skills("report-generator")`
- Dynamic skill loading will be added when data is available

Modify `src/smartinspector/agents/perf_analyzer.py`:
- Replace `load_prompt("perf-analyzer")` with `load_prompt_with_skills("perf-analyzer")`

Modify `src/smartinspector/agents/frame_analyzer.py`:
- Replace `load_prompt("frame-analyzer")` with `load_prompt_with_skills("frame-analyzer", "ui-jank", "shared:si-tag-system")`

- [ ] **Step 6: Verify CLI still works**

Run: `cd /Users/liujun/langchainProjects/AppSmartInspector && uv run smartinspector --help`
Expected: No import errors

- [ ] **Step 7: Run all tests**

Run: `cd /Users/liujun/langchainProjects/AppSmartInspector && uv run pytest tests/ -v`
Expected: All tests pass

- [ ] **Step 8: Commit**

```bash
cd /Users/liujun/langchainProjects/AppSmartInspector && git add src/smartinspector/prompts.py prompts/skills/ src/smartinspector/agents/attributor.py src/smartinspector/graph/nodes/reporter/__init__.py src/smartinspector/agents/perf_analyzer.py src/smartinspector/agents/frame_analyzer.py && git commit -m "feat: add per-dimension skill system with knowledge files and agent adaptation"
```

---

## Task 8: 文档更新 + 验证

**Files:**
- Modify: `CLAUDE.md`

- [ ] **Step 1: Update CLAUDE.md Collector 分析方法表**

Add new dimensions to the Collector 方法表 in the Architecture section:

```
| 16 | 调度延迟 | `sched_runnable` | `SchedLatencyDimension` (Registry) | `[调度延迟]` | runnable → running 延迟 |
| 17 | 锁竞争 | `__intrinsic_thread_state` | `LockContentionDimension` (Registry) | `[锁竞争]` | futex 等待分析 |
| 18 | GC 事件 | `slice` (GC*) | `GcEventsDimension` (Registry) | `[GC分析]` | GC pause 分析 |
| 19 | 文件 IO | `__intrinsic_thread_state` | `FileIODimension` (Registry) | `[主线程IO]` | io_wait 阻塞 |
| 20 | 内存趋势 | `process_counter_track` | `MemoryTrendDimension` (Registry) | `[内存趋势]` | RSS 增长检测 |
| 21 | Binder IPC | `__intrinsic_thread_state` | `BinderIPCDimension` (Registry) | `[Binder IPC]` | binder_thread_read |
| 22 | CPU 降频 | `cpu_counter_track` | `CpuThrottlingDimension` (Registry) | `[CPU降频]` | thermal throttling |
```

- [ ] **Step 2: Update Metric QA 指标表**

Add new metric triggers:

```
| CPU | `sched_latency` | 调度延迟/runnable |
| 系统线程 | `lock_contention` | 锁/futex/锁竞争 |
| 系统线程 | `gc` | gc/垃圾回收 |
| IO | `file_io` | 文件io/磁盘 |
| 内存 | `memory_trend` | 内存趋势/内存泄漏 |
| 系统 | `binder_ipc` | binder/ipc/跨进程 |
| 系统 | `cpu_throttling` | 降频/throttling/cpu频率 |
```

- [ ] **Step 3: Add Dimension Registry section**

Add new section under Architecture:

```markdown
### Dimension Registry

新增分析维度通过 `src/smartinspector/collector/dimensions/` 包注册：

1. 创建 `dimensions/xxx.py`，继承 `AnalysisDimension`
2. 使用 `@register_dimension` 装饰器注册
3. 实现 `collect()`, `compute_hint()`, `format_section()` 方法
4. 在 `prompts/skills/dimensions/` 创建对应知识文件

Registry 自动发现所有维度模块，Pipeline 通过 `DimensionRegistry.all()` 驱动。
```

- [ ] **Step 4: Final verification**

Run: `cd /Users/liujun/langchainProjects/AppSmartInspector && uv run smartinspector --help`
Expected: No errors

Run: `cd /Users/liujun/langchainProjects/AppSmartInspector && uv run pytest tests/ -v`
Expected: All tests pass

- [ ] **Step 5: Commit**

```bash
cd /Users/liujun/langchainProjects/AppSmartInspector && git add CLAUDE.md && git commit -m "docs: update CLAUDE.md with dimension registry and new analysis capabilities"
```
