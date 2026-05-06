"""Scheduling data collection: hot threads, blocked reasons."""

from smartinspector.debug_log import debug_log


class SchedMixin:
    """Mixin for PerfettoCollector providing scheduling-related collection."""

    def collect_sched(self) -> dict:
        """Analyze scheduling data with end_state and blocked reasons."""
        tp = self._open()
        rows = tp.query("""
            SELECT
              thread.name AS comm,
              thread.tid AS tid,
              COUNT(*) AS switches,
              SUM(sched.dur) AS total_dur_ns,
              MODE() WITHIN GROUP (ORDER BY sched.end_state) AS dominant_state
            FROM sched
            JOIN thread ON sched.utid = thread.utid
            GROUP BY thread.name, thread.tid
            ORDER BY switches DESC
            LIMIT 20
        """)
        hot_threads = []
        for r in rows:
            entry = {
                "comm": r.comm,
                "tid": r.tid,
                "switches": r.switches,
                "total_dur_ms": round(r.total_dur_ns / 1e6, 2),
                "dominant_state": r.dominant_state,
            }
            hot_threads.append(entry)

        # Blocked reasons from sched_blocked_reason table
        blocked_reasons: list[dict] = []
        try:
            br_rows = tp.query("""
                SELECT
                  t.name AS comm,
                  br.blocked_reason,
                  br.io_wait,
                  COUNT(*) AS occurrences
                FROM sched_blocked_reason br
                JOIN thread t ON br.utid = t.utid
                GROUP BY t.name, br.blocked_reason, br.io_wait
                ORDER BY occurrences DESC
                LIMIT 10
            """)
            for r in br_rows:
                blocked_reasons.append({
                    "comm": r.comm,
                    "reason": r.blocked_reason,
                    "io_wait": bool(r.io_wait),
                    "occurrences": r.occurrences,
                })
        except Exception as e:
            debug_log("perfetto", f"sched_blocked_reason query failed: {e}")

        result = {"hot_threads": hot_threads}
        if blocked_reasons:
            result["blocked_reasons"] = blocked_reasons

        return result
