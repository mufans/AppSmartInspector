"""Deterministic analysis layer — pre-computes arithmetic for LLM.

Instead of sending raw JSON to LLM and asking it to do math,
this module computes severity classification, call-chain distribution,
RV hotspots, jank correlation, and CPU hotspots with pure code.
LLM only needs to organize language around these conclusions.
"""

import json


def _detect_frame_budget_ms(data: dict) -> float:
    """Detect device frame budget from frame timeline data.

    Uses the median expected_dur_ms from jank_detail/slowest_frames
    to infer the device refresh rate. Falls back to 16.67ms (60Hz).
    """
    ft = data.get("frame_timeline") or {}
    candidates: list[float] = []

    for frame_list_key in ("jank_detail", "slowest_frames"):
        for f in ft.get(frame_list_key, []):
            exp = f.get("expected_dur_ms", 0)
            if 4.0 <= exp <= 50.0:  # reasonable range: 240Hz~20Hz
                candidates.append(exp)

    if candidates:
        candidates.sort()
        # Median
        mid = len(candidates) // 2
        if len(candidates) % 2 == 0:
            return round((candidates[mid - 1] + candidates[mid]) / 2, 2)
        return round(candidates[mid], 2)

    return 16.67  # default 60Hz


def compute_hints(perf_json: str) -> str:
    """Run all deterministic analysis helpers on perf JSON.

    Returns a Chinese-text summary of pre-computed conclusions,
    skipping sections that have no relevant data.
    """
    try:
        data = json.loads(perf_json)
    except (json.JSONDecodeError, TypeError):
        return ""

    frame_budget_ms = _detect_frame_budget_ms(data)

    sections = [
        _classify_severity(data, frame_budget_ms),
        _compute_call_chain_distribution(data),
        _rank_rv_hotspots(data),
        _correlate_jank_frames(data, frame_budget_ms),
        _identify_cpu_hotspots(data),
    ]

    return "\n\n".join(s for s in sections if s)


# ---------------------------------------------------------------------------
# Helper 1: Severity classification
# ---------------------------------------------------------------------------

def _classify_severity(data: dict, frame_budget_ms: float = 16.67) -> str:
    """Classify custom slices into P0/P1/P2 by duration thresholds.

    Thresholds are derived from the device's frame budget:
      P0: > frame_budget (e.g. >16.67ms on 60Hz, >8.33ms on 120Hz)
      P1: >= 25% of frame_budget (e.g. >=4ms on 60Hz, >=2ms on 120Hz)
      P2: < 25% of frame_budget
    """
    slices = (data.get("view_slices") or {}).get("slowest_slices") or []
    custom = [s for s in slices if s.get("is_custom") and s.get("dur_ms", 0) >= 1.0]
    if not custom:
        return ""

    p0_threshold = frame_budget_ms
    p1_threshold = frame_budget_ms * 0.25

    p0, p1, p2 = [], [], []
    for s in custom:
        dur = s["dur_ms"]
        name = s.get("name", "?")
        if dur > p0_threshold:
            p0.append((dur, name))
        elif dur >= p1_threshold:
            p1.append((dur, name))
        else:
            p2.append((dur, name))

    lines = [f"[严重度分类] (帧预算: {frame_budget_ms:.2f}ms)"]
    for label, items in [("P0", p0), ("P1", p1), ("P2", p2)]:
        for dur, name in sorted(items, key=lambda x: -x[0]):
            lines.append(f"  {label}: {name} ({dur:.2f}ms)")

    return "\n".join(lines) if len(lines) > 1 else ""


# ---------------------------------------------------------------------------
# Helper 2: Call-chain time distribution
# ---------------------------------------------------------------------------

def _compute_call_chain_distribution(data: dict) -> str:
    """Compute percentage breakdown of call chains with tree indentation."""
    chains = (data.get("view_slices") or {}).get("call_chains") or []
    if not chains:
        return ""

    lines = ["[调用链时间分布]"]

    for chain in chains[:5]:
        name = chain.get("name", "?")
        dur = chain.get("dur_ms", 0)
        lines.append(f"{name} ({dur:.2f}ms):")

        breakdown = chain.get("breakdown") or []
        if breakdown:
            _format_breakdown(breakdown, dur, lines, indent=2)

    return "\n".join(lines)


def _format_breakdown(items: list, parent_dur: float, lines: list, indent: int):
    """Recursively format breakdown items with tree-prefix percentage."""
    if parent_dur <= 0:
        return

    significant = [
        (item, item.get("dur_ms", 0) / parent_dur * 100)
        for item in items
        if item.get("dur_ms", 0) / parent_dur * 100 >= 5
    ]
    # Sort by percentage descending
    significant.sort(key=lambda x: -x[1])

    prefix = " " * indent
    for i, (item, pct) in enumerate(significant):
        name = item.get("name", "?")
        dur = item.get("dur_ms", 0)
        tree_char = "\u2514\u2500" if i == len(significant) - 1 else "\u251c\u2500"
        lines.append(f"{prefix}{pct:.1f}% {tree_char} {name} ({dur:.2f}ms)")

        children = item.get("children") or []
        if children:
            _format_breakdown(children, dur, lines, indent + 4)


# ---------------------------------------------------------------------------
# Helper 3: RV hotspots ranking
# ---------------------------------------------------------------------------

