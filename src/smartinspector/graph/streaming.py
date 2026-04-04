"""Streaming graph runner: _stream_run."""

from smartinspector.token_tracker import get_tracker


def _stream_run(graph, state):
    """Run the graph with streaming, printing tokens as they arrive.

    Preserves perf_summary, perf_analysis, attribution_data, attribution_result
    across turns (until /clear).
    """
    last_updates = {}

    print("\nai> ", end="", flush=True)

    for chunk in graph.stream(
        state,
        stream_mode=["updates"],
        version="v2",
    ):
        last_updates = chunk["data"]
        for node_name, node_state in chunk["data"].items():
            if node_name in ("android_expert", "perf_analyzer", "explorer", "collector",
                             "analyzer", "attributor"):
                pass
            else:
                # fallback, reporter, and other nodes: print AI message content
                for msg in node_state.get("messages", []):
                    content = getattr(msg, "content", "") if not isinstance(msg, dict) else msg.get("content", "")
                    if content:
                        print(content, flush=True)

    # Print token usage summary
    tracker = get_tracker()
    if tracker.total_calls > 0:
        print(f"\n{tracker.summary()}")

    print("\n")

    # Build updated state
    new_messages = list(state.get("messages", []))
    perf_summary = state.get("perf_summary", "")
    perf_analysis = state.get("perf_analysis", "")
    attribution_data = state.get("attribution_data", "")
    attribution_result = state.get("attribution_result", "")
    trace_path = state.get("_trace_path", "")

    for node_name, node_state in last_updates.items():
        node_msgs = node_state.get("messages", [])
        new_messages.extend(node_msgs)

        node_ps = node_state.get("perf_summary", "")
        if node_ps:
            perf_summary = node_ps

        node_pa = node_state.get("perf_analysis", "")
        if node_pa:
            perf_analysis = node_pa

        node_ad = node_state.get("attribution_data", "")
        if node_ad:
            attribution_data = node_ad

        node_ar = node_state.get("attribution_result", "")
        if node_ar:
            attribution_result = node_ar

        node_tp = node_state.get("_trace_path", "")
        if node_tp:
            trace_path = node_tp

    return {
        "messages": new_messages,
        "perf_summary": perf_summary,
        "perf_analysis": perf_analysis,
        "attribution_data": attribution_data,
        "attribution_result": attribution_result,
        "_trace_path": trace_path,
    }
