"""Analysis Verifier: validate LLM output quality with zero-token heuristic checks.

Implements a two-layer verification system:
  L1 — Format check: ensures the analysis contains concrete numbers,
       method/class names, reasonable length, and severity levels.
  L2 — Consistency check: verifies the analysis covers P0 issues from
       deterministic hints and that key data points are numerically consistent.
"""

import re
from dataclasses import dataclass, field


@dataclass
class VerificationResult:
    """Result of analysis verification."""

    score: float  # 0.0-1.0 quality score
    issues: list[str] = field(default_factory=list)
    passed: bool = True
    warnings: list[str] = field(default_factory=list)

    @property
    def l1_passed(self) -> bool:
        """Whether L1 format checks passed."""
        return not any("L1" in i for i in self.issues)

    @property
    def l2_passed(self) -> bool:
        """Whether L2 consistency checks passed."""
        return not any("L2" in i for i in self.issues)


# ---------------------------------------------------------------------------
# L1: Heuristic Format Check (0 tokens)
# ---------------------------------------------------------------------------

def _l1_check_numbers(text: str) -> list[str]:
    """Check if the analysis contains concrete numeric values."""
    issues: list[str] = []
    # Match numbers with units or standalone decimals (e.g. "16.67ms", "74.95", "30%")
    number_pattern = r'\d+\.?\d*\s*(?:ms|%|帧|次|MB|KB|秒)'
    standalone = r'\b\d+\.?\d+\b'
    matches = re.findall(number_pattern, text) or re.findall(standalone, text)
    if len(matches) < 1:
        issues.append("[L1] 分析结果缺少具体数值数据")
    return issues


def _l1_check_method_names(text: str) -> list[str]:
    """Check if the analysis mentions specific method or class names."""
    issues: list[str] = []
    # Match Java/Kotlin-style identifiers (e.g. ClassName.methodName, onBindView)
    method_pattern = r'(?:[A-Z][a-zA-Z0-9]*\.[a-z][a-zA-Z0-9]*|[A-Z][a-zA-Z0-9]*\.on\w+)'
    # Also match SI$ tags
    si_pattern = r'SI\$\w+'
    matches = re.findall(method_pattern, text) or re.findall(si_pattern, text)
    if not matches:
        issues.append("[L1] 分析结果缺少具体方法名或类名")
    return issues


def _l1_check_length(text: str) -> list[str]:
    """Check if the analysis length is reasonable."""
    issues: list[str] = []
    length = len(text)
    if length < 100:
        issues.append(f"[L1] 分析结果过短 ({length}字符, 最低100)")
    elif length > 10000:
        issues.append(f"[L1] 分析结果过长 ({length}字符, 上限10000)")
    return issues


def _l1_check_severity(text: str) -> list[str]:
    """Check if the analysis includes P0/P1/P2 severity classification."""
    issues: list[str] = []
    if not re.search(r'P[0-2]', text):
        issues.append("[L1] 分析结果缺少 P0/P1/P2 严重度分级")
    return issues


def run_l1_checks(analysis_text: str) -> list[str]:
    """Run all L1 heuristic checks on analysis text.

    Returns:
        List of issue descriptions (empty = all checks passed).
    """
    issues: list[str] = []
    issues.extend(_l1_check_numbers(analysis_text))
    issues.extend(_l1_check_method_names(analysis_text))
    issues.extend(_l1_check_length(analysis_text))
    issues.extend(_l1_check_severity(analysis_text))
    return issues


# ---------------------------------------------------------------------------
# L2: Consistency Check (0 tokens)
# ---------------------------------------------------------------------------

def _extract_numbers_from_text(text: str) -> list[float]:
    """Extract all numeric values from text."""
    return [float(m) for m in re.findall(r'\d+\.?\d*', text) if _is_reasonable_number(float(m))]


def _is_reasonable_number(v: float) -> bool:
    """Filter out numbers that are likely not data values (years, counts, etc.)."""
    return 0.01 <= v <= 100000.0


def _l2_check_p0_coverage(analysis_text: str, raw_hints: str) -> list[str]:
    """Verify that P0 issues from deterministic hints are mentioned in analysis."""
    issues: list[str] = []

    # Extract P0 items from hints
    p0_pattern = r'P0:\s*(.+?)\s*\('
    p0_items = re.findall(p0_pattern, raw_hints)

    if not p0_items:
        return issues

    for item in p0_items:
        # Extract the key identifier (class.method or tag name)
        tokens = re.findall(r'[A-Za-z_]\w+', item)
        # Check if any significant token appears in the analysis
        found = False
        for token in tokens:
            if len(token) > 3 and token in analysis_text:
                found = True
                break
        if not found:
            issues.append(f"[L2] P0 问题未在分析中提及: {item.strip()}")

    return issues


