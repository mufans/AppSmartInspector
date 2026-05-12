"""Memory allocation analysis via Perfetto heap_graph tables."""

from smartinspector.debug_log import debug_log


def collect_heap_graph_analysis(tp, target_upid: int | None = None) -> dict:
    """Analyze Java heap memory from heap_graph tables.

    Provides object-level allocation analysis: top classes by size,
    memory growth trend, and Activity/Fragment leak suspects.

    Args:
        tp: TraceProcessor instance.
        target_upid: Target process upid. If None, queries all processes.

    Returns:
        Dict with heap_objects, memory_trend, leak_suspects.
    """
    result: dict = {}

    # Pre-check: if heap_graph_object table doesn't exist, skip all queries
    upid_filter = f"AND o.upid = {target_upid}" if target_upid else ""
    try:
        tp.query("SELECT 1 FROM heap_graph_object LIMIT 1")
    except Exception:
        return result  # No heap dump data in this trace

    # 1. Java heap object statistics — top 20 classes by total size
    try:
        rows = tp.query(f"""
            SELECT
              c.name AS class_name,
              COUNT(*) AS obj_count,
              SUM(o.self_size) AS total_bytes
            FROM heap_graph_object o
            JOIN heap_graph_class c ON o.type_id = c.id
            WHERE o.reachable = 1
              {upid_filter}
            GROUP BY c.name
            ORDER BY total_bytes DESC
            LIMIT 20
        """)
        heap_objects = []
        for r in rows:
            heap_objects.append({
                "class_name": r.class_name,
                "obj_count": r.obj_count,
                "total_size_kb": round(r.total_bytes / 1024, 1),
            })
        if heap_objects:
            result["heap_objects"] = heap_objects
    except Exception as e:
        debug_log("memory", f"Heap graph object query failed: {e}")

    # 2. Activity/Fragment leak suspects
    #    Find destroyed Activities/Fragments still reachable in the heap
    try:
        leak_rows = tp.query(f"""
            SELECT
              c.name AS class_name,
              COUNT(*) AS obj_count,
              SUM(o.self_size) AS total_bytes
            FROM heap_graph_object o
            JOIN heap_graph_class c ON o.type_id = c.id
            WHERE o.reachable = 1
              {upid_filter}
              AND (c.name LIKE '%Activity%'
                   OR c.name LIKE '%Fragment%')
            GROUP BY c.name
            ORDER BY total_bytes DESC
            LIMIT 10
        """)
        leak_suspects = []
        for r in leak_rows:
            # Filter out base classes that are expected to be alive
            name = r.class_name
            if name in (
                "android.app.Activity",
                "android.app.Fragment",
                "androidx.fragment.app.Fragment",
                "androidx.activity.ComponentActivity",
                "androidx.appcompat.app.AppCompatActivity",
                "androidx.fragment.app.FragmentActivity",
            ):
                continue
            leak_suspects.append({
                "class_name": name,
                "obj_count": r.obj_count,
                "total_size_kb": round(r.total_bytes / 1024, 1),
            })
        if leak_suspects:
            result["leak_suspects"] = leak_suspects
    except Exception as e:
        debug_log("memory", f"Leak suspect query failed: {e}")

    # 3. Dominator tree — objects that retain the most memory
    try:
        dom_rows = tp.query(f"""
            SELECT
              c.name AS class_name,
              COUNT(*) AS obj_count,
              SUM(o.self_size) AS self_bytes
            FROM heap_graph_object o
            JOIN heap_graph_class c ON o.type_id = c.id
            WHERE o.reachable = 1
              {upid_filter}
              AND o.self_size > 1024
            GROUP BY c.name
            ORDER BY self_bytes DESC
            LIMIT 15
        """)
        dominators = []
        for r in dom_rows:
            dominators.append({
                "class_name": r.class_name,
                "obj_count": r.obj_count,
                "self_size_kb": round(r.self_bytes / 1024, 1),
            })
        if dominators:
            result["dominators"] = dominators
    except Exception as e:
        debug_log("memory", f"Dominator query failed: {e}")

    # 4. Reference chain analysis for largest objects
    #    Shows what's keeping large objects alive
    try:
        ref_rows = tp.query(f"""
            SELECT
              owner_type.name AS owner_class,
              owned_type.name AS owned_class,
              ref_field.name AS field_name,
              COUNT(*) AS ref_count
            FROM heap_graph_reference ref
            JOIN heap_graph_object owner_obj ON ref.owner_id = owner_obj.id
            JOIN heap_graph_class owner_type ON owner_obj.type_id = owner_type.id
            JOIN heap_graph_object owned_obj ON ref.owned_id = owned_obj.id
            JOIN heap_graph_class owned_type ON owned_obj.type_id = owned_type.id
            LEFT JOIN heap_graph_field ref_field ON ref.field_name_id = ref_field.id
            WHERE owner_obj.reachable = 1
              {upid_filter}
              AND owned_obj.self_size > 10240
            GROUP BY owner_type.name, owned_type.name, ref_field.name
            ORDER BY ref_count DESC
            LIMIT 15
        """)
        ref_chains = []
        for r in ref_rows:
            ref_chains.append({
                "owner": r.owner_class,
                "owned": r.owned_class,
                "field": r.field_name or "<unknown>",
                "count": r.ref_count,
            })
        if ref_chains:
            result["reference_chains"] = ref_chains
    except Exception as e:
        debug_log("memory", f"Reference chain query failed: {e}")

    return result


