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
