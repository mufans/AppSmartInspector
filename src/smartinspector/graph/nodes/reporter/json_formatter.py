"""Structured JSON report formatter for machine-readable output."""

import json
import datetime
from pathlib import Path


def format_json_report(
    perf_json: str,
    perf_analysis: str = "",
    attributable: list[dict] | None = None,
    trace_path: str = "",
    target: str = "",
) -> dict:
    """Format analysis results as a structured JSON report.

    Args:
        perf_json: Raw performance summary JSON string.
        perf_analysis: LLM-generated analysis markdown text.
        attributable: List of attributable slice dicts from attribution.
        trace_path: Path to the trace file.
        target: Target process package name.

    Returns:
        Structured report dict ready for JSON serialization.
    """
    try:
        perf_data = json.loads(perf_json) if perf_json else {}
    except (json.JSONDecodeError, TypeError):
        perf_data = {}

    report: dict = {
        "version": "1.0",
        "timestamp": datetime.datetime.now().isoformat() + "Z",
        "target": {
            "package": target,
        },
        "trace": {
            "path": trace_path,
        },
        "summary": _extract_summary(perf_data),
        "issues": _extract_issues(perf_data, attributable or []),
        "metrics": _extract_metrics(perf_data),
    }

    if perf_analysis:
        report["analysis"] = perf_analysis

    return report


def _extract_summary(perf_data: dict) -> dict:
    """Extract high-level summary metrics."""
    ft = perf_data.get("frame_timeline") or {}
    cpu = perf_data.get("cpu_usage") or {}

    return {
        "fps": ft.get("fps", 0),
        "total_frames": ft.get("total_frames", 0),
        "jank_frames": ft.get("jank_frames", 0),
        "cpu_usage_pct": cpu.get("cpu_usage_pct", 0),
    }


def _extract_issues(perf_data: dict, attributable: list[dict]) -> list[dict]:
    """Extract performance issues from view slices and attribution data.

    Maps SI$ slices into structured issue objects with severity,
    category, source location, and recommendations.
    """
    issues: list[dict] = []
    view_slices = perf_data.get("view_slices", {})
    slowest = view_slices.get("slowest_slices", []) if view_slices else []

    # Build attribution lookup by raw_name
    attr_by_name: dict[str, dict] = {}
    for a in attributable:
        key = f"{a.get('class_name', '')}.{a.get('method_name', '')}"
        attr_by_name[key] = a

    for s in slowest:
        name = s.get("name", "")
        dur_ms = s.get("dur_ms", 0)
        if not name.startswith("SI$") or dur_ms < 1.0:
            continue

        # Determine category
        category = _classify_issue_category(name)

        # Determine severity
        severity = _classify_issue_severity(dur_ms)

        # Look up attribution result
        attr = attr_by_name.get(name)
        source = None
        recommendation = ""

        if attr:
            source = {
                "file": attr.get("file_path", ""),
                "line_start": attr.get("line_start"),
                "line_end": attr.get("line_end"),
                "snippet": attr.get("source_snippet", ""),
                "finding": attr.get("finding", ""),
            }
            recommendation = attr.get("recommendation", "")

        issue: dict = {
            "severity": severity,
            "category": category,
            "title": _humanize_issue_title(name, dur_ms),
            "duration_ms": dur_ms,
        }

        if source:
            issue["source"] = source
        if recommendation:
            issue["recommendation"] = recommendation

        issues.append(issue)

    # Sort by severity (P0 first) then by duration
    severity_order = {"P0": 0, "P1": 1, "P2": 2}
    issues.sort(key=lambda x: (severity_order.get(x["severity"], 3), -x["duration_ms"]))

    return issues


def _extract_metrics(perf_data: dict) -> dict:
    """Extract detailed metric sections."""
    metrics: dict = {}

    # Frame timeline
    ft = perf_data.get("frame_timeline")
    if ft:
        metrics["frame_timeline"] = {
            "fps": ft.get("fps", 0),
            "total_frames": ft.get("total_frames", 0),
            "jank_frames": ft.get("jank_frames", 0),
            "jank_types": ft.get("jank_types", []),
            "slowest_frames": ft.get("slowest_frames", [])[:5],
        }

    # CPU hotspots
    cpu = perf_data.get("cpu_usage")
    if cpu:
        metrics["cpu_hotspots"] = cpu.get("top_processes", [])

    # Thread state
    thread_state = perf_data.get("thread_state")
    if thread_state:
        metrics["thread_state"] = thread_state

    # IO slices
    io_slices = perf_data.get("io_slices")
    if io_slices:
        metrics["io_slices"] = {
            "total_count": io_slices.get("total_count", 0),
            "summary": io_slices.get("summary", []),
        }

    # View slices summary
    vs = perf_data.get("view_slices")
    if vs:
        metrics["view_slices"] = {
            "summary": vs.get("summary", [])[:10],
        }

    # CPU hotspots (callchain)
    cpu_hotspots = perf_data.get("cpu_hotspots")
    if cpu_hotspots:
        metrics["cpu_callchain_hotspots"] = cpu_hotspots[:10]

    return metrics


# ---------------------------------------------------------------------------
# Issue classification helpers
# ---------------------------------------------------------------------------

_IO_TYPE_MAP = {
    "net#": "network_io",
    "db#": "database_io",
    "img#": "image_io",
}

_ISSUE_CATEGORY_MAP = {
    "RV#": "recycler_view",
    "inflate#": "layout_inflate",
    "view#": "view_draw",
    "block#": "ui_thread_block",
    "handler#": "handler_dispatch",
}


def _classify_issue_category(name: str) -> str:
    """Classify issue category from SI$ tag prefix."""
    body = name[3:] if name.startswith("SI$") else name

    # Check IO types first
    for prefix, category in _IO_TYPE_MAP.items():
        if body.startswith(prefix):
            return category

    # Check other types
    for prefix, category in _ISSUE_CATEGORY_MAP.items():
        if body.startswith(prefix):
            return category

    # Default
    return "custom"


def _classify_issue_severity(dur_ms: float) -> str:
    """Classify issue severity based on duration.

    P0: > 16.67ms (exceeds frame budget)
    P1: >= 4ms (significant portion of frame budget)
    P2: < 4ms (minor)
    """
    if dur_ms > 16.67:
        return "P0"
    if dur_ms >= 4.0:
        return "P1"
    return "P2"


def _humanize_issue_title(name: str, dur_ms: float) -> str:
    """Generate a human-readable issue title from SI$ tag."""
    from smartinspector.commands.attribution import extract_class, extract_method

    class_name = extract_class(name)
    method_name = extract_method(name)

    body = name[3:] if name.startswith("SI$") else name

    # Add context based on tag type
    prefix_label = ""
    for prefix in _IO_TYPE_MAP:
        if body.startswith(prefix):
            prefix_label = f"[{_IO_TYPE_MAP[prefix]}] "
            break
    for prefix in _ISSUE_CATEGORY_MAP:
        if body.startswith(prefix):
            prefix_label = f"[{_ISSUE_CATEGORY_MAP[prefix]}] "
            break

    return f"{prefix_label}{class_name}.{method_name} 耗时 {dur_ms:.1f}ms"
