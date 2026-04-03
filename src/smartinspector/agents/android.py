"""Android Expert agent: Perfetto trace collection and analysis."""

from langchain.agents import create_agent
from langchain_openai import ChatOpenAI

from smartinspector.config import get_llm_kwargs
from smartinspector.tools.perfetto import analyze_perfetto, collect_android_trace
from smartinspector.prompts import load_prompt

_agent = None


def get_android_agent():
    """Return the compiled Android expert agent (singleton)."""
    global _agent
    if _agent is not None:
        return _agent

    llm = ChatOpenAI(**get_llm_kwargs(temperature=0.1, streaming=True))
    prompt = load_prompt("android-expert")
    _agent = create_agent(
        model=llm,
        tools=[analyze_perfetto, collect_android_trace],
        system_prompt=prompt,
    )
    return _agent
