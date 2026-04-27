"""Deterministic analysis layer — pre-computes arithmetic for LLM.

Instead of sending raw JSON to LLM and asking it to do math,
this module computes severity classification, call-chain distribution,
RV hotspots, jank correlation, and CPU hotspots with pure code.
LLM only needs to organize language around these conclusions.
"""

import json
import statistics


# ---------------------------------------------------------------------------
# SQL Summarizer: compress raw SQL results for LLM consumption
# ---------------------------------------------------------------------------

# Default histogram buckets (milliseconds)
_HIST_BUCKETS = [
    (0, 16, "<16ms"),
    (16, 32, "16-32ms"),
    (32, 64, "32-64ms"),
    (64, float("inf"), ">64ms"),
]


def summarize_sql_result(
    rows: list[dict],
    metric_col: str,
    top_n: int = 10,
    threshold_pct: float = 2.0,
    group_col: str | None = None,
) -> str:
    """Compress SQL query results into a statistical summary + outlier samples.

    Applies four compression strategies:
    1. Statistics: count, min, max, avg, p95, p99
    2. Distribution histogram: bucket values into ranges
    3. Outlier sampling: top N rows exceeding avg * threshold_pct
    4. Dedup aggregation: rows sharing the same group_col key are merged

    Args:
        rows: SQL query result rows.
        metric_col: Name of the numeric column to summarize.
        top_n: Max number of outlier rows to include.
        threshold_pct: Outlier threshold as multiple of the average.
        group_col: Optional column to group/dedup by (e.g. "name").

    Returns:
        Compressed text summary suitable for LLM input.
    """
    if not rows:
        return "[SQL摘要] 无数据"

    # Extract numeric values from metric_col
    values: list[float] = []
    for r in rows:
        v = r.get(metric_col)
        if v is not None:
            try:
                values.append(float(v))
            except (ValueError, TypeError):
                pass

    if not values:
        return f"[SQL摘要] {len(rows)} 行, {metric_col} 列无数值"

    lines: list[str] = []
    count = len(values)
    min_v = min(values)
    max_v = max(values)
    avg_v = sum(values) / count

    # Percentiles
    sorted_vals = sorted(values)
    p95 = sorted_vals[int(count * 0.95)] if count >= 20 else max_v
    p99 = sorted_vals[int(count * 0.99)] if count >= 100 else max_v

    lines.append(
        f"[SQL摘要] {count} 行, "
        f"min={min_v:.2f}, max={max_v:.2f}, avg={avg_v:.2f}, "
        f"p95={p95:.2f}, p99={p99:.2f}"
    )

    # Distribution histogram
    bucket_counts = [0] * len(_HIST_BUCKETS)
    for v in values:
        for i, (lo, hi, _) in enumerate(_HIST_BUCKETS):
            if lo <= v < hi:
                bucket_counts[i] += 1
                break
    hist_parts = [
        f"{label}={cnt}" for (_, _, label), cnt in zip(_HIST_BUCKETS, bucket_counts) if cnt > 0
    ]
    lines.append(f"  分布: {', '.join(hist_parts)}")

    # Dedup aggregation by group_col
    if group_col:
        groups: dict[str, dict] = {}
        for r in rows:
            key = str(r.get(group_col, "?"))
            v = r.get(metric_col, 0)
            try:
                v = float(v)
            except (ValueError, TypeError):
                continue
            if key in groups:
                groups[key]["total"] += v
                groups[key]["count"] += 1
                groups[key]["max"] = max(groups[key]["max"], v)
            else:
                groups[key] = {"total": v, "count": 1, "max": v}

        if groups:
            sorted_groups = sorted(groups.items(), key=lambda x: -x[1]["total"])
            agg_lines = []
            for name, stats in sorted_groups[:10]:
                cnt = stats["count"]
                avg_g = stats["total"] / cnt if cnt > 0 else 0
                cnt_label = f", {cnt}次" if cnt > 1 else ""
                agg_lines.append(f"  {name}: 总{stats['total']:.2f}ms, 最大{stats['max']:.2f}ms{cnt_label}")
            lines.append(f"  聚合 (按{group_col}, top {min(len(sorted_groups), 10)}):")
            lines.extend(agg_lines)

    # Outlier sampling
    threshold = avg_v * threshold_pct
    outliers = [(r, float(r.get(metric_col, 0))) for r in rows
                if _safe_float(r.get(metric_col)) > threshold]
    outliers.sort(key=lambda x: -x[1])

    if outliers:
        lines.append(f"  异常采样 (>{threshold:.2f}ms, top {min(len(outliers), top_n)}):")
        for r, v in outliers[:top_n]:
            # Build a compact representation of the row
            parts = [f"{metric_col}={v:.2f}"]
            for k, val in r.items():
                if k != metric_col and val is not None:
                    parts.append(f"{k}={val}")
            lines.append(f"    {', '.join(parts[:4])}")  # max 4 fields per row

    return "\n".join(lines)


