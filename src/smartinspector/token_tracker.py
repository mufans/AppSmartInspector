"""Token usage tracker for SmartInspector LLM calls.

Accumulates token counts from all LLM calls during a session or pipeline run.
Tracks per-stage breakdown (orchestrator, analyzer, attributor, reporter, etc).
"""

import threading


class TokenTracker:
    """Thread-safe token usage accumulator."""

    def __init__(self):
        self._lock = threading.RLock()
        self._stages: dict[str, dict] = {}

    def record(self, stage: str, usage: dict | None) -> None:
        """Record token usage from one LLM call.

        Args:
            stage: Pipeline stage name (e.g. "orchestrator", "attributor").
            usage: Dict from response.usage_metadata or response_metadata.token_usage.
                   Expected keys: input_tokens/prompt_tokens, output_tokens/completion_tokens.
        """
        if not usage:
            return

        # Normalize keys
        input_tokens = usage.get("input_tokens") or usage.get("prompt_tokens") or 0
        output_tokens = usage.get("output_tokens") or usage.get("completion_tokens") or 0

        with self._lock:
            if stage not in self._stages:
                self._stages[stage] = {"input_tokens": 0, "output_tokens": 0, "calls": 0}
            self._stages[stage]["input_tokens"] += input_tokens
            self._stages[stage]["output_tokens"] += output_tokens
            self._stages[stage]["calls"] += 1

    def record_from_message(self, stage: str, message: object) -> None:
        """Record token usage from a LangChain message object.

        Tries usage_metadata first, then response_metadata.token_usage.
        """
        um = getattr(message, "usage_metadata", None)
        if um:
            self.record(stage, um)
            return

        rm = getattr(message, "response_metadata", {})
        tu = rm.get("token_usage") if rm else None
        if tu:
            self.record(stage, tu)

    def record_from_messages(self, stage: str, messages: list) -> None:
        """Record token usage from a list of LangChain messages (skips human/tool)."""
        for msg in messages:
            msg_type = getattr(msg, "type", "")
            if msg_type == "ai":
                self.record_from_message(stage, msg)

    @property
    def total_input(self) -> int:
        with self._lock:
            return sum(s["input_tokens"] for s in self._stages.values())

    @property
    def total_output(self) -> int:
        with self._lock:
            return sum(s["output_tokens"] for s in self._stages.values())

    @property
    def total_tokens(self) -> int:
        return self.total_input + self.total_output

    @property
    def total_calls(self) -> int:
        with self._lock:
            return sum(s["calls"] for s in self._stages.values())

    @staticmethod
    def _fmt(n: int) -> str:
        """Format token count: >=1000 as X.Xk, otherwise plain integer."""
        if n >= 1000:
            return f"{n / 1000:.1f}k"
        return str(n)

    def summary(self) -> str:
        """Return a human-readable summary of token usage."""
        with self._lock:
            if not self._stages:
                return "Token usage: no LLM calls recorded"

            lines = ["Token usage:"]
            lines.append(f"{'Stage':20s} {'Input':>8s} {'Output':>8s} {'Total':>8s} {'Calls':>6s}")
            lines.append("-" * 54)

            for stage in self._stages:
                s = self._stages[stage]
                total = s["input_tokens"] + s["output_tokens"]
                lines.append(
                    f"{stage:20s} {self._fmt(s['input_tokens']):>8s} {self._fmt(s['output_tokens']):>8s} "
                    f"{self._fmt(total):>8s} {s['calls']:>6d}"
                )

            lines.append("-" * 54)
            ti = self.total_input
            to = self.total_output
            lines.append(f"{'TOTAL':20s} {self._fmt(ti):>8s} {self._fmt(to):>8s} {self._fmt(ti + to):>8s} {self.total_calls:>6d}")

            return "\n".join(lines)

    def to_dict(self) -> dict:
        """Return token usage as a serializable dict."""
        with self._lock:
            return {
                "stages": dict(self._stages),
                "total_input": self.total_input,
                "total_output": self.total_output,
                "total_tokens": self.total_tokens,
                "total_calls": self.total_calls,
            }

    def reset(self) -> None:
        """Clear all recorded usage."""
        with self._lock:
            self._stages.clear()


# Global singleton — one tracker per CLI session
_tracker = TokenTracker()


def get_tracker() -> TokenTracker:
    """Get the global token tracker instance."""
    return _tracker
