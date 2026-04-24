"""Cold start analyzer: extract startup phases from Perfetto trace."""

import json
import logging

logger = logging.getLogger(__name__)


class StartupResult:
    """Cold start analysis result."""

    def __init__(
        self,
        total_ms: float = 0,
        phases: list[dict] | None = None,
        critical_path: list[dict] | None = None,
        bottlenecks: list[dict] | None = None,
    ) -> None:
        self.total_ms = total_ms
        self.phases = phases or []
        self.critical_path = critical_path or []
        self.bottlenecks = bottlenecks or []

    def to_dict(self) -> dict:
        return {
            "total_ms": self.total_ms,
            "phases": self.phases,
            "critical_path": self.critical_path,
            "bottlenecks": self.bottlenecks,
        }

    def to_json(self) -> str:
        return json.dumps(self.to_dict(), indent=2, ensure_ascii=False)

    def to_markdown(self) -> str:
        """Format startup analysis as markdown report."""
        lines = ["## 冷启动分析\n"]
        lines.append(f"总耗时: {self.total_ms:.0f}ms\n")
        lines.append("| 阶段 | 耗时 | 占比 |")
        lines.append("|------|------|------|")
        for phase in self.phases:
            name = phase.get("name", "?")
            dur = phase.get("dur_ms", 0)
            pct = phase.get("pct", 0)
            lines.append(f"| {name} | {dur:.0f}ms | {pct:.0f}% |")

        if self.critical_path:
            lines.append("\n### 关键路径\n")
            for item in self.critical_path[:10]:
                name = item.get("name", "?")
                dur = item.get("dur_ms", 0)
                lines.append(f"- **{name}** ({dur:.1f}ms)")

        if self.bottlenecks:
            lines.append("\n### 关键瓶颈\n")
            for bn in self.bottlenecks:
                phase = bn.get("phase", "?")
                name = bn.get("name", "?")
                dur = bn.get("dur_ms", 0)
                lines.append(f"1. **{phase} — {name}** ({dur:.0f}ms)")
                if bn.get("suggestion"):
                    lines.append(f"   - {bn['suggestion']}")

        return "\n".join(lines)


