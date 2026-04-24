"""Headless runner: non-interactive analysis pipeline for CI/automation."""

import json
import logging
import sys
from pathlib import Path

logger = logging.getLogger(__name__)


class HeadlessRunner:
    """Non-interactive analysis runner that bypasses the REPL.

    Executes the full analysis pipeline (collect → analyze → attribute → report)
    and writes results to a file.
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
    ) -> None:
        self.source_dir = source_dir
        self.target = target
        self.trace_path = trace_path
        self.output = output
        self.fmt = fmt
        self.duration = duration
        self.debug = debug

    def run(self) -> str:
        """Execute the analysis pipeline and return the report.

        Returns the report content as a string.
        """
        from smartinspector.config import set_source_dir
        from smartinspector.collector.perfetto import PerfettoCollector
        from smartinspector.agents.deterministic import compute_hints
        from smartinspector.commands.attribution import extract_attributable_slices

        set_source_dir(self.source_dir)

        if self.debug:
            import os
            os.environ["SI_DEBUG"] = "1"

        # Phase 1: Get trace
        if self.trace_path:
            # Analyze existing trace file
            trace_path = self.trace_path
            logger.info("Analyzing existing trace: %s", trace_path)
        else:
            # Collect new trace from device
            logger.info("Collecting trace from device (duration=%dms, target=%s)", self.duration, self.target)
            try:
                trace_path = PerfettoCollector.pull_trace_from_device(
                    duration_ms=self.duration,
                    target_process=self.target,
                )
                logger.info("Trace saved to %s", trace_path)
            except Exception as e:
                error_msg = f"Trace collection failed: {e}"
                logger.error(error_msg)
                return self._format_error(error_msg)

        # Phase 2: Analyze trace
        try:
            collector = PerfettoCollector(trace_path, target_process=self.target)
            summary = collector.summarize()
            perf_json = summary.to_json()
        except Exception as e:
            error_msg = f"Trace analysis failed: {e}"
            logger.error(error_msg)
            return self._format_error(error_msg)

        logger.info("Perf summary: %d bytes", len(perf_json))

        # Phase 3: Deterministic analysis
        hints = compute_hints(perf_json)

        # Phase 4: Attribution
        attributable = extract_attributable_slices(perf_json)
        logger.info("Found %d attributable slices", len(attributable))

        # Phase 5: LLM analysis (if API key available)
        perf_analysis = ""
        from smartinspector.config import get_api_key
        if get_api_key():
            perf_analysis = self._run_llm_analysis(perf_json)
        else:
            logger.warning("No API key configured, skipping LLM analysis")
            perf_analysis = hints

        # Phase 6: Generate report
        if self.fmt == "json":
            report = self._generate_json_report(perf_json, perf_analysis, attributable)
        else:
            report = self._generate_markdown_report(perf_json, perf_analysis, hints, attributable)

        # Write to file if output specified
        if self.output:
            try:
                output_path = Path(self.output)
                output_path.parent.mkdir(parents=True, exist_ok=True)
                output_path.write_text(report, encoding="utf-8")
                logger.info("Report saved to %s", self.output)
            except OSError as e:
                logger.error("Failed to write report: %s", e)

        return report

    def _run_llm_analysis(self, perf_json: str) -> str:
        """Run LLM-based performance analysis."""
        try:
            from smartinspector.graph.nodes.analyzer import perf_analyzer_node
            from smartinspector.graph.state import AgentState

            # Create minimal state for the analyzer
            state: AgentState = {
                "messages": [],
                "perf_summary": perf_json,
                "perf_analysis": "",
                "attribution_data": "",
                "attribution_result": "",
                "trace_duration_ms": self.duration,
                "trace_target_process": self.target or "",
                "skip_wait": True,
                "_route": "full_analysis",
                "_trace_path": self.trace_path or "",
            }

            result = perf_analyzer_node(state)
            return result.get("perf_analysis", "")
        except Exception as e:
            logger.warning("LLM analysis failed: %s", e)
            return ""

    def _generate_json_report(
        self,
        perf_json: str,
        perf_analysis: str,
        attributable: list[dict],
    ) -> str:
        """Generate a structured JSON report."""
        from smartinspector.graph.nodes.reporter.json_formatter import format_json_report
        report = format_json_report(
            perf_json=perf_json,
            perf_analysis=perf_analysis,
            attributable=attributable,
            trace_path=self.trace_path or "",
            target=self.target or "",
        )
        return json.dumps(report, indent=2, ensure_ascii=False)

    def _generate_markdown_report(
        self,
        perf_json: str,
        perf_analysis: str,
        hints: str,
        attributable: list[dict],
    ) -> str:
        """Generate a markdown report."""
        import datetime
        from smartinspector.graph.nodes.reporter.formatter import (
            format_perf_sections,
            format_attribution_section,
        )

        parts = []
        parts.append(f"# SmartInspector Performance Report\n")
        parts.append(f"Generated: {datetime.datetime.now().strftime('%Y-%m-%d %H:%M:%S')}")
        if self.target:
            parts.append(f"Target: {self.target}")
        if self.trace_path:
            parts.append(f"Trace: {self.trace_path}")

        # Perf sections
        sections = format_perf_sections(perf_json)
        parts.extend(sections)

        # Attribution
        attr_json = json.dumps(attributable, ensure_ascii=False)
        attr_sections = format_attribution_section(attr_json)
        parts.extend(attr_sections)

        # Analysis
        if perf_analysis:
            parts.append(f"\n## 性能分析\n{perf_analysis}")

        return "\n\n".join(parts)

    def _format_error(self, message: str) -> str:
        """Format error for output."""
        if self.fmt == "json":
            return json.dumps({"error": message}, ensure_ascii=False)
        return f"# Error\n\n{message}"
