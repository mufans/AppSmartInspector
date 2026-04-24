"""Startup analysis node: cold start phase splitting and bottleneck identification."""

import logging

from langchain_core.messages import AIMessage

from smartinspector.debug_log import debug_log
from smartinspector.graph.state import AgentState, _pass_through, node_error_handler

logger = logging.getLogger(__name__)


@node_error_handler("startup")
def startup_node(state: AgentState) -> dict:
    """Analyze cold start performance from the collected trace."""
    from smartinspector.collector.startup import StartupAnalyzer

    trace_path = state.get("_trace_path", "")
    if not trace_path:
        return {
            "messages": [AIMessage(content="[startup] No trace file available for startup analysis")],
            **_pass_through(state),
        }

    target_process = state.get("trace_target_process", "") or None

    logger.info("Running cold start analysis on %s", trace_path)

    analyzer = StartupAnalyzer(trace_path, target_process=target_process)
    result = analyzer.analyze()

    debug_log("startup", f"startup analysis: total_ms={result.total_ms}, phases={len(result.phases)}, bottlenecks={len(result.bottlenecks)}")

    if result.total_ms <= 0:
        report = (
            "## 冷启动分析\n\n"
            "未能检测到冷启动序列。可能原因：\n"
            "1. 采集期间应用未执行冷启动（可能已有进程在运行）\n"
            "2. 目标进程未正确指定\n"
            "3. trace 中缺少启动相关的 SI$ 标签\n\n"
            "建议：使用 `/full --no-wait` 重新采集，并确保应用从完全停止状态启动。"
        )
    else:
        report = result.to_markdown()

    return {
        "messages": [AIMessage(content=report)],
        "perf_summary": state.get("perf_summary", ""),
        "perf_analysis": report,
        "attribution_data": state.get("attribution_data", ""),
        "attribution_result": state.get("attribution_result", ""),
    }
