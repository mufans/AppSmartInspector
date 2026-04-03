"""SmartInspector CLI - multi-agent orchestration graph.

Architecture:
    START → orchestrator (LLM router)
               ├── android_expert → END
               ├── perf_analyzer  → END
               ├── explorer       → END
               ├── full_analysis  → collector → analyzer → attributor → reporter → END
               └── END (direct reply)
"""

import json
import os
import datetime
from typing import Annotated, TypedDict
import operator

from langchain_core.messages import SystemMessage, HumanMessage, AIMessage
from langchain_openai import ChatOpenAI
from langgraph.graph import StateGraph, START, END

from smartinspector.agents.android import get_android_agent
from smartinspector.agents.explorer import get_explorer_graph
from smartinspector.agents.perf_analyzer import analyze_perf
from smartinspector.agents.attributor import run_attribution
from smartinspector.commands import handle_slash_command
from smartinspector.config import get_llm_kwargs
from smartinspector.token_tracker import get_tracker


# ---------------------------------------------------------------------------
# State
# ---------------------------------------------------------------------------

class AgentState(TypedDict):
    """Shared state flowing through the graph."""
    messages: Annotated[list, operator.add]
    perf_summary: str
    perf_analysis: str
    attribution_data: str    # JSON: list of attributable SI$ slices
    attribution_result: str  # JSON: list of attribution results with source snippets
    _route: str              # internal: orchestrator routing decision
    _trace_path: str         # internal: trace file path from collector


# ---------------------------------------------------------------------------
# Node 1: Orchestrator — pure LLM classification, no tools
# ---------------------------------------------------------------------------

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
        return {
            "messages": [],
            "_route": "end",
            "perf_summary": state.get("perf_summary", ""),
            "perf_analysis": state.get("perf_analysis", ""),
            "attribution_data": state.get("attribution_data", ""),
            "attribution_result": state.get("attribution_result", ""),
        }

    orch_input = [
        SystemMessage(content=_ROUTE_PROMPT),
        HumanMessage(content=user_msg),
    ]

    llm = _get_route_llm()
    response = llm.invoke(orch_input)
    get_tracker().record_from_message("orchestrator", response)
    raw = response.content.strip().lower()

    # Extract valid label
    valid = {"full_analysis", "android", "analyze", "explorer", "end"}
    decision = "end"
    for v in valid:
        if v in raw:
            decision = v
            break

    if decision != "end":
        _ROUTE_LABELS = {
            "full_analysis": "正在启动全量性能分析...",
            "android": "正在采集设备性能数据...",
            "analyze": "正在分析性能数据...",
            "explorer": "正在搜索源码...",
        }
        print(f"  {_ROUTE_LABELS.get(decision, '处理中...')}", flush=True)

    return {
        "messages": [],
        "_route": decision,
        "perf_summary": state.get("perf_summary", ""),
        "perf_analysis": state.get("perf_analysis", ""),
        "attribution_data": state.get("attribution_data", ""),
        "attribution_result": state.get("attribution_result", ""),
    }


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
        "perf_summary": state.get("perf_summary", ""),
        "perf_analysis": state.get("perf_analysis", ""),
        "attribution_data": state.get("attribution_data", ""),
        "attribution_result": state.get("attribution_result", ""),
    }


def route_from_orchestrator(state: AgentState) -> str:
    """Map routing decision to node name."""
    decision = state.get("_route", "end")
    mapping = {
        "full_analysis": "collector",
        "android": "android_expert",
        "analyze": "perf_analyzer",
        "explorer": "explorer",
        "end": "fallback",
    }
    return mapping.get(decision, "fallback")


def route_from_android_expert(state: AgentState) -> str:
    """After android_expert: if perf_summary collected, continue to analysis pipeline."""
    if state.get("perf_summary"):
        return "analyzer"
    return "end"


# ---------------------------------------------------------------------------
# Node 2: Android Expert — Perfetto trace tools (streaming)
# ---------------------------------------------------------------------------

