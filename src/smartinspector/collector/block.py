"""Block event collection: SI$block# slices with SIBlock logcat stack traces."""

import bisect

from smartinspector.debug_log import debug_log
from smartinspector.collector._helpers import _parse_siblock_msg


class BlockMixin:
    """Mixin for PerfettoCollector providing block event collection."""

    def collect_block_events(self) -> list[dict]:
        """Collect block events from SI$block# slices + SIBlock logcat stacks."""
        tp = self._open()

        # 1. Query SI$block# slices
        try:
            slice_rows = tp.query("""
                SELECT name, ts, dur
                FROM slice
                WHERE name LIKE 'SI$block#%'
                ORDER BY ts ASC
            """)
        except Exception as e:
            debug_log("perfetto", f"Block events query failed: {e}")
            return []

        block_slices = []
        for r in slice_rows:
            name = r.name
            dur_ms = 0.0
            if "#" in name:
                last_hash = name.rfind("#")
                suffix = name[last_hash + 1:]
                dur_str = suffix
                if suffix.endswith("ms"):
                    dur_str = suffix[:-2]
                elif suffix.endswith("m"):
                    dur_str = suffix[:-1]
                try:
                    dur_ms = float(dur_str)
                except ValueError:
                    pass
            if dur_ms == 0 and r.dur:
                dur_ms = round(r.dur / 1e6, 2)

            block_slices.append({
                "raw_name": name,
                "ts_ns": r.ts,
                "dur_ms": dur_ms,
            })

        if not block_slices:
            return []

        # 2. Query SIBlock logcat entries for stack traces
        log_entries: list[dict] = []
        try:
            log_rows = tp.query("""
                SELECT ts, msg
                FROM android_logs
                WHERE tag = 'SIBlock'
                ORDER BY ts ASC
            """)
            for r in log_rows:
                log_entries.append({
                    "ts_ns": r.ts,
                    "msg": r.msg or "",
                })
        except Exception as e:
            debug_log("perfetto", f"SIBlock logcat query failed: {e}")

        # 3. Correlate slices with log entries by timestamp
        MATCH_WINDOW_NS = 500_000_000  # 500ms

        if log_entries:
            log_ts_list = sorted(
                [(log["ts_ns"], log) for log in log_entries],
                key=lambda x: x[0],
            )
            log_timestamps = [t for t, _ in log_ts_list]

            for block in block_slices:
                block_ts = block["ts_ns"]
                idx = bisect.bisect_left(log_timestamps, block_ts)
                best_match = None
                best_dist = MATCH_WINDOW_NS + 1

                for candidate_idx in (idx - 1, idx):
                    if 0 <= candidate_idx < len(log_ts_list):
                        dist = abs(log_ts_list[candidate_idx][0] - block_ts)
                        if dist < best_dist:
                            best_dist = dist
                            best_match = log_ts_list[candidate_idx][1]

                if best_match and best_dist <= MATCH_WINDOW_NS:
                    block["stack_trace"] = _parse_siblock_msg(best_match["msg"])
                else:
                    block["stack_trace"] = []
        else:
            for block in block_slices:
                block["stack_trace"] = []

        return block_slices
