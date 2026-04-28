"""Graph construction: create_graph()."""

from langgraph.graph import StateGraph, START, END
from langgraph.checkpoint.memory import MemorySaver

from smartinspector.graph.state import AgentState, RouteDecision
from smartinspector.graph.nodes.orchestrator import (
    orchestrator_node,
    fallback_node,
    route_from_orchestrator,
    route_from_android_expert,
)
from smartinspector.graph.nodes.android import android_expert_node
from smartinspector.graph.nodes.analyzer import perf_analyzer_node, analyzer_node
from smartinspector.graph.nodes.explorer import explorer_node
from smartinspector.graph.nodes.collector import collector_node
from smartinspector.graph.nodes.attributor import attributor_node
from smartinspector.graph.nodes.reporter import reporter_node
from smartinspector.graph.nodes.startup import startup_node
from smartinspector.graph.nodes.metric_qa import metric_qa_node



def _route_from_analyzer(state: AgentState) -> str:
    """After analyzer: TRACE → END; STARTUP → startup; FULL_ANALYSIS → attributor."""
    route = state.get("_route", "")
    if route == RouteDecision.TRACE or route == RouteDecision.TRACE.value:
        return "end"
    if route == RouteDecision.STARTUP or route == RouteDecision.STARTUP.value:
        return "startup"
    return "attributor"


def create_graph():
    """Create the SmartInspector orchestration graph."""
    builder = StateGraph(AgentState)

    # All nodes
    builder.add_node("orchestrator", orchestrator_node)
    builder.add_node("android_expert", android_expert_node)
    builder.add_node("perf_analyzer", perf_analyzer_node)
    builder.add_node("explorer", explorer_node)
    builder.add_node("fallback", fallback_node)
    builder.add_node("startup", startup_node)
    builder.add_node("metric_qa", metric_qa_node)
    # Pipeline nodes
    builder.add_node("collector", collector_node)
    builder.add_node("analyzer", analyzer_node)
    builder.add_node("attributor", attributor_node)
    builder.add_node("reporter", reporter_node)

    # Entry
    builder.add_edge(START, "orchestrator")

    # Orchestrator routing
    builder.add_conditional_edges(
        "orchestrator",
        route_from_orchestrator,
        path_map={
            "android_expert": "android_expert",
            "perf_analyzer": "perf_analyzer",
            "explorer": "explorer",
            "fallback": "fallback",
            "collector": "collector",
            "metric_qa": "metric_qa",
        },
    )

    # Single-path nodes → END
    builder.add_edge("perf_analyzer", END)
    builder.add_edge("explorer", END)
    builder.add_edge("fallback", END)
    builder.add_edge("startup", END)
    builder.add_edge("metric_qa", END)

    # Android expert: if perf_summary detected → continue pipeline, else END
    builder.add_conditional_edges(
        "android_expert",
        route_from_android_expert,
        path_map={
            "analyzer": "analyzer",
            "end": END,
        },
    )

    # collector → analyzer (always)
    builder.add_edge("collector", "analyzer")

    # analyzer → END (trace) / startup / attributor
    builder.add_conditional_edges(
        "analyzer",
        _route_from_analyzer,
        path_map={
            "attributor": "attributor",
            "startup": "startup",
            "end": END,
        },
    )

    # Full pipeline tail: attributor → reporter → END
    builder.add_edge("attributor", "reporter")
    builder.add_edge("reporter", END)

    serde = MemorySaver().serde.with_msgpack_allowlist(
        [("smartinspector.graph.state", "RouteDecision")],
    )
    return builder.compile(checkpointer=MemorySaver(serde=serde))
