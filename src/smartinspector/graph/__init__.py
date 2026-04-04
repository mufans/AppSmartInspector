"""SmartInspector orchestration graph — modular package.

Exports:
    create_graph  — build the compiled LangGraph StateGraph
    run_graph     — alias for _stream_run (streaming execution)
    main          — CLI entry point (REPL loop)
"""

from smartinspector.graph.builder import create_graph
from smartinspector.graph.streaming import _stream_run
from smartinspector.graph.cli import main

# Public alias
run_graph = _stream_run

__all__ = ["create_graph", "run_graph", "main", "_stream_run"]
