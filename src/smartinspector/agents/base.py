"""BaseAgent: abstract base class for all SmartInspector LLM agents.

Provides a unified calling convention, thread-safe LLM singleton management,
token tracking, and an optional verify-and-retry loop.

Agent implementations should subclass *BaseAgent* and override
:meth:`execute` (or the lower-level :meth:`_invoke_llm`).
"""

from __future__ import annotations

import abc
import threading
from typing import Any

from langchain_openai import ChatOpenAI

from smartinspector.config import get_llm_kwargs
from smartinspector.debug_log import info_log
from smartinspector.token_tracker import get_tracker


class BaseAgent(abc.ABC):
    """Abstract base for LLM-powered agents.

    Subclass and override :meth:`execute` with the agent's business logic.
    The base class provides:

    * Thread-safe LLM singleton via :meth:`get_llm`
    * Token tracking via the global ``TokenTracker``
    * A verify-and-retry wrapper via :meth:`run_with_verification`
    * A ``name`` property for logging / tracking purposes

    Attributes:
        _role: LLM role identifier (for model selection via config).
        _temperature: Default LLM temperature for this agent.
    """

    _role: str = "default"
    _temperature: float = 0.1

    def __init__(self) -> None:
        self._llm: ChatOpenAI | None = None
        self._llm_lock = threading.Lock()

    @property
    def name(self) -> str:
        """Agent name, derived from the class name (e.g. ``"PerfAnalyzer"``)."""
        return self.__class__.__name__

    # ── LLM singleton ───────────────────────────────────────────

    def get_llm(self) -> ChatOpenAI:
        """Return a thread-safe singleton LLM instance.

        The instance is lazily created on first access using the agent's
        ``_role`` and ``_temperature`` settings.
        """
        if self._llm is not None:
            return self._llm
        with self._llm_lock:
            if self._llm is not None:
                return self._llm
            self._llm = ChatOpenAI(
                **get_llm_kwargs(role=self._role, temperature=self._temperature),
            )
        return self._llm

    def reset_llm(self) -> None:
        """Force recreation of the LLM instance on next :meth:`get_llm` call.

        Useful in testing or after config changes.
        """
        with self._llm_lock:
            self._llm = None

    # ── Abstract interface ──────────────────────────────────────

    @abc.abstractmethod
    def execute(self, **kwargs: Any) -> Any:
        """Run the agent's core logic.

        Subclasses must implement this method with domain-specific
        parameters and return types.
        """
        ...

    # ── Verify-and-retry helper ─────────────────────────────────

    def run_with_verification(
        self,
        content: str,
        hints: str,
        *,
        verify_fn: Any | None = None,
        max_retries: int = 1,
    ) -> str:
        """Run verification on LLM output and optionally retry.

        This is a convenience wrapper that agents can use to validate
        their LLM output.  If verification fails at the L2 level, the
        content is regenerated once with additional context.

        Args:
            content: The LLM-generated text to verify.
            hints: The deterministic hints that were provided as input.
            verify_fn: A callable ``(text, hints) -> VerificationResult``.
                       Defaults to :func:`verify_analysis` from verifier.py.
            max_retries: Maximum number of retry attempts (default 1).

        Returns:
            The (possibly re-generated) content string.
        """
        if verify_fn is None:
            from smartinspector.agents.verifier import verify_analysis
            verify_fn = verify_analysis

        verification = verify_fn(content, hints)
        if verification.passed:
            return content

        info_log(
            self.name.lower(),
            f"WARNING: Verification issues: {'; '.join(verification.issues)} "
            f"(score={verification.score:.2f})",
        )
        if verification.warnings:
            for w in verification.warnings:
                info_log(self.name.lower(), f"WARNING:   {w}")

        if not verification.l2_passed and max_retries > 0:
            missing = "\n".join(
                f"- {i}" for i in verification.issues if "[L2]" in i
            )
            info_log(self.name.lower(), "Retrying with verification feedback...")
            return missing  # caller should handle retry

        return content

    # ── Token tracking ──────────────────────────────────────────

    def _track_tokens(self, stage: str, message: object) -> None:
        """Record token usage from a LangChain message."""
        get_tracker().record_from_message(stage, message)
