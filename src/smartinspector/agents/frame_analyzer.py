"""Frame Analyzer: analyze a user-selected frame/slice from Perfetto UI.

Takes ts_ns + dur_ns from user input, queries the trace for overlapping
data, runs source code attribution, and calls LLM for frame-level analysis.
"""

import json
import threading

from langchain_openai import ChatOpenAI

from smartinspector.config import get_llm_kwargs
from smartinspector.prompts import load_prompt
from smartinspector.token_tracker import get_tracker

_prompt = load_prompt("frame-analyzer")
_llm = None
_llm_lock = threading.Lock()


def _get_llm():
    global _llm
    if _llm is not None:
        return _llm
    with _llm_lock:
        if _llm is not None:
            return _llm
        _llm = ChatOpenAI(**get_llm_kwargs(temperature=0.1))
    return _llm


def analyze_frame(trace_path: str, ts_ns: int, dur_ns: int,
                  existing_summary: str = "",
                  cached_attribution: str = "",
                  on_progress=None) -> str:
    """Analyze a user-selected time range in a Perfetto trace.

    Args:
        trace_path: Path to the .pb trace file.
        ts_ns: Start timestamp in nanoseconds.
        dur_ns: Duration in nanoseconds.
        existing_summary: Existing perf_summary JSON for context.
        cached_attribution: JSON from prior /full run_attribution() to reuse.

    Returns:
        Markdown analysis from LLM.
    """
    from smartinspector.collector.perfetto import query_frame_slices
    from smartinspector.agents.deterministic import _detect_frame_budget_ms
    from smartinspector.debug_log import debug_log

    # Query trace data for the selected range
    if on_progress:
        on_progress(f"  [frame] 查询 trace 切片 (ts={ts_ns}, dur={dur_ns})...")
    debug_log("frame", f"Step 1: Querying trace slices (ts={ts_ns}, dur={dur_ns})...")
    frame_data = query_frame_slices(trace_path, ts_ns, dur_ns)
    n_slices = len(frame_data.get("slices", []))
    n_frames = len(frame_data.get("frames", []))
    if on_progress:
        on_progress(f"  [frame] 找到 {n_slices} 切片, {n_frames} 帧")
    debug_log("frame", f"  Found {n_slices} slices, {n_frames} frames")

    # Build deterministic hints for the frame
    hints = _build_frame_hints(frame_data, existing_summary)

    # Run source code attribution on SI$ slices in the selected range
    debug_log("frame", "Step 2: Running source attribution...")
    source_section = _run_source_attribution(
        frame_data, existing_summary, cached_attribution, on_progress,
    )
    debug_log("frame", "Step 2 done")

    # Truncate existing summary for context
    summary_context = ""
    if existing_summary:
        try:
            summary_data = json.loads(existing_summary)
            summary_context = json.dumps(summary_data, indent=2, ensure_ascii=False)[:2000]
        except (json.JSONDecodeError, TypeError):
            summary_context = existing_summary[:2000]

    # Truncate frame data for LLM input
    frame_json = json.dumps(frame_data, indent=2, ensure_ascii=False)
    if len(frame_json) > 6000:
        frame_data["slices"] = frame_data["slices"][:20]
        frame_json = json.dumps(frame_data, indent=2, ensure_ascii=False)

    user_content = (
        "## 预计算结论\n\n"
        f"{hints}\n\n"
        "## 选中范围数据\n\n"
        f"```json\n{frame_json}\n```\n\n"
    )
    if source_section:
        user_content += f"## 源码归因\n\n{source_section}\n\n"
    if summary_context:
        user_content += f"## 全量摘要参考（节选）\n\n```json\n{summary_context}\n```\n"

    from langchain_core.messages import HumanMessage, SystemMessage
    if on_progress:
        on_progress("  [frame] 调用 LLM 分析...")
    debug_log("frame", "Step 3: Calling LLM for analysis...")
    debug_log("frame", f"Step 3 input (first 2000 chars):\n{user_content[:2000]}")
    llm = _get_llm()
    response = llm.invoke([
        SystemMessage(content=_prompt),
        HumanMessage(content=user_content),
    ])
    get_tracker().record_from_message("frame_analyzer", response)
    debug_log("frame", f"Step 3 response:\n{response.content}")
    debug_log("frame", "Step 3 done")
    return response.content


