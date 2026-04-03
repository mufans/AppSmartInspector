"""Perfetto trace analysis tool for LangGraph agent."""

import json
import os
import subprocess
import tempfile
from langchain_core.tools import tool

from smartinspector.collector.perfetto import PerfettoCollector


def _get_foreground_package() -> str | None:
    """Get the current foreground activity package name via adb.

    Tries multiple approaches:
    1. dumpsys activity activities (mResumedActivity)
    2. dumpsys window windows (mCurrentFocus / mFocusedApp)
    """
    commands = [
        ["adb", "shell", "dumpsys", "activity", "activities"],
        ["adb", "shell", "dumpsys", "window", "windows"],
    ]
    markers = ["mResumedActivity", "mCurrentFocus", "mFocusedApp"]

    for cmd in commands:
        try:
            result = subprocess.run(
                cmd, capture_output=True, text=True, timeout=5,
            )
            for line in result.stdout.splitlines():
                if not any(m in line for m in markers):
                    continue
                # Extract package name from token containing '/'
                # e.g. "u0 com.example.myapp/.MainActivity t123"
                for token in line.split():
                    if "/" in token and token[0].isalpha():
                        return token.split("/")[0]
        except Exception:
            continue
    return None


@tool
def analyze_perfetto(trace_path: str) -> str:
    """Analyze a Perfetto trace file and return performance summary.

    Use this to analyze Android performance traces (.pb files).
    Returns JSON with CPU scheduling, frame timeline, memory, and hotspot data.

    Args:
        trace_path: Path to the Perfetto trace file (.pb).
    """
    if not os.path.isfile(trace_path):
        return f"Error: trace file not found: {trace_path}"

    try:
        collector = PerfettoCollector(trace_path)
        summary = collector.summarize()
        result = summary.to_json()
        collector.close()
        return result
    except Exception as e:
        return f"Error analyzing trace: {e}"


@tool
def collect_android_trace(
    duration_ms: int = 10000,
    target_process: str | None = None,
) -> str:
    """Collect a Perfetto trace from a connected Android device via adb.

    Use this when the user wants to capture a new performance trace.
    Requires adb and a connected Android device with USB debugging enabled.
    If target_process is not provided, automatically detects the current foreground app.
    If auto-detection fails, falls back to system-wide trace (scheduling, frames only).

    Args:
        duration_ms: Trace duration in milliseconds (default 10000 = 10s).
        target_process: Target app package name for CPU/memory profiling,
                        e.g. "com.example.myapp". If omitted, auto-detects foreground app.
    """
    # Auto-detect foreground package if not specified
    detected = False
    if not target_process:
        target_process = _get_foreground_package()
        if target_process:
            detected = True

    try:
        path = PerfettoCollector.pull_trace_from_device(
            duration_ms=duration_ms,
            target_process=target_process,
        )
        info = {
            "status": "ok",
            "trace_path": path,
            "message": f"Trace collected ({duration_ms}ms). Use analyze_perfetto to analyze it.",
        }
        if target_process:
            info["target_process"] = target_process
            if detected:
                info["note"] = f"Auto-detected foreground app: {target_process}"
        else:
            info["note"] = "System-level trace (no target_process). CPU hotspots and heap data unavailable."
        return json.dumps(info)
    except FileNotFoundError:
        return "Error: adb not found. Please install Android platform tools."
    except Exception as e:
        return f"Error collecting trace: {e}"
