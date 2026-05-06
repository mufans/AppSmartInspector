"""Thread state collection: Running/Sleeping/DiskSleep analysis per SI$ slice."""

from smartinspector.debug_log import debug_log
from smartinspector.collector._helpers import _map_state_label


class ThreadMixin:
    """Mixin for PerfettoCollector providing thread state analysis."""

    def collect_thread_state(self) -> list[dict]:
        """Analyze per-slice thread state distribution with blocking details."""
        tp = self._open()

        main_utid = self._resolve_main_utid(tp)
        if main_utid is None:
            return []

        try:
            slice_rows = tp.query("""
                SELECT name, ts, dur
                FROM slice
                WHERE name LIKE 'SI$%'
                  AND name NOT LIKE 'SI$net#%'
                  AND name NOT LIKE 'SI$db#%'
                  AND name NOT LIKE 'SI$img#%'
                  AND name NOT LIKE 'SI$touch#%'
                  AND dur > 1000000
                ORDER BY dur DESC
                LIMIT 20
            """)
        except Exception as e:
            debug_log("perfetto", f"thread_state: slice query failed: {e}")
            return []

        has_intrinsic_ts = self._check_intrinsic_thread_state(tp)

        if not has_intrinsic_ts:
            debug_log("perfetto", "thread_state: __intrinsic_thread_state not available, using fallback")
            return self._collect_thread_state_fallback(tp, main_utid, slice_rows)

        results = []
        for sr in slice_rows:
            slice_ts = sr.ts
            slice_end = sr.ts + sr.dur
            slice_name = sr.name
            dur_ms = round(sr.dur / 1e6, 2)

            if dur_ms < 1.0:
                continue

            try:
                state_rows = tp.query(f"""
                    SELECT
                        state,
                        SUM(dur) AS total_ns,
                        blocked_function,
                        io_wait,
                        waker_utid
                    FROM __intrinsic_thread_state
                    WHERE utid = {main_utid}
                      AND ts < {slice_end}
                      AND ts + dur > {slice_ts}
                    GROUP BY state, blocked_function, io_wait, waker_utid
                    ORDER BY total_ns DESC
                """)
            except Exception as e:
                debug_log("perfetto", f"thread_state: __intrinsic_thread_state query failed for {slice_name}: {e}")
                results.append(self._query_thread_state_legacy(tp, main_utid, slice_name, slice_ts, sr.dur, dur_ms))
                continue

            state_entries = list(state_rows)
            if not state_entries:
                results.append({
                    "slice_name": slice_name,
                    "dur_ms": dur_ms,
                    "state_distribution": {"Running": 100.0},
                    "dominant_state": "Running",
                    "blocked_function": None,
                    "io_wait": False,
                    "waker_name": None,
                })
                continue

            total_ns = sum(r.total_ns for r in state_entries)
            pct_dist: dict[str, float] = {}
            blocked_fn = None
            io_wait = False
            waker_name = None

            for r in state_entries:
                state_label = _map_state_label(r.state)
                pct = round(r.total_ns / total_ns * 100, 1)
                pct_dist[state_label] = pct_dist.get(state_label, 0) + pct

                if state_label != "Running" and blocked_fn is None:
                    blocked_fn = r.blocked_function
                    io_wait = bool(r.io_wait) if r.io_wait is not None else False
                    if r.waker_utid is not None:
                        try:
                            waker_rows = tp.query(f"""
                                SELECT name FROM thread WHERE utid = {r.waker_utid} LIMIT 1
                            """)
                            for wr in waker_rows:
                                waker_name = wr.name
                                break
                        except Exception:
                            pass

            dominant = max(pct_dist, key=pct_dist.get) if pct_dist else "unknown"
            results.append({
                "slice_name": slice_name,
                "dur_ms": dur_ms,
                "state_distribution": pct_dist,
                "dominant_state": dominant,
                "blocked_function": blocked_fn,
                "io_wait": io_wait,
                "waker_name": waker_name,
            })

        return results

    def _resolve_main_utid(self, tp) -> int | None:
        """Resolve the main thread's utid from the thread table."""
        # Strategy 1: name = 'main'
        try:
            rows = tp.query("SELECT utid FROM thread WHERE name = 'main' LIMIT 1")
            for r in rows:
                return r.utid
        except Exception as e:
            debug_log("perfetto", f"thread_state: strategy 1 (name=main) failed: {e}")

        # Strategy 2: thread named after target package
        if self._target_package:
            try:
                rows = tp.query(f"SELECT utid FROM thread WHERE name = '{self._target_package}' LIMIT 1")
                for r in rows:
                    return r.utid
            except Exception:
                pass

        # Strategy 3: lowest-tid thread in target process
        proc = self._resolve_target_process()
        upid = proc.get("upid")
        if upid is not None:
            try:
                rows = tp.query(f"""
                    SELECT t.utid FROM thread t
                    JOIN process p ON t.upid = p.upid
                    WHERE t.upid = {upid}
                    ORDER BY t.tid ASC
                    LIMIT 1
                """)
                for r in rows:
                    return r.utid
            except Exception as e:
                debug_log("perfetto", f"thread_state: strategy 3 (lowest tid) failed: {e}")

        debug_log("perfetto", "thread_state: could not resolve main thread utid")
        return None

    def _check_intrinsic_thread_state(self, tp) -> bool:
        """Check if __intrinsic_thread_state table is available."""
        try:
            tp.query("SELECT 1 FROM __intrinsic_thread_state LIMIT 1")
            return True
        except Exception:
            return False

    def _collect_thread_state_fallback(self, tp, main_utid: int, slice_rows) -> list[dict]:
        """Fallback: use legacy thread_state table."""
        results = []
        for sr in slice_rows:
            slice_ts = sr.ts
            slice_dur = sr.dur
            slice_name = sr.name
            dur_ms = round(slice_dur / 1e6, 2)

            if dur_ms < 1.0:
                continue

            result = self._query_thread_state_legacy(tp, main_utid, slice_name, slice_ts, slice_dur, dur_ms)
            results.append(result)

        return results

    def _query_thread_state_legacy(self, tp, main_utid: int, slice_name: str,
                                    slice_ts: int, slice_dur: int, dur_ms: float) -> dict:
        """Query single slice using legacy thread_state table."""
        slice_end = slice_ts + slice_dur
        try:
            state_rows = tp.query(f"""
                SELECT
                  state,
                  SUM(
                    MIN(
                      CASE WHEN dur < 0 THEN {slice_end} ELSE ts + dur END,
                      {slice_end}
                    ) -
                    MAX(ts, {slice_ts})
                  ) AS state_dur_ns
                FROM thread_state
                WHERE utid = {main_utid}
                  AND ts < {slice_end}
                  AND (dur < 0 OR ts + dur > {slice_ts})
                GROUP BY state
                ORDER BY state_dur_ns DESC
            """)

            state_dist = {}
            total_state_ns = 0
            for st in state_rows:
                ns = st.state_dur_ns or 0
                total_state_ns += ns
                state_name = _map_state_label(st.state)
                state_dist[state_name] = state_dist.get(state_name, 0) + ns

            if total_state_ns > 0:
                pct_dist = {
                    k: round(v / total_state_ns * 100, 1)
                    for k, v in state_dist.items()
                }
            else:
                pct_dist = state_dist

            dominant = max(pct_dist, key=pct_dist.get) if pct_dist else "unknown"

            return {
                "slice_name": slice_name,
                "dur_ms": dur_ms,
                "state_distribution": pct_dist,
                "dominant_state": dominant,
                "blocked_function": None,
                "io_wait": False,
                "waker_name": None,
            }
        except Exception as e:
            debug_log("perfetto", f"thread_state: legacy query failed for {slice_name}: {e}")
            return {
                "slice_name": slice_name,
                "dur_ms": dur_ms,
                "state_distribution": {},
                "dominant_state": "unknown",
                "blocked_function": None,
                "io_wait": False,
                "waker_name": None,
            }
