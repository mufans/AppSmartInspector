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