def android_expert_node(state: AgentState) -> dict:
    """Run the Android expert agent with streaming tool/token output."""
    agent = get_android_agent()

    tool_calls_seen = set()
    all_messages = []

    for event in agent.stream(
        {"messages": state["messages"]},
        stream_mode=["messages", "updates"],
        version="v2",
    ):
        kind = event.get("type")

        if kind == "messages":
            msg, _ = event["data"]
            if hasattr(msg, "content") and msg.content and isinstance(msg.content, str):
                print(msg.content, end="", flush=True)

        elif kind == "updates":
            for node_name, node_output in event["data"].items():
                msgs = node_output.get("messages", [])
                all_messages.extend(msgs)

                if node_name == "tools":
                    for tm in msgs:
                        tc_id = getattr(tm, "tool_call_id", None)
                        if tc_id and tc_id not in tool_calls_seen:
                            tool_calls_seen.add(tc_id)
                            name = getattr(tm, "name", "tool")
                            content = getattr(tm, "content", "")
                            preview = content[:120].replace("\n", " ")
                            print(f"\n  [tool: {name}] {preview}...", flush=True)
                            print("  ", end="", flush=True)

    print(flush=True)

    if not all_messages:
        result = agent.invoke({"messages": state["messages"]})
        all_messages = result.get("messages", [])

    # Record token usage
    get_tracker().record_from_messages("android_expert", all_messages)

    perf_summary = ""
    if all_messages:
        # Find perf_summary from analyze_perfetto tool output (ToolMessage),
        # not from the final AI summary which is markdown text.
        for msg in all_messages:
            msg_type = getattr(msg, "type", "")
            name = getattr(msg, "name", "")
            content = getattr(msg, "content", "")
            if msg_type == "tool" and name == "analyze_perfetto" and content:
                if '"scheduling"' in content or '"cpu_hotspots"' in content:
                    perf_summary = content
                    break
        # Fallback: check last message for direct JSON
        if not perf_summary:
            last = all_messages[-1]
            content = getattr(last, "content", "")
            if '"scheduling"' in content or '"cpu_hotspots"' in content:
                perf_summary = content

    if perf_summary:
        print("\n  [trace data collected, proceeding to analysis & attribution...]", flush=True)

    return {
        "messages": all_messages,
        "perf_summary": perf_summary,
        "perf_analysis": state.get("perf_analysis", ""),
        "attribution_data": state.get("attribution_data", ""),
        "attribution_result": state.get("attribution_result", ""),
    }


# ---------------------------------------------------------------------------
# Node 3: Perf Analyzer — single-shot LLM interpretation
# ---------------------------------------------------------------------------

def perf_analyzer_node(state: AgentState) -> dict:
    """Run single-shot perf analysis on the JSON summary."""
    perf_json = state.get("perf_summary", "")
    if not perf_json:
        for msg in reversed(state.get("messages", [])):
            content = getattr(msg, "content", "")
            if '"scheduling"' in content or '"cpu_hotspots"' in content:
                perf_json = content
                break

    if not perf_json:
        return {
            "messages": [AIMessage(content="未找到性能摘要数据，无法进行分析。")],
            "perf_summary": "",
            "perf_analysis": "",
            "attribution_data": "",
            "attribution_result": "",
        }

    analysis = analyze_perf(perf_json)

    return {
        "messages": [AIMessage(content=analysis)],
        "perf_summary": perf_json,
        "perf_analysis": analysis,
        "attribution_data": state.get("attribution_data", ""),
        "attribution_result": state.get("attribution_result", ""),
    }


# ---------------------------------------------------------------------------
# Node 4: Code Explorer — source code search
# ---------------------------------------------------------------------------

def explorer_node(state: AgentState) -> dict:
    """Run the code explorer agent."""
    explorer = get_explorer_graph()
    result = explorer.invoke({"messages": state["messages"]})
    return {
        "messages": result.get("messages", []),
        "perf_summary": state.get("perf_summary", ""),
        "perf_analysis": state.get("perf_analysis", ""),
        "attribution_data": state.get("attribution_data", ""),
        "attribution_result": state.get("attribution_result", ""),
    }


# ---------------------------------------------------------------------------
# Node 5: Collector — trace collection (first step of full pipeline)
# ---------------------------------------------------------------------------

def _read_perfetto_config() -> dict:
    """Read perfetto_collection params from WS server config cache.

    The Android app sends config_sync on WS connect (SIClient.onOpen),
    which includes perfetto_collection.trace_duration_ms etc.
    If no config cached (app never connected), returns empty dict → defaults.
    """
    from smartinspector.ws.server import SIServer

    server = SIServer.get()
    config_str = server.get_config()

    if not config_str:
        return {}

    try:
        config = json.loads(config_str)
        return config.get("perfetto_collection", {})
    except (json.JSONDecodeError, AttributeError):
        return {}


