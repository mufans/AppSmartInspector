"""Historical comparison command: /compare."""

import json
import logging

from smartinspector.storage.store import load_analysis_result, list_saved_analyses

logger = logging.getLogger(__name__)


def cmd_compare(args: str, state: dict) -> dict:
    """Compare two analysis results and show performance trends.

    Usage:
        /compare <report1.json> <report2.json>   — compare two specific reports
        /compare latest                           — compare the two most recent reports
        /compare list                             — list all saved analysis reports

    Shows metric deltas with trend arrows and highlights regressions/improvements.
    """
    parts = args.strip().split() if args else []

    if not parts or parts[0] == "help":
        _print_compare_help()
        return state

    if parts[0] == "list":
        return _cmd_compare_list(state)

    if parts[0] == "latest":
        return _cmd_compare_latest(state)

    if len(parts) >= 2:
        return _cmd_compare_files(parts[0], parts[1], state)

    print("Usage: /compare <report1.json> <report2.json>")
    print("       /compare latest")
    print("       /compare list")
    return state


def _print_compare_help():
    """Print compare command help."""
    print("Compare analysis results to identify performance trends.")
    print("")
    print("Usage:")
    print("  /compare <report1.json> <report2.json>   Compare two specific reports")
    print("  /compare latest                          Compare two most recent reports")
    print("  /compare list                            List all saved reports")
    print("")
    print("Reports are automatically saved after each /full analysis.")


def _cmd_compare_list(state: dict) -> dict:
    """List all saved analysis reports."""
    analyses = list_saved_analyses()
    if not analyses:
        print("No saved analysis reports found.")
        print("Run /full to generate and save analysis results.")
        return state

    print(f"Found {len(analyses)} saved reports:\n")
    print(f"{'Timestamp':<22} {'FPS':>6} {'Jank':>6} {'CPU%':>7} {'RSS MB':>8}  File")
    print("-" * 70)
    for a in analyses:
        print(
            f"{a['timestamp']:<22} "
            f"{a['fps']:>6.1f} "
            f"{a['jank_frames']:>6} "
            f"{a['cpu_usage_pct']:>7.1f} "
            f"{a.get('peak_rss_mb', 0):>8.1f}  "
            f"{a['filename']}"
        )
    print("")
    print("Use /compare latest or /compare <file1> <file2> to compare.")

    return state


def _cmd_compare_latest(state: dict) -> dict:
    """Compare the two most recent reports."""
    analyses = list_saved_analyses()
    if len(analyses) < 2:
        print("Need at least 2 saved reports for comparison.")
        print(f"Found {len(analyses)} report(s). Run /full more times.")
        return state

    # analyses is sorted newest-first
    return _compare_results(analyses[1], analyses[0], state)


def _cmd_compare_files(file1: str, file2: str, state: dict) -> dict:
    """Compare two specific report files."""
    data1 = load_analysis_result(file1)
    data2 = load_analysis_result(file2)

    if not data1:
        print(f"Failed to load: {file1}")
        return state
    if not data2:
        print(f"Failed to load: {file2}")
        return state

    info1 = {"filepath": file1, "filename": file1, "timestamp": data1.get("timestamp", "?")}
    info2 = {"filepath": file2, "filename": file2, "timestamp": data2.get("timestamp", "?")}

    state = _compare_results(info1, info2, state)

    # Store comparison data in state for potential reuse
    state["perf_summary"] = data2.get("perf_summary", state.get("perf_summary", ""))
    return state


