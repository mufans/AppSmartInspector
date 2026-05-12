"""InputMixin: input latency breakdown analysis via android.input stdlib module."""

import logging

from smartinspector.debug_log import debug_log

logger = logging.getLogger(__name__)


class InputMixin:
    """Mixin providing input latency breakdown analysis using Perfetto stdlib.

    Expects the host class to provide:
      - ``self._open()`` -> TraceProcessor
      - ``self._target_package`` (str | None) — target app package name
    """

    def collect_input_latency(self) -> list[dict]:
        """Analyze input event latency breakdown for the target process.

        Returns a list of input events sorted by total latency (descending),
        with breakdown into dispatch, handling, and ACK phases.
        """
        tp = self._open()
        target_pkg = getattr(self, "_target_package", None)

        debug_log("input", f"collect_input_latency: target_package={target_pkg}")
        logger.info("Collecting input latency for %s", target_pkg or "all processes")

        # --- Build WHERE clause for target process ---
        where_process = ""
        if target_pkg:
            where_process = (
                f"AND ie.process_name GLOB '{target_pkg}'"
            )

        # --- Top 20 input events by total latency (skip dur=-1) ---
        try:
            rows = tp.query(f"""
                INCLUDE PERFETTO MODULE android.input;

                SELECT
                  ie.dispatch_latency_dur / 1000000.0 AS dispatch_ms,
                  ie.handling_latency_dur / 1000000.0 AS handling_ms,
                  ie.ack_latency_dur / 1000000.0 AS ack_ms,
                  ie.total_latency_dur / 1000000.0 AS total_ms,
                  ie.end_to_end_latency_dur / 1000000.0 AS e2e_ms,
                  ie.event_type,
                  ie.event_action,
                  ie.thread_name,
                  ie.process_name,
                  ie.tid,
                  ie.pid,
                  ie.event_seq,
                  ie.event_channel,
                  ie.input_event_id,
                  ie.dispatch_ts,
                  ie.receive_ts,
                  ie.frame_id
                FROM android_input_events ie
                WHERE ie.total_latency_dur != -1
                  {where_process}
                ORDER BY ie.total_latency_dur DESC
                LIMIT 20
            """)
        except Exception as e:
            debug_log("input", f"main query failed: {e}")
            logger.debug("Input latency main query failed: %s", e)
            return []

        events: list[dict] = []
        for r in rows:
            entry = {
                "dispatch_ms": round(r.dispatch_ms, 3),
                "handling_ms": round(r.handling_ms, 3),
                "ack_ms": round(r.ack_ms, 3),
                "total_ms": round(r.total_ms, 3),
                "e2e_ms": round(r.e2e_ms, 3) if r.e2e_ms is not None else None,
                "event_type": r.event_type,
                "event_action": r.event_action,
                "thread_name": r.thread_name,
                "process_name": r.process_name,
                "tid": r.tid,
                "pid": r.pid,
                "event_seq": r.event_seq,
                "event_channel": r.event_channel,
                "input_event_id": r.input_event_id,
                "dispatch_ts": r.dispatch_ts,
                "receive_ts": r.receive_ts,
                "frame_id": r.frame_id,
            }
            events.append(entry)

        debug_log("input", f"found {len(events)} input events")
        logger.info("Input latency analysis complete: %d events", len(events))
        return events
