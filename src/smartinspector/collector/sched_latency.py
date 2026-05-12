"""SchedLatencyMixin: scheduling latency analysis via sched.latency stdlib module."""

import logging

from smartinspector.debug_log import debug_log

logger = logging.getLogger(__name__)


class SchedLatencyMixin:
    """Mixin providing scheduling latency analysis using Perfetto stdlib.

    Analyzes the runnable→running transition latency for threads in the
    target process, highlighting threads that spend the most time waiting
    to be scheduled.

    Expects the host class to provide:
      - ``self._open()`` -> TraceProcessor
      - ``self._target_package`` (str | None) — target app package name
    """

    def collect_sched_latency(self) -> list[dict]:
        """Analyze scheduling latency for threads in the target process.

        Returns a list of per-thread latency summaries sorted by total
        wait time (descending), limited to the top 20 threads.
        """
        tp = self._open()
        target_pkg = getattr(self, "_target_package", None)

        debug_log("sched_latency", f"collect_sched_latency: target_package={target_pkg}")
        logger.info("Collecting sched latency for %s", target_pkg or "all processes")

        # --- Build WHERE clause for target process ---
        where_process = ""
        if target_pkg:
            where_process = (
                f"AND t.upid = ("
                f"  SELECT upid FROM process WHERE name GLOB '{target_pkg}'"
                f")"
            )

        # --- Per-thread scheduling latency stats, top 20 by total wait ---
        try:
            rows = tp.query(f"""
                INCLUDE PERFETTO MODULE sched.latency;

                SELECT
                  sl.utid,
                  t.name AS thread_name,
                  COUNT(*) AS wait_count,
                  SUM(sl.latency_dur) / 1000000.0 AS total_wait_ms,
                  AVG(sl.latency_dur) / 1000000.0 AS avg_wait_ms,
                  MAX(sl.latency_dur) / 1000000.0 AS max_wait_ms,
                  MIN(sl.latency_dur) / 1000000.0 AS min_wait_ms
                FROM sched_latency_for_running_interval sl
                JOIN thread t ON t.id = sl.utid
                WHERE sl.latency_dur > 0
                  {where_process}
                GROUP BY sl.utid, t.name
                ORDER BY SUM(sl.latency_dur) DESC
                LIMIT 20
            """)
        except Exception as e:
            debug_log("sched_latency", f"query failed: {e}")
            logger.debug("Sched latency query failed: %s", e)
            return []

        results: list[dict] = []
        for r in rows:
            results.append({
                "utid": r.utid,
                "thread_name": r.thread_name,
                "wait_count": r.wait_count,
                "total_wait_ms": round(r.total_wait_ms, 3),
                "avg_wait_ms": round(r.avg_wait_ms, 3),
                "max_wait_ms": round(r.max_wait_ms, 3),
                "min_wait_ms": round(r.min_wait_ms, 3),
            })

        debug_log("sched_latency", f"found {len(results)} threads with latency data")
        logger.info("Sched latency analysis complete: %d threads", len(results))
        return results
