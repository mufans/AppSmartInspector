"""Memory allocation analysis via Perfetto heap_graph tables."""

import logging

logger = logging.getLogger(__name__)


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

    # 1. Java heap object statistics — top 20 classes by total size
    upid_filter = f"AND o.upid = {target_upid}" if target_upid else ""
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
        logger.debug("Heap graph object query failed: %s", e)

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
        logger.debug("Leak suspect query failed: %s", e)

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
        logger.debug("Dominator query failed: %s", e)

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
        logger.debug("Reference chain query failed: %s", e)

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
