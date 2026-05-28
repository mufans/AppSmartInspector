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

        if max_pause < 10.0:
            return ""

        lines = ["[GC分析]"]
        lines.append(
            f"  总暂停: {data.get('total_pause_ms', 0):.1f}ms, "
            f"最大: {max_pause:.1f}ms, "
            f"主线程影响: {main_pause:.1f}ms"
        )

        over_10ms = [e for e in events if e["dur_ms"] > 10.0]
        if over_10ms:
            lines.append("  超过10ms的GC事件:")
            for e in over_10ms[:5]:
                lines.append(
                    f"    {e['name']}: {e['dur_ms']:.1f}ms "
                    f"(reason={e['gc_reason']}, type={e['gc_type']})"
                )

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
