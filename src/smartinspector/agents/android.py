"""Android Expert agent: Perfetto trace collection and analysis."""

import threading

from langchain.agents import create_agent

from smartinspector.llm.factory import LLMFactory
from smartinspector.tools.perfetto import analyze_perfetto, collect_android_trace
from smartinspector.prompts import load_prompt

_agent = None
_agent_lock = threading.Lock()


def get_android_agent():
    """Return the compiled Android expert agent (singleton, thread-safe)."""
    global _agent
    if _agent is not None:
        return _agent
    with _agent_lock:
        if _agent is not None:
            return _agent
        llm = LLMFactory.get("android_expert", temperature=0.1, streaming=True)
        prompt = load_prompt("android-expert")
        _agent = create_agent(
            model=llm,
            tools=[analyze_perfetto, collect_android_trace],
            system_prompt=prompt,
        )
    return _agent