def _l2_check_data_consistency(analysis_text: str, raw_hints: str) -> list[str]:
    """Verify key data points in analysis are consistent with hints (±20%)."""
    issues: list[str] = []

    # Extract key metrics from hints: "FPS=60", "CPU占用 45.2%", "145.00ms"
    hint_metrics: dict[str, float] = {}

    # FPS
    fps_match = re.search(r'fps[=:]\s*(\d+\.?\d*)', raw_hints, re.IGNORECASE)
    if fps_match:
        hint_metrics["fps"] = float(fps_match.group(1))

    # CPU usage
    cpu_match = re.search(r'(?:总CPU|cpu_usage_pct)[^\d]*(\d+\.?\d*)', raw_hints, re.IGNORECASE)
    if cpu_match:
        hint_metrics["cpu"] = float(cpu_match.group(1))

    # Frame budget
    budget_match = re.search(r'帧预算[^\d]*(\d+\.?\d*)', raw_hints)
    if budget_match:
        hint_metrics["frame_budget"] = float(budget_match.group(1))

    if not hint_metrics:
        return issues

    # Check consistency for each metric
    for metric_name, hint_value in hint_metrics.items():
        # Find the closest number in analysis text
        analysis_nums = _extract_numbers_from_text(analysis_text)
        if not analysis_nums:
            continue

        # Find the number closest to the hint value
        closest = min(analysis_nums, key=lambda x: abs(x - hint_value))
        if hint_value > 0:
            diff_pct = abs(closest - hint_value) / hint_value * 100
            if diff_pct > 20:
                issues.append(
                    f"[L2] 数据不一致: {metric_name} 提示值={hint_value:.1f}, "
                    f"分析中最接近值={closest:.1f} (偏差{diff_pct:.0f}%)"
                )

    return issues


def _l2_check_hotspot_coverage(analysis_text: str, raw_hints: str) -> list[str]:
    """Verify that hotspot methods from outlier sampling are covered in analysis."""
    issues: list[str] = []

    # Extract method/class names from hotspot sections in hints
    # Match patterns like "  → ClassName.methodName" or "P0: SI$tag#name"
    hotspot_pattern = r'(?:→|P[0-2]:)\s*(?:SI\$)?([A-Za-z_]\w+(?:\.[A-Za-z_]\w+)*)'
    hotspots = re.findall(hotspot_pattern, raw_hints)

    if not hotspots:
        return issues

    # Check top hotspots (most important ones are usually listed first)
    missed = []
    for hotspot in hotspots[:5]:
        # Extract the short class name
        parts = hotspot.split(".")
        short_name = parts[-1] if parts else hotspot
        if len(short_name) > 3 and short_name not in analysis_text:
            missed.append(hotspot)

    if missed:
        issues.append(f"[L2] 热点方法未覆盖: {', '.join(missed[:3])}")

    return issues


def run_l2_checks(analysis_text: str, raw_hints: str) -> list[str]:
    """Run all L2 consistency checks.

    Args:
        analysis_text: The LLM-generated analysis text.
        raw_hints: The deterministic hints that were provided to the LLM.

    Returns:
        List of issue descriptions (empty = all checks passed).
    """
    issues: list[str] = []
    issues.extend(_l2_check_p0_coverage(analysis_text, raw_hints))
    issues.extend(_l2_check_data_consistency(analysis_text, raw_hints))
    issues.extend(_l2_check_hotspot_coverage(analysis_text, raw_hints))
    return issues


# ---------------------------------------------------------------------------
# Main verification entry point
# ---------------------------------------------------------------------------

def verify_analysis(
    analysis_text: str,
    raw_hints: str,
    expected_fields: list[str] | None = None,
) -> VerificationResult:
    """Validate LLM analysis result quality.

    Runs L1 (format) and L2 (consistency) checks. L3 (depth) is reserved
    for future implementation.

    Args:
        analysis_text: The LLM-generated analysis text to verify.
        raw_hints: The deterministic hints that were provided as input.
        expected_fields: Optional list of field names expected in the output.

    Returns:
        VerificationResult with score, issues, and pass/fail status.
    """
    all_issues: list[str] = []
    warnings: list[str] = []

    # L1: Format checks
    l1_issues = run_l1_checks(analysis_text)
    all_issues.extend(l1_issues)

    # L2: Consistency checks (only if L1 basic format passes)
    if not l1_issues or len(l1_issues) <= 1:
        l2_issues = run_l2_checks(analysis_text, raw_hints)
        all_issues.extend(l2_issues)

    # Check expected fields
    if expected_fields:
        for field_name in expected_fields:
            if field_name not in analysis_text:
                warnings.append(f"缺少预期字段: {field_name}")

    # Compute score
    l1_fail_count = sum(1 for i in all_issues if i.startswith("[L1]"))
    l2_fail_count = sum(1 for i in all_issues if i.startswith("[L2]"))

    # Score: start at 1.0, deduct for failures
    score = max(0.0, 1.0 - l1_fail_count * 0.2 - l2_fail_count * 0.15)

    # Determine pass/fail
    # L1 must fully pass; L2 allows up to 1 minor issue
    l1_passed = l1_fail_count == 0
    l2_passed = l2_fail_count <= 1
    passed = l1_passed and l2_passed

    return VerificationResult(
        score=score,
        issues=all_issues,
        passed=passed,
        warnings=warnings,
    )
