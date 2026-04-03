"""Load prompt text files from the prompts/ directory."""

from pathlib import Path

_PROMPTS_DIR = Path(__file__).resolve().parent.parent.parent / "prompts"


def load_prompt(name: str) -> str:
    """Load a prompt file by name (without .txt extension).

    Args:
        name: Prompt file name, e.g. "main", "code-explorer".

    Returns:
        The prompt text content.
    """
    path = _PROMPTS_DIR / f"{name}.txt"
    return path.read_text(encoding="utf-8")
