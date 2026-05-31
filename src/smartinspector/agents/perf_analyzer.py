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


def _build_dimension_sections(perf_json: str) -> str:
    """Build structured dimension sections using compute_hint + format_section.

    Uses the dimension registry to produce pre-computed hints and formatted
    markdown tables, which are far more actionable for the LLM than raw JSON.

    Args:
        perf_json: Compressed perf JSON string.

    Returns:
        Concatenated markdown sections for all dimensions with data.
    """
    try:
        data = json.loads(perf_json)
    except (json.JSONDecodeError, TypeError):
        return ""

    dimensions_data = data.get("dimensions", {})
    if not dimensions_data:
        return ""

    from smartinspector.collector.dimensions import DimensionRegistry, HintContext
    from smartinspector.agents.deterministic import _detect_frame_budget_ms

    DimensionRegistry.discover()
    frame_budget_ms = _detect_frame_budget_ms(data)
    context = HintContext(
        frame_budget_ms=frame_budget_ms,
        target_process=data.get("metadata", {}).get("target_process", {}).get("name", ""),
    )

    parts: list[str] = []
    for dim in DimensionRegistry.all():
        dim_data = dimensions_data.get(dim.name)
        if not dim_data:
            continue
        sections: list[str] = []
        hint = dim.compute_hint(dim_data, context)
        if hint:
            sections.append(hint)
        section = dim.format_section(dim_data)
        if section:
            sections.append(section)
        if sections:
            parts.append("\n\n".join(sections))

    return "\n\n".join(parts)


def _stream_llm(llm, messages: list) -> tuple[str, int, int]:
    """Stream LLM response to terminal, return (full_content, input_tokens, output_tokens).

    Falls back to non-streaming invoke on stream failure.
    """
    full_content = ""
    input_tokens = 0
    output_tokens = 0
    try:
        for chunk in llm.stream(messages):
            token = chunk.content
            if token:
                print(token, end="", flush=True)  # noqa: LOG — streaming LLM tokens to user
                full_content += token
            um = getattr(chunk, "usage_metadata", None)
            if um:
                input_tokens = um.get("input_tokens", 0)
                if um.get("output_tokens"):
                    output_tokens = um["output_tokens"]
    except Exception as e:
        # Stream interrupted — fallback to invoke
        info_log("perf_analyzer", f"WARNING: Stream interrupted ({e}), retrying...")
        try:
            response = llm.invoke(messages)
            full_content = response.content
            get_tracker().record_from_message("perf_analyzer", response)
        except Exception as e2:
            full_content = full_content or f"[perf_analyzer] Analysis failed: {e2}"
            info_log("perf_analyzer", f"ERROR: Retry also failed: {e2}")

    # Fallback estimate when provider doesn't send output_tokens in stream
    if not output_tokens:
        output_tokens = len(full_content) // 3

    return full_content, input_tokens, output_tokens


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
    dim_skills = load_skills_for_dimensions(perf_json, agent_role="analyzer")
    system_prompt = _base_prompt + dim_skills if dim_skills else _base_prompt

    llm = _get_llm()

    # Build structured dimension sections using compute_hint + format_section.
    # This gives the LLM pre-computed severity conclusions and formatted tables,
    # which are far more actionable than raw JSON.
    dim_sections = _build_dimension_sections(compressed_json)

    user_content = (
        "以下是预计算的分析结论，请据此组织最终报告：\n\n"
        f"{hints}\n\n"
    )

    # Insert dimension sections BEFORE raw JSON so LLM sees structured data first
    if dim_sections:
        user_content += f"{dim_sections}\n\n"

    user_content += f"原始数据参考:\n```json\n{compressed_json[:3000]}\n```"

    from langchain_core.messages import HumanMessage, SystemMessage
    messages = [
        SystemMessage(content=system_prompt),
        HumanMessage(content=user_content),
    ]

    print("  [analyzer] 分析性能数据...", flush=True)  # noqa: LOG — user-facing progress
    result, input_tokens, output_tokens = _stream_llm(llm, messages)
    get_tracker().record("perf_analyzer", {"input_tokens": input_tokens, "output_tokens": output_tokens})

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
            print("\n  [analyzer] 补充遗漏项...", flush=True)  # noqa: LOG — user-facing progress
            retry_messages = [
                SystemMessage(content=system_prompt),
                HumanMessage(content=retry_content),
            ]
            result, retry_in, retry_out = _stream_llm(llm, retry_messages)
            get_tracker().record("perf_analyzer_retry", {"input_tokens": retry_in, "output_tokens": retry_out})

    print()  # newline after streaming output  # noqa: LOG — user-facing progress
    return result
