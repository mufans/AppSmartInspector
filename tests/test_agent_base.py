"""Tests for BaseAgent, SmartTruncator, and related utilities."""

import json
import pytest

from smartinspector.agents.base import BaseAgent
from smartinspector.agents.truncator import (
    SmartTruncator,
    TruncationResult,
    Section,
    _split_json_sections,
    _split_markdown_sections,
)


# ---------------------------------------------------------------------------
# Helpers — concrete test agent
# ---------------------------------------------------------------------------


class StubAgent(BaseAgent):
    """Minimal agent for testing."""

    _role = "default"
    _temperature = 0.1

    def execute(self, **kwargs):
        return kwargs.get("input", "stub result")


class FullAgent(BaseAgent):
    """Agent that tests all BaseAgent methods."""

    def execute(self, **kwargs):
        return "done"


# ---------------------------------------------------------------------------
# BaseAgent
# ---------------------------------------------------------------------------


class TestBaseAgent:
    def test_cannot_instantiate_directly(self):
        with pytest.raises(TypeError):
            BaseAgent()  # type: ignore[abstract]

    def test_concrete_subclass_execute(self):
        agent = StubAgent()
        result = agent.execute(input="hello")
        assert result == "hello"

    def test_name_property(self):
        agent = StubAgent()
        assert agent.name == "StubAgent"

    def test_role_and_temperature_defaults(self):
        agent = StubAgent()
        assert agent._role == "default"
        assert agent._temperature == 0.1

    def test_reset_llm(self):
        agent = StubAgent()
        agent._llm = "fake"  # type: ignore[assignment]
        agent.reset_llm()
        assert agent._llm is None

    def test_track_tokens_does_not_crash(self):
        """_track_tokens should handle None usage gracefully."""
        agent = FullAgent()
        # Pass a mock message with no usage metadata
        class FakeMsg:
            usage_metadata = None
            response_metadata = {}
        agent._track_tokens("test_stage", FakeMsg())

    def test_run_with_verification_passes(self):
        """When verification passes, content is returned unchanged."""
        agent = FullAgent()

        def good_verify(text, hints):
            from smartinspector.agents.verifier import VerificationResult
            return VerificationResult(score=1.0, passed=True)

        result = agent.run_with_verification("good content", "hints", verify_fn=good_verify)
        assert result == "good content"

    def test_run_with_verification_l2_failure_returns_missing(self):
        """When L2 fails, returns the missing items string for retry."""
        agent = FullAgent()

        def bad_l2_verify(text, hints):
            from smartinspector.agents.verifier import VerificationResult
            return VerificationResult(
                score=0.5,
                issues=["[L2] P0 issue missed"],
                passed=False,
            )

        result = agent.run_with_verification("bad content", "hints", verify_fn=bad_l2_verify)
        assert "[L2]" in result

    def test_run_with_verification_l1_failure_no_retry(self):
        """When L1 fails, content is returned as-is (no retry)."""
        agent = FullAgent()

        def bad_l1_verify(text, hints):
            from smartinspector.agents.verifier import VerificationResult
            return VerificationResult(
                score=0.2,
                issues=["[L1] Too short"],
                passed=False,
            )

        result = agent.run_with_verification("bad", "hints", verify_fn=bad_l1_verify)
        assert result == "bad"


# ---------------------------------------------------------------------------
# Section splitting
# ---------------------------------------------------------------------------


class TestSplitJsonSections:
    def test_valid_json(self):
        data = {"cpu_usage": {"pct": 50}, "memory": {"rss": 100}}
        sections = _split_json_sections(json.dumps(data))
        names = {s.name for s in sections}
        assert names == {"cpu_usage", "memory"}

    def test_invalid_json(self):
        sections = _split_json_sections("not json at all")
        assert len(sections) == 1
        assert sections[0].name == "raw"

    def test_non_dict_json(self):
        sections = _split_json_sections("[1, 2, 3]")
        assert len(sections) == 1
        assert sections[0].name == "raw"

    def test_section_priorities(self):
        data = {"attribution": {"x": 1}, "metadata": {"y": 2}}
        sections = _split_json_sections(json.dumps(data))
        by_name = {s.name: s.priority for s in sections}
        assert by_name["attribution"] == 1
        assert by_name["metadata"] == 5


