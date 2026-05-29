"""Load prompt text files from the prompts/ directory."""

from pathlib import Path

_PROMPTS_DIR = Path(__file__).resolve().parent.parent.parent / "prompts"
_SKILLS_DIR = _PROMPTS_DIR / "skills"
_DIMENSIONS_DIR = _SKILLS_DIR / "dimensions"
_SHARED_DIR = _SKILLS_DIR / "shared"

_skill_cache: dict[str, str] = {}
_section_cache: dict[str, str] = {}

_SPLIT_MARKER = "---"


def _split_skill(content: str) -> tuple[str, str]:
    """Split skill content into trigger and detail sections.

    Uses a line-based split to find a standalone --- line (not a markdown
    table separator like |---|).

    Args:
        content: Full skill file content.

    Returns:
        (trigger, detail) tuple. For files without the --- separator,
        returns (full_content, full_content) as fallback.
    """
    lines = content.split("\n")
    for i, line in enumerate(lines):
        if line.strip() == "---":
            trigger = "\n".join(lines[:i]).strip()
            detail = "\n".join(lines[i + 1:]).strip()
            return trigger, detail
    # No separator: fallback to full content for both sections
    return content, content


def load_prompt(name: str) -> str:
    """Load a prompt file by name (without .txt extension).

    Args:
        name: Prompt file name, e.g. "main", "code-explorer".

    Returns:
        The prompt text content.
    """
    path = _PROMPTS_DIR / f"{name}.txt"
    return path.read_text(encoding="utf-8")


def load_skill(name: str, category: str = "dimensions") -> str:
    """Load a skill knowledge file.

    Args:
        name: Skill file name (without .md), e.g. "gc-analysis".
        category: Subdirectory - "dimensions" (default) or "shared".

    Returns:
        Skill knowledge content. Cached after first load.
    """
    cache_key = f"{category}/{name}"
    if cache_key in _skill_cache:
        return _skill_cache[cache_key]

    base = _DIMENSIONS_DIR if category == "dimensions" else _SHARED_DIR
    path = base / f"{name}.md"
    if not path.exists():
        return ""

    content = path.read_text(encoding="utf-8")
    _skill_cache[cache_key] = content
    return content


def load_skill_section(name: str, section: str = "all", category: str = "dimensions") -> str:
    """Load a specific section of a skill knowledge file.

    Args:
        name: Skill file name (without .md), e.g. "gc-analysis".
        section: Which section to load:
            - "trigger": Content before the --- separator (data source,
              domain overview, metric fields, decision tree).
            - "detail": Content after the --- separator (severity, SQL,
              misdiagnosis, cross-dimension, optimization, deep dive).
            - "all": Full content (default, backward compatible).
        category: Subdirectory - "dimensions" (default) or "shared".

    Returns:
        Requested section content. Cached after first load.
    """
    full = load_skill(name, category)
    if not full:
        return ""

    if section == "all":
        return full

    cache_key = f"section:{category}/{name}:{section}"
    if cache_key in _section_cache:
        return _section_cache[cache_key]

    trigger, detail = _split_skill(full)
    if section == "trigger":
        _section_cache[cache_key] = trigger
        return trigger
    if section == "detail":
        _section_cache[cache_key] = detail
        return detail

    return full


def _compute_anomaly_set(perf_json: str) -> set[str]:
    """Compute the set of dimension names that have anomalies.

    Uses compute_hint() from the dimension registry: a non-empty hint
    means the dimension has anomaly-level data.

    Args:
        perf_json: PerfSummary JSON string.

    Returns:
        Set of dimension names (e.g. {"lock_contention", "gc_events"})
        that have anomaly-level data.
    """
    import json

    try:
        data = json.loads(perf_json)
    except (json.JSONDecodeError, TypeError):
        return set()

    dimensions_data = data.get("dimensions", {})
    if not dimensions_data:
        return set()

    try:
        from smartinspector.collector.dimensions import DimensionRegistry, HintContext
        DimensionRegistry.discover()
    except Exception:
        return set()

    frame_budget_ms = 16.67
    metadata = data.get("metadata", {})
    device = metadata.get("device", {})
    refresh_rate = device.get("refresh_rate", 60)
    frame_budget_ms = 1000.0 / refresh_rate if refresh_rate > 0 else 16.67

    context = HintContext(
        frame_budget_ms=frame_budget_ms,
        target_process=metadata.get("target_process", {}).get("name", ""),
    )

    anomalies: set[str] = set()
    for dim in DimensionRegistry.all():
        dim_data = dimensions_data.get(dim.name)
        if not dim_data:
            continue
        hint = dim.compute_hint(dim_data, context)
        if hint:
            anomalies.add(dim.name)

    return anomalies


