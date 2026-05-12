"""AnrMixin: ANR analysis via android.anrs stdlib module."""

import logging

from smartinspector.debug_log import debug_log

logger = logging.getLogger(__name__)


class AnrMixin:
    """Mixin providing ANR detection and analysis using Perfetto stdlib.

    Expects the host class to provide:
      - ``self._open()`` -> TraceProcessor
      - ``self._target_package`` (str | None) — target app package name
    """

    def collect_anrs(self) -> list[dict]:
        """Detect and analyze ANR events for the target process.

        Returns a list of ANR events sorted by timestamp, including
        the Top 10 most expensive main-thread slices during each ANR window.
        """
        tp = self._open()
        target_pkg = getattr(self, "_target_package", None)

        debug_log("anr", f"collect_anrs: target_package={target_pkg}")
        logger.info("Collecting ANR events for %s", target_pkg or "all processes")

        # --- Build WHERE clause for target process ---
        where_process = ""
        if target_pkg:
            where_process = (
                f"AND a.upid = ("
                f"  SELECT upid FROM process WHERE name GLOB '{target_pkg}'"
                f")"
            )

        # --- Query 1: ANR events ---
        try:
            rows = tp.query(f"""
                INCLUDE PERFETTO MODULE android.anrs;

                SELECT
                  a.process_name,
                  a.pid,
                  a.upid,
                  a.error_id,
                  a.ts,
                  a.subject,
                  a.intent,
                  a.component,
                  a.timer_delay,
                  a.anr_type,
                  a.anr_dur_ms,
                  a.default_anr_dur_ms
                FROM android_anrs a
                WHERE 1=1
                  {where_process}
                ORDER BY a.ts
            """)
        except Exception as e:
            debug_log("anr", f"main query failed: {e}")
            logger.debug("ANR main query failed: %s", e)
            return []

        anr_events: list[dict] = []
        for r in rows:
            entry = {
                "process_name": r.process_name,
                "pid": r.pid,
                "upid": r.upid,
                "error_id": r.error_id,
                "ts_ns": r.ts,
                "subject": r.subject,
                "intent": r.intent,
                "component": r.component,
                "timer_delay_ns": r.timer_delay,
                "anr_type": r.anr_type,
                "anr_dur_ms": r.anr_dur_ms,
                "default_anr_dur_ms": r.default_anr_dur_ms,
            }
            anr_events.append(entry)

        if not anr_events:
            debug_log("anr", "no ANR events found")
            return []

        debug_log("anr", f"found {len(anr_events)} ANR events")

        # --- Query 2: Top 10 main-thread slices during each ANR window ---
        # Use the ANR timestamp + anr_dur_ms to define the time window,
        # then find slices on the main thread (thread_track + utid from process).
        try:
            # Build a VALUES clause for the ANR time windows
            window_values = ", ".join(
                f"({e['ts_ns']}, {e['ts_ns'] + e['anr_dur_ms'] * 1000000}, '{e['error_id']}')"
                for e in anr_events
                if e["anr_dur_ms"] is not None and e["anr_dur_ms"] > 0
            )
            if not window_values:
                logger.info("ANR analysis complete: %d events (no valid windows for slice lookup)", len(anr_events))
                return anr_events

            upid = anr_events[0]["upid"]
            slice_rows = tp.query(f"""
                WITH anr_windows(error_ts, anr_end_ts, error_id) AS (
                  VALUES {window_values}
                ),
                main_thread AS (
                  SELECT utid
                  FROM thread
                  WHERE upid = {upid}
                    AND name = 'main'
                  LIMIT 1
                )
                SELECT
                  aw.error_id,
                  s.name AS slice_name,
                  IIF(s.dur = -1, 0, s.dur) / 1000000.0 AS slice_dur_ms,
                  s.ts AS slice_ts
                FROM anr_windows aw
                JOIN main_thread mt
                JOIN thread_track tt ON tt.utid = mt.utid
                JOIN slice s ON s.track_id = tt.id
                WHERE s.ts >= aw.error_ts
                  AND (s.ts + IIF(s.dur = -1, aw.anr_end_ts - s.ts, s.dur)) <= aw.anr_end_ts
                ORDER BY aw.error_id, s.dur DESC
            """)
        except Exception as e:
            debug_log("anr", f"slice lookup query failed: {e}")
            logger.debug("ANR main-thread slice lookup failed: %s", e)
            return anr_events

        # Group top-10 slices per ANR by error_id
        slices_by_anr: dict[str, list[dict]] = {}
        for r in slice_rows:
            slices_by_anr.setdefault(r.error_id, []).append({
                "slice_name": r.slice_name,
                "slice_dur_ms": round(r.slice_dur_ms, 3),
                "slice_ts_ns": r.slice_ts,
            })

        # Keep only top 10 per ANR
        for error_id in slices_by_anr:
            slices_by_anr[error_id] = slices_by_anr[error_id][:10]

        # Attach slices to ANR events
        for entry in anr_events:
            eid = entry["error_id"]
            if eid in slices_by_anr:
                entry["main_thread_slices"] = slices_by_anr[eid]

        logger.info("ANR analysis complete: %d events", len(anr_events))
        return anr_events
