"""Reporter sub-module: data formatting pure functions."""

import json


def format_perf_sections(perf_json: str) -> list[str]:
    """Build user-facing markdown sections from perf JSON.

    Returns a list of markdown strings to include in the LLM prompt.
    """
    user_parts: list[str] = []

    if not perf_json:
        return user_parts

    from smartinspector.agents.deterministic import compute_hints
    hints = compute_hints(perf_json)
    if hints:
        user_parts.append(f"## 预计算结论\n{hints}")

    try:
        perf_data = json.loads(perf_json)
    except Exception:
        perf_data = {}

    # Frame timeline detail
    ft = perf_data.get("frame_timeline", {})
    _total_frames = ft.get("total_frames", 0) if ft else 0
    _avg_fps = ft.get("fps", 0) if ft else 0
    _jank_frames = ft.get("jank_frames", 0) if ft else 0
    if ft and _total_frames > 0:
        ft_lines = ["## 帧时间线\n"]
        ft_lines.append(f"FPS: {_avg_fps:.1f}, 总帧数: {_total_frames}, 卡顿帧: {_jank_frames}")
        jank_types = ft.get("jank_types", [])
        if jank_types:
            ft_lines.append(f"卡顿类型: {', '.join(jank_types)}")
        slowest = ft.get("slowest_frames", [])
        if slowest:
            ft_lines.append("最慢帧 (Top 5):")
            for f in slowest[:5]:
                idx = f.get("frame_index", "?")
                dur = f.get("dur_ms", 0)
                jts = ", ".join(f.get("jank_types", []))
                ft_lines.append(f"  帧#{idx}: {dur:.1f}ms" + (f" [{jts}]" if jts else ""))
        user_parts.append("\n".join(ft_lines))

    # View slices summary (top 10 only, compact)
    vs = perf_data.get("view_slices", {})
    if vs:
        vs_summary = vs.get("summary", [])
        if vs_summary:
            vs_lines = ["## 自定义切片统计 (Top 10)\n"]
            for s in sorted(vs_summary, key=lambda x: -x.get("total_ms", 0))[:10]:
                name = s.get("name", "")
                if not name.startswith("SI$"):
                    continue
                vs_lines.append(f"- {name}: {s.get('count', 0)}次, 最大{s.get('max_ms', 0):.3f}ms, 总{s.get('total_ms', 0):.3f}ms")
            if len(vs_lines) > 1:
                user_parts.append("\n".join(vs_lines))

    return user_parts


def format_attribution_section(attribution_result: str) -> list[str]:
    """Build user-facing markdown sections from attribution JSON."""
    user_parts: list[str] = []

    if not attribution_result:
        return user_parts

    try:
        attr_data = json.loads(attribution_result)
    except Exception:
        return user_parts

    found = [r for r in attr_data if r.get("attributable")]
    system = [r for r in attr_data if r.get("reason") == "system_class"]
    unresolved = [r for r in attr_data
                  if not r.get("attributable") and r.get("reason") not in ("system_class", "found")]

    if found:
        parts = ["## 源码归因结果\n"]
        for r in found:
            type_tag = ""
            raw_name = r.get("raw_name", "")
            if raw_name.startswith("SI$block#"):
                type_tag = " [主线程卡顿]"
            parts.append(f"- {r['class_name']}.{r['method_name']} ({r['dur_ms']:.2f}ms){type_tag}")
            parts.append(f"  位置: {r.get('file_path', '?')}:{r.get('line_start', '?')}-{r.get('line_end', '?')}")
            if r.get("source_snippet"):
                parts.append(f"  发现: {r['source_snippet'][:200]}")
        user_parts.append("\n".join(parts))

    if system:
        parts = ["## 系统框架切片（无法归因到源码）\n"]
        for r in system:
            parts.append(f"- {r['class_name']}.{r['method_name']} ({r['dur_ms']:.2f}ms)")
        parts.append("\n请根据trace数据和调用链上下文推测这些系统切片的性能问题原因，并给出通用优化建议。")
        user_parts.append("\n".join(parts))

    if unresolved:
        parts = ["## 待归因热点（源码未定位）\n"]
        for r in unresolved:
            parts.append(f"- {r['class_name']}.{r['method_name']} ({r['dur_ms']:.2f}ms)")
            parts.append(f"  原因: {r.get('reason', 'unknown')}")
        parts.append("\n这些切片耗时较高但未能在源码中定位。请根据类名和方法名推测可能的性能问题原因，并给出优化建议。")
        user_parts.append("\n".join(parts))

    return user_parts
