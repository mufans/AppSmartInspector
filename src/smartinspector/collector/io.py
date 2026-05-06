"""IO and input event collection: SI$net#/SI$db#/SI$img# slices and SI$touch# events."""

from smartinspector.debug_log import debug_log


class IoMixin:
    """Mixin for PerfettoCollector providing IO and input event collection."""

    def collect_io_slices(self) -> dict:
        """Collect IO slices (SI$net#/SI$db#/SI$img#) from all threads."""
        tp = self._open()
        try:
            rows = tp.query("""
                SELECT name, ts, dur, depth, track_id
                FROM slice
                WHERE name LIKE 'SI$net#%'
                   OR name LIKE 'SI$db#%'
                   OR name LIKE 'SI$img#%'
                ORDER BY ts ASC
            """)
        except Exception as e:
            debug_log("perfetto", f"IO slices query failed: {e}")
            return {}

        slices = []
        name_stats: dict[str, dict] = {}
        for r in rows:
            dur_ms = round(r.dur / 1e6, 2) if r.dur else 0
            name = r.name
            slices.append({
                "name": name,
                "ts_ns": r.ts,
                "dur_ms": dur_ms,
                "depth": r.depth,
            })
            body = name[3:] if name.startswith("SI$") else name
            io_type = "unknown"
            if body.startswith("net#"):
                io_type = "network"
            elif body.startswith("db#"):
                io_type = "database"
            elif body.startswith("img#"):
                io_type = "image"

            if name not in name_stats:
                name_stats[name] = {
                    "name": name,
                    "io_type": io_type,
                    "count": 0,
                    "total_ms": 0.0,
                    "max_ms": 0.0,
                }
            name_stats[name]["count"] += 1
            name_stats[name]["total_ms"] += dur_ms
            name_stats[name]["max_ms"] = max(name_stats[name]["max_ms"], dur_ms)

        if not slices:
            return {}

        return {
            "total_count": len(slices),
            "summary": sorted(name_stats.values(), key=lambda x: -x["total_ms"]),
            "slowest": sorted(slices, key=lambda x: -x["dur_ms"])[:20],
        }

    def collect_input_events(self) -> list[dict]:
        """Collect touch input events from SI$touch# slices."""
        tp = self._open()
        try:
            rows = tp.query("""
                SELECT name, ts, dur
                FROM slice
                WHERE name LIKE 'SI$touch#%'
                ORDER BY ts ASC
            """)
        except Exception as e:
            debug_log("perfetto", f"Input events query failed: {e}")
            return []

        events = []
        for r in rows:
            name = r.name
            body = name[len("SI$touch#"):]
            parts = body.split("#")
            activity = parts[0] if len(parts) >= 1 else "?"
            action = parts[1] if len(parts) >= 2 else "UNKNOWN"
            dur_ms = round(r.dur / 1e6, 2) if r.dur else 0
            events.append({
                "ts_ns": r.ts,
                "dur_ms": dur_ms,
                "activity": activity,
                "action": action,
                "raw_name": name,
            })

        return events