class StartupAnalyzer:
    """Analyze cold start phases from a Perfetto trace.

    Splits the startup sequence into phases:
    - pre_main: process fork → Application.attachBaseContext
    - init: Application.onCreate → first Activity.onCreate
    - first_frame: Activity.onCreate → first doFrame
    - full_draw: first doFrame → first frame rendered
    """

    def __init__(self, trace_path: str, target_process: str | None = None) -> None:
        self.trace_path = trace_path
        self.target_process = target_process

    def _open_tp(self):
        """Open trace processor."""
        from smartinspector.collector.perfetto import PerfettoCollector
        collector = PerfettoCollector(self.trace_path, target_process=self.target_process)
        return collector._open()

    def analyze(self) -> StartupResult:
        """Run the full startup analysis pipeline."""
        tp = self._open_tp()

        try:
            timestamps = self._find_startup_timestamps(tp)
        except Exception as e:
            logger.warning("Failed to find startup timestamps: %s", e)
            return StartupResult()

        if not timestamps:
            logger.info("No startup sequence detected in trace")
            return StartupResult()

        total_ms = timestamps.get("total_ms", 0)
        if total_ms <= 0:
            return StartupResult()

        phases = self._compute_phases(timestamps)
        critical_path = self._extract_critical_path(tp, timestamps)
        bottlenecks = self._identify_bottlenecks(phases, critical_path)

        return StartupResult(
            total_ms=total_ms,
            phases=phases,
            critical_path=critical_path,
            bottlenecks=bottlenecks,
        )

    def _find_startup_timestamps(self, tp) -> dict:
        """Locate key timestamps in the startup sequence.

        Looks for:
        - process_start: first appearance of the target process
        - app_oncreate: SI$Activity.onCreate or Application.onCreate slice
        - activity_oncreate: first Activity.onCreate
        - first_frame: first doFrame slice
        """
        # Resolve target process
        from smartinspector.collector.perfetto import PerfettoCollector
        collector = PerfettoCollector(self.trace_path, target_process=self.target_process)
        target_info = collector._resolve_target_process()
        if not target_info:
            return {}

        upid = target_info.get("upid")
        if not upid:
            return {}

        # Phase 1: Find process start time
        try:
            rows = tp.query(f"""
                SELECT MIN(ts) as start_ts
                FROM process_track
                WHERE upid = {upid}
            """)
            process_start = None
            for r in rows:
                if r.start_ts:
                    process_start = r.start_ts
                    break
        except Exception:
            process_start = None

        # Fallback: use thread table for process start
        if process_start is None:
            try:
                rows = tp.query(f"""
                    SELECT MIN(ts) as start_ts
                    FROM thread
                    WHERE upid = {upid}
                """)
                for r in rows:
                    if r.start_ts:
                        process_start = r.start_ts
                        break
            except Exception:
                pass

        if process_start is None:
            return {}

        # Phase 2: Find Application.onCreate / attachBaseContext
        app_oncreate_ts = None
        try:
            rows = tp.query("""
                SELECT s.ts, s.dur, s.name
                FROM slice s
                JOIN thread_track tt ON s.track_id = tt.id
                JOIN thread t ON tt.utid = t.utid
                WHERE s.name IN ('SI$Application.attachBaseContext', 'SI$Application.onCreate',
                                 'Activity.onCreate', 'performLaunchActivity')
                   OR s.name LIKE 'SI$%Application.onCreate%'
                   OR s.name LIKE 'SI$%Application.attachBaseContext%'
                ORDER BY s.ts ASC
                LIMIT 5
            """)
            for r in rows:
                if r.ts and r.ts > process_start:
                    app_oncreate_ts = r.ts
                    break
        except Exception:
            pass

        # Phase 3: Find first Activity.onCreate
        activity_oncreate_ts = None
        try:
            rows = tp.query("""
                SELECT s.ts, s.dur, s.name
                FROM slice s
                JOIN thread_track tt ON s.track_id = tt.id
                JOIN thread t ON tt.utid = t.utid
                WHERE (s.name LIKE 'SI$%Activity.onCreate'
                       OR s.name LIKE 'SI$%Activity.onStart%'
                       OR s.name = 'Activity.onCreate'
                       OR s.name = 'performLaunchActivity')
                  AND s.ts > 0
                ORDER BY s.ts ASC
                LIMIT 5
            """)
            for r in rows:
                if r.ts and r.ts > process_start:
                    activity_oncreate_ts = r.ts
                    break
        except Exception:
            pass

        # Phase 4: Find first doFrame (first frame rendered)
        first_frame_ts = None
        try:
            rows = tp.query("""
                SELECT s.ts, s.dur, s.name
                FROM slice s
                JOIN thread_track tt ON s.track_id = tt.id
                JOIN thread t ON tt.utid = t.utid
                WHERE s.name LIKE '%doFrame%'
                   OR s.name LIKE 'Choreographer#doFrame%'
                ORDER BY s.ts ASC
                LIMIT 5
            """)
            for r in rows:
                if r.ts and r.ts > process_start:
                    first_frame_ts = r.ts
                    break
        except Exception:
            pass

        # Calculate total duration
        end_ts = first_frame_ts or activity_oncreate_ts or app_oncreate_ts or process_start
        total_ns = end_ts - process_start if end_ts > process_start else 0
        total_ms = total_ns / 1_000_000

        return {
            "process_start": process_start,
            "app_oncreate": app_oncreate_ts,
            "activity_oncreate": activity_oncreate_ts,
            "first_frame": first_frame_ts,
            "total_ms": total_ms,
        }

    def _compute_phases(self, ts: dict) -> list[dict]:
        """Compute startup phases with durations and percentages."""
        process_start = ts.get("process_start", 0)
        app_oncreate = ts.get("app_oncreate")
        activity_oncreate = ts.get("activity_oncreate")
        first_frame = ts.get("first_frame")
        total_ms = ts.get("total_ms", 0)

        if total_ms <= 0:
            return []

        phases = []

        # Phase 1: pre_main (process start → app_oncreate)
        if app_oncreate and app_oncreate > process_start:
            dur_ns = app_oncreate - process_start
            dur_ms = dur_ns / 1_000_000
            phases.append({
                "name": "pre-main (进程启动)",
                "start_ns": process_start,
                "end_ns": app_oncreate,
                "dur_ms": dur_ms,
                "pct": dur_ms / total_ms * 100 if total_ms > 0 else 0,
            })

        # Phase 2: init (app_oncreate → activity_oncreate)
        init_start = app_oncreate or process_start
        if activity_oncreate and activity_oncreate > init_start:
            dur_ns = activity_oncreate - init_start
            dur_ms = dur_ns / 1_000_000
            phases.append({
                "name": "Application.onCreate",
                "start_ns": init_start,
                "end_ns": activity_oncreate,
                "dur_ms": dur_ms,
                "pct": dur_ms / total_ms * 100 if total_ms > 0 else 0,
            })

        # Phase 3: first_frame (activity_oncreate → first doFrame)
        frame_start = activity_oncreate or app_oncreate or process_start
        if first_frame and first_frame > frame_start:
            dur_ns = first_frame - frame_start
            dur_ms = dur_ns / 1_000_000
            phases.append({
                "name": "Activity.onCreate → 首帧",
                "start_ns": frame_start,
                "end_ns": first_frame,
                "dur_ms": dur_ms,
                "pct": dur_ms / total_ms * 100 if total_ms > 0 else 0,
            })

        # Phase 4: first frame render duration
        if first_frame:
            phases.append({
                "name": "首帧渲染",
                "start_ns": first_frame,
                "end_ns": first_frame,  # single point
                "dur_ms": 16.67,  # approximate one frame budget
                "pct": 16.67 / total_ms * 100 if total_ms > 0 else 0,
            })

        return phases

    def _extract_critical_path(self, tp, ts: dict) -> list[dict]:
        """Extract the longest slices on the main thread during startup.

        Identifies the critical path by finding the longest slices
        between process_start and first_frame.
        """
        process_start = ts.get("process_start", 0)
        first_frame = ts.get("first_frame")
        end_bound = first_frame or process_start + 5_000_000_000  # 5s default

        if process_start <= 0:
            return []

        try:
            rows = tp.query(f"""
                SELECT s.name, s.ts, s.dur
                FROM slice s
                JOIN thread_track tt ON s.track_id = tt.id
                JOIN thread t ON tt.utid = t.utid
                WHERE s.ts >= {process_start}
                  AND s.ts < {end_bound}
                  AND s.dur > 0
                  AND (s.name LIKE 'SI$%' OR s.name LIKE '%doFrame%')
                ORDER BY s.dur DESC
                LIMIT 20
            """)

            critical_path = []
            for r in rows:
                dur_ms = r.dur / 1_000_000 if r.dur else 0
                if dur_ms >= 1.0:
                    critical_path.append({
                        "name": r.name,
                        "ts_ns": r.ts,
                        "dur_ms": dur_ms,
                    })

            return sorted(critical_path, key=lambda x: x["ts_ns"])

        except Exception as e:
            logger.debug("Critical path extraction failed: %s", e)
            return []

    def _identify_bottlenecks(
        self,
        phases: list[dict],
        critical_path: list[dict],
    ) -> list[dict]:
        """Identify bottlenecks from phases and critical path."""
        bottlenecks = []

        for phase in phases:
            phase_name = phase.get("name", "?")
            phase_start = phase.get("start_ns", 0)
            phase_end = phase.get("end_ns", 0)
            phase_dur = phase.get("dur_ms", 0)

            # Skip short phases
            if phase_dur < 50:
                continue

            # Find slices within this phase
            phase_slices = [
                s for s in critical_path
                if phase_start <= s.get("ts_ns", 0) < phase_end
            ]

            if not phase_slices:
                continue

            # Find the slowest slice in this phase
            slowest = max(phase_slices, key=lambda x: x.get("dur_ms", 0))
            suggestion = self._suggest_optimization(slowest.get("name", ""))

            bottlenecks.append({
                "phase": phase_name,
                "name": slowest["name"],
                "dur_ms": slowest["dur_ms"],
                "phase_dur_ms": phase_dur,
                "pct_of_phase": slowest["dur_ms"] / phase_dur * 100 if phase_dur > 0 else 0,
                "suggestion": suggestion,
            })

        return sorted(bottlenecks, key=lambda x: -x.get("dur_ms", 0))

    @staticmethod
    def _suggest_optimization(slice_name: str) -> str:
        """Generate optimization suggestion based on slice type."""
        name = slice_name.lower()
        if "inflate" in name:
            return "布局优化: 考虑使用 ViewStub 延迟加载或减少布局层级"
        if "bind" in name or "adapter" in name:
            return "列表优化: 简化 ViewHolder 绑定逻辑，避免在 onBindViewHolder 中创建对象"
        if "database" in name or "db" in name or "query" in name:
            return "数据库优化: 使用异步查询或预加载，避免主线程 IO"
        if "net" in name or "http" in name or "request" in name:
            return "网络优化: 使用缓存策略或预加载关键数据"
        if "image" in name or "glide" in name or "coil" in name or "decode" in name:
            return "图片优化: 使用缩略图、WebP 格式或降低解码分辨率"
        if "init" in name or "initialize" in name or "setup" in name:
            return "延迟初始化: 考虑将非关键组件移至后台线程初始化"
        return "检查是否可异步化或延迟执行"
