"""LLMFactory: centralized LLM instance creation and caching.

Provides thread-safe singleton LLM instances per role.  Replaces the
scattered global ``_llm = None`` + ``_llm_lock`` pattern found in each
agent module.

Usage::

    from smartinspector.llm.factory import LLMFactory

    # Simple invoke-style LLM
    llm = LLMFactory.get("perf_analyzer", temperature=0.1)

    # LLM with bound tools (attributor pattern)
    llm_tools = LLMFactory.get_with_tools("attributor", tools=[grep, glob, read])

    # Reset for testing
    LLMFactory.reset()
"""

import threading

from langchain_openai import ChatOpenAI

from smartinspector.config import get_llm_kwargs


class LLMFactory:
    """Centralized LLM instance management with thread-safe caching."""

    _instances: dict[str, ChatOpenAI] = {}
    _lock = threading.Lock()

    @classmethod
    def get(cls, role: str = "default", **overrides) -> ChatOpenAI:
        """Get or create a cached LLM instance for the given role.

        Args:
            role: Logical role name, e.g. "default", "attributor",
                  "router", "perf_analyzer", "frame_analyzer",
                  "metric_qa", "reporter".
            **overrides: Additional kwargs passed to ChatOpenAI.
                         Common: temperature, max_tokens, streaming.

        Returns:
            A ChatOpenAI instance (cached per role+overrides key).
        """
        key = cls._make_key(role, overrides)
        if key in cls._instances:
            return cls._instances[key]
        with cls._lock:
            if key in cls._instances:
                return cls._instances[key]
            kwargs = get_llm_kwargs(role=role)
            kwargs.update(overrides)
            cls._instances[key] = ChatOpenAI(**kwargs)
            return cls._instances[key]

    @classmethod
    def get_with_tools(cls, role: str, tools: list, **overrides):
        """Get an LLM instance with tools bound.

        Args:
            role: Logical role name.
            tools: List of tool callables to bind.
            **overrides: Additional kwargs for ChatOpenAI.

        Returns:
            A ChatOpenAI instance with tools bound via ``bind_tools()``.
        """
        return cls.get(role, **overrides).bind_tools(tools)

    @classmethod
    def reset(cls):
        """Clear all cached instances (for testing)."""
        with cls._lock:
            cls._instances.clear()

    @classmethod
    def _make_key(cls, role: str, overrides: dict) -> str:
        """Build a cache key from role and overrides."""
        frozen = frozenset(overrides.items())
        return f"{role}:{frozen}"
