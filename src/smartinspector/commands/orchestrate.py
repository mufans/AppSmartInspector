"""Orchestration commands: /full, /report."""

import json
import datetime


def _build_report_header(perf_json: str, trace_path: str = "") -> str:
    """Build pre-formatted report header tables with exact metric values.

    Shared by reporter_node (graph.py).
    Returns markdown string with 测试概要 + 性能总览 + 热点线程 + 内存详情.
    """
    try:
        perf_data = json.loads(perf_json) if isinstance(perf_json, str) else perf_json
    except Exception:
        return ""

    cpu_usage = perf_data.get("cpu_usage", {})
    proc_mem = perf_data.get("process_memory", {})
    ft = perf_data.get("frame_timeline", {})
    metadata = perf_data.get("metadata", {})

    # ── Metadata ──
    trace_dur_ms = cpu_usage.get("trace_dur_ms", 0)
    pkg = metadata.get("trace_name", "")
    if not pkg:
        sched = perf_data.get("scheduling", {})
        pkg = sched.get("package", "")

    # ── CPU ──
    cpu_peak = cpu_usage.get("cpu_usage_pct", 0)

    # ── Memory ──
    mem_processes = proc_mem.get("processes", [])
    target_mem_mb = 0.0
    if mem_processes:
        target = mem_processes[0]
        target_mem_mb = target.get("rss_kb", 0) / 1024

    # ── FPS ──
    avg_fps = ft.get("fps", 0) if ft else 0
    total_frames = ft.get("total_frames", 0) if ft else 0
    jank_frames = ft.get("jank_frames", 0) if ft else 0
    lowest_fps = 0.0
    if ft and avg_fps > 0:
        slowest_frames = ft.get("slowest_frames", [])
        if slowest_frames:
            worst_dur_ms = slowest_frames[0].get("dur_ms", 0)
            if worst_dur_ms > 0:
                lowest_fps = round(1000.0 / worst_dur_ms, 1)

    # ── Evaluations ──
    fps_eval = "优" if avg_fps >= 58 else ("良" if avg_fps >= 50 else "差")
    cpu_eval = "优" if cpu_peak < 30 else ("良" if cpu_peak < 60 else "差")
    mem_eval = "优" if target_mem_mb < 200 else ("良" if target_mem_mb < 500 else "差")

    # ── Build header markdown ──
    h = "# 性能测试报告\n\n"
    h += "## 测试概要\n\n"
    h += "| 项目 | 内容 |\n|------|------|\n"
    h += f"| 应用 | {pkg or '--'} |\n"
    h += f"| 时长 | {trace_dur_ms/1000:.1f}s |\n" if trace_dur_ms else "| 时长 | -- |\n"
    h += f"| 日期 | {datetime.datetime.now().strftime('%Y-%m-%d %H:%M')} |\n"
    if trace_path:
        h += f"| Trace | `{trace_path}` |\n"
    h += "\n---\n\n"
    h += "## 性能总览\n\n"
    h += "| 指标 | 数值 | 评价 |\n|------|------|------|\n"
    h += f"| 平均 FPS | {avg_fps:.1f} | {fps_eval} |\n"
    h += f"| 最低 FPS | {lowest_fps:.1f} | |\n" if lowest_fps > 0 else "| 最低 FPS | -- | |\n"
    h += f"| 卡顿次数 | {jank_frames} | |\n"
    h += f"| CPU 峰值 | {cpu_peak:.1f}% | {cpu_eval} |\n"
    h += f"| 内存峰值 RSS | {target_mem_mb:.0f}MB | {mem_eval} |\n" if target_mem_mb > 0 else "| 内存峰值 RSS | -- | |\n"
    h += "\n---\n"

    # ── Hot threads ──
    top_procs = cpu_usage.get("top_processes", [])
    if top_procs:
        h += "\n## 热点线程\n\n"
        for proc in top_procs[:3]:
            h += f"- **{proc['process']}** ({proc.get('cpu_pct', 0):.1f}%)\n"
            for t in proc.get("threads", [])[:5]:
                h += f"  - {t['name']}: {t['cpu_pct']:.1f}% ({t.get('switches', 0)}次调度)\n"
        h += "\n"

    # ── Memory detail ──
    if mem_processes:
        h += "\n## 内存使用详情\n\n"
        for p in mem_processes[:5]:
            rss = p.get("rss_kb", 0)
            anon = p.get("rss_anon_kb", 0)
            avg_rss = p.get("avg_rss_kb", 0)
            name = p.get("name", "?")
            h += f"- **{name}**: 峰值RSS {rss/1024:.0f}MB, 平均RSS {avg_rss/1024:.0f}MB, 匿名 {anon/1024:.0f}MB\n"
        h += "\n"

    # ── Slowest frames ──
    if ft and total_frames > 0:
        h += "\n## 帧时间线\n\n"
        h += f"FPS: {avg_fps:.1f}, 总帧数: {total_frames}, 卡顿帧: {jank_frames}\n"
        jank_types = ft.get("jank_types", [])
        if jank_types:
            h += f"卡顿类型: {', '.join(jank_types)}\n"
        slowest = ft.get("slowest_frames", [])
        if slowest:
            h += "\n最慢帧 (Top 5):\n"
            for f in slowest[:5]:
                idx = f.get("frame_index", "?")
                dur = f.get("dur_ms", 0)
                jts = ", ".join(f.get("jank_types", []))
                h += f"  帧#{idx}: {dur:.1f}ms" + (f" [{jts}]" if jts else "") + "\n"
        h += "\n"

    return h