def compress_perf_json(perf_json: str) -> str:
    """Compress large list fields in a perf JSON string using summarize_sql_result.

    Targets the heaviest fields that bloat LLM token usage:
    - view_slices.slowest_slices
    - view_slices.call_chains
    - block_events
    - frame_timeline.jank_detail / slowest_frames
    - cpu_usage.top_processes[].threads
    - thread_state

    Each list is replaced with its summarized text if it exceeds a size threshold.

    Args:
        perf_json: Raw perf summary JSON string.

    Returns:
        JSON string with large lists replaced by compressed summaries.
    """
    try:
        data = json.loads(perf_json)
    except (json.JSONDecodeError, TypeError):
        return perf_json

    modified = False

    # view_slices.slowest_slices
    vs = data.get("view_slices") or {}
    slowest = vs.get("slowest_slices") or []
    if len(slowest) > 20:
        summary = summarize_sql_result(slowest, "dur_ms", top_n=10, group_col="name")
        vs["slowest_slices_summary"] = summary
        vs["slowest_slices"] = slowest[:5]  # keep top 5 raw rows
        modified = True

    # block_events
    block_events = data.get("block_events") or []
    if len(block_events) > 10:
        summary = summarize_sql_result(block_events, "dur_ms", top_n=5, group_col="name")
        data["block_events_summary"] = summary
        data["block_events"] = block_events[:3]
        modified = True

    # frame_timeline jank_detail / slowest_frames
    ft = data.get("frame_timeline") or {}
    for key in ("jank_detail", "slowest_frames"):
        frames = ft.get(key) or []
        if len(frames) > 10:
            summary = summarize_sql_result(frames, "dur_ms", top_n=5)
            ft[f"{key}_summary"] = summary
            ft[key] = frames[:3]
            modified = True

    # cpu_usage top_processes threads
    cpu = data.get("cpu_usage") or {}
    top_procs = cpu.get("top_processes") or []
    for proc in top_procs:
        threads = proc.get("threads") or []
        if len(threads) > 10:
            summary = summarize_sql_result(threads, "cpu_pct", top_n=5, group_col="name")
            proc["threads_summary"] = summary
            proc["threads"] = threads[:3]
            modified = True

    # thread_state
    thread_states = data.get("thread_state") or []
    if len(thread_states) > 10:
        summary = summarize_sql_result(thread_states, "dur_ms", top_n=5, group_col="slice_name")
        data["thread_state_summary"] = summary
        data["thread_state"] = thread_states[:5]
        modified = True

    if not modified:
        return perf_json

    return json.dumps(data, ensure_ascii=False)