def analyze_memory_trend(process_memory: dict) -> dict:
    """Analyze memory growth trend from process_counter_track data.

    Args:
        process_memory: Output from PerfettoCollector.collect_process_memory().

    Returns:
        Dict with growth rate and anomaly detection.
    """
    processes = process_memory.get("processes", [])
    if not processes:
        return {}

    result: dict = {"processes": []}
    for p in processes:
        name = p.get("name", "?")
        rss_kb = p.get("rss_kb", 0)
        avg_rss_kb = p.get("avg_rss_kb", 0)
        anon_kb = p.get("rss_anon_kb", 0)

        entry = {
            "name": name,
            "peak_rss_mb": round(rss_kb / 1024, 1),
            "avg_rss_mb": round(avg_rss_kb / 1024, 1),
            "anon_mb": round(anon_kb / 1024, 1),
        }

        # Detect high memory variance (peak >> avg)
        if rss_kb > 0 and avg_rss_kb > 0:
            variance_ratio = rss_kb / avg_rss_kb
            if variance_ratio > 2.0:
                entry["anomaly"] = f"Peak/Avg ratio {variance_ratio:.1f}x — possible memory spike"
            entry["variance_ratio"] = round(variance_ratio, 2)

        # Flag high anonymous memory (potential leak indicator)
        if anon_kb > 0 and rss_kb > 0:
            anon_ratio = anon_kb / rss_kb
            if anon_ratio > 0.7:
                entry["high_anon"] = f"匿名内存占比 {anon_ratio:.0%} — 可能存在内存泄漏"
            entry["anon_ratio"] = round(anon_ratio, 2)

        result["processes"].append(entry)

    return result


