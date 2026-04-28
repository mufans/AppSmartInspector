"""Orchestrator node: LLM-based routing + fallback node."""

from langchain_core.messages import SystemMessage, HumanMessage, AIMessage
from langchain_openai import ChatOpenAI

from smartinspector.config import get_llm_kwargs
from smartinspector.token_tracker import get_tracker
from smartinspector.graph.state import AgentState, RouteDecision, _pass_through, node_error_handler
import logging

logger = logging.getLogger(__name__)


_ROUTE_PROMPT = """Classify this user message. Reply with ONE label only.

Categories (pick ONE):
- full_analysis : wants a COMPLETE performance analysis pipeline including trace collection, analysis, source attribution, and report (keywords: 全面分析/完整分析/全量分析/full/归因/冷启动/启动耗时/启动时间/启动分析/启动优化/应用启动/app启动/cold start/启动性能)
- explorer : wants to SEARCH or READ source code (keywords: 源码/代码/搜索/查看/定位/函数/grep/.ets/.ts/.java)
- android : wants to COLLECT or ANALYZE performance from Android device (keywords: trace/adb/采集/perfetto/FPS/CPU/内存指标)
- analyze : wants deep interpretation of an ALREADY EXISTING perf JSON summary that is present in context (keywords: 解读perf_summary/分析这份数据/解读一下这个)
- metric_qa : user is asking about a SPECIFIC performance metric from already-collected data. MUST include colon and metric_id.
  cpu占用率/cpu usage → metric_qa:cpu
  cpu热点/火焰图/hot function → metric_qa:cpu_hotspot
  调度/上下文切换/context switch → metric_qa:sched
  阻塞/卡住/block/ANR → metric_qa:blocked
  内存/memory/RSS → metric_qa:memory
  堆/heap/对象/leak/内存泄漏 → metric_qa:heap
  帧率/fps/卡顿/jank/掉帧 → metric_qa:frame
  滚动/recycler/列表 → metric_qa:rv
  绘制/draw/measure/layout → metric_qa:view
  compose/重组/recompose → metric_qa:compose
  布局加载/inflate → metric_qa:inflate
  启动/冷启动/startup → metric_qa:startup
  io/磁盘 → metric_qa:io
  网络/network/请求 → metric_qa:network
  数据库/db/query/sql → metric_qa:db
  图片/glide/coil → metric_qa:image
  线程状态/sleeping → metric_qa:thread_state
  系统状态/cpu频率 → metric_qa:sys
  触摸/touch → metric_qa:input
  性能怎么样/overall/summary → metric_qa:overview
- end : general Q&A, advice, or vague analysis request WITHOUT existing data (keywords: 什么是/怎么优化/如何/为什么)

CRITICAL:
- If the user wants the full pipeline (trace + analyze + source attribution) → MUST be full_analysis
- 启动/冷启动 related analysis MUST be full_analysis (needs trace collection first)
- If the user mentions 源码/代码/搜索/查看文件/函数名 → MUST be explorer
- If the user says 分析性能/帮我分析 but has NOT provided perf data → MUST be end (let LLM guide them)
- analyze should ONLY be used when user explicitly references existing perf data already in context
- metric_qa is for SHORT follow-up questions about SPECIFIC metrics (cpu/fps/memory/etc.) when data has ALREADY been collected

Examples:
- "帮我全面分析一下这个页面的性能" → full_analysis
- "分析冷启动耗时" → full_analysis
- "测一下应用启动时间" → full_analysis
- "搜索一下 LazyForEach 的实现" → explorer
- "采集一下 trace" → android
- "你好" → end
- "怎么优化列表滑动" → end
- "分析一下刚才采集的这份数据" → analyze
- "cpu占用率怎么样" → metric_qa:cpu
- "帧率怎么样" → metric_qa:frame
- "内存有没有泄漏" → metric_qa:heap
- "滚动卡不卡" → metric_qa:rv
- "性能怎么样" → metric_qa:overview

Reply with exactly one label: full_analysis explorer android analyze metric_qa:<id> end"""

