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

        duration_ns = samples[-1][0] - samples[0][0]
        duration_s = duration_ns / 1e9 if duration_ns > 0 else 1
        slope = delta_mb / duration_s

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