def _safe_float(v) -> float:
    """Safely convert a value to float, returning 0 on failure."""
    try:
        return float(v)
    except (ValueError, TypeError):
        return 0.0


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
        _detect_empty_scenario(data),
        _classify_severity(data, frame_budget_ms),
        _compute_call_chain_distribution(data),
        _rank_rv_hotspots(data),
        _correlate_jank_frames(data, frame_budget_ms),
        _identify_cpu_hotspots(data),
        _analyze_thread_state(data),
        _analyze_io_slices(data),
        _analyze_compose_slices(data),
        _analyze_memory(data),
    ]

    return "\n\n".join(s for s in sections if s)


# ---------------------------------------------------------------------------
# Helper 0: Empty scenario detection
# ---------------------------------------------------------------------------

def _detect_empty_scenario(data: dict) -> str:
    """Detect when there is no UI activity (FPS=0, no frames, low CPU)."""
    ft = data.get("frame_timeline") or {}
    fps = ft.get("fps", 0)
    total_frames = ft.get("total_frames", 0)

    cpu = data.get("cpu_usage") or {}
    cpu_pct = cpu.get("cpu_usage_pct", 0)

    if fps == 0 and total_frames == 0 and cpu_pct < 15:
        lines = [
            "[疑似无UI活动]",
            "  FPS为0，总帧数为0，CPU占用低。可能原因：",
            "  1) 应用未启动或未在前台运行",
            "  2) 采集期间未对应用进行操作",
            "  3) target_process 未匹配到目标进程",
        ]
        return "\n".join(lines)

    return ""


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


# ---------------------------------------------------------------------------
# Helper 6: Thread state analysis (Running vs Sleeping vs DiskSleep)
# ---------------------------------------------------------------------------

# blocked_function to human-readable meaning mapping
BLOCKED_FN_MEANING: dict[str, str] = {
    "futex_wait_queue_me": "等待锁释放 (futex)",
    "futex_wait": "等待锁释放 (futex)",
    "folio_wait_bit_common": "等待磁盘IO (页缓存)",
    "wait_woken": "等待被唤醒",
    "msleep": "内核主动睡眠",
    "rpmh_write_batch": "等待硬件资源电源管理",
    "sde_encoder_helper_wait_for_irq": "等待显示硬件中断",
    "spi_geni_transfer_one": "等待SPI总线传输 (通常为触控IC)",
    "do_writepages": "等待磁盘写入",
    "journal_commit": "等待文件系统日志提交",
    "bio_wait": "等待块IO完成",
    "pipe_wait": "等待管道数据",
    "unix_stream_recvmsg": "等待Unix Socket数据",
    "binder_thread_read": "等待Binder IPC回复",
    "worker_thread": "工作线程等待",
    "rcu_gp_fqs_loop": "RCU内核周期",
}


def _analyze_thread_state(data: dict) -> str:
    """Analyze per-slice thread state distribution to distinguish code-slow vs blocked.

    For each SI$ slice with thread_state data, reports whether the thread was
    primarily Running (code is slow) or Sleeping/DiskSleep (blocked by IO/lock).
    When blocked_function data is available, provides human-readable blocking reasons.
    """
    thread_states = data.get("thread_state") or []
    if not thread_states:
        return ""

    lines = ["[线程状态分析]"]

    # Classify slices by dominant state
    blocked_slices = []   # Sleeping/DiskSleep dominant
    running_slices = []   # Running dominant, slow

    for ts in thread_states:
        dominant = ts.get("dominant_state", "unknown")
        dur = ts.get("dur_ms", 0)
        name = ts.get("slice_name", "?")
        dist = ts.get("state_distribution", {})

        if dominant in ("Sleeping", "DiskSleep"):
            blocked_slices.append((name, dominant, dur, dist, ts))
        elif dominant == "Running" and dur > 5:
            running_slices.append((name, dur, dist, ts))

    if blocked_slices:
        lines.append("  以下切片主要处于阻塞状态（非代码慢，而是被IO/锁挂起）：")
        for name, state, dur, dist, ts in sorted(blocked_slices, key=lambda x: -x[2]):
            dist_str = ", ".join(f"{k} {v:.0f}%" for k, v in dist.items())
            # Shorten slice name for readability
            short = name.replace("SI$", "").split("#")[0] if "#" in name else name.replace("SI$", "")
            lines.append(f"    {short} ({dur:.1f}ms): {dist_str}")
            # Show blocking reason if available
            bf = ts.get("blocked_function")
            if bf:
                meaning = BLOCKED_FN_MEANING.get(bf, bf)
                lines.append(f"      阻塞原因: {meaning}")
            if ts.get("io_wait"):
                lines.append("      类型: IO等待")
            if ts.get("waker_name"):
                lines.append(f"      唤醒者: {ts['waker_name']}")

    if running_slices:
        lines.append("  以下切片主要在执行用户代码（无IO/锁阻塞）：")
        for name, dur, dist, ts in sorted(running_slices, key=lambda x: -x[1])[:5]:
            running_pct = dist.get("Running", 0)
            short = name.replace("SI$", "").split("#")[0] if "#" in name else name.replace("SI$", "")
            lines.append(f"    {short} ({dur:.1f}ms): Running {running_pct:.0f}%")

    if not blocked_slices and not running_slices:
        return ""

    return "\n".join(lines)


