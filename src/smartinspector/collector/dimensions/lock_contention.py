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
