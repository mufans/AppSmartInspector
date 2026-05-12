"""OomMixin: OOM score + RSS/Swap tracking via android.memory.process stdlib module."""

import logging

from smartinspector.debug_log import debug_log

logger = logging.getLogger(__name__)


class OomMixin:
    """Mixin providing OOM score and RSS/Swap memory analysis using Perfetto stdlib.

    Expects the host class to provide:
      - ``self._open()`` -> TraceProcessor
      - ``self._target_package`` (str | None) — target app package name
    """

    def collect_oom_rss_swap(self) -> dict:
        """Analyze OOM score transitions with RSS/Swap memory for the target process.

        Returns a dict with:
          - "oom_transitions": list of OOM score + memory snapshots sorted by time
          - "lmk_events": list of LMK kill events from the trace
        """
        tp = self._open()
        target_pkg = getattr(self, "_target_package", None)

        debug_log("oom", f"collect_oom_rss_swap: target_package={target_pkg}")
        logger.info("Collecting OOM + RSS/Swap for %s", target_pkg or "all processes")

        # --- Build WHERE clause for target process ---
        where_process = ""
        if target_pkg:
            where_process = (
                f"AND m.process_name GLOB '{target_pkg}'"
            )

        # --- Query 1: OOM score transitions with RSS/Swap ---
        oom_transitions: list[dict] = []
        try:
            rows = tp.query(f"""
                INCLUDE PERFETTO MODULE android.memory.process;

                SELECT
                  m.ts,
                  m.dur / 1000000.0 AS dur_ms,
                  m.upid,
                  m.process_name,
                  m.pid,
                  m.score AS oom_score,
                  m.bucket AS oom_bucket,
                  m.anon_rss / 1024 / 1024 AS anon_rss_mb,
                  m.file_rss / 1024 / 1024 AS file_rss_mb,
                  m.shmem_rss / 1024 / 1024 AS shmem_rss_mb,
                  m.rss / 1024 / 1024 AS rss_mb,
                  m.swap / 1024 / 1024 AS swap_mb,
                  m.anon_rss_and_swap / 1024 / 1024 AS anon_rss_swap_mb,
                  m.rss_and_swap / 1024 / 1024 AS rss_swap_mb,
                  m.oom_adj_reason,
                  m.oom_adj_trigger
                FROM memory_oom_score_with_rss_and_swap_per_process m
                WHERE 1=1
                  {where_process}
                ORDER BY m.ts
            """)

            for r in rows:
                entry = {
                    "ts": r.ts,
                    "dur_ms": round(r.dur_ms, 3) if r.dur_ms is not None else None,
                    "upid": r.upid,
                    "process_name": r.process_name,
                    "pid": r.pid,
                    "oom_score": r.oom_score,
                    "oom_bucket": r.oom_bucket,
                    "anon_rss_mb": round(r.anon_rss_mb, 3) if r.anon_rss_mb is not None else None,
                    "file_rss_mb": round(r.file_rss_mb, 3) if r.file_rss_mb is not None else None,
                    "shmem_rss_mb": round(r.shmem_rss_mb, 3) if r.shmem_rss_mb is not None else None,
                    "rss_mb": round(r.rss_mb, 3) if r.rss_mb is not None else None,
                    "swap_mb": round(r.swap_mb, 3) if r.swap_mb is not None else None,
                    "anon_rss_swap_mb": round(r.anon_rss_swap_mb, 3) if r.anon_rss_swap_mb is not None else None,
                    "rss_swap_mb": round(r.rss_swap_mb, 3) if r.rss_swap_mb is not None else None,
                    "oom_adj_reason": r.oom_adj_reason,
                    "oom_adj_trigger": r.oom_adj_trigger,
                }
                oom_transitions.append(entry)
        except Exception as e:
            debug_log("oom", f"oom transitions query failed: {e}")
            logger.debug("OOM transitions query failed: %s", e)

        debug_log("oom", f"found {len(oom_transitions)} OOM transitions")

        # --- Query 2: LMK kill events ---
        lmk_events: list[dict] = []
        try:
            lmk_rows = tp.query("""
                INCLUDE PERFETTO MODULE android.memory.lmk;

                SELECT
                  lmk.ts,
                  lmk.upid,
                  lmk.pid,
                  lmk.process_name,
                  lmk.oom_score_adj,
                  lmk.kill_reason,
                  lmk.kill_reason_raw
                FROM android_lmk_events lmk
                ORDER BY lmk.ts
            """)

            for r in lmk_rows:
                entry = {
                    "ts": r.ts,
                    "upid": r.upid,
                    "pid": r.pid,
                    "process_name": r.process_name,
                    "oom_score_adj": r.oom_score_adj,
                    "kill_reason": r.kill_reason,
                    "kill_reason_raw": r.kill_reason_raw,
                }
                lmk_events.append(entry)
        except Exception as e:
            debug_log("oom", f"lmk events query failed: {e}")
            logger.debug("LMK events query failed: %s", e)

        debug_log("oom", f"found {len(lmk_events)} LMK events")
        logger.info(
            "OOM + RSS/Swap analysis complete: %d transitions, %d LMK events",
            len(oom_transitions), len(lmk_events),
        )

        return {
            "oom_transitions": oom_transitions,
            "lmk_events": lmk_events,
        }
