"""Persistent storage for performance analysis results."""

import json
import logging
import os
from datetime import datetime
from pathlib import Path

logger = logging.getLogger(__name__)

# Default reports directory
_DEFAULT_REPORTS_DIR = Path(__file__).resolve().parent.parent.parent.parent.parent / "reports"


def _get_reports_dir() -> Path:
    """Get the reports directory, creating it if needed."""
    reports_dir = _DEFAULT_REPORTS_DIR
    reports_dir.mkdir(parents=True, exist_ok=True)
    return reports_dir


def save_analysis_result(
    perf_summary: str,
    perf_analysis: str = "",
    attribution_result: str = "",
    trace_path: str = "",
    output_dir: str | None = None,
) -> str:
    """Save analysis result as a timestamped JSON file for historical comparison.

    Args:
        perf_summary: JSON string from PerfettoCollector.
        perf_analysis: Markdown string from LLM analysis.
        attribution_result: JSON string from attribution agent.
        trace_path: Path to the original trace file.
        output_dir: Optional output directory override.

    Returns:
        Path to the saved JSON file.
    """
    out_dir = Path(output_dir) if output_dir else _get_reports_dir()
    out_dir.mkdir(parents=True, exist_ok=True)

    timestamp = datetime.now().strftime("%Y-%m-%d_%H%M%S")
    filename = f"{timestamp}_analysis.json"
    filepath = out_dir / filename

    # Extract key metrics from perf_summary
    try:
        perf_data = json.loads(perf_summary) if isinstance(perf_summary, str) else perf_summary
    except (json.JSONDecodeError, TypeError):
        perf_data = {}

    metrics = _extract_metrics(perf_data)

    record = {
        "version": "1.0",
        "timestamp": timestamp,
        "created_at": datetime.now().isoformat(),
        "trace_path": trace_path,
        "metrics": metrics,
        "perf_summary": perf_summary,
        "perf_analysis": perf_analysis,
        "attribution_result": attribution_result,
    }

    filepath.write_text(json.dumps(record, indent=2, ensure_ascii=False))
    logger.info("Saved analysis result to: %s", filepath)
    return str(filepath)


def _extract_metrics(perf_data: dict) -> dict:
    """Extract comparable metrics from perf summary data.

    Returns a flat dict of metric name -> value for easy comparison.
    """
    metrics: dict = {}

    # Frame timeline
    ft = perf_data.get("frame_timeline") or {}
    if ft:
        metrics["fps"] = ft.get("fps", 0)
        metrics["total_frames"] = ft.get("total_frames", 0)
        metrics["jank_frames"] = ft.get("jank_frames", 0)

    # CPU
    cpu = perf_data.get("cpu_usage") or {}
    if cpu:
        metrics["cpu_usage_pct"] = cpu.get("cpu_usage_pct", 0)

    # Process memory (target process)
    proc_mem = perf_data.get("process_memory") or {}
    processes = proc_mem.get("processes", [])
    if processes:
        # First non-system process is usually the target
        target = None
        for p in processes:
            if p.get("name", "") not in ("system_server", "com.android.systemui"):
                target = p
                break
        if not target:
            target = processes[0]
        metrics["peak_rss_mb"] = round(target.get("rss_kb", 0) / 1024, 1)
        metrics["avg_rss_mb"] = round(target.get("avg_rss_kb", 0) / 1024, 1)

    # IO slices
    io_slices = perf_data.get("io_slices") or {}
    if io_slices:
        metrics["io_total_count"] = io_slices.get("total_count", 0)
        io_summary = io_slices.get("summary", [])
        for s in io_summary:
            io_type = s.get("io_type", "unknown")
            metrics[f"io_{io_type}_total_ms"] = round(s.get("total_ms", 0), 1)

    # Slowest slices (top 5 by duration)
    view_slices = perf_data.get("view_slices") or {}
    slowest = view_slices.get("slowest_slices", [])
    custom_slices = [s for s in slowest if s.get("is_custom")]
    metrics["slowest_slices"] = [
        {"name": s.get("name", ""), "dur_ms": s.get("dur_ms", 0)}
        for s in custom_slices[:5]
    ]

    # Compose
    compose_slices = perf_data.get("compose_slices") or {}
    if compose_slices:
        metrics["compose_total_count"] = compose_slices.get("total_count", 0)
        composables = compose_slices.get("composables", [])
        metrics["compose_recompositions"] = sum(c.get("recompose_count", 0) for c in composables)

    # Memory (heap)
    memory = perf_data.get("memory") or {}
    heap_objects = memory.get("heap_objects") or memory.get("heap_graph_classes") or []
    if heap_objects:
        total_heap_kb = sum(o.get("total_size_kb", 0) for o in heap_objects)
        metrics["total_heap_mb"] = round(total_heap_kb / 1024, 1)

    return metrics


def load_analysis_result(filepath: str) -> dict | None:
    """Load a saved analysis result from JSON file.

    Args:
        filepath: Path to the JSON file.

    Returns:
        Parsed dict, or None if file not found or invalid.
    """
    try:
        path = Path(filepath)
        if not path.exists():
            logger.warning("Analysis file not found: %s", filepath)
            return None
        data = json.loads(path.read_text())
        return data
    except (json.JSONDecodeError, OSError) as e:
        logger.error("Failed to load analysis file: %s", e)
        return None


def list_saved_analyses(output_dir: str | None = None) -> list[dict]:
    """List all saved analysis results, sorted by timestamp (newest first).

    Args:
        output_dir: Optional directory override.

    Returns:
        List of dicts with filename, timestamp, and metrics summary.
    """
    out_dir = Path(output_dir) if output_dir else _get_reports_dir()
    if not out_dir.exists():
        return []

    results = []
    for f in sorted(out_dir.glob("*_analysis.json"), reverse=True):
        try:
            data = json.loads(f.read_text())
            metrics = data.get("metrics", {})
            results.append({
                "filename": f.name,
                "filepath": str(f),
                "timestamp": data.get("timestamp", ""),
                "fps": metrics.get("fps", 0),
                "jank_frames": metrics.get("jank_frames", 0),
                "cpu_usage_pct": metrics.get("cpu_usage_pct", 0),
                "peak_rss_mb": metrics.get("peak_rss_mb", 0),
            })
        except (json.JSONDecodeError, OSError):
            continue

    return results
