"""Android expert node: Perfetto trace tools with streaming."""

from langchain_core.messages import AIMessage

from smartinspector.agents.android import get_android_agent
from smartinspector.graph.state import AgentState, _pass_through
from smartinspector.token_tracker import get_tracker


def android_expert_node(state: AgentState) -> dict:
    """Run the Android expert agent with streaming tool/token output."""
    agent = get_android_agent()

    tool_calls_seen = set()
    all_messages = []

    for event in agent.stream(
        {"messages": state["messages"]},
        stream_mode=["messages", "updates"],
        version="v2",
    ):
        kind = event.get("type")

        if kind == "messages":
            msg, _ = event["data"]
            if hasattr(msg, "content") and msg.content and isinstance(msg.content, str):
                print(msg.content, end="", flush=True)

        elif kind == "updates":
            for node_name, node_output in event["data"].items():
                msgs = node_output.get("messages", [])
                all_messages.extend(msgs)

                if node_name == "tools":
                    for tm in msgs:
                        tc_id = getattr(tm, "tool_call_id", None)
                        if tc_id and tc_id not in tool_calls_seen:
                            tool_calls_seen.add(tc_id)
                            name = getattr(tm, "name", "tool")
                            content = getattr(tm, "content", "")
                            preview = content[:120].replace("\n", " ")
                            print(f"\n  [tool: {name}] {preview}...", flush=True)
                            print("  ", end="", flush=True)

    print(flush=True)

    if not all_messages:
        result = agent.invoke({"messages": state["messages"]})
        all_messages = result.get("messages", [])

    # Record token usage
    get_tracker().record_from_messages("android_expert", all_messages)

    perf_summary = ""
    if all_messages:
        # Find perf_summary from analyze_perfetto tool output (ToolMessage),
        # not from the final AI summary which is markdown text.
        for msg in all_messages:
            msg_type = getattr(msg, "type", "")
            name = getattr(msg, "name", "")
            content = getattr(msg, "content", "")
            if msg_type == "tool" and name == "analyze_perfetto" and content:
                if '"scheduling"' in content or '"cpu_hotspots"' in content:
                    perf_summary = content
                    break
        # Fallback: check last message for direct JSON
        if not perf_summary:
            last = all_messages[-1]
            content = getattr(last, "content", "")
            if '"scheduling"' in content or '"cpu_hotspots"' in content:
                perf_summary = content

    if perf_summary:
        print("\n  [trace data collected, proceeding to analysis & attribution...]", flush=True)

    return {
        "messages": all_messages,
        "perf_summary": perf_summary,
        "perf_analysis": state.get("perf_analysis", ""),
        "attribution_data": state.get("attribution_data", ""),
        "attribution_result": state.get("attribution_result", ""),
    }
