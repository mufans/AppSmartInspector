"""Memory allocation analysis via Perfetto heap_graph tables."""

from smartinspector.debug_log import debug_log

# Lifecycle methods that indicate the component is being created or visible
_CREATING = {"onCreate", "onStart", "onResume"}
# Lifecycle methods that indicate the component is being destroyed
_DESTROYING = {"onPause", "onStop", "onDestroy", "onDestroyView"}


def _extract_lifecycle_state(tp) -> dict[str, str]:
    """Extract Activity/Fragment lifecycle state from SI$ slices.

    Scans SI$Activity.* and SI$Fragment.* slices to determine which
    concrete components are alive (have onCreate/onStart/onResume without
    a subsequent onDestroy) at trace end time.

    Returns:
        Dict mapping FQN class name -> state string:
        - "alive": onCreate/onStart/onResume seen, no onDestroy after
        - "destroyed": onDestroy seen after the last create/resume
    """
    alive: dict[str, str] = {}

    try:
        # Query all SI$Activity.* and SI$Fragment.* lifecycle slices
        rows = tp.query("""
            SELECT
              name,
              ts
            FROM slice
            WHERE (name LIKE 'SI$%.Activity.%'
                   OR name LIKE 'SI$%.onCreate'
                   OR name LIKE 'SI$%.onStart'
                   OR name LIKE 'SI$%.onResume'
                   OR name LIKE 'SI$%.onPause'
                   OR name LIKE 'SI$%.onStop'
                   OR name LIKE 'SI$%.onDestroy'
                   OR name LIKE 'SI$%.onDestroyView')
              AND (name LIKE 'SI$%Activity.%'
                   OR name LIKE 'SI$%Fragment.%')
            ORDER BY ts ASC
        """)
    except Exception as e:
        debug_log("memory", f"Lifecycle slice query failed: {e}")
        return alive

    for r in rows:
        name = r.name
        # Parse: SI$<fqn_class>.<method>  or  SI$<fqn_class>$<inner>.<method>
        # e.g. SI$com.smartinspector.hook.ui.LockContentionActivity.onResume
        if not name.startswith("SI$"):
            continue
        tag = name[3:]  # strip SI$
        dot_idx = tag.rfind(".")
        if dot_idx < 0:
            continue
        class_fqn = tag[:dot_idx]
        method = tag[dot_idx + 1:]

        # Normalize inner class to outer class for heap_graph matching
        # Heap dumps use the full FQN including $, e.g.
        # com.smartinspector.hook.ui.LockContentionActivity
        # But slices use the concrete class, so keep as-is

        if method in _CREATING:
            alive[class_fqn] = "alive"
        elif method in _DESTROYING:
            alive[class_fqn] = "destroyed"

    return alive