class TestSplitMarkdownSections:
    def test_no_headers(self):
        sections = _split_markdown_sections("Just some text")
        assert len(sections) == 1
        assert sections[0].name == "header"

    def test_with_headers(self):
        text = "## P0 Issues\nSome content\n## P1 Issues\nMore content"
        sections = _split_markdown_sections(text)
        assert len(sections) == 2
        assert sections[0].name == "P0 Issues"
        assert sections[1].name == "P1 Issues"

    def test_preamble_and_headers(self):
        text = "Summary\n## Section A\nBody A\n## Section B\nBody B"
        sections = _split_markdown_sections(text)
        assert len(sections) == 3
        assert sections[0].name == "header"
        assert sections[1].name == "Section A"


# ---------------------------------------------------------------------------
# Section
# ---------------------------------------------------------------------------


class TestSection:
    def test_est_tokens(self):
        s = Section(name="test", content="a" * 400)
        assert s.est_tokens == 100

    def test_est_tokens_minimum(self):
        s = Section(name="test", content="hi")
        assert s.est_tokens == 1

    def test_priority_default(self):
        s = Section(name="test", content="x")
        assert s.priority == 5


# ---------------------------------------------------------------------------
# SmartTruncator
# ---------------------------------------------------------------------------


class TestSmartTruncator:
    def test_empty_content(self):
        t = SmartTruncator(budget_tokens=1000)
        result = t.truncate("")
        assert result.content == ""
        assert result.original_tokens == 0
        assert result.sections_kept == 0

    def test_content_fits_budget(self):
        t = SmartTruncator(budget_tokens=10000)
        content = "x" * 100  # ~25 tokens, well under budget
        result = t.truncate(content)
        assert result.content == content
        assert result.original_tokens == result.truncated_tokens
        assert result.sections_dropped == []

    def test_json_truncation_drops_low_priority(self):
        """High-priority sections are kept, low-priority are dropped."""
        data = {
            "attribution": {"items": ["a" * 100]},  # priority 1
            "thread_state": [{"state": "Running"}],  # priority 2
            "metadata": {"device": "x" * 500},  # priority 5
        }
        content = json.dumps(data)
        # Use a very tight budget to force drops
        t = SmartTruncator(budget_tokens=50, chars_per_token=4)
        result = t.truncate(content, content_type="json")
        assert result.original_tokens > result.truncated_tokens
        assert "attribution" in result.content

    def test_markdown_truncation(self):
        """Markdown sections are prioritized correctly."""
        text = (
            "## attribution\nFound the issue\n"
            "## metadata\n" + "x" * 2000 + "\n"
            "## severity\nP0 issue found\n"
        )
        t = SmartTruncator(budget_tokens=100, chars_per_token=4)
        result = t.truncate(text, content_type="markdown")
        assert result.original_tokens > result.truncated_tokens

    def test_auto_detect_json(self):
        t = SmartTruncator(budget_tokens=10000)
        result = t.truncate('{"key": "value"}')
        assert result.sections_kept >= 1

    def test_auto_detect_markdown(self):
        t = SmartTruncator(budget_tokens=10000)
        result = t.truncate("## Hello\nWorld")
        assert result.sections_kept >= 1

    def test_truncation_result_fields(self):
        t = SmartTruncator(budget_tokens=10, chars_per_token=4)
        content = "a" * 1000  # ~250 tokens, far over budget
        result = t.truncate(content, content_type="markdown")
        assert isinstance(result, TruncationResult)
        assert result.original_tokens > result.truncated_tokens
        assert result.truncated_tokens <= t.budget_tokens + 50  # some slack for truncation markers
        assert isinstance(result.sections_dropped, list)

    def test_reassemble_json_preserves_structure(self):
        """Reassembled JSON should be valid and contain kept keys."""
        data = {
            "cpu_usage": {"pct": 50},
            "memory": {"rss": 100},
        }
        t = SmartTruncator(budget_tokens=10000)
        result = t.truncate(json.dumps(data), content_type="json")
        parsed = json.loads(result.content)
        assert "cpu_usage" in parsed
        assert "memory" in parsed

    def test_budget_reduction(self):
        """Tighter budget should produce shorter output."""
        data = {
            "attribution": {"x": "a" * 200},
            "metadata": {"y": "b" * 200},
            "cpu_usage": {"z": "c" * 200},
            "thread_state": [{"w": "d" * 200}],
        }
        content = json.dumps(data)

        t_generous = SmartTruncator(budget_tokens=5000)
        t_tight = SmartTruncator(budget_tokens=100)

        r_generous = t_generous.truncate(content, content_type="json")
        r_tight = t_tight.truncate(content, content_type="json")

        assert len(r_tight.content) < len(r_generous.content)
