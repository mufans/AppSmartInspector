"""Metric QA node: natural language queries on specific performance metrics."""

import json
import logging
import threading

from langchain_core.messages import AIMessage, HumanMessage, SystemMessage
from langchain_openai import ChatOpenAI

from smartinspector.config import get_llm_kwargs
from smartinspector.graph.state import AgentState, _pass_through, node_error_handler
from smartinspector.prompts import load_prompt
from smartinspector.token_tracker import get_tracker

logger = logging.getLogger(__name__)

_prompt = load_prompt("metric-qa")
_llm = None
_llm_lock = threading.Lock()


def _get_llm():
    global _llm
    if _llm is not None:
        return _llm
    with _llm_lock:
        if _llm is not None:
            return _llm
        _llm = ChatOpenAI(**get_llm_kwargs(temperature=0.1))
    return _llm


# Metric ID → display name (Chinese)
METRIC_NAMES: dict[str, str] = {
    "cpu": "CPU 占用率",
    "cpu_hotspot": "CPU 热点函数",
    "sched": "线程调度",
    "blocked": "主线程阻塞",
    "memory": "内存占用",
    "heap": "堆分析 / 对象分布",
    "frame": "帧率 / 卡顿",
    "rv": "RecyclerView",
    "view": "View 绘制",
    "compose": "Compose 重组",
    "inflate": "布局加载",
    "startup": "冷启动",
    "io": "IO 总览",
    "network": "网络请求",
    "db": "数据库查询",
    "image": "图片加载",
    "thread_state": "线程状态分布",
    "sys": "系统状态",
    "input": "输入事件",
    "overview": "性能总览",
}

# Metric ID → perf_summary top-level JSON keys
METRIC_DATA_MAP: dict[str, list[str]] = {
    "cpu": ["cpu_usage"],
    "cpu_hotspot": ["cpu_hotspots"],
    "sched": ["scheduling"],
    "blocked": ["block_events"],
    "memory": ["process_memory"],
    "heap": ["memory"],
    "frame": ["frame_timeline"],
    "rv": ["view_slices"],
    "view": ["view_slices"],
    "compose": ["compose_slices"],
    "inflate": ["view_slices"],
    "startup": [],
    "io": ["io_slices"],
    "network": ["io_slices"],
    "db": ["io_slices"],
    "image": ["io_slices"],
    "thread_state": ["thread_state"],
    "sys": ["sys_stats"],
    "input": ["input_events"],
    "overview": [],
}


def _filter_rv(data: dict) -> dict:
    """Keep only rv_instances from view_slices."""
    vs = data.get("view_slices", {})
    return {"rv_instances": vs.get("rv_instances", [])} if isinstance(vs, dict) else {}


def _filter_view(data: dict) -> dict:
    """Keep only slowest_slices from view_slices."""
    vs = data.get("view_slices", {})
    return {"slowest_slices": vs.get("slowest_slices", [])} if isinstance(vs, dict) else {}


def _filter_inflate(data: dict) -> dict:
    """Keep only SI$inflate# slices from view_slices."""
    vs = data.get("view_slices", {})
    if not isinstance(vs, dict):
        return {}
    slices = vs.get("slowest_slices", [])
    inflate_slices = [
        s for s in slices
        if isinstance(s, dict) and s.get("name", "").startswith("SI$inflate#")
    ]
    return {"inflate_slices": inflate_slices}


def _filter_io_type(io_type: str):
    """Return a filter function that keeps only io_slices of a given io_type."""
    def _filter(data: dict) -> dict:
        ios = data.get("io_slices", {})
        if not isinstance(ios, dict):
            return {}
        all_slices = ios.get("slices", [])
        filtered = [s for s in all_slices if isinstance(s, dict) and s.get("io_type") == io_type]
        return {"slices": filtered, "summary": ios.get("summary", "")}
    return _filter


