"""CpuUtilizationMixin: precise CPU utilization via linux.cpu.utilization.process/thread stdlib modules."""

import logging

from smartinspector.debug_log import debug_log

logger = logging.getLogger(__name__)


class CpuUtilizationMixin:
    """Mixin providing frequency-weighted CPU utilization analysis using Perfetto stdlib.

    Expects the host class to provide:
      - ``self._open()`` -> TraceProcessor
      - ``self._target_package`` (str | None) — target app package name
    """

    def collect_process_cpu_utilization(self) -> list[dict]:
        """Collect per-second CPU utilization and cycle stats for the target process.

        Uses ``cpu_cycles_per_process`` for aggregate cycle/frequency info and
        ``cpu_process_utilization_per_second()`` for per-second utilization.

        Returns a list of dicts with keys:
          ts, utilization, unnormalized_utilization, millicycles, megacycles,
          runtime_ms, min_freq_khz, max_freq_khz, avg_freq_khz
        """
        tp = self._open()
        target_pkg = getattr(self, "_target_package", None)

        debug_log("cpu_utilization", f"collect_process_cpu_utilization: target_package={target_pkg}")
        logger.info("Collecting process CPU utilization for %s", target_pkg or "all processes")

        if not target_pkg:
            debug_log("cpu_utilization", "no target package, skipping")
            return []

        # --- Resolve upid for the target process ---
        try:
            upid_rows = tp.query(
                f"SELECT upid FROM process WHERE name GLOB '{target_pkg}'"
            )
            upids = [r.upid for r in upid_rows]
        except Exception as e:
            debug_log("cpu_utilization", f"upid lookup failed: {e}")
            logger.debug("Process upid lookup failed: %s", e)
            return []

        if not upids:
            debug_log("cpu_utilization", "no matching process found")
            return []

        results: list[dict] = []

        for upid in upids:
            # --- Query 1: Aggregate CPU cycles for this process ---
            try:
                cycle_rows = tp.query(f"""
                    INCLUDE PERFETTO MODULE linux.cpu.utilization.process;

                    SELECT
                      cp.upid,
                      p.name AS process_name,
                      cp.millicycles,
                      cp.megacycles,
                      cp.runtime / 1000000.0 AS runtime_ms,
                      cp.min_freq,
                      cp.max_freq,
                      cp.avg_freq
                    FROM cpu_cycles_per_process cp
                    JOIN process p ON p.upid = cp.upid
                    WHERE cp.upid = {upid}
                """)
            except Exception as e:
                debug_log("cpu_utilization", f"process cycles query failed: {e}")
                logger.debug("Process CPU cycles query failed: %s", e)
                continue

            cycle_info: dict | None = None
            for r in cycle_rows:
                cycle_info = {
                    "upid": r.upid,
                    "process_name": r.process_name,
                    "millicycles": r.millicycles,
                    "megacycles": r.megacycles,
                    "runtime_ms": round(r.runtime_ms, 3),
                    "min_freq_khz": r.min_freq,
                    "max_freq_khz": r.max_freq,
                    "avg_freq_khz": r.avg_freq,
                }
                break

            # --- Query 2: Per-second utilization for this process ---
            try:
                util_rows = tp.query(f"""
                    INCLUDE PERFETTO MODULE linux.cpu.utilization.process;

                    SELECT
                      ts,
                      utilization,
                      unnormalized_utilization
                    FROM cpu_process_utilization_per_second({upid})
                    ORDER BY ts
                """)
            except Exception as e:
                debug_log("cpu_utilization", f"process utilization per second query failed: {e}")
                logger.debug("Process utilization per second query failed: %s", e)
                # Still return aggregate info if available
                if cycle_info:
                    results.append(cycle_info)
                continue

            for r in util_rows:
                entry = {
                    "ts": r.ts,
                    "utilization": round(r.utilization, 6),
                    "unnormalized_utilization": round(r.unnormalized_utilization, 6),
                    "upid": upid,
                    "process_name": cycle_info["process_name"] if cycle_info else None,
                    "millicycles": cycle_info["millicycles"] if cycle_info else None,
                    "megacycles": cycle_info["megacycles"] if cycle_info else None,
                    "runtime_ms": cycle_info["runtime_ms"] if cycle_info else None,
                    "min_freq_khz": cycle_info["min_freq_khz"] if cycle_info else None,
                    "max_freq_khz": cycle_info["max_freq_khz"] if cycle_info else None,
                    "avg_freq_khz": cycle_info["avg_freq_khz"] if cycle_info else None,
                }
                results.append(entry)

        debug_log("cpu_utilization", f"found {len(results)} process utilization data points")
        logger.info("Process CPU utilization analysis complete: %d data points", len(results))
        return results

    def collect_thread_cpu_utilization(self) -> list[dict]:
        """Collect CPU cycles and per-second utilization per thread for the target process.

        Uses ``cpu_cycles_per_thread`` for aggregate stats and
        ``cpu_thread_utilization_per_second()`` for per-second utilization.

        Returns a list of dicts sorted by megacycles (descending), with keys:
          utid, thread_name, millicycles, megacycles, runtime_ms,
          min_freq_khz, max_freq_khz, avg_freq_khz,
          per_second (optional list of {ts, utilization, unnormalized_utilization})
        """
        tp = self._open()
        target_pkg = getattr(self, "_target_package", None)

        debug_log("cpu_utilization", f"collect_thread_cpu_utilization: target_package={target_pkg}")
        logger.info("Collecting thread CPU utilization for %s", target_pkg or "all processes")

        if not target_pkg:
            debug_log("cpu_utilization", "no target package, skipping")
            return []

        # --- Thread-level CPU cycles (top 15 by megacycles) ---
        try:
            rows = tp.query(f"""
                INCLUDE PERFETTO MODULE linux.cpu.utilization.thread;

                SELECT
                  ct.utid,
                  t.name AS thread_name,
                  ct.millicycles,
                  ct.megacycles,
                  ct.runtime / 1000000.0 AS runtime_ms,
                  ct.min_freq,
                  ct.max_freq,
                  ct.avg_freq
                FROM cpu_cycles_per_thread ct
                JOIN thread t ON t.utid = ct.utid
                WHERE t.upid = (SELECT upid FROM process WHERE name GLOB '{target_pkg}')
                ORDER BY ct.megacycles DESC
                LIMIT 15
            """)
        except Exception as e:
            debug_log("cpu_utilization", f"thread cycles query failed: {e}")
            logger.debug("Thread CPU cycles query failed: %s", e)
            return []

        threads: list[dict] = []
        for r in rows:
            threads.append({
                "utid": r.utid,
                "thread_name": r.thread_name,
                "millicycles": r.millicycles,
                "megacycles": r.megacycles,
                "runtime_ms": round(r.runtime_ms, 3),
                "min_freq_khz": r.min_freq,
                "max_freq_khz": r.max_freq,
                "avg_freq_khz": r.avg_freq,
            })

        if not threads:
            debug_log("cpu_utilization", "no thread CPU data found")
            return []

        debug_log("cpu_utilization", f"found {len(threads)} threads with CPU data")

        # --- Per-second utilization for top threads ---
        for thread in threads:
            utid = thread["utid"]
            try:
                util_rows = tp.query(f"""
                    INCLUDE PERFETTO MODULE linux.cpu.utilization.thread;

                    SELECT
                      ts,
                      utilization,
                      unnormalized_utilization
                    FROM cpu_thread_utilization_per_second({utid})
                    ORDER BY ts
                """)

                per_second: list[dict] = []
                for r in util_rows:
                    per_second.append({
                        "ts": r.ts,
                        "utilization": round(r.utilization, 6),
                        "unnormalized_utilization": round(r.unnormalized_utilization, 6),
                    })

                if per_second:
                    thread["per_second"] = per_second
            except Exception as e:
                debug_log("cpu_utilization", f"thread utilization per second query failed for utid={utid}: {e}")
                logger.debug("Thread utilization per second query failed for utid=%s: %s", utid, e)

        logger.info("Thread CPU utilization analysis complete: %d threads", len(threads))
        return threads
