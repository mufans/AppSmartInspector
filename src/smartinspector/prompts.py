"""Load prompt text files from the prompts/ directory."""

from pathlib import Path

_PROMPTS_DIR = Path(__file__).resolve().parent.parent.parent / "prompts"
_SKILLS_DIR = _PROMPTS_DIR / "skills"
_DIMENSIONS_DIR = _SKILLS_DIR / "dimensions"
_SHARED_DIR = _SKILLS_DIR / "shared"

_skill_cache: dict[str, str] = {}


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


def load_skills_for_dimensions(perf_json: str) -> str:
    """Load dimension skill knowledge on-demand based on actual trace data.

    Scans perf JSON for dimensions that have meaningful data (non-empty,
    non-error) and loads the corresponding skill knowledge files.

    Args:
        perf_json: PerfSummary JSON string.

    Returns:
        Concatenated skill knowledge text for dimensions with data.
    """
    import json

    try:
        data = json.loads(perf_json)
    except (json.JSONDecodeError, TypeError):
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

    # Load and concatenate
    parts: list[str] = []
    for skill_name in unique_skills:
        content = load_skill(skill_name, category="dimensions")
        if content:
            parts.append(f"\n\n# Knowledge: {skill_name}\n\n{content}")

    return "".join(parts)


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