# ---------------------------------------------------------------------------
# Helper 7: IO slices analysis (network / database / image)
# ---------------------------------------------------------------------------

_IO_TYPE_LABELS: dict[str, str] = {
    "network": "网络IO",
    "database": "数据库IO",
    "image": "图片加载",
}


def _analyze_io_slices(data: dict) -> str:
    """Analyze IO slices: aggregate by type, flag main-thread IO.

    Collects SI$net#/SI$db#/SI$img# slices from the io_slices field,
    groups them by IO type, and reports total/max/count per category.
    """
    io_slices = data.get("io_slices") or {}
    if not io_slices:
        return ""

    summary = io_slices.get("summary") or []
    if not summary:
        return ""

    # Aggregate by IO type
    by_type: dict[str, dict] = {}
    for s in summary:
        io_type = s.get("io_type", "unknown")
        if io_type not in by_type:
            by_type[io_type] = {
                "count": 0,
                "total_ms": 0.0,
                "max_ms": 0.0,
                "top_items": [],
            }
        entry = by_type[io_type]
        entry["count"] += s.get("count", 0)
        entry["total_ms"] += s.get("total_ms", 0)
        entry["max_ms"] = max(entry["max_ms"], s.get("max_ms", 0))
        entry["top_items"].append(s)

    lines = ["[IO分析]"]

    for io_type, stats in sorted(by_type.items(), key=lambda x: -x[1]["total_ms"]):
        label = _IO_TYPE_LABELS.get(io_type, io_type)
        lines.append(
            f"  {label}: {stats['count']}次, "
            f"总耗时{stats['total_ms']:.1f}ms, "
            f"最大{stats['max_ms']:.1f}ms"
        )
        # Show top 3 slowest items per type
        top_items = sorted(stats["top_items"], key=lambda x: -x.get("max_ms", 0))[:3]
        for item in top_items:
            name = item.get("name", "?")
            # Shorten: SI$net#com.example.ApiClient.execute → ApiClient.execute
            short = name.replace("SI$", "")
            for prefix in ("net#", "db#", "img#"):
                if short.startswith(prefix):
                    short = short[len(prefix):]
                    break
            count = item.get("count", 0)
            max_ms = item.get("max_ms", 0)
            total_ms = item.get("total_ms", 0)
            lines.append(
                f"    → {short}: {count}次, "
                f"最大{max_ms:.1f}ms, "
                f"总{total_ms:.1f}ms"
            )

    total_count = io_slices.get("total_count", 0)
    if total_count > 0:
        lines.append(f"  IO操作总计: {total_count}次")

    return "\n".join(lines)


# ---------------------------------------------------------------------------
# Helper 8: Compose recomposition analysis
# ---------------------------------------------------------------------------

