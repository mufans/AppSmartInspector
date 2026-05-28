"""Perf Analyzer: single-shot LLM call to interpret performance summaries."""

import json
import threading

from langchain_openai import ChatOpenAI

from smartinspector.config import get_llm_kwargs
from smartinspector.debug_log import info_log
from smartinspector.prompts import load_prompt, load_skills_for_dimensions
from smartinspector.token_tracker import get_tracker

_base_prompt = load_prompt("perf-analyzer")
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


def analyze_perf(perf_json: str) -> str:
    """Run a single-shot LLM analysis on a performance summary JSON.

    Uses deterministic pre-computation for arithmetic and threshold
    classification, then asks LLM to organize language around those
    conclusions. Applies SQL summarization to compress large data
    and verification to ensure output quality.

    Args:
        perf_json: JSON string from Android Expert or other collector.

    Returns:
        Structured problem list in Chinese.
    """
    from smartinspector.agents.deterministic import compute_hints, compress_perf_json
    from smartinspector.agents.verifier import verify_analysis

    hints = compute_hints(perf_json)

    # Compress large list fields in perf_json to reduce token usage
    compressed_json = compress_perf_json(perf_json)

    # Load dimension skills on-demand based on actual trace data
    dim_skills = load_skills_for_dimensions(perf_json)
    system_prompt = _base_prompt + dim_skills if dim_skills else _base_prompt

    llm = _get_llm()
    user_content = (
        "以下是预计算的分析结论，请据此组织最终报告：\n\n"
        f"{hints}\n\n"
        f"原始数据参考:\n```json\n{compressed_json[:3000]}\n```"
    )
    from langchain_core.messages import HumanMessage, SystemMessage
    response = llm.invoke([
        SystemMessage(content=system_prompt),
        HumanMessage(content=user_content),
    ])
    get_tracker().record_from_message("perf_analyzer", response)

    result = response.content

    # Verify analysis quality
    verification = verify_analysis(result, hints)
    if not verification.passed:
        info_log("perf_analyzer",
            f"WARNING: Analysis verification issues: {'; '.join(verification.issues)} (score={verification.score:.2f})"
        )
        if verification.warnings:
            for w in verification.warnings:
                info_log("perf_analyzer", f"WARNING:   {w}")

        # If L2 failed, retry once with additional context
        if not verification.l2_passed:
            missing = "\n".join(f"- {i}" for i in verification.issues if "[L2]" in i)
            retry_content = (
                f"{user_content}\n\n"
                "## 验证反馈\n"
                "上次分析存在以下遗漏，请补充：\n"
                f"{missing}\n\n"
                "请在分析中明确覆盖以上遗漏项。"
            )
            retry_response = llm.invoke([
                SystemMessage(content=system_prompt),
                HumanMessage(content=retry_content),
            ])
            get_tracker().record_from_message("perf_analyzer_retry", retry_response)
            result = retry_response.content

    return result
