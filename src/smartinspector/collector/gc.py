"""GcMixin: garbage collection analysis via android.garbage_collection stdlib module."""

import logging

from smartinspector.debug_log import debug_log

logger = logging.getLogger(__name__)


class GcMixin:
    """Mixin providing GC event analysis using Perfetto stdlib.

    Expects the host class to provide:
      - ``self._open()`` -> TraceProcessor
      - ``self._target_package`` (str | None) — target app package name
    """

    def collect_garbage_collection(self) -> list[dict]:
        """Analyze garbage collection events for the target process.

        Returns a list of GC events sorted by wall duration (descending),
        including CPU time breakdown (running, runnable, io_wait, non_io_wait).
        """
        tp = self._open()
        target_pkg = getattr(self, "_target_package", None)

        debug_log("gc", f"collect_garbage_collection: target_package={target_pkg}")
        logger.info("Collecting GC events for %s", target_pkg or "all processes")

        # --- Build WHERE clause for target process ---
        where_process = ""
        if target_pkg:
            where_process = (
                f"AND gc.upid = ("
                f"  SELECT upid FROM process WHERE name GLOB '{target_pkg}'"
                f")"
            )

        # --- Top 20 GC events by wall duration (skip dur=-1) ---
        try:
            rows = tp.query(f"""
                INCLUDE PERFETTO MODULE android.garbage_collection;

                SELECT
                  gc.gc_ts,
                  gc.gc_dur / 1000000.0 AS gc_dur_ms,
                  gc.gc_running_dur / 1000000.0 AS running_ms,
                  gc.gc_runnable_dur / 1000000.0 AS runnable_ms,
                  gc.gc_unint_io_dur / 1000000.0 AS io_wait_ms,
                  gc.gc_unint_non_io_dur / 1000000.0 AS non_io_wait_ms,
                  gc.gc_int_dur / 1000000.0 AS int_wait_ms,
                  gc.gc_type,
                  gc.is_mark_compact,
                  gc.reclaimed_mb,
                  gc.min_heap_mb,
                  gc.max_heap_mb,
                  gc.gc_id,
                  gc.tid,
                  gc.pid,
                  gc.utid,
                  gc.upid,
                  gc.thread_name,
                  gc.process_name
                FROM android_garbage_collection_events gc
                WHERE gc.gc_dur != -1
                  {where_process}
                ORDER BY gc.gc_dur DESC
                LIMIT 20
            """)
        except Exception as e:
            debug_log("gc", f"main query failed: {e}")
            logger.debug("GC events main query failed: %s", e)
            return []

        events: list[dict] = []
        for r in rows:
            entry = {
                "gc_ts": r.gc_ts,
                "gc_dur_ms": round(r.gc_dur_ms, 3),
                "running_ms": round(r.running_ms, 3),
                "runnable_ms": round(r.runnable_ms, 3),
                "io_wait_ms": round(r.io_wait_ms, 3),
                "non_io_wait_ms": round(r.non_io_wait_ms, 3),
                "int_wait_ms": round(r.int_wait_ms, 3),
                "gc_type": r.gc_type,
                "is_mark_compact": bool(r.is_mark_compact),
                "reclaimed_mb": round(r.reclaimed_mb, 3) if r.reclaimed_mb is not None else None,
                "min_heap_mb": round(r.min_heap_mb, 3) if r.min_heap_mb is not None else None,
                "max_heap_mb": round(r.max_heap_mb, 3) if r.max_heap_mb is not None else None,
                "gc_id": r.gc_id,
                "tid": r.tid,
                "pid": r.pid,
                "utid": r.utid,
                "upid": r.upid,
                "thread_name": r.thread_name,
                "process_name": r.process_name,
            }
            events.append(entry)

        debug_log("gc", f"found {len(events)} GC events")
        logger.info("GC analysis complete: %d events", len(events))
        return events
