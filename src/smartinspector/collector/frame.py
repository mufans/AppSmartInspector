"""Frame/View/Compose slice collection: frame timeline, view slices, compose slices."""

from smartinspector.debug_log import debug_log


class FrameMixin:
    """Mixin for PerfettoCollector providing frame/UI-related collection."""

    def collect_frame_timeline(self) -> dict:
        """Analyze frame rendering timeline (jank detection)."""
        tp = self._open()

        # Build expected timeline lookup: display_frame_token -> expected dur
        expected_map: dict[int, float] = {}
        try:
            exp_rows = tp.query("""
                SELECT
                  display_frame_token,
                  MAX(dur) AS expected_dur_ns
                FROM expected_frame_timeline_slice
                GROUP BY display_frame_token
            """)
            for r in exp_rows:
                expected_map[r.display_frame_token] = round(r.expected_dur_ns / 1e6, 2)
        except Exception as e:
            debug_log("perfetto", f"Expected frame timeline query failed: {e}")

        try:
            rows = tp.query("""
                SELECT
                  display_frame_token,
                  MIN(ts) AS frame_ts,
                  MAX(dur) AS frame_dur_ns,
                  GROUP_CONCAT(DISTINCT jank_type) AS jank_types,
                  GROUP_CONCAT(DISTINCT layer_name) AS layers
                FROM actual_frame_timeline_slice
                WHERE dur > 0
                  AND surface_frame_token > 0
                GROUP BY display_frame_token
                ORDER BY frame_ts ASC
            """)
        except Exception as e:
            debug_log("perfetto", f"Frame timeline query failed: {e}")
            return {}

        USER_JANK_TYPES = {
            "App Deadline Missed", "Dropped Frame",
            "SurfaceFlinger CPU Deadline Missed", "SurfaceFlinger GPU Deadline Missed",
            "SurfaceFlinger Scheduling Delay", "Display HAL",
            "Unknown Jank",
        }

        frames = []
        for r in rows:
            dur_ms = round(r.frame_dur_ns / 1e6, 2)
            all_jank = [j.strip() for j in (r.jank_types or "").split(",") if j.strip() and j.strip() != "None"]
            jank_list = [j for j in all_jank if j in USER_JANK_TYPES]
            expected_dur = expected_map.get(r.display_frame_token, 0)
            frames.append({
                "ts_ns": r.frame_ts,
                "dur_ms": dur_ms,
                "expected_dur_ms": expected_dur,
                "jank_types": jank_list,
                "layers": (r.layers or ""),
                "is_jank": len(jank_list) > 0,
            })

        if not frames:
            return {"total_frames": 0}

        for i, f in enumerate(frames):
            f["frame_index"] = i + 1

        jank = [f for f in frames if f["is_jank"]]

        fps = 0.0
        if len(frames) > 1:
            total_s = (frames[-1]["ts_ns"] - frames[0]["ts_ns"]) / 1e9
            if total_s > 0:
                fps = round(len(frames) / total_s, 1)

        slowest = sorted(frames, key=lambda x: -x["dur_ms"])[:10]
        jank_detail = sorted(jank, key=lambda x: -x["dur_ms"])[:10]

        return {
            "fps": fps,
            "total_frames": len(frames),
            "jank_frames": len(jank),
            "jank_types": list(set(jt for f in jank for jt in f["jank_types"])),
            "slowest_frames": slowest,
            "jank_detail": jank_detail,
        }

    def collect_view_slices(self) -> dict:
        """Collect View-level slice data: doFrame, measure, layout, draw, RV events."""
        tp = self._open()
        try:
            rows = tp.query("""
                SELECT
                  id,
                  name,
                  ts,
                  dur,
                  depth,
                  parent_id,
                  track_id
                FROM slice
                WHERE (name LIKE 'SI$%'
                       AND name NOT LIKE 'SI$net#%'
                       AND name NOT LIKE 'SI$db#%'
                       AND name NOT LIKE 'SI$img#%'
                       AND name NOT LIKE 'SI$touch#%')
                   OR name LIKE '%doFrame%'
                   OR name LIKE '%Choreographer%'
                   OR name LIKE '%Traversal%'
                   OR name LIKE '%performDraw%'
                   OR name LIKE '%performMeasure%'
                   OR name LIKE '%performLayout%'
                ORDER BY ts ASC
            """)

            rows = list(rows)
            slice_ids_in_set = {r.id for r in rows}
            missing_parent_ids = set()
            for r in rows:
                if r.parent_id and r.parent_id not in slice_ids_in_set:
                    missing_parent_ids.add(r.parent_id)

            if missing_parent_ids:
                id_list = ",".join(str(pid) for pid in missing_parent_ids)
                try:
                    parent_rows = tp.query(f"""
                        SELECT id, name, ts, dur, depth, parent_id, track_id
                        FROM slice
                        WHERE id IN ({id_list})
                    """)
                    filtered_parents = [r for r in parent_rows if not r.name.startswith("SI$touch#")]
                    rows = list(rows) + filtered_parents
                    for r in filtered_parents:
                        slice_ids_in_set.add(r.id)
                        if r.parent_id and r.parent_id not in slice_ids_in_set:
                            missing_parent_ids.add(r.parent_id)

                    level2_parents = set()
                    for r in parent_rows:
                        if r.parent_id and r.parent_id not in slice_ids_in_set:
                            level2_parents.add(r.parent_id)
                    if level2_parents:
                        id_list2 = ",".join(str(pid) for pid in level2_parents)
                        try:
                            gp_rows = tp.query(f"""
                                SELECT id, name, ts, dur, depth, parent_id, track_id
                                FROM slice
                                WHERE id IN ({id_list2})
                            """)
                            rows = list(rows) + [r for r in gp_rows if not r.name.startswith("SI$touch#")]
                        except Exception as e:
                            debug_log("perfetto", f"Grandparent slice query failed: {e}")
                except Exception as e:
                    debug_log("perfetto", f"Parent slice query failed: {e}")
        except Exception as e:
            debug_log("perfetto", f"View slices query failed: {e}")
            return {}

        slices = []
        slice_by_id: dict[int, dict] = {}
        for r in rows:
            dur_ms = round(r.dur / 1e6, 2) if r.dur else 0
            is_custom = r.name.startswith("SI$")
            s = {
                "id": r.id,
                "name": r.name,
                "ts_ns": r.ts,
                "dur_ms": dur_ms,
                "depth": r.depth,
                "parent_id": r.parent_id,
                "is_custom": is_custom,
            }
            slices.append(s)
            slice_by_id[r.id] = s

        if not slices:
            return {}

        slowest = sorted(
            [s for s in slices if s["is_custom"]],
            key=lambda x: -x["dur_ms"],
        )[:30]

        name_stats: dict[str, dict] = {}
        for s in slices:
            n = s["name"]
            if n not in name_stats:
                name_stats[n] = {"name": n, "count": 0, "total_ms": 0.0, "max_ms": 0.0, "is_custom": s["is_custom"]}
            name_stats[n]["count"] += 1
            name_stats[n]["total_ms"] += s["dur_ms"]
            name_stats[n]["max_ms"] = max(name_stats[n]["max_ms"], s["dur_ms"])

        rv_instances: dict[str, dict] = {}
        for s in slices:
            n = s["name"]
            rv_prefix = None
            if n.startswith("SI$RV#"):
                rv_prefix = "SI$"
            elif n.startswith("RV#"):
                rv_prefix = ""
            if rv_prefix is None:
                continue
            tag_body = n[len(rv_prefix):]
            last_hash = tag_body.rfind("#")
            after_hash = tag_body[last_hash + 1:] if last_hash >= 0 else tag_body
            last_dot = after_hash.rfind(".")
            if last_dot >= 0:
                instance_key = tag_body[:last_hash + 1] + after_hash[:last_dot]
                method = after_hash[last_dot + 1:]
            else:
                instance_key = tag_body
                method = "unknown"
            if instance_key not in rv_instances:
                rv_instances[instance_key] = {
                    "instance": instance_key,
                    "total_ms": 0.0,
                    "count": 0,
                    "methods": {},
                    "max_dur_ms": 0.0,
                    "max_dur_method": "",
                }
            inst = rv_instances[instance_key]
            inst["total_ms"] += s["dur_ms"]
            inst["count"] += 1
            if s["dur_ms"] > inst["max_dur_ms"]:
                inst["max_dur_ms"] = s["dur_ms"]
                inst["max_dur_method"] = method
            if method not in inst["methods"]:
                inst["methods"][method] = {"count": 0, "total_ms": 0.0, "max_ms": 0.0}
            m = inst["methods"][method]
            m["count"] += 1
            m["total_ms"] += s["dur_ms"]
            m["max_ms"] = max(m["max_ms"], s["dur_ms"])

        rv_sorted = sorted(rv_instances.values(), key=lambda x: -x["total_ms"])

        children_map: dict[int, list[dict]] = {}
        for s in slices:
            pid = s.get("parent_id")
            if pid:
                if pid not in children_map:
                    children_map[pid] = []
                children_map[pid].append(s)

        def _build_chain(slice_id: int) -> list[str]:
            chain = []
            visited = set()
            current_id = slice_id
            while current_id and current_id not in visited:
                visited.add(current_id)
                current = slice_by_id.get(current_id)
                if not current:
                    break
                chain.append(f"{current['name']} [{current['dur_ms']:.2f}ms]")
                current_id = current.get("parent_id")
            return chain

        def _get_children_breakdown(parent_id: int) -> list[dict]:
            kids = children_map.get(parent_id, [])
            kids_sorted = sorted(kids, key=lambda x: -x["dur_ms"])
            result = []
            seen_methods = set()
            for k in kids_sorted:
                name = k["name"]
                if name in seen_methods:
                    continue
                seen_methods.add(name)
                entry = {"name": name, "dur_ms": k["dur_ms"]}
                sub = _get_children_breakdown(k["id"])
                if sub:
                    entry["children"] = sub
                result.append(entry)
            return result

        slowest_custom = sorted(
            [s for s in slices if s["is_custom"] and s["dur_ms"] >= 1.0],
            key=lambda x: -x["dur_ms"],
        )[:10]

        call_chains = []
        for s in slowest_custom:
            raw_chain = _build_chain(s["id"])
            breakdown = _get_children_breakdown(s["id"])
            call_chains.append({
                "name": s["name"],
                "dur_ms": s["dur_ms"],
                "chain": list(reversed(raw_chain)),
                "breakdown": breakdown,
            })

        target_process_info = {}
        target_pkg = self._target_process_cache.get("name", "") if self._target_process_cache else ""
        if target_pkg:
            target_process_info = self._resolve_target_process(target_pkg)
        elif self._target_process_cache is None:
            for s in slowest[:3]:
                name = s.get("name", "")
                if name.startswith("SI$"):
                    body = name[3:]
                    parts = body.split(".")
                    if len(parts) >= 3:
                        candidate_pkg = ".".join(parts[:3])
                        info = self._resolve_target_process(candidate_pkg)
                        if info.get("upid"):
                            target_process_info = info
                            break

        if target_process_info.get("upid"):
            target_upid = target_process_info["upid"]
            try:
                track_ids = set(s.get("track_id") for s in slowest if s.get("track_id"))
                if track_ids:
                    id_list = ",".join(str(tid) for tid in track_ids)
                    track_proc_map = {}
                    for r in tp.query(f"""
                        SELECT t.id AS track_id, p.upid, p.name AS process_name
                        FROM thread_track t
                        JOIN thread th ON t.utid = th.utid
                        JOIN process p ON th.upid = p.upid
                        WHERE t.id IN ({id_list})
                    """):
                        track_proc_map[r.track_id] = {"upid": r.upid, "name": r.process_name}

                    for s in slowest:
                        proc_info = track_proc_map.get(s.get("track_id"))
                        if proc_info:
                            s["process_name"] = proc_info["name"]
                            s["is_target"] = proc_info["upid"] == target_upid
            except Exception as e:
                debug_log("perfetto", f"track-process annotation failed: {e}")

        result = {
            "summary": sorted(name_stats.values(), key=lambda x: -x["total_ms"]),
            "slowest_slices": slowest,
            "rv_instances": rv_sorted,
            "call_chains": call_chains,
        }

        if target_process_info:
            result["target_process"] = target_process_info

        return result

    def collect_compose_slices(self) -> dict:
        """Collect Jetpack Compose recomposition slices (SI$compose#)."""
        tp = self._open()
        try:
            rows = tp.query("""
                SELECT name, ts, dur, depth, track_id
                FROM slice
                WHERE name LIKE 'SI$compose#%'
                ORDER BY ts ASC
            """)
        except Exception as e:
            debug_log("perfetto", f"Compose slices query failed: {e}")
            return {}

        slices = []
        composable_stats: dict[str, dict] = {}

        for r in rows:
            dur_ms = round(r.dur / 1e6, 2) if r.dur else 0
            name = r.name
            slices.append({
                "name": name,
                "ts_ns": r.ts,
                "dur_ms": dur_ms,
                "depth": r.depth,
            })

            body = name[len("SI$compose#"):]
            last_hash = body.rfind("#")
            if last_hash >= 0:
                composable_name = body[:last_hash]
                compose_type = body[last_hash + 1:]
            else:
                composable_name = body
                compose_type = "unknown"

            if composable_name not in composable_stats:
                composable_stats[composable_name] = {
                    "name": composable_name,
                    "first_count": 0,
                    "recompose_count": 0,
                    "total_ms": 0.0,
                    "max_ms": 0.0,
                }
            stats = composable_stats[composable_name]
            if compose_type == "first":
                stats["first_count"] += 1
            elif compose_type == "recompose":
                stats["recompose_count"] += 1
            stats["total_ms"] += dur_ms
            stats["max_ms"] = max(stats["max_ms"], dur_ms)

        if not slices:
            return {}

        sorted_stats = sorted(
            composable_stats.values(),
            key=lambda x: -x["total_ms"],
        )

        return {
            "total_count": len(slices),
            "composables": sorted_stats,
            "slowest": sorted(slices, key=lambda x: -x["dur_ms"])[:20],
        }
