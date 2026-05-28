"""Streaming graph runner: _stream_run."""

from smartinspector.token_tracker import get_tracker
from smartinspector.graph.state import AgentState


def _merge_state(base: dict, updates: dict) -> dict:
    """Merge graph node updates into base state.

    - messages: appended (operator.add semantics)
    - other fields: last non-empty value wins
    """
    result = dict(base)
    for key in AgentState.__annotations__:
        if key not in updates or not updates[key]:
            continue
        if key == "messages":
            result[key] = result.get(key, []) + updates[key]
        else:
            result[key] = updates[key]
    return result


def _stream_run(graph, state):
    """Run the graph with streaming, printing tokens as they arrive.

    Preserves perf_summary, perf_analysis, attribution_data, attribution_result
    across turns (until /clear).
    """
    config = {"configurable": {"thread_id": "main"}}

    print("\nai> ", end="", flush=True)

    try:
        last_updates = {}
        for chunk in graph.stream(
            state,
            config=config,
            stream_mode=["updates"],
            version="v2",
        ):
            last_updates = chunk["data"]
            for node_name, node_state in chunk["data"].items():
                if node_name in ("android_expert", "perf_analyzer", "explorer", "collector",
                                 "analyzer", "attributor", "reporter", "frame_analyzer"):
                    pass
                else:
                    # fallback and other nodes: print AI message content
                    for msg in node_state.get("messages", []):
                        content = getattr(msg, "content", "") if not isinstance(msg, dict) else msg.get("content", "")
                        if content:
                            print(content, flush=True)
    except Exception as e:
        print(f"\n  [stream error] {e}", flush=True)

    # Print token usage summary
    tracker = get_tracker()
    if tracker.total_calls > 0:
        print(f"\n{tracker.summary()}")

    print("\n")

    # Use get_state() to get the final state instead of manual rebuild
    final_state = graph.get_state(config)
    result = {
        "messages": list(state.get("messages", [])) + list(final_state.values.get("messages", [])),
        "perf_summary": final_state.values.get("perf_summary", state.get("perf_summary", "")),
        "perf_analysis": final_state.values.get("perf_analysis", state.get("perf_analysis", "")),
        "attribution_data": final_state.values.get("attribution_data", state.get("attribution_data", "")),
        "attribution_result": final_state.values.get("attribution_result", state.get("attribution_result", "")),
        "_trace_path": final_state.values.get("_trace_path", state.get("_trace_path", "")),
    }
    return result
