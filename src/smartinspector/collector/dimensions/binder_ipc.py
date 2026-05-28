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
