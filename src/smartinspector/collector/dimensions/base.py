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
