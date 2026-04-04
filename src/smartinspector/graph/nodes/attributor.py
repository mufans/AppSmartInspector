"""Attributor node: source code attribution (pipeline step 3)."""

import json

from langchain_core.messages import AIMessage

from smartinspector.agents.attributor import run_attribution
from smartinspector.graph.state import AgentState, _pass_through


def _format_attribution_summary(results: list[dict]) -> str:
    """Format attribution results as a human-readable summary."""
    lines = ["[source attribution results]\n"]

    for r in results:
        if r.get("attributable"):
            fp = r.get("file_path", "?")
            ls = r.get("line_start", "?")
            le = r.get("line_end", "?")
            snippet = r.get("source_snippet", "")
            lines.append(f"  FOUND: {r['class_name']}.{r['method_name']} ({r['dur_ms']:.2f}ms)")
            lines.append(f"    Location: {fp}:{ls}-{le}")
            if snippet:
                lines.append(f"    Finding: {snippet[:200]}")
        else:
            reason = r.get("reason", "unknown")
            lines.append(f"  SYSTEM: {r['class_name']}.{r['method_name']} ({r['dur_ms']:.2f}ms) [{reason}]")

    return "\n".join(lines)


def attributor_node(state: AgentState) -> dict:
    """Extract attributable slices and search source code."""
    from smartinspector.commands.attribution import extract_attributable_slices

    perf_json = state.get("perf_summary", "")
    if not perf_json:
        return {
            "messages": [AIMessage(content="[attributor] No perf data for attribution")],
            **_pass_through(state, extra_keys=("_trace_path",)),
        }

    print("  [attributor] Extracting attributable slices...", flush=True)
    attributable = extract_attributable_slices(perf_json, min_dur_ms=1.0)

    if not attributable:
        print("  [attributor] No attributable slices found", flush=True)
        return {
            "messages": [AIMessage(content="[attributor] No attributable slices found")],
            "perf_summary": perf_json,
            "perf_analysis": state.get("perf_analysis", ""),
            "attribution_data": "",
            "attribution_result": "",
            "_trace_path": state.get("_trace_path", ""),
        }

    print(f"  [attributor] Found {len(attributable)} slices, searching source code...", flush=True)
    for s in attributable[:5]:
        print(f"    {s['dur_ms']:>8.2f}ms  {s['class_name']}.{s['method_name']}  ({s.get('search_type', 'java')})", flush=True)

    results = run_attribution(attributable)

    # Summarize results
    found = sum(1 for r in results if r.get("attributable"))
    system = sum(1 for r in results if r.get("reason") == "system_class")
    print(f"  [attributor] Done: {found} attributed, {system} system classes", flush=True)

    return {
        "messages": [AIMessage(content=_format_attribution_summary(results))],
        "perf_summary": perf_json,
        "perf_analysis": state.get("perf_analysis", ""),
        "attribution_data": json.dumps(attributable),
        "attribution_result": json.dumps(results),
        "_trace_path": state.get("_trace_path", ""),
    }
