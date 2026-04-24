"""Headless runner: non-interactive analysis pipeline via LangGraph."""

import json
import logging
from pathlib import Path

logger = logging.getLogger(__name__)


class HeadlessRunner:
    """Non-interactive analysis runner using the LangGraph pipeline.

    Executes the full analysis pipeline (collect -> analyze -> attribute -> report)
    through the LangGraph graph, following the Pipeline Architecture Rule.
    Supports cmd parameter to select execution path (full_analysis, startup, etc.).
    """

    def __init__(
        self,
        source_dir: str = ".",
        target: str | None = None,
        trace_path: str | None = None,
        output: str | None = None,
        fmt: str = "markdown",
        duration: int = 10000,
        debug: bool = False,
        cmd: str = "full_analysis",
    ) -> None:
        self.source_dir = source_dir
        self.target = target
        self.trace_path = trace_path
        self.output = output
        self.fmt = fmt
        self.duration = duration
        self.debug = debug
        self.cmd = cmd

    def run(self) -> str:
        """Execute the analysis pipeline via LangGraph and return the report.

        Builds initial state and invokes the graph with the selected cmd route.
        """
        from smartinspector.config import set_source_dir
        from smartinspector.graph import create_graph
        from smartinspector.graph.state import RouteDecision

        set_source_dir(self.source_dir)

        if self.debug:
            import os
            os.environ["SI_DEBUG"] = "1"

        # Determine route based on cmd parameter
        route = self._resolve_route(self.cmd)

        # Build initial state for the graph
        initial_state = {
            "messages": [],
            "perf_summary": "",
            "perf_analysis": "",
            "attribution_data": "",
            "attribution_result": "",
            "trace_duration_ms": self.duration,
            "trace_target_process": self.target or "",
            "skip_wait": route in (RouteDecision.STARTUP, RouteDecision.STARTUP.value),
            "_route": route,
            "_trace_path": self.trace_path or "",
        }

        logger.info("Headless run: cmd=%s, route=%s, target=%s, trace=%s",
                     self.cmd, route, self.target, self.trace_path)

        graph = create_graph()
        config = {"configurable": {"thread_id": "headless"}}

        try:
            # Invoke the graph (non-streaming for headless/CI)
            result_state = graph.invoke(initial_state, config=config)
        except Exception as e:
            error_msg = f"Pipeline execution failed: {e}"
            logger.error(error_msg)
            return self._format_error(error_msg)

        # Extract final state values
        final = result_state
        perf_analysis = final.get("perf_analysis", "")
        perf_summary = final.get("perf_summary", "")
        attribution_result = final.get("attribution_result", "")

        # Extract the report from messages (last AI message)
        report = ""
        messages = final.get("messages", [])
        for msg in reversed(messages):
            content = getattr(msg, "content", "") if not isinstance(msg, dict) else msg.get("content", "")
            if content and not content.startswith("["):
                report = content
                break

        # Generate output based on format
        if self.fmt == "json":
            output = self._format_json_output(
                perf_summary, perf_analysis, attribution_result, report,
            )
        else:
            output = report or perf_analysis or self._format_error("No analysis result produced")

        # Write to file if output specified
        if self.output:
            try:
                output_path = Path(self.output)
                output_path.parent.mkdir(parents=True, exist_ok=True)
                output_path.write_text(output, encoding="utf-8")
                logger.info("Report saved to %s", self.output)
            except OSError as e:
                logger.error("Failed to write report: %s", e)

        return output

    def _resolve_route(self, cmd: str) -> str:
        """Map cmd parameter to RouteDecision value."""
        from smartinspector.graph.state import RouteDecision

        cmd_to_route = {
            "full_analysis": RouteDecision.FULL_ANALYSIS,
            "full": RouteDecision.FULL_ANALYSIS,
            "startup": RouteDecision.STARTUP,
            "analyze": RouteDecision.ANALYZE,
            "trace": RouteDecision.TRACE,
        }
        decision = cmd_to_route.get(cmd, RouteDecision.FULL_ANALYSIS)
        return decision if isinstance(decision, str) else decision.value

    def _format_json_output(
        self,
        perf_summary: str,
        perf_analysis: str,
        attribution_result: str,
        report: str,
    ) -> str:
        """Format output as structured JSON."""
        result = {
            "report": report,
            "perf_analysis": perf_analysis,
        }

        if perf_summary:
            try:
                result["perf_summary"] = json.loads(perf_summary)
            except (json.JSONDecodeError, TypeError):
                result["perf_summary"] = perf_summary

        if attribution_result:
            try:
                result["attribution"] = json.loads(attribution_result)
            except (json.JSONDecodeError, TypeError):
                result["attribution"] = attribution_result

        if self.target:
            result["target"] = self.target
        if self.trace_path:
            result["trace_path"] = self.trace_path

        return json.dumps(result, indent=2, ensure_ascii=False)

    def _format_error(self, message: str) -> str:
        """Format error for output."""
        if self.fmt == "json":
            return json.dumps({"error": message}, ensure_ascii=False)
        return f"# Error\n\n{message}"
