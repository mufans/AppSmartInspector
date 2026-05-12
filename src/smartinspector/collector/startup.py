"""Cold start analyzer: extract startup phases from Perfetto trace."""

import json

from smartinspector.debug_log import info_log, debug_log


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

        # Overall assessment
        total = self.total_ms
        if total < 500:
            assessment = "优秀 (< 500ms)"
        elif total < 1000:
            assessment = "良好 (500-1000ms)"
        elif total < 2500:
            assessment = "一般 (1000-2500ms)"
        else:
            assessment = "较慢 (> 2500ms)"

        lines.append(f"**总耗时: {total:.0f}ms** — {assessment}\n")

        # Phase breakdown table
        if self.phases:
            lines.append("### 启动阶段\n")
            lines.append("| 阶段 | 耗时 | 占比 |")
            lines.append("|------|------|------|")
            for phase in self.phases:
                name = phase.get("name", "?")
                dur = phase.get("dur_ms", 0)
                pct = phase.get("pct", 0)
                lines.append(f"| {name} | {dur:.0f}ms | {pct:.0f}% |")

        # Critical path — top slices by duration
        if self.critical_path:
            lines.append("\n### 关键路径 (耗时最长的操作)\n")
            top_slices = sorted(self.critical_path, key=lambda x: -x.get("dur_ms", 0))[:10]
            for item in top_slices:
                name = item.get("name", "?")
                dur = item.get("dur_ms", 0)
                thread = item.get("thread_name", "")
                thread_info = f" [{thread}]" if thread else ""
                lines.append(f"- **{name}**{thread_info} — {dur:.1f}ms")

        # Bottleneck analysis with root cause and suggestions
        if self.bottlenecks:
            lines.append("\n### 瓶颈分析与优化建议\n")
            for i, bn in enumerate(self.bottlenecks, 1):
                phase = bn.get("phase", "?")
                name = bn.get("name", "?")
                dur = bn.get("dur_ms", 0)
                pct = bn.get("pct_of_phase", 0)
                thread = bn.get("thread_name", "")

                lines.append(f"{i}. **{name}**")
                if thread:
                    lines.append(f"   - 所在线程: `{thread}`")
                lines.append(f"   - 阶段: {phase}，耗时 {dur:.1f}ms" +
                             (f"（占该阶段 {pct:.0f}%）" if pct > 0 else ""))
                if bn.get("suggestion"):
                    lines.append(f"   - 建议: {bn['suggestion']}")
        elif self.critical_path:
            # Fallback: no bottleneck phases identified, but we have critical path
            lines.append("\n### 耗时分析\n")
            top_5 = sorted(self.critical_path, key=lambda x: -x.get("dur_ms", 0))[:5]
            for item in top_5:
                name = item.get("name", "?")
                dur = item.get("dur_ms", 0)
                suggestion = self._suggest_optimization_static(name)
                lines.append(f"- **{name}** — {dur:.1f}ms")
                if suggestion:
                    lines.append(f"  - 建议: {suggestion}")

        # Performance summary
        lines.append("\n### 总结\n")
        if total < 500:
            lines.append("冷启动性能优秀，无明显瓶颈。")
        elif total < 1000:
            if self.bottlenecks:
                top_bn = self.bottlenecks[0]
                lines.append(f"冷启动性能良好，主要耗时在 **{top_bn.get('name', '未知')}** "
                             f"({top_bn.get('dur_ms', 0):.0f}ms)。")
            else:
                lines.append("冷启动性能良好，建议关注耗时最长的操作。")
        elif total < 2500:
            lines.append(f"冷启动性能一般（{total:.0f}ms），建议优化上述瓶颈操作。")
        else:
            lines.append(f"冷启动较慢（{total:.0f}ms），建议重点优化耗时最长的阶段。")
            if self.bottlenecks:
                top_names = [bn["name"] for bn in self.bottlenecks[:3]]
                lines.append(f"优先优化: {', '.join(top_names)}")

        return "\n".join(lines)

    @staticmethod
    def _suggest_optimization_static(slice_name: str) -> str:
        """Generate optimization suggestion based on slice type (static version)."""
        return StartupAnalyzer._suggest_optimization(slice_name)


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
            info_log("startup", f"WARNING: Failed to find startup timestamps: {e}")
            return StartupResult()

        if not timestamps:
            info_log("startup", "No startup sequence detected in trace")
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
        target_info = collector._resolve_target_process(self.target_process)
        if not target_info:
            return {}

        upid = target_info.get("upid")
        if not upid:
            return {}

        # Phase 1: Find process start time from thread.start_ts
        try:
            rows = tp.query(f"""
                SELECT MIN(start_ts) as start_ts
                FROM thread
                WHERE upid = {upid}
            """)
            process_start = None
            for r in rows:
                if r.start_ts:
                    process_start = r.start_ts
                    break
        except Exception:
            process_start = None

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

        # Resolve target process upid for filtering
        from smartinspector.collector.perfetto import PerfettoCollector
        collector = PerfettoCollector(self.trace_path, target_process=self.target_process)
        target_info = collector._resolve_target_process(self.target_process)
        upid = target_info.get("upid") if target_info else None

        try:
            # Query slices on main thread (or any thread in target process)
            upid_filter = f"AND t.upid = {upid}" if upid else ""
            rows = tp.query(f"""
                SELECT s.name, s.ts, s.dur, t.name as thread_name
                FROM slice s
                JOIN thread_track tt ON s.track_id = tt.id
                JOIN thread t ON tt.utid = t.utid
                WHERE s.ts >= {process_start}
                  AND s.ts < {end_bound}
                  AND s.dur > 0
                  {upid_filter}
                ORDER BY s.dur DESC
                LIMIT 30
            """)

            critical_path = []
            for r in rows:
                dur_ms = r.dur / 1_000_000 if r.dur else 0
                if dur_ms >= 0.5:
                    critical_path.append({
                        "name": r.name,
                        "ts_ns": r.ts,
                        "dur_ms": dur_ms,
                        "thread_name": r.thread_name if hasattr(r, "thread_name") else "",
                    })

            return sorted(critical_path, key=lambda x: x["ts_ns"])

        except Exception as e:
            debug_log("startup", f"Critical path extraction failed: {e}")
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

            # Find slices within this phase
            phase_slices = [
                s for s in critical_path
                if phase_start <= s.get("ts_ns", 0) < phase_end
            ]

            if not phase_slices:
                continue

            # Top 3 slowest slices in this phase
            top_slices = sorted(phase_slices, key=lambda x: -x.get("dur_ms", 0))[:3]
            for s in top_slices:
                suggestion = self._suggest_optimization(s.get("name", ""))
                bottlenecks.append({
                    "phase": phase_name,
                    "name": s["name"],
                    "dur_ms": s["dur_ms"],
                    "phase_dur_ms": phase_dur,
                    "pct_of_phase": s["dur_ms"] / phase_dur * 100 if phase_dur > 0 else 0,
                    "suggestion": suggestion,
                    "thread_name": s.get("thread_name", ""),
                })

        # If no phases but have critical_path, report top slices directly
        if not phases and critical_path:
            top_slices = sorted(critical_path, key=lambda x: -x.get("dur_ms", 0))[:5]
            for s in top_slices:
                suggestion = self._suggest_optimization(s.get("name", ""))
                bottlenecks.append({
                    "phase": "启动阶段",
                    "name": s["name"],
                    "dur_ms": s["dur_ms"],
                    "phase_dur_ms": 0,
                    "pct_of_phase": 0,
                    "suggestion": suggestion,
                    "thread_name": s.get("thread_name", ""),
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


class StartupMixin:
    """Mixin providing startup analysis using Perfetto stdlib.

    Expects the host class to provide:
      - ``self._open()`` -> TraceProcessor
      - ``self._target_package`` (str | None) — target app package name
    """

    def collect_startup_metrics(self) -> list[dict]:
        """Collect startup metrics (TTID/TTFD) for the target process.

        Uses android.startup.startups and android.startup.time_to_display
        stdlib modules to detect app startups and report Time To Initial
        Display (TTID) and Time To Full Display (TTFD) metrics.

        Returns a list of startup events sorted by timestamp, or an empty
        list if no startup events are found in the trace.
        """
        tp = self._open()
        target_pkg = getattr(self, "_target_package", None)

        debug_log("startup", f"collect_startup_metrics: target_package={target_pkg}")
        logger.info("Collecting startup metrics for %s", target_pkg or "all processes")

        # --- Build WHERE clause for target process ---
        where_package = ""
        if target_pkg:
            where_package = f"AND s.package GLOB '{target_pkg}'"

        try:
            rows = tp.query(f"""
                INCLUDE PERFETTO MODULE android.startup.startups;
                INCLUDE PERFETTO MODULE android.startup.time_to_display;

                SELECT
                  s.startup_id,
                  s.ts,
                  s.dur / 1000000.0 AS startup_dur_ms,
                  s.package,
                  s.startup_type,
                  ttd.time_to_initial_display / 1000000.0 AS ttid_ms,
                  ttd.time_to_full_display / 1000000.0 AS ttfd_ms,
                  ttd.upid
                FROM android_startups s
                LEFT JOIN android_startup_time_to_display ttd
                  ON ttd.startup_id = s.startup_id
                WHERE 1=1
                  {where_package}
                ORDER BY s.ts
            """)
        except Exception as e:
            debug_log("startup", f"startup metrics query failed: {e}")
            logger.debug("Startup metrics query failed: %s", e)
            return []

        startups: list[dict] = []
        for r in rows:
            entry = {
                "startup_id": r.startup_id,
                "ts_ns": r.ts,
                "startup_dur_ms": round(r.startup_dur_ms, 3),
                "package": r.package,
                "startup_type": r.startup_type,
                "ttid_ms": round(r.ttid_ms, 3) if r.ttid_ms is not None else None,
                "ttfd_ms": round(r.ttfd_ms, 3) if r.ttfd_ms is not None else None,
                "upid": r.upid,
            }
            startups.append(entry)

        debug_log("startup", f"found {len(startups)} startup events")
        logger.info("Startup metrics complete: %d startups", len(startups))
        return startups

    def collect_startup_breakdown(self) -> list[dict]:
        """Collect startup bottleneck breakdown for the target process.

        Uses android.startup.startup_breakdowns stdlib module to get
        an opinionated breakdown of startup bottlenecks (binder, io, cpu,
        lock, etc.) for each detected startup.

        Returns a list of breakdown segments sorted by duration (descending),
        limited to the top 50 segments. Returns an empty list if no startup
        breakdown data is available in the trace.
        """
        tp = self._open()
        target_pkg = getattr(self, "_target_package", None)

        debug_log("startup", f"collect_startup_breakdown: target_package={target_pkg}")
        logger.info("Collecting startup breakdown for %s", target_pkg or "all processes")

        # --- Build WHERE clause for target process ---
        where_package = ""
        if target_pkg:
            where_package = (
                f"AND sb.startup_id IN ("
                f"  SELECT startup_id FROM android_startups"
                f"  WHERE package GLOB '{target_pkg}'"
                f")"
            )

        try:
            rows = tp.query(f"""
                INCLUDE PERFETTO MODULE android.startup.startups;
                INCLUDE PERFETTO MODULE android.startup.startup_breakdowns;

                SELECT
                  sb.startup_id,
                  sb.slice_id,
                  sb.thread_state_id,
                  sb.ts,
                  sb.dur / 1000000.0 AS segment_dur_ms,
                  sb.reason
                FROM android_startup_opinionated_breakdown sb
                WHERE sb.dur > 0
                  AND sb.dur != -1
                  {where_package}
                ORDER BY sb.dur DESC
                LIMIT 50
            """)
        except Exception as e:
            debug_log("startup", f"startup breakdown query failed: {e}")
            logger.debug("Startup breakdown query failed: %s", e)
            return []

        breakdown: list[dict] = []
        for r in rows:
            entry = {
                "startup_id": r.startup_id,
                "slice_id": r.slice_id,
                "thread_state_id": r.thread_state_id,
                "ts_ns": r.ts,
                "segment_dur_ms": round(r.segment_dur_ms, 3),
                "reason": r.reason,
            }
            breakdown.append(entry)

        debug_log("startup", f"found {len(breakdown)} breakdown segments")
        logger.info("Startup breakdown complete: %d segments", len(breakdown))
        return breakdown
