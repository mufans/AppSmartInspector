"""Analyzer nodes: perf_analyzer_node and analyzer_node."""

import logging

from langchain_core.messages import AIMessage

from smartinspector.agents.perf_analyzer import analyze_perf
from smartinspector.graph.state import AgentState, node_error_handler

logger = logging.getLogger(__name__)


@node_error_handler("perf_analyzer")
def perf_analyzer_node(state: AgentState) -> dict:
    """Run single-shot perf analysis on the JSON summary."""
    perf_json = state.get("perf_summary", "")
    if not perf_json:
        for msg in reversed(state.get("messages", [])):
            content = getattr(msg, "content", "")
            if '"scheduling"' in content or '"cpu_hotspots"' in content:
                perf_json = content
                break

    if not perf_json:
        return {
            "messages": [AIMessage(content="未找到性能摘要数据，无法进行分析。")],
            "perf_summary": "",
            "perf_analysis": "",
            "attribution_data": "",
            "attribution_result": "",
        }

    analysis = analyze_perf(perf_json)

    return {
        "messages": [AIMessage(content=analysis)],
        "perf_summary": perf_json,
        "perf_analysis": analysis,
        "attribution_data": state.get("attribution_data", ""),
        "attribution_result": state.get("attribution_result", ""),
    }


@node_error_handler("analyzer")
def analyzer_node(state: AgentState) -> dict:
    """Analyze perf summary JSON with LLM."""
    perf_json = state.get("perf_summary", "")
    if not perf_json:
        return {
            "messages": [AIMessage(content="[analyzer] No perf data to analyze")],
            "perf_summary": "",
            "perf_analysis": "",
            "attribution_data": "",
            "attribution_result": "",
            "_trace_path": state.get("_trace_path", ""),
        }

    logger.info("Analyzing performance...")
    analysis = analyze_perf(perf_json)
    logger.info("Analysis complete (%d chars)", len(analysis))

    return {
        "messages": [AIMessage(content=analysis)],
        "perf_summary": perf_json,
        "perf_analysis": analysis,
        "attribution_data": "",
        "attribution_result": "",
        "_trace_path": state.get("_trace_path", ""),
    }