def _rank_rv_hotspots(data: dict) -> str:
    """Rank RecyclerView methods by max_ms, compute avg_ms."""
    instances = (data.get("view_slices") or {}).get("rv_instances") or []
    if not instances:
        return ""

    lines = ["[RV热点排名]"]

    for inst in instances:
        view_id = inst.get("view_id", "?")
        adapter = inst.get("adapter_name", "?")
        methods = inst.get("methods") or {}
        if not methods:
            continue

        lines.append(f"RV#{view_id}#{adapter}:")

        # Sort methods by max_ms descending
        ranked = sorted(
            methods.items(),
            key=lambda kv: kv[1].get("max_ms", 0),
            reverse=True,
        )

        for method, stats in ranked[:5]:
            count = stats.get("count", 0)
            max_ms = stats.get("max_ms", 0)
            total_ms = stats.get("total_ms", 0)
            avg_ms = total_ms / count if count > 0 else 0
            lines.append(
                f"  {method}: {count}\u6b21, \u6700\u5927{max_ms:.2f}ms, \u5747\u503c{avg_ms:.2f}ms"
            )

    return "\n".join(lines) if len(lines) > 1 else ""


# ---------------------------------------------------------------------------
# Helper 4: Jank frame correlation
# ---------------------------------------------------------------------------

def _correlate_jank_frames(data: dict, frame_budget_ms: float = 16.67) -> str:
    """Correlate jank frames with slowest slices and input events."""
    ft = data.get("frame_timeline") or {}
    jank_detail = ft.get("jank_detail") or []
    slowest = (data.get("view_slices") or {}).get("slowest_slices") or []
    input_events = data.get("input_events") or []

    if not jank_detail or not slowest:
        return ""

    has_input = bool(input_events)
    lines = ["[卡顿帧关联]"]

    for frame in jank_detail[:5]:
        f_idx = frame.get("frame_index", "?")
        f_dur = frame.get("dur_ms", 0)
        f_exp = frame.get("expected_dur_ms") or frame_budget_ms
        f_ts = frame.get("ts_ns", 0)
        f_end = f_ts + f_dur * 1_000_000  # ms → ns

        # Skip if no timestamp
        if f_ts <= 0:
            continue

        overx = f_dur / f_exp if f_exp > 0 else 0

        # Check for preceding input event (within 50ms before frame start)
        input_info = ""
        if has_input:
            INPUT_WINDOW_NS = 50_000_000  # 50ms before frame
            for ie in input_events:
                ie_ts = ie.get("ts_ns", 0)
                if ie_ts > 0 and f_ts - INPUT_WINDOW_NS <= ie_ts <= f_ts:
                    action = ie.get("action", "?")
                    activity = ie.get("activity", "?")
                    delta_ms = (f_ts - ie_ts) / 1_000_000
                    input_info = f" [触发: {activity}#{action}, {delta_ms:.1f}ms前]"
                    break

        lines.append(
            f"\u5e27#{f_idx} ({f_dur:.2f}ms, \u9884\u671f{f_exp:.2f}ms, \u8d85\u51fa{overx:.1f}x){input_info}:"
        )

        # Find overlapping custom slices
        matched = []
        for s in slowest:
            if not s.get("is_custom"):
                continue
            s_ts = s.get("ts_ns", 0)
            s_dur_ms = s.get("dur_ms", 0)
            s_end = s_ts + s_dur_ms * 1_000_000

            # Check overlap
            if s_ts > 0 and s_ts < f_end and s_end > f_ts:
                overlap_ns = min(s_end, f_end) - max(s_ts, f_ts)
                overlap_ms = overlap_ns / 1_000_000
                pct = overlap_ms / f_dur * 100 if f_dur > 0 else 0
                if pct >= 5:
                    matched.append((pct, s.get("name", "?"), s_dur_ms))

        matched.sort(key=lambda x: -x[0])
        for pct, name, dur in matched[:3]:
            lines.append(
                f"  \u2192 {name} ({dur:.2f}ms) \u5360\u6b64\u5e27{pct:.1f}%"
            )

    return "\n".join(lines) if len(lines) > 1 else ""


# ---------------------------------------------------------------------------
# Helper 5: CPU hotspots
# ---------------------------------------------------------------------------

def _identify_cpu_hotspots(data: dict) -> str:
    """Identify user-process threads with high CPU usage."""
    cpu = data.get("cpu_usage") or {}
    top_procs = cpu.get("top_processes") or []
    if not top_procs:
        return ""

    lines = ["[CPU热点]"]
    cpu_pct_total = cpu.get("cpu_usage_pct", 0)
    num_cpus = cpu.get("num_cpus", 0)
    if cpu_pct_total > 0:
        cores_suffix = f" ({num_cpus}\u6838)" if num_cpus else ""
        lines.append(f"  \u603bCPU: {cpu_pct_total:.1f}%{cores_suffix}")

    for proc in top_procs[:3]:
        proc_name = proc.get("name", "?")
        proc_cpu = proc.get("cpu_pct", 0)
        threads = proc.get("threads") or []

        hot_threads = [t for t in threads if t.get("cpu_pct", 0) > 5]
        if not hot_threads and proc_cpu < 5:
            continue

        lines.append(f"  {proc_name} ({proc_cpu:.1f}%):")
        for t in sorted(hot_threads, key=lambda x: -x.get("cpu_pct", 0))[:5]:
            lines.append(f"    {t.get('name', '?')}: {t['cpu_pct']:.1f}%")

    return "\n".join(lines) if len(lines) > 1 else ""