def _analyze_compose_slices(data: dict) -> str:
    """Analyze Jetpack Compose recomposition data.

    Reports composables with excessive recompositions and high duration,
    highlighting first-composition vs recomposition counts.
    """
    compose_slices = data.get("compose_slices") or {}
    if not compose_slices:
        return ""

    composables = compose_slices.get("composables") or []
    if not composables:
        return ""

    lines = ["[Compose重组分析]"]

    # Filter to composables with significant recompositions or duration
    significant = [
        c for c in composables
        if c.get("recompose_count", 0) > 0 or c.get("total_ms", 0) > 1.0
    ]

    if not significant:
        return ""

    # Sort by total_ms descending
    significant.sort(key=lambda x: -x.get("total_ms", 0))

    for c in significant[:10]:
        name = c.get("name", "?")
        first = c.get("first_count", 0)
        recompose = c.get("recompose_count", 0)
        total_ms = c.get("total_ms", 0)
        max_ms = c.get("max_ms", 0)

        parts = [f"  {name}:"]
        parts.append(f"首次组合{first}次")
        if recompose > 0:
            parts.append(f"重组{recompose}次")
        parts.append(f"总耗时{total_ms:.1f}ms")
        parts.append(f"最大{max_ms:.1f}ms")
        lines.append(" ".join(parts))

        # Flag excessive recompositions (more than 3 recompositions per first composition)
        if first > 0 and recompose / first > 3:
            lines.append(f"    ⚠ 重组率过高: {recompose}/{first} = {recompose/first:.1f}x, 建议检查state稳定性")

    total_count = compose_slices.get("total_count", 0)
    if total_count > 0:
        lines.append(f"  Compose操作总计: {total_count}次")

    return "\n".join(lines)


# ---------------------------------------------------------------------------
# Helper 9: Memory allocation analysis
# ---------------------------------------------------------------------------

def _analyze_memory(data: dict) -> str:
    """Analyze heap memory allocation and detect potential leaks.

    Reports top heap objects by size, leak suspects (destroyed Activities/Fragments
    still in heap), and memory growth anomalies.
    """
    memory = data.get("memory")
    if not memory:
        return ""

    lines = ["[内存分配分析]"]

    # Top heap objects
    heap_objects = memory.get("heap_objects") or memory.get("heap_graph_classes") or []
    if heap_objects:
        lines.append("  堆内存Top对象:")
        for obj in heap_objects[:10]:
            name = obj.get("class_name", "?")
            count = obj.get("obj_count", 0)
            size_kb = obj.get("total_size_kb", 0)
            # Shorten class name for readability
            short = name.rsplit(".", 1)[-1] if "." in name else name
            lines.append(f"    {short}: {count}个, {size_kb:.1f}KB")

    # Leak suspects
    leak_suspects = memory.get("leak_suspects") or []
    if leak_suspects:
        lines.append("  潜在泄漏:")
        for suspect in leak_suspects[:5]:
            name = suspect.get("class_name", "?")
            count = suspect.get("obj_count", 0)
            size_kb = suspect.get("total_size_kb", 0)
            short = name.rsplit(".", 1)[-1] if "." in name else name
            lines.append(f"    ⚠ {short}: {count}个实例, {size_kb:.1f}KB")

    # Process memory trend
    proc_mem = data.get("process_memory") or {}
    processes = proc_mem.get("processes", [])
    if processes:
        from smartinspector.collector.memory import analyze_memory_trend
        trend = analyze_memory_trend(proc_mem)
        trend_procs = trend.get("processes", [])
        for p in trend_procs:
            if p.get("anomaly") or p.get("high_anon"):
                peak = p.get("peak_rss_mb", 0)
                name = p.get("name", "?")
                lines.append(f"  {name}: 峰值{peak:.0f}MB")
                if p.get("anomaly"):
                    lines.append(f"    {p['anomaly']}")
                if p.get("high_anon"):
                    lines.append(f"    {p['high_anon']}")

    if len(lines) <= 1:
        return ""

    return "\n".join(lines)