def collector_node(state: AgentState) -> dict:
    """Collect and analyze a Perfetto trace.

    Runs PerfettoCollector.pull_trace_from_device() + summarize().
    Reads perfetto params from WS server config cache (sent by app via config_sync).
    """
    from smartinspector.collector.perfetto import PerfettoCollector

    print("  [collector] Starting trace collection...", flush=True)

    try:
        # Read perfetto params from WS server config cache (app sends via config_sync)
        pc = _read_perfetto_config()
        duration_ms = int(pc.get("trace_duration_ms", 10000))
        buffer_size_kb = int(pc.get("buffer_size_kb", 65536))
        target_process = pc.get("target_process", "") or None

        print(f"  [collector] Config: duration={duration_ms}ms, buffer={buffer_size_kb}KB", flush=True)

        trace_path = PerfettoCollector.pull_trace_from_device(
            duration_ms=duration_ms,
            target_process=target_process,
            buffer_size_kb=buffer_size_kb,
        )
        print(f"  [collector] Trace saved to {trace_path}", flush=True)

        collector = PerfettoCollector(trace_path)
        summary = collector.summarize()

        # Request block events from app via WS (structured JSON, more reliable
        # than querying Perfetto's android_logs table which is often empty)
        try:
            from smartinspector.ws.server import SIServer
            server = SIServer.get()
            if server.has_connections():
                print("  [collector] Requesting block events from app...", flush=True)
                ws_events = server.request_block_events(timeout=5.0)
                if ws_events:
                    # Merge WS events into perf_summary, replacing SQL-based results
                    # WS events have: msgClass, durationMs, stackTrace, timestampMs
                    merged = []
                    for ev in ws_events:
                        raw_name = f"SI$block#{ev.get('msgClass', 'Unknown')}#{ev.get('durationMs', 0)}ms"
                        merged.append({
                            "raw_name": raw_name,
                            "ts_ns": 0,
                            "dur_ms": ev.get("durationMs", 0),
                            "stack_trace": ev.get("stackTrace", []),
                        })
                    summary.block_events = merged
                    print(f"  [collector] Got {len(merged)} block events from app", flush=True)
                else:
                    print("  [collector] No block events from app", flush=True)
        except Exception as e:
            print(f"  [collector] Block events request failed: {e}", flush=True)

        perf_json = summary.to_json()

        print(f"  [collector] Analysis complete ({len(perf_json)} bytes)", flush=True)

        return {
            "messages": [AIMessage(content="[trace collected and analyzed]")],
            "perf_summary": perf_json,
            "perf_analysis": state.get("perf_analysis", ""),
            "attribution_data": "",
            "attribution_result": "",
            "_trace_path": trace_path,
        }
        print(f"  [collector] Stored trace_path in state: {trace_path}", flush=True)
    except Exception as e:
        error_msg = f"Trace collection failed: {e}"
        print(f"  [collector] ERROR: {error_msg}", flush=True)
        return {
            "messages": [AIMessage(content=error_msg)],
            "perf_summary": "",
            "perf_analysis": "",
            "attribution_data": "",
            "attribution_result": "",
            "_trace_path": "",
        }


# ---------------------------------------------------------------------------
# Node 6: Analyzer — LLM performance analysis (pipeline step 2)
# ---------------------------------------------------------------------------

def analyzer_node(state: AgentState) -> dict:
    """Analyze perf summary JSON with LLM."""
    perf_json = state.get("perf_summary", "")
    if not perf_json:
        return {
            "messages": [AIMessage(content="[analyzer] No perf data to analyze")],
            "perf_summary": "",
            "perf_analysis": "",
            "attribution_data": "",
            "attribution_result": "",
            "_trace_path": state.get("_trace_path", ""),
        }

    print("  [analyzer] Analyzing performance...", flush=True)
    analysis = analyze_perf(perf_json)
    print(f"  [analyzer] Analysis complete ({len(analysis)} chars)", flush=True)

    return {
        "messages": [AIMessage(content=analysis)],
        "perf_summary": perf_json,
        "perf_analysis": analysis,
        "attribution_data": "",
        "attribution_result": "",
        "_trace_path": state.get("_trace_path", ""),
    }


# ---------------------------------------------------------------------------
# Node 7: Attributor — source code attribution (pipeline step 3)
# ---------------------------------------------------------------------------