def _run_source_attribution(frame_data: dict, existing_summary: str,
                            cached_attribution: str = "",
                            on_progress=None) -> str:
    """Run source code attribution on SI$ slices found in the selected range.

    Extracts SI$ slices from frame_data, combines with block_events from
    perf_summary, and calls run_attribution to search source code.
    """
    from smartinspector.commands.attribution import (
        extract_class,
        extract_method,
        extract_fqn,
        classify_search_type,
        is_system_method,
        _extract_method_from_stack,
    )
    from smartinspector.agents.attributor import run_attribution

    slices = frame_data.get("slices", [])
    si_slices = [s for s in slices if s.get("name", "").startswith("SI$")]
    if not si_slices:
        return ""

    # Build attributable list from SI$ slices in the selected range
    import re as _re

    attributable = []
    seen_keys: dict[str, dict] = {}
    for s in si_slices:
        name = s["name"]
        if classify_search_type(name) == "system":
            continue
        if is_system_method(name):
            continue

        # SI$block# slices have trace dur≈0 (beginSection+endSection are
        # adjacent).  Extract real duration from the tag suffix (#NNNms).
        dur_ms = s["dur_ms"]
        if name.startswith("SI$block#") and dur_ms < 0.01:
            dur_match = _re.search(r'#(\d+(?:\.\d+)?)ms$', name)
            if dur_match:
                dur_ms = float(dur_match.group(1))

        # Skip slices with negligible duration — no analysis value
        if dur_ms < 0.01:
            continue

        class_name = extract_class(name)
        method_name = extract_method(name)

        # Anonymous inner class detection (e.g., CpuBurnWorker$startMainThreadWork$1)
        # extract_method returns the enclosing method name from _extract_method_from_anonymous,
        # but the actual executing method is the anonymous class's method (e.g., Runnable.run).
        raw_fqn = extract_fqn(name)
        context_method = ""
        if raw_fqn and _re.search(r'\$\d+$', raw_fqn) and method_name:
            context_method = method_name
            # Try to get the actual method from the slice's stack trace
            # (populated by _correlate_block_stacks_from_logcat in query_frame_slices)
            stack = s.get("stack_trace", [])
            if stack:
                stack_method = _extract_method_from_stack(stack)
                if stack_method and stack_method != method_name:
                    method_name = stack_method

        key = f"{class_name}.{method_name}"
        if key in seen_keys:
            # Accumulate duration and call count for repeated slices
            existing = seen_keys[key]
            existing["dur_ms"] += dur_ms
            existing["call_count"] = existing.get("call_count", 1) + 1
            continue
        seen_keys[key] = None  # placeholder, replaced below

        item = {
            "raw_name": name,
            "class_name": class_name,
            "method_name": method_name,
            "dur_ms": dur_ms,
            "type": "slice",
            "search_type": classify_search_type(name),
            "instance": None,
        }
        if context_method:
            item["context_method"] = context_method
        seen_keys[key] = item
        attributable.append(item)

    if not attributable:
        return ""

    # Attach block_events from existing summary for stack traces
    if existing_summary:
        try:
            summary_data = json.loads(existing_summary)
            block_events = summary_data.get("block_events", [])
            if block_events:
                from smartinspector.commands.attribution import _attach_block_stacks
                _attach_block_stacks(attributable, block_events)
                # Remove system entries marked by block event matching
                attributable = [e for e in attributable if not e.get("_system")]
        except Exception:
            pass

    if not attributable:
        return ""

    from smartinspector.debug_log import debug_log

    debug_log("frame", f"Source attribution: {len(attributable)} slices")

    # Match CLI attributor_node format (graph/nodes/attributor.py:61-63)
    if on_progress:
        on_progress(f"  [attributor] Found {len(attributable)} slices, searching source code...")
        for s in attributable[:5]:
            on_progress(f"    {s['dur_ms']:>8.2f}ms  {s['class_name']}.{s['method_name']}  ({s.get('search_type', 'java')})")

    # Try to reuse cached attribution results from /full
    if cached_attribution:
        try:
            cached_results = json.loads(cached_attribution)
            cache_by_key = {}
            for r in cached_results:
                if r.get("attributable"):
                    key = f"{r['class_name']}.{r['method_name']}"
                    cache_by_key[key] = r
                    # Also index by context_method for anonymous inner class matching
                    # (e.g., cached as "CpuBurnWorker.run" but frame has "CpuBurnWorker.startMainThreadWork")
                    if r.get("context_method"):
                        alt_key = f"{r['class_name']}.{r['context_method']}"
                        if alt_key not in cache_by_key:
                            cache_by_key[alt_key] = r
            matched = []
            unmatched = []
            for item in attributable:
                key = f"{item['class_name']}.{item['method_name']}"
                if key in cache_by_key:
                    matched.append(cache_by_key[key])
                else:
                    unmatched.append(item)

            if unmatched:
                debug_log("frame", f"  {len(matched)} matched from cache, {len(unmatched)} new")
                new_results = run_attribution(unmatched, on_progress)
                matched.extend(new_results)
            else:
                debug_log("frame", f"  All {len(matched)} matched from cache (0 new)")

            results = matched
        except Exception:
            debug_log("frame", "  Cache parse failed, running full attribution")
            results = run_attribution(attributable, on_progress)
    else:
        results = run_attribution(attributable, on_progress)

    found = sum(1 for r in results if r.get("attributable"))
    system = sum(1 for r in results if r.get("reason") == "system_class")
    # Match CLI attributor_node format (graph/nodes/attributor.py:71)
    if on_progress:
        on_progress(f"  [attributor] Done: {found} attributed, {system} system classes")
    debug_log("frame", f"Source attribution done: {found} found")

    # Format results for LLM
    lines = []
    found = [r for r in results if r.get("attributable")]
    if found:
        lines.append(f"找到 {len(found)} 个用户代码的源码位置:\n")
        for r in found:
            fp = r.get("file_path", "?")
            ls = r.get("line_start", "?")
            le = r.get("line_end", "?")
            snippet = r.get("source_snippet", "")
            call_count = r.get("call_count", 0)
            dur_label = f"{r['dur_ms']:.2f}ms"
            if call_count > 1:
                dur_label += f" (累计, {call_count}次调用)"
            # For anonymous inner classes, show the actual raw tag name
            # so LLM knows this is an anonymous class execution (e.g. Runnable.run)
            raw_name = r.get("raw_name", "")
            method_label = f"{r['class_name']}.{r['method_name']}"
            if "$" in raw_name and r.get("context_method"):
                method_label += f" (匿名内部类, 定义在 {r['context_method']} 内)"
            lines.append(f"### {method_label} ({dur_label})")
            lines.append(f"- 文件: `{fp}:{ls}-{le}`")
            if snippet:
                lines.append(f"- 分析: {snippet[:300]}")
            lines.append("")
    else:
        lines.append("未找到可归因的用户源码（全部为系统/框架类）")

    return "\n".join(lines)


