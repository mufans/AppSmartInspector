"""Smart truncator: token-budget-aware message truncation for LLM inputs.

Instead of blind ``text[:N]`` slicing, the truncator splits content into
*priority sections* and keeps the most important ones within a token budget.

Token estimation uses the heuristic ``1 token ≈ 4 characters`` which is
reasonable for mixed Chinese/English technical text with DeepSeek-class
models.

Usage::

    truncator = SmartTruncator(budget_tokens=3000)
    result = truncator.truncate(perf_json_string)
"""

from __future__ import annotations

import json
import re
from dataclasses import dataclass, field

from smartinspector.debug_log import debug_log


# ---------------------------------------------------------------------------
# Constants
# ---------------------------------------------------------------------------

# Approximate characters per token for mixed Chinese/English technical text.
_CHARS_PER_TOKEN = 4.0

# Section priorities: lower number = higher priority (kept first).
# Sections not in this mapping default to priority 5 (lowest).
_SECTION_PRIORITY: dict[str, int] = {
    "attribution": 1,
    "severity": 1,
    "thread_state": 2,
    "frame_timeline": 2,
    "view_slices": 2,
    "cpu_hotspots": 3,
    "block_events": 3,
    "io_slices": 3,
    "scheduling": 4,
    "cpu_usage": 4,
    "memory": 4,
    "metadata": 5,
    "sys_stats": 5,
    "input_events": 5,
    "compose_slices": 5,
}


# ---------------------------------------------------------------------------
# Section splitter
# ---------------------------------------------------------------------------


@dataclass
class Section:
    """A named content chunk produced by the truncator."""

    name: str
    content: str
    priority: int = 5

    @property
    def est_tokens(self) -> int:
        return max(1, int(len(self.content) / _CHARS_PER_TOKEN))


def _split_json_sections(json_str: str) -> list[Section]:
    """Split a perf-summary JSON string into named sections.

    Parses the top-level JSON keys into individual *Section* objects.
    Malformed JSON falls back to a single ``"raw"`` section.
    """
    try:
        data = json.loads(json_str)
    except (json.JSONDecodeError, TypeError):
        return [Section(name="raw", content=json_str, priority=5)]

    if not isinstance(data, dict):
        return [Section(name="raw", content=json_str, priority=5)]

    sections: list[Section] = []
    for key, value in data.items():
        chunk = json.dumps({key: value}, indent=2, ensure_ascii=False)
        priority = _SECTION_PRIORITY.get(key, 5)
        sections.append(Section(name=key, content=chunk, priority=priority))

    return sections


def _split_markdown_sections(text: str) -> list[Section]:
    """Split markdown text into sections by ``##`` headers.

    Each header + body becomes one *Section*.  Text before the first
    header is labelled ``"header"`` with priority 1.
    """
    parts = re.split(r"(^## .+$)", text, flags=re.MULTILINE)
    sections: list[Section] = []

    if parts and not parts[0].startswith("## "):
        preamble = parts[0].strip()
        if preamble:
            sections.append(Section(name="header", content=preamble, priority=1))

    for i in range(1, len(parts), 2):
        header = parts[i].strip()
        body = parts[i + 1].strip() if i + 1 < len(parts) else ""
        name = header.lstrip("# ").strip()
        # Infer priority from section title keywords
        priority = 3  # default
        name_lower = name.lower()
        for kw, pri in _SECTION_PRIORITY.items():
            if kw in name_lower:
                priority = pri
                break
        sections.append(Section(name=name, content=f"{header}\n{body}", priority=priority))

    return sections


# ---------------------------------------------------------------------------
# SmartTruncator
# ---------------------------------------------------------------------------


@dataclass
class TruncationResult:
    """Result of smart truncation."""

    content: str
    original_tokens: int
    truncated_tokens: int
    sections_kept: int
    sections_dropped: list[str] = field(default_factory=list)


