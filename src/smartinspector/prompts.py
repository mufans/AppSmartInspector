"""Load prompt instructions and dimension skill knowledge on demand."""

from pathlib import Path

_PROMPTS_DIR = Path(__file__).resolve().parent.parent.parent / "prompts"
_SKILLS_DIR = _PROMPTS_DIR / "skills"
_DIMENSIONS_DIR = _SKILLS_DIR / "dimensions"
_SHARED_DIR = _SKILLS_DIR / "shared"

_skill_cache: dict[str, str] = {}


def load_prompt(name: str) -> str:
    """Load a prompt file by name (without .txt extension).

    Args:
        name: Prompt file name, e.g. "attributor", "report-generator".

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