def _build_frame_hints(frame_data: dict, existing_summary: str) -> str:
    """Build deterministic hints for a specific frame selection."""
    slices = frame_data.get("slices", [])
    frames = frame_data.get("frames", [])
    call_chains = frame_data.get("call_chains", [])
    dur_ms = frame_data.get("dur_ms", 0)

    sections = []

    # Detect frame budget from existing summary
    frame_budget_ms = 16.67
    if existing_summary:
        try:
            from smartinspector.agents.deterministic import _detect_frame_budget_ms
            frame_budget_ms = _detect_frame_budget_ms(json.loads(existing_summary))
        except Exception:
            pass

    # SI$ slice classification
    si_slices = [s for s in slices if s.get("name", "").startswith("SI$")]
    if si_slices:
        import re as _re
        p0_threshold = frame_budget_ms

        # Compute effective duration for each slice (fix SI$block# dur≈0)
        for s in si_slices:
            s["_effective_dur"] = s["dur_ms"]
            name = s["name"]
            if name.startswith("SI$block#") and s["_effective_dur"] < 0.01:
                dur_match = _re.search(r'#(\d+(?:\.\d+)?)ms$', name)
                if dur_match:
                    s["_effective_dur"] = float(dur_match.group(1))

        # Sort by effective duration DESC so high-impact slices appear first
        si_slices.sort(key=lambda s: s["_effective_dur"], reverse=True)

        # Aggregate by method key to show cumulative impact for repeated calls
        agg: dict[str, dict] = {}
        agg_order: list[str] = []
        for s in si_slices:
            skey = s["name"].split("#")[0] if "#" in s["name"] and not s["name"].startswith("SI$block") else s["name"]
            # Normalize: strip duration suffix from block tags for grouping
            if s["name"].startswith("SI$block#"):
                # SI$block#pkg.Class$method$N#NNms -> group by prefix without ms suffix
                _m = _re.match(r'(SI\$block#.*?)(#\d+(?:\.\d+)?ms)$', s["name"])
                skey = _m.group(1) if _m else s["name"]
            if skey in agg:
                agg[skey]["dur"] += s["_effective_dur"]
                agg[skey]["count"] += 1
            else:
                agg[skey] = {"name": s["name"], "dur": s["_effective_dur"], "count": 1}
                agg_order.append(skey)

        # Re-sort aggregated by total dur DESC
        agg_sorted = sorted(agg.values(), key=lambda a: a["dur"], reverse=True)

        lines = [f"[选中范围 SI$ 切片] (共 {len(si_slices)} 个, 范围 {dur_ms:.2f}ms)"]
        for a in agg_sorted[:10]:
            sdur = a["dur"]
            level = "P0" if sdur > p0_threshold else ("P1" if sdur >= p0_threshold * 0.25 else "P2")
            count_label = f", {a['count']}次调用累计" if a["count"] > 1 else ""
            lines.append(f"  {level}: {a['name']} ({sdur:.2f}ms{count_label})")
        sections.append("\n".join(lines))

        # Clean up temp field
        for s in si_slices:
            s.pop("_effective_dur", None)
    else:
        sections.append(f"[选中范围] 无 SI$ 用户代码切片 (共 {len(slices)} 个系统切片)")

    # Jank frame info
    jank_frames = [f for f in frames if f.get("is_jank")]
    if jank_frames:
        lines = [f"[Jank 帧] 选中范围内 {len(jank_frames)}/{len(frames)} 个 jank 帧"]
        for f in jank_frames[:3]:
            lines.append(
                f"  帧 {f['dur_ms']:.2f}ms, jank: {', '.join(f.get('jank_types', []))}"
            )
        sections.append("\n".join(lines))
    elif frames:
        sections.append(f"[帧状态] 选中范围内 {len(frames)} 帧, 无 jank")

    # Call chain summary
    if call_chains:
        lines = ["[调用链]"]
        for chain in call_chains[:3]:
            name = chain.get("name", "?")
            dur = chain.get("dur_ms", 0)
            children = chain.get("children", [])
            lines.append(f"  {name} ({dur:.2f}ms)")
            for c in children[:5]:
                pct = (c["dur_ms"] / dur * 100) if dur > 0 else 0
                if pct >= 5:
                    lines.append(f"    {pct:.1f}% -> {c['name']} ({c['dur_ms']:.2f}ms)")
        sections.append("\n".join(lines))

    return "\n\n".join(sections)