def attributor_node(state: AgentState) -> dict:
    """Extract attributable slices and search source code."""
    from smartinspector.commands.attribution import extract_attributable_slices

    perf_json = state.get("perf_summary", "")
    if not perf_json:
        return {
            "messages": [AIMessage(content="[attributor] No perf data for attribution")],
            "perf_summary": state.get("perf_summary", ""),
            "perf_analysis": state.get("perf_analysis", ""),
            "attribution_data": "",
            "attribution_result": "",
            "_trace_path": state.get("_trace_path", ""),
        }

    print("  [attributor] Extracting attributable slices...", flush=True)
    attributable = extract_attributable_slices(perf_json, min_dur_ms=1.0)

    if not attributable:
        print("  [attributor] No attributable slices found", flush=True)
        return {
            "messages": [AIMessage(content="[attributor] No attributable slices found")],
            "perf_summary": perf_json,
            "perf_analysis": state.get("perf_analysis", ""),
            "attribution_data": "",
            "attribution_result": "",
            "_trace_path": state.get("_trace_path", ""),
        }

    print(f"  [attributor] Found {len(attributable)} slices, searching source code...", flush=True)
    for s in attributable[:5]:
        print(f"    {s['dur_ms']:>8.2f}ms  {s['class_name']}.{s['method_name']}  ({s.get('search_type', 'java')})", flush=True)

    results = run_attribution(attributable)

    # Summarize results
    found = sum(1 for r in results if r.get("attributable"))
    system = sum(1 for r in results if r.get("reason") == "system_class")
    print(f"  [attributor] Done: {found} attributed, {system} system classes", flush=True)

    return {
        "messages": [AIMessage(content=_format_attribution_summary(results))],
        "perf_summary": perf_json,
        "perf_analysis": state.get("perf_analysis", ""),
        "attribution_data": json.dumps(attributable),
        "attribution_result": json.dumps(results),
        "_trace_path": state.get("_trace_path", ""),
    }


def _format_attribution_summary(results: list[dict]) -> str:
    """Format attribution results as a human-readable summary."""
    lines = ["[source attribution results]\n"]

    for r in results:
        if r.get("attributable"):
            fp = r.get("file_path", "?")
            ls = r.get("line_start", "?")
            le = r.get("line_end", "?")
            snippet = r.get("source_snippet", "")
            lines.append(f"  FOUND: {r['class_name']}.{r['method_name']} ({r['dur_ms']:.2f}ms)")
            lines.append(f"    Location: {fp}:{ls}-{le}")
            if snippet:
                lines.append(f"    Finding: {snippet[:200]}")
        else:
            reason = r.get("reason", "unknown")
            lines.append(f"  SYSTEM: {r['class_name']}.{r['method_name']} ({r['dur_ms']:.2f}ms) [{reason}]")

    return "\n".join(lines)


# ---------------------------------------------------------------------------
# Node 8: Reporter — generate final report (pipeline step 4)
# ---------------------------------------------------------------------------

