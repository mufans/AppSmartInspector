"""Smart quick analysis command: /quick — deterministic analysis without LLM."""

import json
import logging

from smartinspector.collector.perfetto import PerfettoCollector
from smartinspector.agents.deterministic import compute_hints
from smartinspector.commands.attribution import extract_attributable_slices
from smartinspector.commands.orchestrate import _build_report_header
from smartinspector.storage.store import save_analysis_result

logger = logging.getLogger(__name__)


def cmd_quick(args: str, state: dict) -> dict:
    """Run a fast, deterministic performance analysis without LLM calls.

    Pure computation pipeline: collector → deterministic hints → fast-path
    attribution → formatted report. No LLM API calls, suitable for quick
    feedback during development.

    Usage:
        /quick <trace.pb>          — analyze an existing trace file
        /quick                     — analyze the last recorded trace

    The output is a markdown report with pre-computed conclusions,
    severity classification, and fast-path source attribution.
    """
    trace_path = args.strip() or state.get("_trace_path", "")
    if not trace_path:
        print("Usage: /quick <trace.pb>")
        print("  Or use /trace or /record first to load a trace.")
        return state

    print(f"[quick] Running fast analysis on: {trace_path}", flush=True)  # noqa: LOG

    try:
        # 1. Collect data
        print("  [1/4] Collecting trace data...", flush=True)  # noqa: LOG
        target_process = state.get("trace_target_process")
        collector = PerfettoCollector(trace_path, target_process=target_process)
        summary = collector.summarize()
        perf_json = summary.to_json()
        collector.close()

        # 2. Deterministic analysis (no LLM)
        print("  [2/4] Computing deterministic hints...", flush=True)  # noqa: LOG
        hints = compute_hints(perf_json)

        # 3. Fast-path attribution (no LLM search)
        print("  [3/4] Running fast-path attribution...", flush=True)  # noqa: LOG
        attributable = extract_attributable_slices(perf_json)

        # 4. Format report
        print("  [4/4] Formatting report...", flush=True)  # noqa: LOG
        report = _format_quick_report(perf_json, hints, attributable, trace_path)

        # Print the report
        print(report)

        # Update state
        state["perf_summary"] = perf_json
        state["_trace_path"] = trace_path

        # Auto-save for historical comparison
        try:
            save_analysis_result(
                perf_summary=perf_json,
                trace_path=trace_path,
            )
        except Exception as e:
            logger.debug("Quick analysis auto-save failed: %s", e)

    except FileNotFoundError:
        print(f"ERROR: Trace file not found: {trace_path}")
    except Exception as e:
        print(f"ERROR: {e}")
        logger.error("Quick analysis failed: %s", e, exc_info=True)

    return state


def _format_quick_report(
    perf_json: str,
    hints: str,
    attributable: list[dict],
    trace_path: str,
) -> str:
    """Format a quick analysis report from pre-computed data.

    Args:
        perf_json: JSON string from PerfettoCollector.
        hints: Pre-computed deterministic hints string.
        attributable: List of attributable slices.
        trace_path: Path to the trace file.

    Returns:
        Markdown report string.
    """
    try:
        perf_data = json.loads(perf_json)
    except (json.JSONDecodeError, TypeError):
        perf_data = {}

    parts: list[str] = []

    # Header with metrics
    header = _build_report_header(perf_json, trace_path)
    if header:
        parts.append(header)

    # Quick analysis label
    parts.append("## 快速分析报告（确定性分析，无LLM）\n")

    # Deterministic hints
    if hints:
        parts.append(f"### 预计算结论\n\n{hints}")

    # Attribution summary (fast path only)
    if attributable:
        attr_lines = ["### 热点定位（快速路径）\n"]
        for i, entry in enumerate(attributable[:10], 1):
            class_name = entry.get("class_name", "?")
            method_name = entry.get("method_name", "?")
            dur_ms = entry.get("dur_ms", 0)
            search_type = entry.get("search_type", "java")

            attr_lines.append(f"{i}. **{class_name}.{method_name}** ({dur_ms:.2f}ms)")
            if entry.get("count"):
                attr_lines.append(f"   调用{entry['count']}次, 总{entry.get('total_ms', 0):.1f}ms")
            if entry.get("call_context"):
                attr_lines.append(f"   调用链: {entry['call_context']}")
            if search_type == "xml":
                attr_lines.append(f"   类型: XML布局")
            elif entry.get("io_type"):
                attr_lines.append(f"   类型: {entry['io_type']} IO")

        parts.append("\n".join(attr_lines))

        # Summary
        p0_count = sum(1 for e in attributable if e["dur_ms"] > 16.67)
        p1_count = sum(1 for e in attributable if 4 <= e["dur_ms"] <= 16.67)
        parts.append(f"\n**热点统计**: P0({p0_count}个, >16.67ms) P1({p1_count}个, 4-16.67ms) 共{len(attributable)}个")
    else:
        parts.append("### 热点定位\n\n未发现显著性能热点。")

    # Tips
    parts.append(
        "\n> 提示: 这是快速确定性分析，不含LLM深度分析。\n"
        "> 使用 /full 获取包含LLM分析的完整报告。"
    )

    return "\n\n".join(parts)
