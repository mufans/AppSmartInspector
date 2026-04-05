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
    last_updates = {}

    print("\nai> ", end="", flush=True)

    try:
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
    except Exception as e:
        print(f"\n  [stream error] {e}")

    # Print token usage summary
    tracker = get_tracker()
    if tracker.total_calls > 0:
        print(f"\n{tracker.summary()}")

    print("\n")

    # Merge all node outputs into state
    merged = dict(state)
    for node_name, node_state in last_updates.items():
        merged = _merge_state(merged, node_state)

    return merged