def reporter_node(state: AgentState) -> dict:
    """Generate the final performance report using LLM with streaming output."""
    from smartinspector.prompts import load_prompt

    report_prompt = load_prompt("report-generator")

    perf_json = state.get("perf_summary", "")
    perf_analysis = state.get("perf_analysis", "")
    attribution_result = state.get("attribution_result", "")

    # Build user content with all available data
    user_parts = []
    if perf_json:
        from smartinspector.agents.deterministic import compute_hints
        hints = compute_hints(perf_json)
        if hints:
            user_parts.append(f"## 预计算结论\n{hints}")

        # Extract key sections separately to avoid truncation losing CPU/memory data
        try:
            perf_data = json.loads(perf_json)
        except Exception:
            perf_data = {}

        # ── Pre-computed metrics for 性能总览 table ──
        # Compute explicit values so the LLM can fill the table directly
        cpu_usage = perf_data.get("cpu_usage", {})
        proc_mem = perf_data.get("process_memory", {})
        ft = perf_data.get("frame_timeline", {})
        metadata = perf_data.get("metadata", {})

        # ── Pre-generate report header tables (code does this, not LLM) ──
        trace_path = state.get("_trace_path", "")
        print(f"  [reporter] trace_path from state: '{trace_path}'", flush=True)

        from smartinspector.commands.orchestrate import _build_report_header
        header_md = _build_report_header(perf_json, trace_path)

        # Pass the pre-formatted header to LLM as context
        user_parts.append(header_md)

        # ── Frame timeline detail ──
        _total_frames = ft.get("total_frames", 0) if ft else 0
        _avg_fps = ft.get("fps", 0) if ft else 0
        _jank_frames = ft.get("jank_frames", 0) if ft else 0
        if ft and _total_frames > 0:
            ft_lines = [f"## 帧时间线\n"]
            ft_lines.append(f"FPS: {_avg_fps:.1f}, 总帧数: {_total_frames}, 卡顿帧: {_jank_frames}")
            jank_types = ft.get("jank_types", [])
            if jank_types:
                ft_lines.append(f"卡顿类型: {', '.join(jank_types)}")
            # Top 5 slowest frames
            slowest = ft.get("slowest_frames", [])
            if slowest:
                ft_lines.append(f"最慢帧 (Top 5):")
                for f in slowest[:5]:
                    idx = f.get("frame_index", "?")
                    dur = f.get("dur_ms", 0)
                    jts = ", ".join(f.get("jank_types", []))
                    ft_lines.append(f"  帧#{idx}: {dur:.1f}ms" + (f" [{jts}]" if jts else ""))
            user_parts.append("\n".join(ft_lines))

        # ── View slices summary (top 10 only, compact) ──
        vs = perf_data.get("view_slices", {})
        if vs:
            vs_summary = vs.get("summary", [])
            if vs_summary:
                vs_lines = ["## 自定义切片统计 (Top 10)\n"]
                for s in sorted(vs_summary, key=lambda x: -x.get("total_ms", 0))[:10]:
                    name = s.get("name", "")
                    if not name.startswith("SI$"):
                        continue
                    vs_lines.append(f"- {name}: {s.get('count', 0)}次, 最大{s.get('max_ms', 0):.3f}ms, 总{s.get('total_ms', 0):.3f}ms")
                if len(vs_lines) > 1:
                    user_parts.append("\n".join(vs_lines))
    if perf_analysis:
        user_parts.append(f"## 性能分析\n{perf_analysis}")
    if attribution_result:
        try:
            attr_data = json.loads(attribution_result)
            found = [r for r in attr_data if r.get("attributable")]
            system = [r for r in attr_data if r.get("reason") == "system_class"]
            # Slices that were searched but source not found (parse_failed/error/unresolved)
            unresolved = [r for r in attr_data
                          if not r.get("attributable") and r.get("reason") not in ("system_class", "found")]

            if found:
                parts = ["## 源码归因结果\n"]
                for r in found:
                    parts.append(f"- {r['class_name']}.{r['method_name']} ({r['dur_ms']:.2f}ms)")
                    parts.append(f"  位置: {r.get('file_path', '?')}:{r.get('line_start', '?')}-{r.get('line_end', '?')}")
                    if r.get("source_snippet"):
                        parts.append(f"  发现: {r['source_snippet'][:200]}")
                user_parts.append("\n".join(parts))

            if system:
                parts = ["## 系统框架切片（无法归因到源码）\n"]
                for r in system:
                    parts.append(f"- {r['class_name']}.{r['method_name']} ({r['dur_ms']:.2f}ms)")
                parts.append("\n请根据trace数据和调用链上下文推测这些系统切片的性能问题原因，并给出通用优化建议。")
                user_parts.append("\n".join(parts))

            if unresolved:
                parts = ["## 待归因热点（源码未定位）\n"]
                for r in unresolved:
                    parts.append(f"- {r['class_name']}.{r['method_name']} ({r['dur_ms']:.2f}ms)")
                    parts.append(f"  原因: {r.get('reason', 'unknown')}")
                parts.append("\n这些切片耗时较高但未能在源码中定位。请根据类名和方法名推测可能的性能问题原因，并给出优化建议。")
                user_parts.append("\n".join(parts))
        except Exception:
            pass

    if not user_parts:
        return {
            "messages": [AIMessage(content="[reporter] No data available for report")],
            "perf_summary": perf_json,
            "perf_analysis": perf_analysis,
            "attribution_data": state.get("attribution_data", ""),
            "attribution_result": attribution_result,
        }

    print("\n  [reporter] Generating report...", flush=True)
    if state.get("_trace_path"):
        print(f"  [reporter] Trace file: {state['_trace_path']}", flush=True)
    else:
        print(f"  [reporter] WARNING: no trace_path in state", flush=True)

    user_content = "\n\n".join(user_parts)
    llm = _get_route_llm()

    from langchain_core.messages import SystemMessage, HumanMessage

    messages = [
        SystemMessage(content=report_prompt),
        HumanMessage(content=user_content),
    ]

    # Stream with retry: fall back to non-streaming if stream breaks
    full_content = ""
    input_tokens = 0
    try:
        for chunk in llm.stream(messages):
            token = chunk.content
            if token:
                full_content += token
            um = getattr(chunk, "usage_metadata", None)
            if um:
                input_tokens = um.get("input_tokens", 0)
    except Exception as e:
        # Stream failed (network error, API disconnect) — retry with invoke
        print(f"\n  [reporter] Stream interrupted ({e}), retrying...", flush=True)
        try:
            response = llm.invoke(messages)
            full_content = response.content
            get_tracker().record_from_message("reporter", response)
        except Exception as e2:
            full_content = full_content or f"[reporter] Report generation failed: {e2}"
            print(f"  [reporter] Retry also failed: {e2}", flush=True)

    # Record token usage (estimate output from content length if metadata incomplete)
    output_tokens = len(full_content) // 3  # rough estimate for CJK text
    get_tracker().record("reporter", {"input_tokens": input_tokens, "output_tokens": output_tokens})

    print("\n  [reporter] Report generated", flush=True)

    # Combine pre-generated header with LLM analysis
    complete_report = header_md + "\n" + full_content

    # Save report to file
    report_dir = os.path.join(os.getcwd(), "reports")
    os.makedirs(report_dir, exist_ok=True)
    timestamp = datetime.datetime.now().strftime("%Y%m%d_%H%M%S")
    report_path = os.path.join(report_dir, f"perf_report_{timestamp}.md")
    try:
        with open(report_path, "w", encoding="utf-8") as f:
            f.write(complete_report)
        size_kb = len(complete_report.encode("utf-8")) / 1024
        print(f"  [reporter] Report saved to {report_path} ({size_kb:.1f}KB)", flush=True)
        complete_report += f"\n\n---\n报告已保存至: {report_path}"
    except OSError as e:
        print(f"  [reporter] Failed to save report: {e}", flush=True)

    return {
        "messages": [AIMessage(content=complete_report)],
        "perf_summary": perf_json,
        "perf_analysis": perf_analysis,
        "attribution_data": state.get("attribution_data", ""),
        "attribution_result": attribution_result,
    }