class SmartTruncator:
    """Token-budget-aware truncator for LLM input content.

    Args:
        budget_tokens: Maximum estimated tokens in the output.
        chars_per_token: Characters per token heuristic (default 4).
    """

    def __init__(self, budget_tokens: int = 3000, chars_per_token: float = _CHARS_PER_TOKEN) -> None:
        self.budget_tokens = budget_tokens
        self.chars_per_token = chars_per_token

    def _est_tokens(self, text: str) -> int:
        return max(1, int(len(text) / self.chars_per_token))

    def truncate(self, content: str, content_type: str = "auto") -> TruncationResult:
        """Truncate *content* to fit within the token budget.

        Args:
            content: The input text to truncate.
            content_type: ``"json"``, ``"markdown"``, or ``"auto"`` (detect).

        Returns:
            A *TruncationResult* with the truncated content and metadata.
        """
        if not content:
            return TruncationResult(content="", original_tokens=0, truncated_tokens=0, sections_kept=0)

        original_tokens = self._est_tokens(content)

        # Short-circuit: content already fits
        if original_tokens <= self.budget_tokens:
            return TruncationResult(
                content=content,
                original_tokens=original_tokens,
                truncated_tokens=original_tokens,
                sections_kept=1,
            )

        # Detect content type
        if content_type == "auto":
            content_type = "json" if content.strip().startswith("{") else "markdown"

        # Split into sections
        if content_type == "json":
            sections = _split_json_sections(content)
        else:
            sections = _split_markdown_sections(content)

        if not sections:
            return TruncationResult(
                content=content[: self.budget_tokens * int(self.chars_per_token)],
                original_tokens=original_tokens,
                truncated_tokens=self.budget_tokens,
                sections_kept=1,
            )

        # Sort by priority (lower = higher priority), then by est_tokens ascending
        # within the same priority level (keep smaller sections first to fit more).
        sections.sort(key=lambda s: (s.priority, s.est_tokens))

        kept: list[Section] = []
        dropped: list[str] = []
        used_tokens = 0

        for sec in sections:
            if used_tokens + sec.est_tokens <= self.budget_tokens:
                kept.append(sec)
                used_tokens += sec.est_tokens
            else:
                # Try to fit a truncated version of this section
                remaining = self.budget_tokens - used_tokens
                if remaining > 50:  # only if meaningful content fits
                    max_chars = remaining * int(self.chars_per_token)
                    truncated_content = sec.content[:max_chars]
                    kept.append(Section(
                        name=sec.name,
                        content=truncated_content + "\n...[truncated]",
                        priority=sec.priority,
                    ))
                    used_tokens += remaining
                dropped.append(sec.name)

        # Reconstruct: sort kept sections back by their original order (priority)
        result_parts = [s.content for s in sorted(kept, key=lambda s: s.priority)]

        if content_type == "json":
            # Reassemble as a JSON object
            result_content = self._reassemble_json(kept)
        else:
            result_content = "\n\n".join(result_parts)

        debug_log(
            "truncator",
            f"Truncated {original_tokens} → {used_tokens} tokens "
            f"({len(kept)} kept, {len(dropped)} dropped: {dropped})",
        )

        return TruncationResult(
            content=result_content,
            original_tokens=original_tokens,
            truncated_tokens=used_tokens,
            sections_kept=len(kept),
            sections_dropped=dropped,
        )

    def _reassemble_json(self, sections: list[Section]) -> str:
        """Reassemble kept sections back into a single JSON object string."""
        merged: dict = {}
        for sec in sections:
            try:
                parsed = json.loads(sec.content)
                if isinstance(parsed, dict):
                    merged.update(parsed)
                else:
                    # Non-dict value, wrap in a generic key
                    merged[sec.name] = parsed
            except (json.JSONDecodeError, TypeError):
                # Truncated section — store as string under its key
                merged[sec.name] = sec.content
        return json.dumps(merged, indent=2, ensure_ascii=False)
