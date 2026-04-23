"""Reporter sub-module: report file saving."""

import logging
import os
import datetime

logger = logging.getLogger(__name__)


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
        logger.info("Report saved to %s (%.1fKB)", report_path, size_kb)
        return report_path
    except OSError as e:
        logger.error("Failed to save report: %s", e)
        return None
