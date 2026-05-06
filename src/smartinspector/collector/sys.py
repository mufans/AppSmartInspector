"""System stats and thread info collection."""

from smartinspector.debug_log import debug_log


class SysMixin:
    """Mixin for PerfettoCollector providing system stats and thread collection."""

    def collect_sys_stats(self) -> dict:
        """Collect system-level CPU stats from linux.sys_stats data source."""
        tp = self._open()

        result: dict = {}

        # 1. System CPU idle time samples
        try:
            cpu_rows = tp.query("""
                SELECT
                  c.ts,
                  c.value AS cpu_util
                FROM counter c
                JOIN cpu_counter_track cct ON c.track_id = cct.id
                WHERE cct.name = 'cpuidle_time'
                ORDER BY c.ts ASC
            """)
            samples = [{"ts_ns": r.ts, "value": r.cpu_util} for r in cpu_rows]
            if samples:
                result["cpu_idle_samples"] = samples
        except Exception as e:
            debug_log("perfetto", f"CPU idle samples query failed: {e}")

        # 2. CPU frequency per core
        try:
            freq_rows = tp.query("""
                SELECT
                  cct.cpu,
                  c.ts,
                  c.value AS freq_khz
                FROM counter c
                JOIN cpu_counter_track cct ON c.track_id = cct.id
                WHERE cct.name = 'cpufreq'
                ORDER BY cct.cpu, c.ts ASC
            """)
            freq_by_core: dict[int, list] = {}
            for r in freq_rows:
                freq_by_core.setdefault(r.cpu, []).append({
                    "ts_ns": r.ts,
                    "freq_khz": r.freq_khz,
                })
            if freq_by_core:
                result["cpu_freq_by_core"] = freq_by_core
        except Exception as e:
            debug_log("perfetto", f"CPU frequency query failed: {e}")

        # 3. Fork rate
        try:
            fork_rows = tp.query("""
                SELECT
                  c.ts,
                  c.value AS fork_count
                FROM counter c
                JOIN cpu_counter_track cct ON c.track_id = cct.id
                WHERE cct.name = 'num_forks'
                ORDER BY c.ts ASC
            """)
            forks = [{"ts_ns": r.ts, "forks": r.fork_count} for r in fork_rows]
            if forks:
                result["fork_rate"] = forks
        except Exception as e:
            debug_log("perfetto", f"Fork rate query failed: {e}")

        return result

    def collect_threads(self) -> list[dict]:
        """Collect thread info."""
        tp = self._open()
        rows = tp.query("SELECT tid, name FROM thread ORDER BY tid")
        threads = []
        for r in rows:
            if r.name:
                threads.append({"tid": r.tid, "name": r.name})
        return threads
