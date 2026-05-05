"""Reporter sub-module: LLM report generation with streaming."""

from langchain_core.messages import SystemMessage, HumanMessage

from smartinspector.debug_log import info_log
from smartinspector.token_tracker import get_tracker


def generate_report(report_prompt: str, user_content: str) -> str:
    """Generate the report via LLM with streaming and retry.

    Returns the LLM-generated report text (without header).
    """
    from smartinspector.graph.nodes.orchestrator import _get_route_llm

    llm = _get_route_llm()

    messages = [
        SystemMessage(content=report_prompt),
        HumanMessage(content=user_content),
    ]

    # Stream with retry: fall back to non-streaming if stream breaks
    full_content = ""
    input_tokens = 0
    try:
        for chunk in llm.stream(messages):
            token = chunk.content
            if token:
                print(token, end="", flush=True)  # noqa: LOG — streaming LLM tokens to user
                full_content += token
            um = getattr(chunk, "usage_metadata", None)
            if um:
                input_tokens = um.get("input_tokens", 0)
    except Exception as e:
        # Stream failed (network error, API disconnect) — retry with invoke
        info_log("reporter", f"WARNING: Stream interrupted ({e}), retrying...")
        try:
            response = llm.invoke(messages)
            full_content = response.content
            get_tracker().record_from_message("reporter", response)
        except Exception as e2:
            full_content = full_content or f"[reporter] Report generation failed: {e2}"
            info_log("reporter", f"ERROR: Retry also failed: {e2}")

    # Record token usage (estimate output from content length if metadata incomplete)
    output_tokens = len(full_content) // 3  # rough estimate for CJK text
    get_tracker().record("reporter", {"input_tokens": input_tokens, "output_tokens": output_tokens})

    print("\n  [reporter] Report generated", flush=True)  # noqa: LOG — user-facing progress

    return full_content
