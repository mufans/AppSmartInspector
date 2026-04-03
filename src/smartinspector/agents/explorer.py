"""Code Explorer agent: searches source code with grep/glob/read tools."""

from langchain.agents import create_agent
from langchain_openai import ChatOpenAI

from smartinspector.config import get_llm_kwargs
from smartinspector.tools.grep import grep
from smartinspector.tools.glob import glob
from smartinspector.tools.read import read
from smartinspector.prompts import load_prompt

# Compile the agent once, reuse across calls
_agent = None


def _get_agent():
    global _agent
    if _agent is not None:
        return _agent

    llm = ChatOpenAI(**get_llm_kwargs(temperature=0.1, streaming=True))
    system_prompt = load_prompt("code-explorer")
    _agent = create_agent(
        model=llm,
        tools=[grep, glob, read],
        system_prompt=system_prompt,
    )
    return _agent


def run_explorer(query: str) -> str:
    """Run the code explorer agent on a query.

    Args:
        query: What to search for, e.g. "find the LazyForEach usage in ListPage.ets"

    Returns:
        The explorer agent's text response.
    """
    agent = _get_agent()
    state = {"messages": [{"role": "user", "content": query}]}
    result = agent.invoke(state)
    messages = result.get("messages", [])
    if messages:
        last = messages[-1]
        return getattr(last, "content", str(last))
    return ""


# Expose as a LangGraph-compatible compiled graph
def get_explorer_graph():
    """Return the compiled explorer agent graph (for use as a subgraph node)."""
    return _get_agent()
