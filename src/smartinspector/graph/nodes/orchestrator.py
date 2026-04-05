"""Orchestrator node: LLM-based routing + fallback node."""

from langchain_core.messages import SystemMessage, HumanMessage, AIMessage
from langchain_openai import ChatOpenAI

from smartinspector.config import get_llm_kwargs
from smartinspector.token_tracker import get_tracker
from smartinspector.graph.state import AgentState, RouteDecision, _pass_through


_ROUTE_PROMPT = """Classify this user message. Reply with ONE word only.

Categories (pick ONE):
- full_analysis : wants a COMPLETE performance analysis pipeline including trace collection, analysis, source attribution, and report (keywords: 全面分析/完整分析/全量分析/full/归因)
- explorer : wants to SEARCH or READ source code (keywords: 源码/代码/搜索/查看/定位/函数/grep/.ets/.ts/.java)
- android : wants to COLLECT or ANALYZE performance from Android device (keywords: trace/adb/采集/perfetto/FPS/CPU/内存指标)
- analyze : wants deep interpretation of an ALREADY EXISTING perf JSON summary that is present in context (keywords: 解读perf_summary/分析这份数据/解读一下这个)
- end : general Q&A, advice, or vague analysis request WITHOUT existing data (keywords: 什么是/怎么优化/如何/为什么/分析性能/帮我分析)

CRITICAL:
- If the user wants the full pipeline (trace + analyze + source attribution) → MUST be full_analysis
- If the user mentions 源码/代码/搜索/查看文件/函数名 → MUST be explorer
- If the user says 分析性能/帮我分析 but has NOT provided perf data → MUST be end (let LLM guide them)
- analyze should ONLY be used when user explicitly references existing perf data already in context

Reply with exactly one word: full_analysis explorer android analyze end"""

_route_llm = None


def _get_route_llm():
    global _route_llm
    if _route_llm is not None:
        return _route_llm
    _route_llm = ChatOpenAI(**get_llm_kwargs(temperature=0))
    return _route_llm


def orchestrator_node(state: AgentState) -> dict:
    """Pure LLM classification to decide routing."""
    messages = state.get("messages", [])

    # Extract last user message only
    user_msg = ""
    for m in reversed(messages):
        if isinstance(m, dict):
            if m.get("role") == "user":
                user_msg = m.get("content", "")
                break
        else:
            content = getattr(m, "content", "")
            msg_type = getattr(m, "type", "")
            if content and msg_type == "human":
                user_msg = content
                break

    if not user_msg:
        return {"messages": [], "_route": RouteDecision.END, **_pass_through(state)}

    orch_input = [
        SystemMessage(content=_ROUTE_PROMPT),
        HumanMessage(content=user_msg),
    ]

    llm = _get_route_llm()
    try:
        response = llm.invoke(orch_input)
        get_tracker().record_from_message("orchestrator", response)
        raw = response.content.strip().lower()
    except Exception as e:
        print(f"  [orchestrator] LLM call failed: {e}", flush=True)
        raw = ""

    # Extract valid label
    valid = {rd.value: rd for rd in RouteDecision}
    decision = RouteDecision.END
    for v, rd in valid.items():
        if v in raw:
            decision = rd
            break

    if decision != RouteDecision.END:
        _ROUTE_LABELS = {
            RouteDecision.FULL_ANALYSIS: "正在启动全量性能分析...",
            RouteDecision.ANDROID: "正在采集设备性能数据...",
            RouteDecision.ANALYZE: "正在分析性能数据...",
            RouteDecision.EXPLORER: "正在搜索源码...",
        }
        print(f"  {_ROUTE_LABELS.get(decision, '处理中...')}", flush=True)

    return {"messages": [], "_route": decision, **_pass_through(state)}


_FALLBACK_SYSTEM = """你是 SmartInspector，一个移动端性能分析助手。你的核心能力：

1. **全面分析**：自动采集性能 trace → 分析瓶颈 → 归因到源码 → 生成报告
2. **源码搜索**：搜索和查看项目源码（grep/glob/read）
3. **性能采集**：从设备采集性能数据（trace/FPS/CPU/内存）
4. **数据解读**：深入解读已有的性能分析结果

对用户的问候、闲聊、感谢等，请友好简短地回应，同时自然地提示你能做什么。
不要列编号清单，用口语化的方式回复。保持 2-3 句话即可。"""


def fallback_node(state: AgentState) -> dict:
    """Use LLM to generate a friendly reply for non-performance queries."""
    messages = state.get("messages", [])

    # Extract recent conversation for context
    recent = []
    for m in messages[-6:]:  # last 3 turns
        if isinstance(m, dict):
            role = m.get("role", "")
            content = m.get("content", "")
            if role == "user":
                recent.append(HumanMessage(content=content))
            elif role == "assistant":
                recent.append(AIMessage(content=content))
        else:
            recent.append(m)

    llm = _get_route_llm()
    response = llm.invoke([
        SystemMessage(content=_FALLBACK_SYSTEM),
        *recent,
    ])
    get_tracker().record_from_message("fallback", response)

    return {
        "messages": [AIMessage(content=response.content)],
        **_pass_through(state),
    }


def route_from_orchestrator(state: AgentState) -> str:
    """Map routing decision to node name."""
    decision = state.get("_route", "end")
    mapping = {
        RouteDecision.FULL_ANALYSIS: "collector",
        RouteDecision.ANDROID: "android_expert",
        RouteDecision.ANALYZE: "perf_analyzer",
        RouteDecision.EXPLORER: "explorer",
        RouteDecision.END: "fallback",
        RouteDecision.TRACE: "collector",
    }
    # Accept both enum values and raw strings for robustness
    if decision in mapping:
        return mapping[decision]
    return mapping.get(RouteDecision(decision), "fallback")


def route_from_android_expert(state: AgentState) -> str:
    """After android_expert: if perf_summary collected, continue to analysis pipeline."""
    if state.get("perf_summary"):
        return "analyzer"
    return "end"