def cmd_full(args: str, state: dict) -> dict:
    """Full flow: trace -> analyze -> attribute -> report.

    Reuses the LangGraph pipeline (collector → analyzer → attributor → reporter)
    by injecting a synthetic message that routes to the collector node.

    Usage: /full [duration_ms] [package_name]
    """
    from smartinspector.graph import create_graph, _stream_run

    # Always create a fresh graph — avoids module-level state issues
    # when running as `python graph.py` (__main__ vs smartinspector.graph)
    graph = create_graph()

    # Add synthetic user message to trigger full_analysis routing
    state["messages"] = state.get("messages", []) + [
        {"role": "user", "content": "请进行全面性能分析"},
    ]

    return _stream_run(graph, state)


def cmd_report(args: str, state: dict) -> dict:
    """Generate a performance report from collected data.

    Usage: /report [output_path]
    """
    perf_json = state.get("perf_summary", "")
    analysis = state.get("perf_analysis", "")
    attribution_result = state.get("attribution_result", "")
    trace_path = state.get("_trace_path", "")

    if not perf_json and not analysis:
        print("No data available. Use /trace or /full first.")
        return state

    # Build full report markdown
    report_parts = []

    # Header with metrics table
    if perf_json:
        header = _build_report_header(perf_json, trace_path)
        if header:
            report_parts.append(header)

    # Analysis section
    if analysis:
        report_parts.append(f"## Analysis\n\n{analysis}")

    # Attribution section
    if attribution_result:
        try:
            results = json.loads(attribution_result)
            found = [r for r in results if r.get("attributable")]
            if found:
                attr_section = "## Source Attribution\n\n"
                for r in found:
                    attr_section += f"- `{r['class_name']}.{r['method_name']}` ({r['dur_ms']:.2f}ms)\n"
                    attr_section += f"  {r.get('file_path', '?')}:{r.get('line_start', '?')}-{r.get('line_end', '?')}\n"
                report_parts.append(attr_section)
        except Exception:
            pass

    report_md = "\n\n".join(report_parts)

    # Output: file or terminal
    output_path = args.strip()
    if output_path:
        import pathlib
        path = pathlib.Path(output_path)
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(report_md)
        print(f"Report saved to: {path}")
    else:
        print(report_md)

    return state
