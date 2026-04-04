"""Reporter sub-module: report file saving."""

import os
import datetime


def save_report(content: str) -> str | None:
    """Save *content* to a timestamped markdown file under ./reports/.

    Returns the file path on success, or None on failure.
    """
    report_dir = os.path.join(os.getcwd(), "reports")
    os.makedirs(report_dir, exist_ok=True)
    timestamp = datetime.datetime.now().strftime("%Y%m%d_%H%M%S")
    report_path = os.path.join(report_dir, f"perf_report_{timestamp}.md")
    try:
        with open(report_path, "w", encoding="utf-8") as f:
            f.write(content)
        size_kb = len(content.encode("utf-8")) / 1024
        print(f"  [reporter] Report saved to {report_path} ({size_kb:.1f}KB)", flush=True)
        return report_path
    except OSError as e:
        print(f"  [reporter] Failed to save report: {e}", flush=True)
        return None