def load_skills_for_dimensions(
    perf_json: str,
    agent_role: str = "",
) -> str:
    """Load dimension skill knowledge with progressive loading strategy.

    Scans perf JSON for dimensions that have meaningful data and loads the
    corresponding skill knowledge files. The loading depth depends on the
    agent_role parameter and whether each dimension has anomaly-level data.

    Loading strategy by agent_role:
        - "analyzer" (perf_analyzer): Anomaly dims get trigger+detail,
          normal dims get trigger only.
        - "reporter": Anomaly dims get trigger only, normal dims skip.
        - "attributor": No dimension skills loaded at all.
        - "frame_analyzer": Anomaly dims get trigger only, normal dims skip.
        - "" (empty/default): Full content for all (backward compatible).

    Args:
        perf_json: PerfSummary JSON string.
        agent_role: Agent role for loading strategy. One of "analyzer",
            "reporter", "attributor", "frame_analyzer", or "" (full load).

    Returns:
        Concatenated skill knowledge text for dimensions with data.
    """
    import json

    try:
        data = json.loads(perf_json)
    except (json.JSONDecodeError, TypeError):
        return ""

    # Attributor: skip dimension skills entirely
    if agent_role == "attributor":
        return ""

    # 1. Check dimension registry data
    dimensions_data = data.get("dimensions", {})
    skill_names: list[str] = []

    if dimensions_data:
        try:
            from smartinspector.collector.dimensions import DimensionRegistry
            DimensionRegistry.discover()
            for dim in DimensionRegistry.all():
                dim_data = dimensions_data.get(dim.name)
                if not dim_data:
                    continue
                # Skip error/empty dimension data
                if isinstance(dim_data, dict) and dim_data.get("error"):
                    continue
                if dim_data in ("", None, [], {}):
                    continue
                skill_names.append(dim.skill_name)
        except Exception:
            pass

    # 2. Check non-dimension fields for additional skill context
    # Frame timeline jank → ui-jank
    ft = data.get("frame_timeline") or {}
    if ft.get("jank_frames", 0) > 0 or ft.get("jank_detail"):
        skill_names.append("ui-jank")

    # Startup metrics → startup
    startup = data.get("startup_metrics") or {}
    if startup.get("startups") or startup.get("breakdowns"):
        skill_names.append("startup")

    if not skill_names:
        return ""

    # Deduplicate while preserving order
    seen: set[str] = set()
    unique_skills: list[str] = []
    for s in skill_names:
        if s not in seen:
            seen.add(s)
            unique_skills.append(s)

    # Compute anomaly dimensions (skip for default/full-load mode)
    anomaly_skill_names: set[str] = set()
    if agent_role and agent_role != "attributor":
        anomaly_dims = _compute_anomaly_set(perf_json)
        # Map dimension names to skill names
        try:
            from smartinspector.collector.dimensions import DimensionRegistry
            dim_by_name = {d.name: d.skill_name for d in DimensionRegistry.all()}
            anomaly_skill_names = {
                dim_by_name[d] for d in anomaly_dims if d in dim_by_name
            }
        except Exception:
            pass

    # Load and concatenate with strategy
    parts: list[str] = []
    for skill_name in unique_skills:
        content = _load_skill_for_role(
            skill_name, agent_role, anomaly_skill_names
        )
        if content:
            parts.append(f"\n\n# Knowledge: {skill_name}\n\n{content}")

    return "".join(parts)


def _load_skill_for_role(
    skill_name: str,
    agent_role: str,
    anomaly_skill_names: set[str],
) -> str:
    """Load skill content according to agent role and anomaly status.

    Args:
        skill_name: Skill file name (without .md).
        agent_role: Agent role string.
        anomaly_skill_names: Set of skill names with anomaly data.

    Returns:
        Skill content string for the given role.
    """
    is_anomaly = skill_name in anomaly_skill_names

    if not agent_role:
        # Default: full content (backward compatible)
        return load_skill_section(skill_name, "all")

    if agent_role == "analyzer":
        # perf_analyzer: anomaly dims get full, normal dims get trigger
        if is_anomaly:
            return load_skill_section(skill_name, "all")
        return load_skill_section(skill_name, "trigger")

    if agent_role in ("reporter", "frame_analyzer"):
        # reporter/frame_analyzer: anomaly dims get trigger, normal dims skip
        if is_anomaly:
            return load_skill_section(skill_name, "trigger")
        return ""

    # Unknown role: full content
    return load_skill_section(skill_name, "all")


def load_prompt_with_skills(name: str, *skill_names: str) -> str:
    """Load a prompt file and append dimension skill knowledge.

    Args:
        name: Prompt instruction file name (without .txt).
        *skill_names: Skill names to append.
            - "gc-analysis" -> dimensions/gc-analysis.md
            - "shared:si-tag-system" -> shared/si-tag-system.md

    Returns:
        Combined prompt text with instructions + knowledge sections.
    """
    parts = [load_prompt(name)]

    for skill_name in skill_names:
        if skill_name.startswith("shared:"):
            ref = load_skill(skill_name[7:], category="shared")
        else:
            ref = load_skill(skill_name, category="dimensions")
        if ref:
            parts.append(f"\n\n# Knowledge: {skill_name}\n\n{ref}")

    return "\n".join(parts)
