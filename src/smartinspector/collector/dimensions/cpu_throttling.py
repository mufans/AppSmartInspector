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
        return ["sys_stats"]

    @property
    def perf_summary_key(self) -> str:
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