# ---------------------------------------------------------------------------
# Build graph
# ---------------------------------------------------------------------------

def create_graph():
    """Create the SmartInspector orchestration graph."""
    builder = StateGraph(AgentState)

    # All nodes
    builder.add_node("orchestrator", orchestrator_node)
    builder.add_node("android_expert", android_expert_node)
    builder.add_node("perf_analyzer", perf_analyzer_node)
    builder.add_node("explorer", explorer_node)
    builder.add_node("fallback", fallback_node)
    # Pipeline nodes
    builder.add_node("collector", collector_node)
    builder.add_node("analyzer", analyzer_node)
    builder.add_node("attributor", attributor_node)
    builder.add_node("reporter", reporter_node)

    # Entry
    builder.add_edge(START, "orchestrator")

    # Orchestrator routing
    builder.add_conditional_edges(
        "orchestrator",
        route_from_orchestrator,
        path_map={
            "android_expert": "android_expert",
            "perf_analyzer": "perf_analyzer",
            "explorer": "explorer",
            "fallback": "fallback",
            "collector": "collector",
        },
    )

    # Single-path nodes → END
    builder.add_edge("perf_analyzer", END)
    builder.add_edge("explorer", END)
    builder.add_edge("fallback", END)

    # Android expert: if perf_summary detected → continue pipeline, else END
    builder.add_conditional_edges(
        "android_expert",
        route_from_android_expert,
        path_map={
            "analyzer": "analyzer",
            "end": END,
        },
    )

    # Full pipeline chain: collector → analyzer → attributor → reporter → END
    builder.add_edge("collector", "analyzer")
    builder.add_edge("analyzer", "attributor")
    builder.add_edge("attributor", "reporter")
    builder.add_edge("reporter", END)

    return builder.compile()


# ---------------------------------------------------------------------------
# Streaming CLI
# ---------------------------------------------------------------------------


