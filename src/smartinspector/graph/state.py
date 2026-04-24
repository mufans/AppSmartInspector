"""AgentState definition and state helpers."""

from enum import Enum
from typing import Annotated, TypedDict
import functools
import operator


class RouteDecision(str, Enum):
    """Routing decisions returned by the orchestrator node.

    Stored in ``AgentState["_route"]``.  Extends ``str`` so that
    LangGraph conditional-edge mappings (which expect plain strings)
    continue to work without any ``.value`` conversion.
    """
    FULL_ANALYSIS = "full_analysis"
    STARTUP = "startup"          # cold start analysis: collector → startup_analyzer
    ANDROID = "android"
    ANALYZE = "analyze"
    EXPLORER = "explorer"
    END = "end"
    TRACE = "trace"              # /trace command: collector → analyzer
    QUICK = "quick"              # /quick command: deterministic, no LLM


class AgentState(TypedDict):
    """Shared state flowing through the graph."""
    messages: Annotated[list, operator.add]
    perf_summary: str            # JSON: PerfettoCollector summary
    perf_analysis: str           # Markdown: LLM performance analysis
    attribution_data: str        # JSON: list of attributable SI$ slices
    attribution_result: str      # JSON: list of attribution results with source snippets
    trace_duration_ms: int       # CLI override: trace duration in ms
    trace_target_process: str    # CLI override: target process name
    skip_wait: bool              # CLI flag: skip waiting for app connection (for startup profiling)
    _route: str                  # internal: RouteDecision value (orchestrator routing)
    _trace_path: str             # internal: trace file path from collector


# State keys that every node must pass through unchanged.
_PASS_THROUGH_KEYS = (
    "perf_summary", "perf_analysis", "attribution_data", "attribution_result",
)


def _pass_through(state: AgentState, *, extra_keys: tuple = ()) -> dict:
    """Build a dict of pass-through fields from *state*.

    Every node returns a partial state update.  Fields that the node does
    not modify must still be forwarded so LangGraph merges them correctly.

    Usage::

        return {
            "messages": [...],
            "my_field": new_value,
            **_pass_through(state),
        }
    """
    keys = _PASS_THROUGH_KEYS + extra_keys
    return {k: state.get(k, "") for k in keys}


def node_error_handler(node_name: str):
    """Decorator for graph nodes: catch unhandled exceptions and return safe state.

    Usage::

        @node_error_handler("my_node")
        def my_node(state: AgentState) -> dict:
            ...
    """
    def decorator(func):
        @functools.wraps(func)
        def wrapper(state: AgentState) -> dict:
            try:
                return func(state)
            except Exception as e:
                from langchain_core.messages import AIMessage
                print(f"  [{node_name}] ERROR: {e}", flush=True)
                return {
                    "messages": [AIMessage(content=f"[{node_name}] Error: {e}")],
                    **_pass_through(state),
                }
        return wrapper
    return decorator
