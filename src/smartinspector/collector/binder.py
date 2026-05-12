"""BinderMixin: binder transaction analysis via android.binder + android.binder_breakdown stdlib modules."""

import logging

from smartinspector.debug_log import debug_log

logger = logging.getLogger(__name__)


class BinderMixin:
    """Mixin providing binder transaction analysis using Perfetto stdlib.

    Expects the host class to provide:
      - ``self._open()`` -> TraceProcessor
      - ``self._target_package`` (str | None) — target app package name
    """

    def collect_binder_txns(self) -> list[dict]:
        """Collect top binder transactions for the target process.

        Returns a list of sync binder transactions sorted by client duration
        (descending), limited to the top 30.
        """
        tp = self._open()
        target_pkg = getattr(self, "_target_package", None)

        debug_log("binder", f"collect_binder_txns: target_package={target_pkg}")
        logger.info("Collecting binder txns for %s", target_pkg or "all processes")

        # --- Build WHERE clause for target process ---
        where_process = ""
        if target_pkg:
            where_process = (
                f"AND bt.client_upid = ("
                f"  SELECT upid FROM process WHERE name GLOB '{target_pkg}'"
                f")"
            )

        try:
            rows = tp.query(f"""
                INCLUDE PERFETTO MODULE android.binder;

                SELECT
                  bt.binder_txn_id,
                  bt.client_ts,
                  bt.client_dur / 1000000.0 AS client_dur_ms,
                  bt.server_dur / 1000000.0 AS server_dur_ms,
                  bt.aidl_name,
                  bt.method_name,
                  bt.client_process,
                  bt.client_thread,
                  bt.client_tid,
                  bt.client_pid,
                  bt.server_process,
                  bt.server_thread,
                  bt.server_tid,
                  bt.server_pid,
                  bt.is_main_thread,
                  bt.is_sync
                FROM android_binder_txns bt
                WHERE bt.is_sync = TRUE
                  AND bt.client_dur != -1
                  AND bt.client_dur > 1000000
                  {where_process}
                ORDER BY bt.client_dur DESC
                LIMIT 30
            """)
        except Exception as e:
            debug_log("binder", f"binder txns query failed: {e}")
            logger.debug("Binder txns query failed: %s", e)
            return []

        txns: list[dict] = []
        for r in rows:
            entry = {
                "binder_txn_id": r.binder_txn_id,
                "client_ts_ns": r.client_ts,
                "client_dur_ms": round(r.client_dur_ms, 3),
                "server_dur_ms": round(r.server_dur_ms, 3) if r.server_dur_ms is not None else None,
                "aidl_name": r.aidl_name,
                "method_name": r.method_name,
                "client_process": r.client_process,
                "client_thread": r.client_thread,
                "client_tid": r.client_tid,
                "client_pid": r.client_pid,
                "server_process": r.server_process,
                "server_thread": r.server_thread,
                "server_tid": r.server_tid,
                "server_pid": r.server_pid,
                "is_main_thread": bool(r.is_main_thread),
                "is_sync": bool(r.is_sync),
            }
            txns.append(entry)

        debug_log("binder", f"found {len(txns)} binder transactions")
        logger.info("Binder txn analysis complete: %d transactions", len(txns))
        return txns

    def collect_binder_breakdown(self) -> list[dict]:
        """Collect binder latency breakdown (client + server) for target process.

        Returns a list of latency segments sorted by duration (descending),
        limited to the top 50 segments longer than 1ms.
        """
        tp = self._open()
        target_pkg = getattr(self, "_target_package", None)

        debug_log("binder", f"collect_binder_breakdown: target_package={target_pkg}")
        logger.info("Collecting binder breakdown for %s", target_pkg or "all processes")

        # --- Build WHERE clause for target process ---
        where_process = ""
        if target_pkg:
            where_process = (
                f"AND bt.client_upid = ("
                f"  SELECT upid FROM process WHERE name GLOB '{target_pkg}'"
                f")"
            )

        try:
            rows = tp.query(f"""
                INCLUDE PERFETTO MODULE android.binder;
                INCLUDE PERFETTO MODULE android.binder_breakdown;

                SELECT
                  bb.binder_txn_id,
                  bb.binder_reply_id,
                  bb.ts,
                  bb.dur / 1000000.0 AS segment_dur_ms,
                  bb.server_reason,
                  bb.client_reason,
                  bb.reason,
                  bb.reason_type
                FROM android_binder_client_server_breakdown bb
                JOIN android_binder_txns bt
                  ON bt.binder_txn_id = bb.binder_txn_id
                WHERE bb.dur > 1000000
                  AND bb.dur != -1
                  {where_process}
                ORDER BY bb.dur DESC
                LIMIT 50
            """)
        except Exception as e:
            debug_log("binder", f"binder breakdown query failed: {e}")
            logger.debug("Binder breakdown query failed: %s", e)
            return []

        breakdown: list[dict] = []
        for r in rows:
            entry = {
                "binder_txn_id": r.binder_txn_id,
                "binder_reply_id": r.binder_reply_id,
                "ts_ns": r.ts,
                "segment_dur_ms": round(r.segment_dur_ms, 3),
                "server_reason": r.server_reason,
                "client_reason": r.client_reason,
                "reason": r.reason,
                "reason_type": r.reason_type,
            }
            breakdown.append(entry)

        debug_log("binder", f"found {len(breakdown)} breakdown segments")
        logger.info("Binder breakdown analysis complete: %d segments", len(breakdown))
        return breakdown
