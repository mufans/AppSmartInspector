"""Perf Analyzer: single-shot LLM call to interpret performance summaries."""

import json
import threading

from langchain_openai import ChatOpenAI

from smartinspector.config import get_llm_kwargs
from smartinspector.prompts import load_prompt
from smartinspector.token_tracker import get_tracker

_prompt = load_prompt("perf-analyzer")
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
    conclusions.

    Args:
        perf_json: JSON string from Android Expert or other collector.

    Returns:
        Structured problem list in Chinese.
    """
    from smartinspector.agents.deterministic import compute_hints

    hints = compute_hints(perf_json)

    llm = _get_llm()
    user_content = (
        "以下是预计算的分析结论，请据此组织最终报告：\n\n"
        f"{hints}\n\n"
        f"原始数据参考:\n```json\n{perf_json[:3000]}\n```"
    )
    from langchain_core.messages import HumanMessage, SystemMessage
    response = llm.invoke([
        SystemMessage(content=_prompt),
        HumanMessage(content=user_content),
    ])
    get_tracker().record_from_message("perf_analyzer", response)
    return response.content
