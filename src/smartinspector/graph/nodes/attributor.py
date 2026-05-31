"""Attributor node: source code attribution (pipeline step 3)."""

import json

from langchain_core.messages import AIMessage

from smartinspector.agents.attributor import run_attribution
from smartinspector.debug_log import debug_log
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


def _make_attributor_progress(total: int):
    """Create a progress callback for run_attribution that prints per-slice results."""
    done = [0]

    def on_progress(event, data=None):
        if event == "group_start":
            label = data or ""
            print(f"  [attributor] 搜索: {label}", flush=True)  # noqa: LOG — user-facing progress
        elif event == "group_done":
            results = data or []
            for r in results:
                done[0] += 1
                name = f"{r.get('class_name', '?')}.{r.get('method_name', '?')}"
                dur = r.get('dur_ms', 0)
                if r.get("attributable"):
                    fp = r.get("file_path", "?")
                    ls = r.get("line_start", "?")
                    print(  # noqa: LOG — user-facing progress
                        f"    ✓ [{done[0]}/{total}] {name} ({dur:.1f}ms) → {fp}:{ls}",
                        flush=True,
                    )
                else:
                    reason = r.get("reason", "unknown")
                    print(  # noqa: LOG — user-facing progress
                        f"    ✗ [{done[0]}/{total}] {name} ({dur:.1f}ms) [{reason}]",
                        flush=True,
                    )
        else:
            # Legacy single-arg calls from _search_group (e.g. iteration/tool messages)
            print(f"  {event}", flush=True)  # noqa: LOG — user-facing progress

    return on_progress


def attributor_node(state: AgentState) -> dict:
    """Extract attributable slices and search source code."""
    from smartinspector.commands.attribution import extract_attributable_slices

    perf_json = state.get("perf_summary", "")
    if not perf_json:
        return {
            "messages": [AIMessage(content="[attributor] No perf data for attribution")],
            **_pass_through(state, extra_keys=("_trace_path",)),
        }

    print("  [attributor] 提取可归因切片...", flush=True)  # noqa: LOG — user-facing progress
    attributable = extract_attributable_slices(perf_json, min_dur_ms=1.0)
    debug_log("attributor", f"extract_attributable_slices: {len(attributable)} slices")
    if attributable:
        debug_log("attributor", f"slices detail: {json.dumps(attributable[:20], ensure_ascii=False)}")

    if not attributable:
        print("  [attributor] 未找到可归因切片", flush=True)  # noqa: LOG — user-facing progress
        return {
            "messages": [AIMessage(content="[attributor] No attributable slices found")],
            "perf_summary": perf_json,
            "perf_analysis": state.get("perf_analysis", ""),
            "attribution_data": "",
            "attribution_result": "",
            "_trace_path": state.get("_trace_path", ""),
        }

    print(f"  [attributor] 发现 {len(attributable)} 个切片，搜索源码...", flush=True)  # noqa: LOG — user-facing progress
    for s in attributable[:5]:
        print(f"    {s['dur_ms']:>8.2f}ms  {s['class_name']}.{s['method_name']}  ({s.get('search_type', 'java')})", flush=True)  # noqa: LOG — user-facing progress

    on_progress = _make_attributor_progress(len(attributable))
    results = run_attribution(attributable, on_progress=on_progress, perf_json=perf_json)
    debug_log("attributor", f"run_attribution results: {json.dumps(results, ensure_ascii=False)}")

    # Summarize results
    found = sum(1 for r in results if r.get("attributable"))
    system = sum(1 for r in results if r.get("reason") == "system_class")
    print(f"  [attributor] 完成: {found} 已定位, {system} 系统类", flush=True)  # noqa: LOG — user-facing progress

    return {
        "messages": [AIMessage(content=_format_attribution_summary(results))],
        "perf_summary": perf_json,
        "perf_analysis": state.get("perf_analysis", ""),
        "attribution_data": json.dumps(attributable),
        "attribution_result": json.dumps(results),
        "_trace_path": state.get("_trace_path", ""),
    }
