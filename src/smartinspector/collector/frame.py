"""FrameMixin: per-frame metrics analysis via android.frames.per_frame_metrics stdlib module."""

import logging

from smartinspector.debug_log import debug_log

logger = logging.getLogger(__name__)


class FrameMixin:
    """Mixin providing per-frame metrics analysis using Perfetto stdlib.

    Expects the host class to provide:
      - ``self._open()`` -> TraceProcessor
      - ``self._target_package`` (str | None) — target app package name
    """

    def collect_frame_metrics(self) -> list[dict]:
        """Analyze per-frame metrics (overrun, cpu_time, ui_time, vsync_delay, jank).

        Uses the android.frames.per_frame_metrics stdlib module which provides
        the android_frame_stats aggregated table with overrun, cpu_time, ui_time,
        and jank classification (was_jank, was_slow_frame, was_big_jank, was_huge_jank).

        Also joins android_app_vsync_delay_per_frame for app VSYNC delay per frame.

        Returns a list of frame metric entries sorted by overrun descending,
        limited to top 30 worst frames. Each entry includes:
          - frame_id, ts, dur
          - overrun_ms, cpu_time_ms, ui_time_ms, app_vsync_delay_ms
          - was_jank, was_slow_frame, was_big_jank, was_huge_jank
          - process_name
        """
        tp = self._open()
        target_pkg = getattr(self, "_target_package", None)

        debug_log("frame", f"collect_frame_metrics: target_package={target_pkg}")
        logger.info("Collecting per-frame metrics for %s", target_pkg or "all processes")

        # --- Build WHERE clause for target process ---
        where_process = ""
        if target_pkg:
            where_process = (
                f"AND af.process_name GLOB '{target_pkg}'"
            )

        # --- Query: per-frame metrics from android_frame_stats + timeline ---
        try:
            rows = tp.query(f"""
                INCLUDE PERFETTO MODULE android.frames.per_frame_metrics;
                INCLUDE PERFETTO MODULE android.frames.timeline;

                SELECT
                  fs.frame_id,
                  af.ts,
                  IIF(af.dur = -1, 0, af.dur) / 1000000.0 AS dur_ms,
                  fs.overrun / 1000000.0 AS overrun_ms,
                  fs.cpu_time / 1000000.0 AS cpu_time_ms,
                  fs.ui_time / 1000000.0 AS ui_time_ms,
                  vsync.app_vsync_delay / 1000000.0 AS app_vsync_delay_ms,
                  fs.was_jank,
                  fs.was_slow_frame,
                  fs.was_big_jank,
                  fs.was_huge_jank,
                  af.process_name
                FROM android_frame_stats fs
                JOIN android_frames af ON af.frame_id = fs.frame_id
                LEFT JOIN android_app_vsync_delay_per_frame vsync
                  ON vsync.frame_id = fs.frame_id
                WHERE 1=1
                  {where_process}
                ORDER BY fs.overrun DESC
                LIMIT 30
            """)
        except Exception as e:
            debug_log("frame", f"per-frame metrics query failed: {e}")
            logger.debug("Per-frame metrics query failed: %s", e)
            return []

        frames: list[dict] = []
        for r in rows:
            entry = {
                "frame_id": r.frame_id,
                "ts_ns": r.ts,
                "dur_ms": round(r.dur_ms, 3) if r.dur_ms is not None else 0,
                "overrun_ms": round(r.overrun_ms, 3) if r.overrun_ms is not None else 0,
                "cpu_time_ms": round(r.cpu_time_ms, 3) if r.cpu_time_ms is not None else 0,
                "ui_time_ms": round(r.ui_time_ms, 3) if r.ui_time_ms is not None else 0,
                "app_vsync_delay_ms": round(r.app_vsync_delay_ms, 3) if r.app_vsync_delay_ms is not None else None,
                "was_jank": bool(r.was_jank) if r.was_jank is not None else False,
                "was_slow_frame": bool(r.was_slow_frame) if r.was_slow_frame is not None else False,
                "was_big_jank": bool(r.was_big_jank) if r.was_big_jank is not None else False,
                "was_huge_jank": bool(r.was_huge_jank) if r.was_huge_jank is not None else False,
                "process_name": r.process_name,
            }
            frames.append(entry)

        debug_log("frame", f"found {len(frames)} frames with metrics")
        logger.info("Per-frame metrics analysis complete: %d frames", len(frames))
        return frames
