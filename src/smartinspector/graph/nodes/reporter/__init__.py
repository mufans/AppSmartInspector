"""Reporter node: generate final report (pipeline step 4)."""

from langchain_core.messages import AIMessage

from smartinspector.config import get_report_max_tokens
from smartinspector.debug_log import debug_log

from smartinspector.graph.state import AgentState
from smartinspector.graph.nodes.reporter.formatter import (
    format_perf_sections,
    format_attribution_section,
)
from smartinspector.graph.nodes.reporter.generator import generate_report
from smartinspector.graph.nodes.reporter.persistence import save_report


def reporter_node(state: AgentState) -> dict:
    """Generate the final performance report using LLM with streaming output."""
    from smartinspector.prompts import load_prompt
    from smartinspector.commands.orchestrate import _build_report_header

    report_prompt = load_prompt("report-generator")

    perf_json = state.get("perf_summary", "")
    perf_analysis = state.get("perf_analysis", "")
    attribution_result = state.get("attribution_result", "")

    # Build user content with all available data
    # IMPORTANT: attribution section MUST come first (before header/analysis)
    # to avoid being truncated when total content exceeds token budget.
    # Attribution data is the core input for problem generation;
    # header is reference data that can survive partial truncation.
    user_parts: list[str] = []

    # Attribution first — highest priority, must not be truncated
    user_parts.extend(format_attribution_section(attribution_result))

    if perf_json:
        user_parts.extend(format_perf_sections(perf_json))

        # Pre-generate report header tables
        trace_path = state.get("_trace_path", "")
        print(f"  [reporter] trace_path from state: '{trace_path}'", flush=True)

        header_md = _build_report_header(perf_json, trace_path)
        # Insert header after attribution and perf sections
        user_parts.append(header_md)

    if perf_analysis:
        user_parts.append(f"## 性能分析\n{perf_analysis}")

    if not user_parts:
        return {
            "messages": [AIMessage(content="[reporter] No data available for report")],
            "perf_summary": perf_json,
            "perf_analysis": perf_analysis,
            "attribution_data": state.get("attribution_data", ""),
            "attribution_result": attribution_result,
        }

    print("\n  [reporter] Generating report...", flush=True)
    if state.get("_trace_path"):
        print(f"  [reporter] Trace file: {state['_trace_path']}", flush=True)
    else:
        print("  [reporter] WARNING: no trace_path in state", flush=True)

    user_content = "\n\n".join(user_parts)

    # Token estimation and truncation (CJK: 1 token ≈ 1.5 chars)
    MAX_REPORT_INPUT_TOKENS = get_report_max_tokens()
    estimated_tokens = len(user_content) / 1.5
    debug_log("reporter", f"user_content: {len(user_content)} chars, ~{estimated_tokens:.0f} tokens, max={MAX_REPORT_INPUT_TOKENS}")
    if estimated_tokens > MAX_REPORT_INPUT_TOKENS:
        target_chars = int(MAX_REPORT_INPUT_TOKENS * 1.5)
        if len(user_content) > target_chars:
            debug_log("reporter", f"TRUNCATING user_content from {len(user_content)} to {target_chars} chars")
            user_content = user_content[:target_chars] + "\n\n[... 数据过长已截断 ...]"
    debug_log("reporter", f"attribution section: {user_content[-1500:] if len(user_content) > 1500 else user_content}")
    full_content = generate_report(report_prompt, user_content)
    debug_log("reporter", f"LLM output ({len(full_content)} chars): {full_content[:2000]}")
    debug_log("reporter", f"attribution_result JSON: {attribution_result}")

    # Prepend pre-generated header (LLM does not output header per prompt instructions)
    complete_report = header_md + "\n" + full_content if perf_json else full_content

    # Save report to file
    report_path = save_report(complete_report)
    if report_path:
        complete_report += f"\n\n---\n报告已保存至: {report_path}"

    return {
        "messages": [AIMessage(content=complete_report)],
        "perf_summary": perf_json,
        "perf_analysis": perf_analysis,
        "attribution_data": state.get("attribution_data", ""),
        "attribution_result": attribution_result,
    }