_route_llm = None


def _get_route_llm():
    global _route_llm
    if _route_llm is not None:
        return _route_llm
    _route_llm = ChatOpenAI(**get_llm_kwargs(temperature=0, max_tokens=5))
    return _route_llm


@node_error_handler("orchestrator")
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
    # Special-case metric_qa: preserve the full "metric_qa:<id>" string
    if raw.startswith("metric_qa"):
        decision = raw  # e.g. "metric_qa:cpu"
    else:
        valid = {rd.value: rd for rd in RouteDecision}
        decision = RouteDecision.END
        for v, rd in valid.items():
            if v in raw:
                decision = rd
                break

    if isinstance(decision, str) and decision.startswith("metric_qa"):
        print("  正在查询性能指标...", flush=True)  # noqa: LOG — user-facing progress
    elif decision != RouteDecision.END:
        _ROUTE_LABELS = {
            RouteDecision.FULL_ANALYSIS: "正在启动全量性能分析...",
            RouteDecision.STARTUP: "正在启动冷启动分析...",
            RouteDecision.ANDROID: "正在采集设备性能数据...",
            RouteDecision.ANALYZE: "正在分析性能数据...",
            RouteDecision.EXPLORER: "正在搜索源码...",
        }
        print(f"  {_ROUTE_LABELS.get(decision, '处理中...')}", flush=True)  # noqa: LOG — user-facing progress

    # Detect cold-start / startup profiling intent for skip_wait
    skip_wait = False
    if decision == RouteDecision.FULL_ANALYSIS and user_msg:
        _STARTUP_KEYWORDS = (
            "冷启动", "启动耗时", "启动时间", "启动性能", "cold start", "cold_start",
            "启动分析", "启动优化", "开机", "app启动", "应用启动",
        )
        user_msg_lower = user_msg.lower()
        skip_wait = any(kw in user_msg_lower for kw in _STARTUP_KEYWORDS)
        if skip_wait:
            # Re-route to dedicated startup analysis pipeline
            decision = RouteDecision.STARTUP
            logger.info("Detected startup analysis intent, routing to startup analyzer")

    return {"messages": [], "_route": decision, "skip_wait": skip_wait, **_pass_through(state)}


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

    # Extract recent conversation for context (filter out ToolMessage to save tokens)
    recent = []
    for m in messages:
        if isinstance(m, dict):
            role = m.get("role", "")
            content = m.get("content", "")
            if role == "user":
                recent.append(HumanMessage(content=content))
            elif role == "assistant":
                recent.append(AIMessage(content=content))
        else:
            msg_type = getattr(m, "type", "")
            if msg_type in ("human", "ai"):
                recent.append(m)
    # Keep only the last 6 valid conversation messages
    recent = recent[-6:]

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

    # Handle metric_qa:<id> format — route to metric_qa node regardless of id
    if isinstance(decision, str) and decision.startswith("metric_qa"):
        return "metric_qa"

    # Mapping supports both enum values and string values
    mapping = {
        RouteDecision.FULL_ANALYSIS: "collector",
        RouteDecision.FULL_ANALYSIS.value: "collector",
        RouteDecision.STARTUP: "collector",
        RouteDecision.STARTUP.value: "collector",
        RouteDecision.ANDROID: "android_expert",
        RouteDecision.ANDROID.value: "android_expert",
        RouteDecision.ANALYZE: "perf_analyzer",
        RouteDecision.ANALYZE.value: "perf_analyzer",
        RouteDecision.EXPLORER: "explorer",
        RouteDecision.EXPLORER.value: "explorer",
        RouteDecision.END: "fallback",
        RouteDecision.END.value: "fallback",
        RouteDecision.TRACE: "collector",
        RouteDecision.TRACE.value: "collector",
    }
    return mapping.get(decision, "fallback")


def route_from_android_expert(state: AgentState) -> str:
    """After android_expert: if perf_summary collected, continue to analysis pipeline."""
    if state.get("perf_summary"):
        return "analyzer"
    return "end"
