"""SliceEnhancedMixin: slice-level CPU time and thread state analysis via stdlib modules."""

import logging

from smartinspector.debug_log import debug_log

logger = logging.getLogger(__name__)


class SliceEnhancedMixin:
    """Mixin providing slice-level CPU time and thread state analysis using Perfetto stdlib.

    Expects the host class to provide:
      - ``self._open()`` -> TraceProcessor
      - ``self._target_package`` (str | None) — target app package name
    """

    def collect_slice_cpu_time(self) -> list[dict]:
        """Analyze actual CPU time for each SI$ slice (excluding wait/sleep).

        Returns a list of slices with CPU time, total duration, and CPU ratio,
        sorted by CPU time descending, limited to Top 20.
        """
        tp = self._open()
        target_pkg = getattr(self, "_target_package", None)

        debug_log("slice_enhanced", f"collect_slice_cpu_time: target_package={target_pkg}")
        logger.info("Collecting slice CPU time for %s", target_pkg or "all processes")

        where_process = ""
        if target_pkg:
            where_process = (
                f"AND tsct.upid = ("
                f"  SELECT upid FROM process WHERE name GLOB '{target_pkg}'"
                f")"
            )

        try:
            rows = tp.query(f"""
                INCLUDE PERFETTO MODULE slices.cpu_time;

                SELECT
                  tsct.id,
                  tsct.name,
                  tsct.cpu_time / 1000000.0 AS cpu_time_ms,
                  tsct.thread_name,
                  tsct.process_name,
                  s.dur / 1000000.0 AS total_dur_ms,
                  CASE
                    WHEN s.dur > 0 AND s.dur != -1
                    THEN ROUND(tsct.cpu_time * 100.0 / s.dur, 1)
                    ELSE 0
                  END AS cpu_ratio
                FROM thread_slice_cpu_time tsct
                JOIN slice s ON s.id = tsct.id
                WHERE tsct.name GLOB 'SI$*'
                  AND tsct.cpu_time > 0
                  {where_process}
                ORDER BY tsct.cpu_time DESC
                LIMIT 20
            """)
        except Exception as e:
            debug_log("slice_enhanced", f"cpu_time query failed: {e}")
            logger.debug("Slice CPU time query failed: %s", e)
            return []

        results: list[dict] = []
        for r in rows:
            results.append({
                "id": r.id,
                "slice_name": r.name,
                "cpu_time_ms": round(r.cpu_time_ms, 3),
                "total_dur_ms": round(r.total_dur_ms, 3),
                "cpu_ratio": r.cpu_ratio,
                "thread_name": r.thread_name,
                "process_name": r.process_name,
            })

        debug_log("slice_enhanced", f"found {len(results)} slices with CPU time")
        logger.info("Slice CPU time analysis complete: %d slices", len(results))
        return results

    def collect_slice_time_in_state(self) -> list[dict]:
        """Analyze thread state distribution within each SI$ slice.

        Returns a list of slices with their thread state breakdown
        (Running, Sleeping, Runnable, etc.), limited to Top 10 slices
        by total duration.
        """
        tp = self._open()
        target_pkg = getattr(self, "_target_package", None)

        debug_log("slice_enhanced", f"collect_slice_time_in_state: target_package={target_pkg}")
        logger.info("Collecting slice time-in-state for %s", target_pkg or "all processes")

        where_process = ""
        if target_pkg:
            where_process = (
                f"AND tsts.upid = ("
                f"  SELECT upid FROM process WHERE name GLOB '{target_pkg}'"
                f")"
            )

        try:
            rows = tp.query(f"""
                INCLUDE PERFETTO MODULE slices.time_in_state;
                INCLUDE PERFETTO MODULE sched.states;

                SELECT
                  tsts.id,
                  tsts.name,
                  tsts.thread_name,
                  tsts.process_name,
                  sched_state_to_human_readable_string(tsts.state) AS state_name,
                  tsts.state,
                  tsts.dur / 1000000.0 AS state_dur_ms,
                  tsts.io_wait,
                  tsts.blocked_function
                FROM thread_slice_time_in_state tsts
                WHERE tsts.name GLOB 'SI$*'
                  {where_process}
                ORDER BY tsts.dur DESC
                LIMIT 50
            """)
        except Exception as e:
            debug_log("slice_enhanced", f"time_in_state query failed: {e}")
            logger.debug("Slice time-in-state query failed: %s", e)
            return []

        # Group state entries by slice id
        slices_by_id: dict[int, dict] = {}
        for r in rows:
            sid = r.id
            if sid not in slices_by_id:
                slices_by_id[sid] = {
                    "id": sid,
                    "slice_name": r.name,
                    "thread_name": r.thread_name,
                    "process_name": r.process_name,
                    "states": [],
                }
            slices_by_id[sid]["states"].append({
                "state": r.state_name,
                "raw_state": r.state,
                "dur_ms": round(r.state_dur_ms, 3),
                "io_wait": bool(r.io_wait) if r.io_wait is not None else None,
                "blocked_function": r.blocked_function,
            })

        # Sort by max state duration per slice, take Top 10
        results = sorted(
            slices_by_id.values(),
            key=lambda s: max(st["dur_ms"] for st in s["states"]),
            reverse=True,
        )[:10]

        debug_log("slice_enhanced", f"found {len(results)} slices with time-in-state")
        logger.info("Slice time-in-state analysis complete: %d slices", len(results))
        return results