class HeapGraphMixin:
    """Mixin providing heap graph analysis using Perfetto stdlib.

    Expects the host class to provide:
      - ``self._open()`` -> TraceProcessor
      - ``self._target_package`` (str | None) — target app package name
    """

    def collect_heap_graph_stats(self) -> list[dict]:
        """Collect heap graph summary statistics.

        Uses ``android.memory.heap_graph.heap_graph_stats`` to get per-dump
        summary (total/reachable object counts, heap sizes, OOM score, RSS).
        """
        tp = self._open()
        target_pkg = getattr(self, "_target_package", None)

        debug_log("memory", f"collect_heap_graph_stats: target_package={target_pkg}")
        logger.info("Collecting heap graph stats for %s", target_pkg or "all processes")

        where_process = ""
        if target_pkg:
            where_process = f"AND p.name GLOB '{target_pkg}'"

        stats: list[dict] = []
        try:
            rows = tp.query(f"""
                INCLUDE PERFETTO MODULE android.memory.heap_graph.heap_graph_stats;

                SELECT
                  s.upid,
                  p.name AS process_name,
                  s.graph_sample_ts,
                  s.total_heap_size,
                  s.total_native_alloc_registry_size,
                  s.total_obj_count,
                  s.reachable_heap_size,
                  s.reachable_native_alloc_registry_size,
                  s.reachable_obj_count,
                  s.oom_score_adj,
                  s.anon_rss_and_swap_size,
                  s.dmabuf_rss_size
                FROM android_heap_graph_stats s
                JOIN process p ON s.upid = p.upid
                WHERE 1=1
                  {where_process}
                ORDER BY s.graph_sample_ts
            """)

            for r in rows:
                entry = {
                    "upid": r.upid,
                    "process_name": r.process_name,
                    "graph_sample_ts": r.graph_sample_ts,
                    "total_heap_size": r.total_heap_size,
                    "total_native_alloc_registry_size": r.total_native_alloc_registry_size,
                    "total_obj_count": r.total_obj_count,
                    "reachable_heap_size": r.reachable_heap_size,
                    "reachable_native_alloc_registry_size": r.reachable_native_alloc_registry_size,
                    "reachable_obj_count": r.reachable_obj_count,
                    "oom_score_adj": r.oom_score_adj,
                    "anon_rss_and_swap_size": r.anon_rss_and_swap_size,
                    "dmabuf_rss_size": r.dmabuf_rss_size,
                }
                stats.append(entry)
        except Exception as e:
            debug_log("memory", f"heap_graph_stats query failed: {e}")
            logger.debug("Heap graph stats query failed: %s", e)

        debug_log("memory", f"found {len(stats)} heap graph stats entries")
        logger.info("Heap graph stats: %d entries", len(stats))
        return stats

    def collect_heap_class_aggregation(self) -> list[dict]:
        """Collect per-class heap memory aggregation (Top 20 by total size).

        Uses ``android.memory.heap_graph.heap_graph_class_aggregation`` to get
        class-level breakdown with object counts, sizes, and dominator stats.
        """
        tp = self._open()
        target_pkg = getattr(self, "_target_package", None)

        debug_log("memory", f"collect_heap_class_aggregation: target_package={target_pkg}")
        logger.info("Collecting heap class aggregation for %s", target_pkg or "all processes")

        where_process = ""
        if target_pkg:
            where_process = f"AND p.name GLOB '{target_pkg}'"

        aggregation: list[dict] = []
        try:
            rows = tp.query(f"""
                INCLUDE PERFETTO MODULE android.memory.heap_graph.heap_graph_class_aggregation;

                SELECT
                  a.upid,
                  p.name AS process_name,
                  a.graph_sample_ts,
                  a.type_name,
                  a.is_libcore_or_array,
                  a.obj_count,
                  a.size_bytes,
                  a.native_size_bytes,
                  a.reachable_obj_count,
                  a.reachable_size_bytes,
                  a.reachable_native_size_bytes,
                  a.dominated_obj_count,
                  a.dominated_size_bytes,
                  a.dominated_native_size_bytes
                FROM android_heap_graph_class_aggregation a
                JOIN process p ON a.upid = p.upid
                WHERE 1=1
                  {where_process}
                ORDER BY a.size_bytes DESC
                LIMIT 20
            """)

            for r in rows:
                entry = {
                    "upid": r.upid,
                    "process_name": r.process_name,
                    "graph_sample_ts": r.graph_sample_ts,
                    "type_name": r.type_name,
                    "is_libcore_or_array": bool(r.is_libcore_or_array),
                    "obj_count": r.obj_count,
                    "size_bytes": r.size_bytes,
                    "native_size_bytes": r.native_size_bytes,
                    "reachable_obj_count": r.reachable_obj_count,
                    "reachable_size_bytes": r.reachable_size_bytes,
                    "reachable_native_size_bytes": r.reachable_native_size_bytes,
                    "dominated_obj_count": r.dominated_obj_count,
                    "dominated_size_bytes": r.dominated_size_bytes,
                    "dominated_native_size_bytes": r.dominated_native_size_bytes,
                }
                aggregation.append(entry)
        except Exception as e:
            debug_log("memory", f"heap_class_aggregation query failed: {e}")
            logger.debug("Heap class aggregation query failed: %s", e)

        debug_log("memory", f"found {len(aggregation)} class aggregation entries")
        logger.info("Heap class aggregation: %d entries (Top 20)", len(aggregation))
        return aggregation

    def collect_heap_dominator_tree(self) -> list[dict]:
        """Collect heap dominator tree entries (largest retained size).

        Uses ``android.memory.heap_graph.dominator_tree`` to get reachable
        objects with their immediate dominators and dominated set summaries.
        """
        tp = self._open()
        target_pkg = getattr(self, "_target_package", None)

        debug_log("memory", f"collect_heap_dominator_tree: target_package={target_pkg}")
        logger.info("Collecting heap dominator tree for %s", target_pkg or "all processes")

        # Build process filter from target_package using heap_graph_object
        where_process = ""
        if target_pkg:
            where_process = f"AND p.name GLOB '{target_pkg}'"

        dominator_tree: list[dict] = []
        try:
            rows = tp.query(f"""
                INCLUDE PERFETTO MODULE android.memory.heap_graph.dominator_tree;

                SELECT
                  dt.id,
                  dt.idom_id,
                  dt.dominated_obj_count,
                  dt.dominated_size_bytes,
                  dt.dominated_native_size_bytes,
                  dt.depth,
                  hgo.self_size,
                  hgc.name AS class_name,
                  p.name AS process_name
                FROM heap_graph_dominator_tree dt
                JOIN heap_graph_object hgo ON dt.id = hgo.id
                JOIN heap_graph_class hgc ON hgo.type_id = hgc.id
                JOIN heap_graph_reference hgr ON hgo.owner_upid = hgr.owner_upid
                JOIN process p ON hgo.owner_upid = p.upid
                WHERE 1=1
                  {where_process}
                ORDER BY dt.dominated_size_bytes DESC
                LIMIT 50
            """)

            for r in rows:
                entry = {
                    "id": r.id,
                    "idom_id": r.idom_id,
                    "dominated_obj_count": r.dominated_obj_count,
                    "dominated_size_bytes": r.dominated_size_bytes,
                    "dominated_native_size_bytes": r.dominated_native_size_bytes,
                    "depth": r.depth,
                    "self_size": r.self_size,
                    "class_name": r.class_name,
                    "process_name": r.process_name,
                }
                dominator_tree.append(entry)
        except Exception as e:
            # Try simpler query without JOINs to heap_graph_object/class
            # (column availability varies by Perfetto version)
            debug_log("memory", f"dominator tree full query failed, trying fallback: {e}")
            logger.debug("Dominator tree full query failed, trying fallback: %s", e)
            try:
                rows = tp.query("""
                    INCLUDE PERFETTO MODULE android.memory.heap_graph.dominator_tree;

                    SELECT
                      id,
                      idom_id,
                      dominated_obj_count,
                      dominated_size_bytes,
                      dominated_native_size_bytes,
                      depth
                    FROM heap_graph_dominator_tree
                    ORDER BY dominated_size_bytes DESC
                    LIMIT 50
                """)

                for r in rows:
                    entry = {
                        "id": r.id,
                        "idom_id": r.idom_id,
                        "dominated_obj_count": r.dominated_obj_count,
                        "dominated_size_bytes": r.dominated_size_bytes,
                        "dominated_native_size_bytes": r.dominated_native_size_bytes,
                        "depth": r.depth,
                    }
                    dominator_tree.append(entry)
            except Exception as e2:
                debug_log("memory", f"dominator tree fallback query failed: {e2}")
                logger.debug("Dominator tree fallback query failed: %s", e2)

        debug_log("memory", f"found {len(dominator_tree)} dominator tree entries")
        logger.info("Heap dominator tree: %d entries", len(dominator_tree))
        return dominator_tree
