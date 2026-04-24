"""Reporter node: generate final report (pipeline step 4)."""

import logging

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

logger = logging.getLogger(__name__)


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
        logger.debug("trace_path from state: '%s'", trace_path)

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

    print("\n  [reporter] Generating report...", flush=True)  # noqa: LOG — user-facing progress
    if state.get("_trace_path"):
        logger.info("Trace file: %s", state['_trace_path'])
    else:
        logger.warning("no trace_path in state")

    user_content = "\n\n".join(user_parts)

    # Token estimation and truncation (CJK: 1 token ≈ 1.5 chars)
    MAX_REPORT_INPUT_TOKENS = get_report_max_tokens()
    estimated_tokens = len(user_content) / 1.5
    debug_log("reporter", f"user_content: {len(user_content)} chars, ~{estimated_tokens:.0f} tokens, max={MAX_REPORT_INPUT_TOKENS}")
    if estimated_tokens > MAX_REPORT_INPUT_TOKENS:
        target_chars = int(MAX_REPORT_INPUT_TOKENS * 1.5)
        if len(user_content) > target_chars:
            # Truncate at paragraph (\n\n) boundaries to avoid cutting
            # mid-table, mid-code-block, or mid-attribution entry
            sections = user_content.split("\n\n")
            truncated: list[str] = []
            total = 0
            for sec in sections:
                if total + len(sec) > target_chars and truncated:
                    break
                truncated.append(sec)
                total += len(sec)
            user_content = "\n\n".join(truncated) + "\n\n[... 数据过长已截断 ...]"
            debug_log("reporter", f"TRUNCATING user_content from {len(user_content)} to ~{total} chars ({len(truncated)}/{len(sections)} sections)")
    debug_log("reporter", f"attribution section: {user_content[-1500:] if len(user_content) > 1500 else user_content}")
    full_content = generate_report(report_prompt, user_content)
    debug_log("reporter", f"LLM output ({len(full_content)} chars): {full_content[:2000]}")
    debug_log("reporter", f"attribution_result JSON: {attribution_result}")

    # Prepend pre-generated header (LLM does not output header per prompt instructions)
    complete_report = (header_md + "\n" + full_content) if perf_json else full_content

    # Save report to file
    report_path = save_report(complete_report)
    if report_path:
        complete_report += f"\n\n---\n报告已保存至: {report_path}"

    # Auto-save analysis result for historical comparison
    try:
        from smartinspector.storage.store import save_analysis_result
        analysis_path = save_analysis_result(
            perf_summary=perf_json,
            perf_analysis=perf_analysis,
            attribution_result=attribution_result,
            trace_path=state.get("_trace_path", ""),
        )
        logger.info("Auto-saved analysis result for comparison: %s", analysis_path)
    except Exception as e:
        logger.debug("Auto-save analysis result failed: %s", e)

    return {
        "messages": [AIMessage(content=complete_report)],
        "perf_summary": perf_json,
        "perf_analysis": perf_analysis,
        "attribution_data": state.get("attribution_data", ""),
        "attribution_result": attribution_result,
    }