_METRIC_FILTERS: dict[str, callable] = {
    "rv": _filter_rv,
    "view": _filter_view,
    "inflate": _filter_inflate,
    "network": _filter_io_type("network"),
    "db": _filter_io_type("database"),
    "image": _filter_io_type("image"),
}


def extract_metric_data(perf_json_str: str, metric_id: str) -> str:
    """Extract the data segment for a given metric from perf_summary JSON.

    Args:
        perf_json_str: Raw perf_summary JSON string.
        metric_id: One of the keys in METRIC_DATA_MAP.

    Returns:
        JSON string of the extracted data segment, or empty string if not found.
    """
    try:
        perf = json.loads(perf_json_str)
    except (json.JSONDecodeError, TypeError):
        return perf_json_str[:2000] if perf_json_str else ""

    # overview: aggregate all top-level keys with brief summaries
    if metric_id == "overview":
        overview = {}
        for key in ("cpu_usage", "cpu_hotspots", "scheduling", "block_events",
                     "process_memory", "memory", "frame_timeline", "view_slices",
                     "io_slices", "thread_state", "sys_stats", "input_events"):
            if key in perf:
                val = perf[key]
                if isinstance(val, dict):
                    # Keep first-level summary only
                    overview[key] = {k: v for k, v in list(val.items())[:5]}
                else:
                    overview[key] = val
        return json.dumps(overview, ensure_ascii=False, indent=2)

    # startup: not in perf_summary normally — extract from perf_analysis if available
    if metric_id == "startup":
        return "startup 数据不在 perf_summary 中，请参考已有的启动分析结果。"

    keys = METRIC_DATA_MAP.get(metric_id, [])
    if not keys:
        return json.dumps(perf, ensure_ascii=False)[:2000]

    extracted = {}
    for key in keys:
        if key in perf:
            extracted[key] = perf[key]

    # Apply metric-specific filter
    filter_fn = _METRIC_FILTERS.get(metric_id)
    if filter_fn:
        extracted = filter_fn(extracted)

    if not extracted:
        return ""

    return json.dumps(extracted, ensure_ascii=False, indent=2)


@node_error_handler("metric_qa")
def metric_qa_node(state: AgentState) -> dict:
    """Answer natural language queries about specific performance metrics."""
    # 1. Parse metric_id from _route
    route = state.get("_route", "")
    metric_id = route.split(":")[1] if ":" in route else "overview"
    if metric_id not in METRIC_NAMES:
        metric_id = "overview"

    # 2. Check perf_summary exists
    perf_summary = state.get("perf_summary", "")
    if not perf_summary:
        return {
            "messages": [AIMessage(content="请先运行 /full 或 /trace 采集数据后再查询指标。")],
            **_pass_through(state),
        }

    # 3. Extract metric data
    data = extract_metric_data(perf_summary, metric_id)
    metric_name = METRIC_NAMES.get(metric_id, "性能总览")

    if not data:
        return {
            "messages": [AIMessage(content=f"该 trace 中没有采集到「{metric_name}」相关数据。")],
            **_pass_through(state),
        }

    # 4. Get user's question from messages
    user_question = ""
    for m in reversed(state.get("messages", [])):
        if isinstance(m, dict):
            if m.get("role") == "user":
                user_question = m.get("content", "")
                break
        else:
            if getattr(m, "type", "") == "human":
                user_question = getattr(m, "content", "")
                break

    # 5. Call LLM
    logger.info("Metric QA: metric_id=%s, metric_name=%s", metric_id, metric_name)
    system_prompt = _prompt.format(metric_name=metric_name, data=data)
    user_content = user_question or f"请分析一下{metric_name}的情况。"

    llm = _get_llm()
    response = llm.invoke([
        SystemMessage(content=system_prompt),
        HumanMessage(content=user_content),
    ])
    get_tracker().record_from_message("metric_qa", response)

    return {
        "messages": [AIMessage(content=response.content)],
        **_pass_through(state),
    }
