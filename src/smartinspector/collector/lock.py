"""LockMixin: lock contention analysis via android.monitor_contention stdlib module."""

import logging

from smartinspector.debug_log import debug_log

logger = logging.getLogger(__name__)


class LockMixin:
    """Mixin providing lock contention analysis using Perfetto stdlib.

    Expects the host class to provide:
      - ``self._open()`` -> TraceProcessor
      - ``self._target_package`` (str | None) — target app package name
    """

    def collect_lock_contention(self) -> list[dict]:
        """Analyze Java monitor contention for the target process.

        Returns a list of contention events sorted by duration (descending),
        including thread-state breakdown for the blocking thread when available.
        """
        tp = self._open()
        target_pkg = getattr(self, "_target_package", None)

        debug_log("lock", f"collect_lock_contention: target_package={target_pkg}")
        logger.info("Collecting lock contention for %s", target_pkg or "all processes")

        # --- Build WHERE clause for target process ---
        where_process = ""
        if target_pkg:
            where_process = (
                f"AND mc.upid = ("
                f"  SELECT upid FROM process WHERE name GLOB '{target_pkg}'"
                f")"
            )

        # --- Query 1: Top 20 contention events (dur > 1ms, skip dur=-1) ---
        try:
            rows = tp.query(f"""
                INCLUDE PERFETTO MODULE android.monitor_contention;

                SELECT
                  mc.id,
                  mc.ts,
                  mc.dur / 1000000.0 AS dur_ms,
                  mc.short_blocked_method,
                  mc.short_blocking_method,
                  mc.blocked_src,
                  mc.blocking_src,
                  mc.blocked_thread_name,
                  mc.blocking_thread_name,
                  mc.is_blocked_thread_main,
                  mc.is_blocking_thread_main,
                  mc.waiter_count,
                  mc.blocked_thread_tid,
                  mc.blocking_thread_tid,
                  mc.pid
                FROM android_monitor_contention mc
                WHERE mc.dur > 1000000
                  AND mc.dur != -1
                  {where_process}
                ORDER BY mc.dur DESC
                LIMIT 20
            """)
        except Exception as e:
            debug_log("lock", f"main query failed: {e}")
            logger.debug("Lock contention main query failed: %s", e)
            return []

        contentions: list[dict] = []
        contention_ids: list[int] = []
        for r in rows:
            entry = {
                "id": r.id,
                "ts_ns": r.ts,
                "dur_ms": round(r.dur_ms, 3),
                "short_blocked_method": r.short_blocked_method,
                "short_blocking_method": r.short_blocking_method,
                "blocked_src": r.blocked_src,
                "blocking_src": r.blocking_src,
                "blocked_thread": r.blocked_thread_name,
                "blocking_thread": r.blocking_thread_name,
                "is_blocked_main": bool(r.is_blocked_thread_main),
                "is_blocking_main": bool(r.is_blocking_thread_main),
                "waiter_count": r.waiter_count,
                "blocked_tid": r.blocked_thread_tid,
                "blocking_tid": r.blocking_thread_tid,
                "pid": r.pid,
            }
            contentions.append(entry)
            contention_ids.append(r.id)

        if not contentions:
            debug_log("lock", "no contention events found")
            return []

        debug_log("lock", f"found {len(contentions)} contention events")

        # --- Query 2: Thread-state breakdown for heavy contentions (>5ms) ---
        try:
            id_list = ",".join(str(i) for i in contention_ids)
            ts_rows = tp.query(f"""
                SELECT
                  mc.id,
                  mc.short_blocked_method,
                  mc.dur / 1000000.0 AS contention_dur_ms,
                  mcts.thread_state,
                  mcts.thread_state_dur / 1000000.0 AS state_dur_ms,
                  mcts.thread_state_count
                FROM android_monitor_contention mc
                JOIN android_monitor_contention_chain_thread_state_by_txn mcts
                  ON mcts.id = mc.id
                WHERE mc.id IN ({id_list})
                  AND mc.dur > 5000000
                  AND mc.dur != -1
                  {where_process}
                ORDER BY mc.dur DESC, mcts.thread_state_dur DESC
            """)

            # Group thread states by contention id
            ts_by_id: dict[int, list[dict]] = {}
            for r in ts_rows:
                ts_by_id.setdefault(r.id, []).append({
                    "state": r.thread_state,
                    "dur_ms": round(r.state_dur_ms, 3),
                    "count": r.thread_state_count,
                })

            # Attach thread-state breakdown to contentions
            for entry in contentions:
                if entry["id"] in ts_by_id:
                    entry["blocking_thread_states"] = ts_by_id[entry["id"]]
        except Exception as e:
            debug_log("lock", f"thread state breakdown query failed: {e}")
            logger.debug("Lock contention thread-state breakdown failed: %s", e)

        logger.info("Lock contention analysis complete: %d events", len(contentions))
        return contentions