def collect_heap_graph_analysis(tp, target_upid: int | None = None) -> dict:
    """Analyze Java heap memory from heap_graph tables.

    Provides object-level allocation analysis: top classes by size,
    leak suspects (filtered by lifecycle state), and reference chains.

    Args:
        tp: TraceProcessor instance.
        target_upid: Target process upid. If None, queries all processes.

    Returns:
        Dict with heap_objects, leak_suspects, alive_components, etc.
    """
    result: dict = {}

    # Pre-check: if heap_graph_object table doesn't exist, skip all queries
    upid_filter = f"AND o.upid = {target_upid}" if target_upid else ""
    upid_filter_ref = f"AND owner_obj.upid = {target_upid}" if target_upid else ""
    try:
        tp.query("SELECT 1 FROM heap_graph_object LIMIT 1")
    except Exception:
        return result  # No heap dump data in this trace

    # 0. Collect lifecycle state from SI$ slices for leak filtering
    lifecycle_state = _extract_lifecycle_state(tp)
    alive_components = {k for k, v in lifecycle_state.items() if v == "alive"}
    destroyed_components = {k for k, v in lifecycle_state.items() if v == "destroyed"}
    if lifecycle_state:
        debug_log("memory", f"lifecycle: {len(alive_components)} alive, {len(destroyed_components)} destroyed")

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

    # 2. Activity/Fragment leak suspects — filtered by lifecycle state
    #    Only flag components that are NOT currently alive (no active lifecycle).
    #    A component with 1 instance in heap that IS alive is expected.
    #    A destroyed component still in heap, or multiple instances, is suspicious.
    #
    #    Note: heap_graph class_name for Activity/Fragment instances appears as
    #    "java.lang.Class<com.example.MyActivity>". We extract the inner FQN
    #    to match against lifecycle slice names like "com.example.MyActivity.onResume".
    def _normalize_heap_class(name: str) -> str:
        """Extract inner FQN from java.lang.Class<...> wrapper."""
        if name.startswith("java.lang.Class<") and name.endswith(">"):
            return name[16:-1]
        return name

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
            LIMIT 20
        """)
        leak_suspects = []
        alive_in_heap = []
        for r in leak_rows:
            raw_name = r.class_name
            # Filter out base classes (match both raw and normalized forms)
            normalized = _normalize_heap_class(raw_name)
            if normalized in (
                "android.app.Activity",
                "android.app.Fragment",
                "androidx.fragment.app.Fragment",
                "androidx.activity.ComponentActivity",
                "androidx.appcompat.app.AppCompatActivity",
                "androidx.fragment.app.FragmentActivity",
                "androidx.core.app.ComponentActivity",
                "android.content.ContextWrapper",
                "android.content.ContextThemeWrapper",
                "android.view.ContextThemeWrapper",
            ):
                continue

            entry = {
                "class_name": raw_name,
                "obj_count": r.obj_count,
                "total_size_kb": round(r.total_bytes / 1024, 1),
            }

            # Check lifecycle state using normalized FQN
            if normalized in alive_components:
                # Component is alive — having 1 instance is EXPECTED
                entry["state"] = "alive"
                alive_in_heap.append(entry)
            elif normalized in destroyed_components or r.obj_count > 1:
                # Destroyed but still in heap, OR multiple instances → suspicious
                entry["state"] = "suspect"
                leak_suspects.append(entry)
            else:
                # No lifecycle data for this class — treat as suspect only
                # if multiple instances exist, otherwise assume alive
                # (lifecycle hook may not be active for this class)
                if r.obj_count > 1:
                    entry["state"] = "suspect"
                    leak_suspects.append(entry)
                else:
                    entry["state"] = "unknown"
                    alive_in_heap.append(entry)

        if leak_suspects:
            result["leak_suspects"] = leak_suspects
        if alive_in_heap:
            result["alive_components"] = alive_in_heap
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
    try:
        ref_rows = tp.query(f"""
            SELECT
              owner_type.name AS owner_class,
              owned_type.name AS owned_class,
              ref.field_name AS field_name,
              COUNT(*) AS ref_count
            FROM heap_graph_reference ref
            JOIN heap_graph_object owner_obj ON ref.owner_id = owner_obj.id
            JOIN heap_graph_class owner_type ON owner_obj.type_id = owner_type.id
            JOIN heap_graph_object owned_obj ON ref.owned_id = owned_obj.id
            JOIN heap_graph_class owned_type ON owned_obj.type_id = owned_type.id
            WHERE owner_obj.reachable = 1
              {upid_filter_ref}
              AND owned_obj.self_size > 4096
            GROUP BY owner_type.name, owned_type.name, ref.field_name
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

    # 5. Leak suspect reference chains — who holds leaked Activity/Fragment instances
    #    Strategy: search by field_name pattern first (fast, no big JOIN),
    #    then do targeted JOIN for owner class names.
    suspect_names = [s["class_name"] for s in result.get("leak_suspects", [])]
    if suspect_names:
        suspect_fqns = [_normalize_heap_class(s) for s in suspect_names]
        suspect_refs = []
        for fqn in suspect_fqns:
            short = fqn.rsplit(".", 1)[-1] if "." in fqn else fqn
            try:
                # Step A: Find references by field_name pattern (fast, no JOIN)
                ref_rows = tp.query(f"""
                    SELECT ref.owner_id, ref.owned_id, ref.field_name,
                           ref.deobfuscated_field_name
                    FROM __intrinsic_heap_graph_reference ref
                    WHERE ref.field_name GLOB '*{short}*'
                    LIMIT 30
                """)
                owner_ids = set()
                field_rows = []
                for r in ref_rows:
                    oid = r.owner_id if r.owner_id and r.owner_id != 0 else None
                    field_rows.append({
                        "field": r.field_name or "<unknown>",
                        "deobfuscated": r.deobfuscated_field_name or "",
                        "owner_id": oid,
                    })
                    if oid:
                        owner_ids.add(oid)

                # Step B: Resolve owner class names (small targeted JOIN)
                if owner_ids:
                    id_list = ",".join(str(i) for i in list(owner_ids)[:20])
                    owner_rows = tp.query(f"""
                        SELECT o.id, c.name AS class_name
                        FROM heap_graph_object o
                        JOIN heap_graph_class c ON o.type_id = c.id
                        WHERE o.id IN ({id_list})
                    """)
                    owner_map = {r.id: r.class_name for r in owner_rows}
                else:
                    owner_map = {}

                for fr in field_rows:
                    raw_owner = owner_map.get(fr["owner_id"], "") if fr["owner_id"] else ""
                    owner_name = _normalize_heap_class(raw_owner) if raw_owner else raw_owner
                    entry = {
                        "owner": owner_name,
                        "owned": fqn,
                        "field": fr["field"],
                        "deobfuscated": fr["deobfuscated"],
                    }
                    suspect_refs.append(entry)
            except Exception as e:
                debug_log("memory", f"Leak suspect ref query for {fqn} failed: {e}")

        if suspect_refs:
            result["leak_reference_chains"] = suspect_refs

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