def _compare_results(info_a: dict, info_b: dict, state: dict) -> dict:
    """Compare two analysis results and print the comparison report.

    Args:
        info_a: Older report (filepath/filename/timestamp or full data).
        info_b: Newer report.
    """
    # Load full data
    data_a = load_analysis_result(info_a.get("filepath", ""))
    data_b = load_analysis_result(info_b.get("filepath", ""))

    # If data already loaded (from list), use metrics directly
    if data_a is None and "metrics" not in info_a:
        print(f"Failed to load: {info_a.get('filepath', '?')}")
        return state
    if data_b is None and "metrics" not in info_b:
        print(f"Failed to load: {info_b.get('filepath', '?')}")
        return state

    metrics_a = (data_a or info_a).get("metrics", {})
    metrics_b = (data_b or info_b).get("metrics", {})
    ts_a = (data_a or info_a).get("timestamp", info_a.get("timestamp", "?"))
    ts_b = (data_b or info_b).get("timestamp", info_b.get("timestamp", "?"))

    # Print comparison header
    print(f"\n## 性能对比报告\n")
    print(f"| 指标 | 报告 A ({ts_a}) | 报告 B ({ts_b}) | 变化 |")
    print("|------|--------------|--------------|------|")

    # Compare numeric metrics
    # (key, display_name, higher_is_better)
    numeric_metrics = [
        ("fps", "FPS", True),
        ("total_frames", "总帧数", True),
        ("jank_frames", "卡顿帧", False),
        ("cpu_usage_pct", "CPU%", False),
        ("peak_rss_mb", "峰值RSS (MB)", False),
        ("avg_rss_mb", "平均RSS (MB)", False),
        ("io_total_count", "IO操作数", False),
        ("total_heap_mb", "堆内存 (MB)", False),
        ("compose_recompositions", "Compose重组", False),
    ]

    regressions = []
    improvements = []

    for key, label, higher_is_better in numeric_metrics:
        val_a = metrics_a.get(key)
        val_b = metrics_b.get(key)

        if val_a is None and val_b is None:
            continue

        val_a = val_a or 0
        val_b = val_b or 0

        delta = val_b - val_a
        if val_a > 0:
            pct = round(delta / val_a * 100, 1)
        elif delta != 0:
            pct = float("inf")
        else:
            pct = 0

        # Format values
        if isinstance(val_a, float):
            a_str = f"{val_a:.1f}"
            b_str = f"{val_b:.1f}"
        else:
            a_str = str(val_a)
            b_str = str(val_b)

        # Format delta
        if pct == float("inf"):
            delta_str = "+∞%"
        elif pct == 0:
            delta_str = "—"
        else:
            sign = "+" if delta > 0 else ""
            arrow = "↑" if delta > 0 else "↓"
            delta_str = f"{sign}{pct}% {arrow}"

        print(f"| {label} | {a_str} | {b_str} | {delta_str} |")

        # Track regressions and improvements
        if abs(delta) > 0 and pct != float("inf"):
            improved = (delta < 0) if not higher_is_better else (delta > 0)
            if improved:
                improvements.append((label, val_a, val_b, pct))
            else:
                regressions.append((label, val_a, val_b, pct))

    # Compare slowest slices
    slices_a = metrics_a.get("slowest_slices", [])
    slices_b = metrics_b.get("slowest_slices", [])
    if slices_a and slices_b:
        print(f"\n### 切片耗时对比 (Top 5)\n")
        print("| 切片 | 报告A (ms) | 报告B (ms) | 变化 |")
        print("|------|-----------|-----------|------|")

        # Build lookup from report A
        a_lookup = {s["name"]: s["dur_ms"] for s in slices_a}
        b_lookup = {s["name"]: s["dur_ms"] for s in slices_b}
        all_names = list(dict.fromkeys(
            [s["name"] for s in slices_a[:5]] + [s["name"] for s in slices_b[:5]]
        ))

        for name in all_names[:10]:
            dur_a = a_lookup.get(name, 0)
            dur_b = b_lookup.get(name, 0)
            short_name = name.replace("SI$", "")
            if len(short_name) > 40:
                short_name = short_name[:37] + "..."

            if dur_a > 0 and dur_b > 0:
                pct = round((dur_b - dur_a) / dur_a * 100, 1)
                sign = "+" if pct > 0 else ""
                arrow = "↑" if pct > 0 else "↓"
                delta_str = f"{sign}{pct}% {arrow}"
            elif dur_b > 0:
                delta_str = "NEW"
            else:
                delta_str = "GONE"

            print(f"| {short_name} | {dur_a:.2f} | {dur_b:.2f} | {delta_str} |")

            # Track significant regressions in slices
            if dur_a > 0 and dur_b > 0 and (dur_b - dur_a) / dur_a > 0.2:
                regressions.append((short_name, dur_a, dur_b, round((dur_b - dur_a) / dur_a * 100, 1)))

    # Summary
    if regressions:
        print(f"\n### 回归项 ⚠\n")
        for label, old, new, pct in regressions[:5]:
            sign = "+" if pct > 0 else ""
            print(f"- {label}: {old} → {new} ({sign}{pct}%)")

    if improvements:
        print(f"\n### 改善项 ✓\n")
        for label, old, new, pct in improvements[:5]:
            print(f"- {label}: {old} → {new} ({pct:.0f}%)")

    if not regressions and not improvements:
        print("\n指标无显著变化。")

    return state
