"""SurfaceFlingerMixin: App-SurfaceFlinger frame timeline matching via android.surfaceflinger stdlib module."""

import logging

from smartinspector.debug_log import debug_log

logger = logging.getLogger(__name__)


class SurfaceFlingerMixin:
    """Mixin providing App-SurfaceFlinger frame timeline matching using Perfetto stdlib.

    Expects the host class to provide:
      - ``self._open()`` -> TraceProcessor
      - ``self._target_package`` (str | None) — target app package name
    """

    def collect_surfaceflinger_timeline(self) -> list[dict]:
        """Analyze App-SurfaceFlinger frame timeline matching for the target process.

        Returns a list of matched frame timeline entries sorted by app frame timestamp,
        including app/SF timestamps, durations, expected deadlines, and match type.
        """
        tp = self._open()
        target_pkg = getattr(self, "_target_package", None)

        debug_log("surfaceflinger", f"collect_surfaceflinger_timeline: target_package={target_pkg}")
        logger.info("Collecting SF frame timeline matching for %s", target_pkg or "all processes")

        # --- Build WHERE clause for target process ---
        where_process = ""
        if target_pkg:
            where_process = (
                f"AND m.app_upid = ("
                f"  SELECT upid FROM process WHERE name GLOB '{target_pkg}'"
                f")"
            )

        # --- Query: match app frames with SF frames, enriched with timing ---
        try:
            rows = tp.query(f"""
                INCLUDE PERFETTO MODULE android.surfaceflinger;

                SELECT
                  m.app_upid,
                  m.app_vsync AS app_vsync_id,
                  m.sf_upid,
                  m.sf_vsync AS sf_vsync_id,
                  MIN(app_a.ts) AS app_frame_ts,
                  MAX(app_a.dur) / 1000000.0 AS app_dur_ms,
                  MIN(sf_a.ts) AS sf_frame_ts,
                  MAX(sf_a.dur) / 1000000.0 AS sf_dur_ms,
                  MIN(app_e.ts) AS app_expected_ts,
                  MAX(app_e.dur) / 1000000.0 AS app_expected_dur_ms,
                  MIN(sf_e.ts) AS sf_expected_ts,
                  MAX(sf_e.dur) / 1000000.0 AS sf_expected_dur_ms
                FROM android_app_to_sf_frame_timeline_match m
                LEFT JOIN actual_frame_timeline_slice app_a
                  ON app_a.upid = m.app_upid
                  AND app_a.surface_frame_token = m.app_vsync
                LEFT JOIN actual_frame_timeline_slice sf_a
                  ON sf_a.upid = m.sf_upid
                  AND sf_a.display_frame_token = m.sf_vsync
                LEFT JOIN expected_frame_timeline_slice app_e
                  ON app_e.upid = m.app_upid
                  AND app_e.surface_frame_token = m.app_vsync
                LEFT JOIN expected_frame_timeline_slice sf_e
                  ON sf_e.upid = m.sf_upid
                  AND sf_e.display_frame_token = m.sf_vsync
                WHERE 1=1
                  {where_process}
                GROUP BY m.app_upid, m.app_vsync, m.sf_upid, m.sf_vsync
                ORDER BY app_frame_ts
                LIMIT 200
            """)
        except Exception as e:
            debug_log("surfaceflinger", f"query failed: {e}")
            logger.debug("SF frame timeline query failed: %s", e)
            return []

        results: list[dict] = []
        for r in rows:
            app_dur = r.app_dur_ms
            app_expected_dur = r.app_expected_dur_ms

            # Classify match type based on deadline comparison
            if app_dur is None or app_dur == -1:
                match_type = "unknown"
            elif app_expected_dur is not None and app_expected_dur > 0 and app_dur > app_expected_dur:
                match_type = "late"
            else:
                match_type = "on_time"

            entry = {
                "app_upid": r.app_upid,
                "app_vsync_id": r.app_vsync_id,
                "sf_upid": r.sf_upid,
                "sf_vsync_id": r.sf_vsync_id,
                "app_frame_ts": r.app_frame_ts,
                "app_dur_ms": round(app_dur, 3) if app_dur is not None and app_dur != -1 else None,
                "sf_frame_ts": r.sf_frame_ts,
                "sf_dur_ms": round(r.sf_dur_ms, 3) if r.sf_dur_ms is not None and r.sf_dur_ms != -1 else None,
                "app_expected_ts": r.app_expected_ts,
                "app_expected_dur_ms": round(app_expected_dur, 3) if app_expected_dur is not None and app_expected_dur != -1 else None,
                "sf_expected_ts": r.sf_expected_ts,
                "sf_expected_dur_ms": round(r.sf_expected_dur_ms, 3) if r.sf_expected_dur_ms is not None and r.sf_expected_dur_ms != -1 else None,
                "match_type": match_type,
            }
            results.append(entry)

        debug_log("surfaceflinger", f"found {len(results)} matched frame entries")
        logger.info("SF frame timeline analysis complete: %d entries", len(results))
        return results
