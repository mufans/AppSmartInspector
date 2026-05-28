"""Reporter sub-module: report file saving."""

import os
import datetime

from smartinspector.debug_log import info_log


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
        info_log("reporter", f"Report saved to {report_path} ({size_kb:.1f}KB)")
        return report_path
    except OSError as e:
        info_log("reporter", f"ERROR: Failed to save report: {e}")
        return None
