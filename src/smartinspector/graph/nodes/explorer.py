"""Explorer node: source code search agent."""

from smartinspector.agents.explorer import get_explorer_graph
from smartinspector.graph.state import AgentState, _pass_through, node_error_handler


@node_error_handler("explorer")
def explorer_node(state: AgentState) -> dict:
    """Run the code explorer agent."""
    explorer = get_explorer_graph()
    result = explorer.invoke({"messages": state["messages"]})
    return {
        "messages": result.get("messages", []),
        **_pass_through(state),
    }