def _stream_run(graph, state):
    """Run the graph with streaming, printing tokens as they arrive.

    Preserves perf_summary, perf_analysis, attribution_data, attribution_result
    across turns (until /clear).
    """
    last_updates = {}

    print("\nai> ", end="", flush=True)

    final_output = ""

    for chunk in graph.stream(
        state,
        stream_mode=["updates"],
        version="v2",
    ):
        last_updates = chunk["data"]
        for node_name, node_state in chunk["data"].items():
            if node_name in ("android_expert", "perf_analyzer", "explorer", "collector",
                             "analyzer", "attributor"):
                pass
            else:
                # fallback, reporter, and other nodes: print AI message content
                for msg in node_state.get("messages", []):
                    content = getattr(msg, "content", "") if not isinstance(msg, dict) else msg.get("content", "")
                    if content:
                        print(content, flush=True)

    # Print token usage summary
    from smartinspector.token_tracker import get_tracker
    tracker = get_tracker()
    if tracker.total_calls > 0:
        print(f"\n{tracker.summary()}")

    print("\n")

    # Build updated state
    new_messages = list(state.get("messages", []))
    perf_summary = state.get("perf_summary", "")
    perf_analysis = state.get("perf_analysis", "")
    attribution_data = state.get("attribution_data", "")
    attribution_result = state.get("attribution_result", "")
    trace_path = state.get("_trace_path", "")

    for node_name, node_state in last_updates.items():
        node_msgs = node_state.get("messages", [])
        new_messages.extend(node_msgs)

        node_ps = node_state.get("perf_summary", "")
        if node_ps:
            perf_summary = node_ps

        node_pa = node_state.get("perf_analysis", "")
        if node_pa:
            perf_analysis = node_pa

        node_ad = node_state.get("attribution_data", "")
        if node_ad:
            attribution_data = node_ad

        node_ar = node_state.get("attribution_result", "")
        if node_ar:
            attribution_result = node_ar

        node_tp = node_state.get("_trace_path", "")
        if node_tp:
            trace_path = node_tp

    return {
        "messages": new_messages,
        "perf_summary": perf_summary,
        "perf_analysis": perf_analysis,
        "attribution_data": attribution_data,
        "attribution_result": attribution_result,
        "_trace_path": trace_path,
    }


def main():
    """Run the interactive chat loop."""
    import argparse
    from smartinspector.config import get_source_dir, set_source_dir

    parser = argparse.ArgumentParser(description="SmartInspector CLI")
    parser.add_argument("--source-dir", default="", help="Source code directory for attribution search")
    args, _ = parser.parse_known_args()

    if args.source_dir:
        set_source_dir(args.source_dir)

    print("SmartInspector v0.5.0")
    if args.source_dir:
        print(f"Source dir: {get_source_dir()}")
    else:
        print(f"Source dir: {get_source_dir()} (use --source-dir or /config source_dir <path> to change)")
    print("Type /help for commands, 'quit' or Ctrl+C to exit\n")

    # Auto-start WS server + adb reverse so app can connect on launch
    import subprocess
    from smartinspector.ws.server import SIServer
    server = SIServer.get(port=9876)
    server.start()
    try:
        subprocess.run(
            ["adb", "reverse", "tcp:9876", "tcp:9876"],
            capture_output=True, text=True, timeout=5,
        )
        print("  WS server ready on :9876, adb reverse set")
    except Exception as e:
        print(f"  WS server ready on :9876 (adb reverse failed: {e})")
    print()

    graph = create_graph()
    state = {
        "messages": [],
        "perf_summary": "",
        "perf_analysis": "",
        "attribution_data": "",
        "attribution_result": "",
        "_trace_path": "",
    }

    from prompt_toolkit import PromptSession
    from prompt_toolkit.history import FileHistory
    import pathlib as pathlib

    session = PromptSession(history=FileHistory(str(pathlib.Path.home() / ".smartinspector_history")))

    while True:
        try:
            user_input = session.prompt("you> ").strip()
        except (EOFError, KeyboardInterrupt):
            print("\nbye!")
            break

        if not user_input:
            continue
        if user_input.lower() in ("quit", "exit", "q"):
            print("bye!")
            break

        # Slash commands bypass the LLM graph
        if user_input.startswith("/"):
            state = handle_slash_command(user_input, state)
            continue

        state["messages"] = state["messages"] + [
            {"role": "user", "content": user_input}
        ]

        state = _stream_run(graph, state)


if __name__ == "__main__":
    main()
