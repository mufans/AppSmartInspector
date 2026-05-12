# SmartInspector Architecture

> Multi-agent performance analysis CLI for mobile apps.

## System Overview

```
┌─────────────────────────────────────────────────────────────┐
│                  CLI (graph/cli.py REPL)                     │
│  you> [input] → graph.stream() → ai> [streaming output]     │
│  + 启动前置检查 (adb + API key)                              │
│  + 自动启动 WS server (动态端口 + ready event) + adb reverse    │
│  + Tab 补全 + 全局异常保护                                    │
└──────────────────────────┬──────────────────────────────────┘
                           │
              ┌────────────▼────────────┐
              │     Orchestrator Node   │
              │  (LLM intent classify)  │
              │  few-shot + max_tokens=5│
              │  try/except → fallback  │
              └──┬──────┬──────┬──┬──┬──┘
                 │      │      │  │  │
      ┌──────────▼┐ ┌──▼──┐ ┌▼─┐│ ┌▼───────┐
      │ collector  │ │Perf │ │Ex││ │Fallback │
      │ (pipeline) │ │Anal.│ │pl││ │         │
      └──────┬─────┘ └──┬──┘ └┬─┘│ └─────────┘
             │           │     │  │
             ▼           ▼     ▼  ▼
      ┌──────────┐      END  END END
      │ analyzer │
      └────┬─────┘
           │
     ┌─────▼──────┐
     │ attributor │
     └─────┬──────┘
           │
     ┌─────▼──────┐
     │  reporter  │
     └─────┬──────┘
           ▼
          END

  State: MemorySaver checkpointer (get_state() 替代手动合并)
  Streaming: reporter 真正流式输出，其他节点静默处理
  Error: node_error_handler 装饰器统一捕获 + graph.stream try/except

## Directory Structure

```
smartinspector/
├── src/smartinspector/
│   ├── cli.py                   # CLI entry point (argparse)
│   ├── main.py                  # Legacy entry (deprecated)
│   ├── prompts.py               # Prompt file loader
│   ├── perfetto_compat.py       # macOS IPv4 fix for perfetto lib
│   ├── config.py                # Global config (LLM models, source dir, hook config persistence, env var overrides)
│   ├── token_tracker.py         # LLM token usage tracking
│   │
│   ├── graph/                   # LangGraph orchestration (modular package)
│   │   ├── __init__.py          #   Public exports (create_graph, run_graph, main)
│   │   ├── builder.py           #   Graph construction (nodes + edges + conditional routing)
│   │   ├── cli.py               #   CLI REPL loop (prompt_toolkit, Tab补全, WS auto-start, 全局异常保护)
│   │   ├── state.py             #   AgentState, RouteDecision enum, _pass_through()
│   │   ├── streaming.py         #   _stream_run() — streaming graph execution, MemorySaver, error handling
│   │   └── nodes/               #   LangGraph graph nodes
│   │       ├── orchestrator.py  #     LLM routing + fallback node (few-shot, error handling)
│   │       ├── android.py       #     Android Expert: trace collect + analyze
│   │       ├── analyzer.py      #     perf_analyzer_node (standalone) + analyzer_node (pipeline)
│   │       ├── explorer.py      #     Code Explorer: grep/glob/read
│   │       ├── collector.py     #     Trace collection node (pipeline step 1, WS+SQL block events merge)
│   │       ├── attributor.py    #     Source attribution node (pipeline step 3, structured output)
│   │       └── reporter/        #     Report generation (pipeline step 4)
│   │           ├── __init__.py  #       reporter_node entry (streaming output)
│   │           ├── generator.py #       LLM report generation (streaming + retry + token estimation)
│   │           ├── formatter.py #       Data formatting (perf JSON + attribution → Markdown)
│   │           └── persistence.py #     Report file saving (./reports/)
│   │
│   ├── agents/                  # Agent definitions (LLM + tools)
│   │   ├── android.py           #   Android Expert: trace collect + analyze
│   │   ├── explorer.py          #   Code Explorer: grep/glob/read
│   │   ├── perf_analyzer.py     #   Perf Analyzer: single-shot LLM interpretation
│   │   ├── attributor.py        #   Source attribution: run_attribution()
│   │   └── deterministic.py     #   Deterministic pre-computation (reduces LLM tokens)
│   │
│   ├── collector/               # Data collection & processing
│   │   ├── perfetto.py          #   PerfettoCollector: adb collect → SQL query → JSON (CPU调用链, 系统级CPU, WS+SQL合并, context manager)
│   │   ├── frame.py             #   FrameMixin: per-frame metrics (overrun, cpu_time, ui_time, vsync_delay, jank)
│   │   ├── startup.py           #   StartupMixin: TTID/TTFD startup metrics + bottleneck breakdown
│   │   ├── memory.py            #   MemoryMixin: heap graph stats, class aggregation, dominator tree
│   │   ├── lock.py              #   LockMixin: Java monitor lock contention analysis
│   │   ├── binder.py            #   BinderMixin: binder transactions + latency breakdown
│   │   ├── gc.py                #   GcMixin: garbage collection event analysis
│   │   ├── anr.py               #   AnrMixin: ANR detection + main-thread slice analysis
│   │   ├── slice_enhanced.py    #   SliceEnhancedMixin: SI$ slice CPU time + thread state distribution
│   │   ├── input.py             #   InputMixin: input event latency breakdown (dispatch/handling/ACK)
│   │   ├── sched_latency.py     #   SchedLatencyMixin: scheduling latency per thread
│   │   ├── oom.py               #   OomMixin: OOM score transitions + RSS/Swap + LMK events
│   │   ├── cpu_utilization.py   #   CpuUtilizationMixin: frequency-weighted CPU utilization (process/thread)
│   │   └── surfaceflinger.py    #   SurfaceFlingerMixin: App-SF frame timeline matching
│   │
│   ├── commands/                # Slash command implementations
│   │   ├── __init__.py          #   Command registry (SLASH_COMMANDS dict + handle_slash_command)
│   │   ├── attribution.py       #   SI$ tag parsing + attribution extraction
│   │   ├── device.py            #   /devices, /connect, /status, /disconnect
│   │   ├── hook.py              #   /config, /hooks, /hook, /debug
│   │   ├── orchestrate.py       #   /full, /report (文件输出)
│   │   ├── session.py           #   /help, /clear (全字段清理), /summary, /tokens
│   │   └── trace.py             #   /trace, /record, /analyze
│   │
│   ├── tools/                   # LangChain @tool functions
│   │   ├── perfetto.py          #   analyze_perfetto, collect_android_trace
│   │   ├── grep.py              #   ripgrep content search
│   │   ├── glob.py              #   ripgrep file pattern search
│   │   ├── read.py              #   file reader with line numbers
│   │   ├── rg.py                #   ripgrep binary finder
│   │   └── path_utils.py        #   shared path validation (prevent traversal)
│   │
│   └── ws/                      # WebSocket communication
│       └── server.py            #   SIServer (心跳检测, ready event, 动态端口, msg_id+ACK)
│
├── prompts/                     # System prompts (text files)
│   ├── main.txt                 #   Main persona (HarmonyOS perf tool)
│   ├── android-expert.txt       #   Android agent prompt
│   ├── perf-analyzer.txt        #   Perf analysis prompt
│   ├── code-explorer.txt        #   Code search prompt
│   ├── report-generator.txt     #   Report format prompt
│   ├── compaction.txt           #   Context compression prompt
│   ├── monkey-driver.txt        #   Monkey test driver prompt
│   ├── reference-hdc-commands.txt
│   └── reference-opencode-prompts.txt
│
├── platform/android/            # Android trace hook library
│   ├── app/                     #   Demo app
│   └── tracelib/                #   Reusable Android Library
│       ├── src/main/java/.../
│       │   ├── TraceHook.java          # Core hook logic
│       │   ├── HookConfig.java         # Config model
│       │   └── HookConfigManager.java  # Config load/apply
│       ├── src/debug/java/.../
│       │   └── HookConfigActivity.java # Config panel (debug only)
│       └── src/release/java/.../
│           └── TraceHook.java          # No-op stubs (release)
│
├── reports/                     # Generated performance reports (Markdown)
├── bin/                         # trace_processor_shell binary
└── tests/                       # Unit tests
```

## State Design

```python
class RouteDecision(str, Enum):
    """Routing decisions returned by the orchestrator node."""
    FULL_ANALYSIS = "full_analysis"   # full pipeline: collector → analyzer → attributor → reporter
    ANDROID = "android"               # android expert agent
    ANALYZE = "analyze"               # standalone perf analysis
    EXPLORER = "explorer"             # source code search
    END = "end"                       # general Q&A / fallback
    TRACE = "trace"                   # /trace command: collector → analyzer

class AgentState(TypedDict):
    messages: Annotated[list, operator.add]   # Accumulated conversation
    perf_summary: str                          # JSON: PerfettoCollector summary
    perf_analysis: str                         # Markdown: LLM performance analysis
    attribution_data: str                      # JSON: list of attributable SI$ slices
    attribution_result: str                    # JSON: attribution results with source snippets
    _route: str                                # internal: RouteDecision value
    _trace_path: str                           # internal: trace file path from collector
```

State flows through the graph, each node returns a partial state update:

```
orchestrator  → { _route: "android", messages: [] }
android_expert → { messages: [...], perf_summary: "{...json...}" }
collector     → { messages: [...], perf_summary: "{...json...}", _trace_path: "/tmp/xxx.pb" }
analyzer      → { messages: [AIMessage], perf_analysis: "..." }
attributor    → { messages: [...], attribution_data: "[...]", attribution_result: "[...]" }
reporter      → { messages: [AIMessage (full report)] }
explorer      → { messages: [...] }
fallback      → { messages: [AIMessage] }
```

Pass-through fields (`perf_summary`, `perf_analysis`, `attribution_data`, `attribution_result`) are forwarded by every node via `_pass_through(state)` so they persist across graph executions until `/clear`.

The CLI loop in `_stream_run()` (graph/streaming.py) uses `graph.get_state(config)` with MemorySaver checkpointer to retrieve the final state after graph execution, instead of manual state merging.

### Error Handling

All graph nodes are protected by a `node_error_handler` decorator that catches exceptions, logs the error, and returns a fallback state to prevent graph crashes. The orchestrator LLM call itself is wrapped in try/except. The REPL main loop and `_stream_run()` have global exception protection to ensure a single node failure never kills the session.

## Orchestrator Node

**Purpose**: Classify user intent, route to appropriate agent or pipeline.

**Model**: `deepseek-chat`, temperature=0

**Classification categories**:

| Route | Keywords | Target Node |
|-------|----------|-------------|
| `full_analysis` | 全面分析/完整分析/全量分析/full/归因/冷启动/启动耗时/启动时间/启动分析/启动优化/应用启动/app启动/cold start/启动性能 | collector (pipeline entry) |
| `android` | trace/adb/采集/perfetto/FPS/CPU/内存指标 | android_expert |
| `analyze` | 解读perf_summary/分析这份数据/解读一下这个 | perf_analyzer |
| `explorer` | 源码/代码/搜索/查看/定位/函数/grep/.ets/.ts/.java | explorer |
| `end` | 什么是/怎么优化/如何/为什么/general Q&A | fallback |
| `trace` | /trace command | collector (→ analyzer → END) |

**Graph routing** (graph/builder.py):

```
orchestrator → route_from_orchestrator():
    full_analysis → collector → analyzer → attributor → reporter → END
    trace         → collector → analyzer → END
    android       → android_expert → (analyzer | END)
    analyze       → perf_analyzer → END
    explorer      → explorer → END
    end           → fallback → END
```

## Slash Commands

CLI 输入 `/command` 前缀时，不经过 LLM 路由，直接执行对应动作。

```
you> /help          → 列出所有可用指令
you> /trace 5000    → 跳过对话，直接采集 5s trace + 分析
you> /config        → 查看 App 端当前 hook 配置
you> /debug         → 打开设备端 Hook 调试配置面板
```

### 设备连接类

| 指令 | 功能 | 实现方式 |
|------|------|---------|
| `/devices` | 列出已连接的 adb 设备 | `adb devices -l` |
| `/connect <host:port>` | 通过 adb TCP 连接远程设备 | `adb connect <host:port>` |
| `/status` | 查看会话状态（消息数、perf 数据、设备连接） | state 字段 + adb get-state fallback |
| `/disconnect` | 断开 TCP 设备连接 | `adb disconnect <device>` |

### Trace 采集类

| 指令 | 功能 | 参数 |
|------|------|------|
| `/trace [duration_ms] [pkg]` | 快速采集 + 自动分析（走 collector → analyzer 流水线） | 默认 10000ms，可选指定目标包名 |
| `/record [duration_ms] [pkg]` | 只采集不分析，返回 .pb 路径 | 默认 10000ms，可选指定目标包名 |
| `/analyze [path]` | 分析 trace 文件（无参数时分析上次 `/record` 结果） | 可选 trace 文件路径 |

### Hook 配置类

| 指令 | 功能 | 参数 |
|------|------|------|
| `/config` | 查看当前 hook 配置 | 通过 WS 从 App 获取 |
| `/config <json>` | 推送 JSON 配置到 App | 如 `'{"rv_adapter": false}'` |
| `/config reset` | 恢复 hook 默认配置 | — |
| `/config source_dir <path>` | 设置源码搜索目录（自动持久化） | 目录路径 |
| `/hooks` | 查看所有 hook 点开关状态 | WS `get_config` 或 fallback |
| `/hook on <name>` | 开启指定 hook 点 | `layout_inflate` / `view_traverse` 等 |
| `/hook off <name>` | 关闭指定 hook 点 | hook 点名称 |
| `/hook add <class> <method>` | 新增自定义 hook 点 | 全限定类名 + 方法名 |
| `/hook rm <class>` | 删除自定义 hook 点 | 全限定类名 |
| `/debug` | 打开设备端 Hook 调试配置面板 | `adb shell am start` HookConfigActivity |

### 会话类

| 指令 | 功能 |
|------|------|
| `/clear` | 清除对话上下文和分析状态（perf_summary/analysis/attribution） |
| `/summary` | 显示当前 perf_summary 摘要（FPS/CPU/内存/线程/切片等） |
| `/tokens` | 显示当前会话的 token 使用统计 |
| `/help` | 列出所有可用指令 |

### 编排类

| 指令 | 功能 |
|------|------|
| `/full [--no-wait]` | 一键完整流程:采集 → 分析 → 源码归因 → 报告。 `--no-wait` 跳过等待 App 连接，适用于冷启动耗时分析 |
| `/report [path]` | 对当前会话生成正式性能分析报告 |

### 实现位置

```python
# commands/__init__.py — command registry pattern

from smartinspector.commands.device import cmd_devices, cmd_connect, cmd_status, cmd_disconnect
from smartinspector.commands.trace import cmd_trace, cmd_record, cmd_analyze
from smartinspector.commands.hook import cmd_config, cmd_hooks, cmd_hook, cmd_debug
from smartinspector.commands.session import cmd_help, cmd_clear, cmd_summary, cmd_tokens
from smartinspector.commands.orchestrate import cmd_full, cmd_report

SLASH_COMMANDS = {
    "/help": cmd_help,
    "/devices": cmd_devices,
    "/connect": cmd_connect,
    "/status": cmd_status,
    "/disconnect": cmd_disconnect,
    "/trace": cmd_trace,
    "/record": cmd_record,
    "/analyze": cmd_analyze,
    "/config": cmd_config,
    "/hooks": cmd_hooks,
    "/hook": cmd_hook,
    "/debug": cmd_debug,
    "/clear": cmd_clear,
    "/summary": cmd_summary,
    "/tokens": cmd_tokens,
    "/full": cmd_full,
    "/report": cmd_report,
}

def handle_slash_command(user_input: str, state: dict) -> dict:
    """Parse slash command, dispatch to handler, return updated state."""
    ...
```

---

## Agent Nodes

### 1. Android Expert (`graph/nodes/android.py`)

**Role**: Collect and analyze Perfetto traces from Android devices.

**Model**: `deepseek-chat`, temperature=0.1, streaming=True

**Tools**:

| Tool | Location | Purpose |
|------|----------|---------|
| `collect_android_trace` | `tools/perfetto.py` | adb perfetto collect → pull .pb file |
| `analyze_perfetto` | `tools/perfetto.py` | .pb file → SQL queries → JSON summary |

**Workflow**: collect_android_trace → analyze_perfetto → JSON summary (~2KB)

**Streaming**: `agent.stream(stream_mode=["messages", "updates"])` for real-time output.

**Post-routing**: If `perf_summary` is populated after this node, continues to `analyzer`; otherwise goes to `END`.

### 2. Collector (`graph/nodes/collector.py`)

**Role**: First step of the full analysis pipeline. Collects a Perfetto trace and generates a structured JSON summary.

**Model**: None (deterministic)

**Workflow**:
1. Reads Perfetto params from WS server config cache (sent by app via `config_sync`)
2. `PerfettoCollector.pull_trace_from_device()` → `.pb` file
3. `PerfettoCollector.summarize()` → `PerfSummary` JSON
4. Optionally requests block events from app via WS

**Output**: `{ perf_summary: "<json>", _trace_path: "/tmp/xxx.pb" }`

### 3. Analyzer (`graph/nodes/analyzer.py`)

**Role**: LLM interpretation of perf JSON summary. Used in two contexts:
- `perf_analyzer_node` — standalone analysis (orchestrator routes `analyze`)
- `analyzer_node` — pipeline step after collector

**Model**: `deepseek-chat`, temperature=0.1, no tools

**Input**: `perf_summary` JSON from state

**Output**: Structured problem analysis in Chinese (P0/P1/P2 severity)

**Pipeline routing**: After `analyzer_node`, routes to `attributor` (for `full_analysis`) or `END` (for `trace`).

### 4. Attributor (`graph/nodes/attributor.py`)

**Role**: Extract attributable SI$ slices from perf summary and search source code.

**Model**: Uses `agents/attributor.run_attribution()` which delegates to LLM agent

**Workflow**:
1. `extract_attributable_slices(perf_json)` → filter SI$ slices, parse class/method names
2. `run_attribution(attributable)` → for each slice: Glob → Grep → Read source → LLM analysis
3. Format results as human-readable summary

**Output**: `{ attribution_data: "<json>", attribution_result: "<json>" }`

### 5. Reporter (`graph/nodes/reporter/`)

**Role**: Generate the final Markdown performance report with LLM.

**Sub-modules**:
- `formatter.py` — builds Markdown sections from perf JSON and attribution results
- `generator.py` — LLM report generation with streaming and retry on failure
- `persistence.py` — saves report to `./reports/perf_report_YYYYMMDD_HHMMSS.md`

**Output**: Complete Markdown report (header tables + LLM analysis + source attribution)

### 6. Code Explorer (`graph/nodes/explorer.py`)

**Role**: Search and read source code files.

**Model**: `deepseek-chat`, temperature=0.1, streaming=True

**Tools**: `grep` (regex search), `glob` (file pattern), `read` (file reader)

**Output**: `[file_path]:[line_number]` + code snippet + analysis.

### 7. Fallback (`graph/nodes/orchestrator.py`)

**Role**: Friendly LLM reply for non-performance queries (greetings, Q&A).

**Model**: `deepseek-chat`, temperature=0

**Input**: Recent conversation context (last 3 turns)

**Output**: Short, friendly response with natural capability hints.

## Data Collection Layer

### PerfettoCollector (`collector/perfetto.py`)

**Protocols**: Supports `with` statement (context manager) for automatic trace file cleanup.

**Memory units**: Perfetto `process_counter_track` stores `mem.rss` / `mem.rss.anon` in **bytes**; collector converts to KB via `/1024` before storing as `rss_kb` fields. Display layer (`orchestrate.py`) converts KB → MB via `/1024`.

**PerfSummary structure**:
```
PerfSummary
├── fps: dict | None
├── cpu_hotspots: list[dict]            # Function-level CPU sampling
├── memory: dict | None                 # Java heap graph
├── scheduling: dict                    # Thread scheduling switches
├── frame_timeline: dict                # FPS, jank frames, slowest frames
├── view_slices: dict                   # View-level slices + RV instance grouping
│   ├── summary: list[dict]            # Aggregated by slice name
│   ├── slowest_slices: list[dict]     # Top 30 slowest individual slices
│   └── rv_instances: list[dict]       # Grouped by RV#[viewId]#[Adapter]
│       └── methods: dict              # Per-method stats (count, total_ms, max_ms)
└── metadata: dict                      # Trace metadata + table diagnosis
```

**SQL data sources**:

| Method | Perfetto Table | What it queries |
|--------|---------------|-----------------|
| `collect_sched()` | `sched` + `thread` | Context switches per thread |
| `collect_cpu_hotspots()` | `perf_sample` + `stack_profile_*` | CPU callstack sampling |
| `collect_frame_timeline()` | `actual_frame_timeline_slice` | Frame jank from SurfaceFlinger |
| `collect_memory()` | `heap_graph_object` + `heap_graph_class` | Java heap allocation |
| `collect_view_slices()` | `slice` | Custom TraceHook tags + system atrace |
| `collect_threads()` | `thread` | Thread listing |
| `collect_sys_stats()` | `sys_stats` | System-level CPU metrics |

### Stdlib Analysis Modules (Mixin 模式)

所有模块以 Mixin 形式挂载到 `PerfettoCollector`，通过 `INCLUDE PERFETTO MODULE` 引入 Perfetto 标准库。每个 Mixin 依赖宿主提供 `self._open()` (返回 TraceProcessor) 和 `self._target_package`。

#### P0 — 核心模块

| Mixin | 方法 | Perfetto Stdlib (`INCLUDE`) | 采集数据 | 解决的性能问题 |
|-------|------|---------------------------|---------|---------------|
| `FrameMixin` | `collect_frame_metrics()` | `android.frames.per_frame_metrics`<br>`android.frames.timeline` | 每帧行为指标：overrun、cpu_time、ui_time、vsync_delay、jank 分类 (was_jank/was_slow_frame/was_big_jank/was_huge_jank)，Top 30 最差帧 | 定位 UI 卡顿根因：区分 CPU 耗时过高、UI 线程慢、VSYNC 延迟等不同类型的帧超时 |
| `StartupMixin` | `collect_startup_metrics()` | `android.startup.startups`<br>`android.startup.time_to_display` | 应用启动事件：TTID (Time To Initial Display)、TTFD (Time To Full Display)、启动类型 (cold/warm/hot) | 量化启动性能：精确定位每次冷启动/暖启动的首次绘制时间和全屏展示时间 |
| `StartupMixin` | `collect_startup_breakdown()` | `android.startup.startups`<br>`android.startup.startup_breakdowns` | 启动瓶颈分解：每段耗时的 reason (binder/io/cpu/lock 等)，Top 50 最长段 | 分解启动耗时归因：识别启动期间是 binder 调用、磁盘 IO、CPU 计算还是锁竞争占用了时间 |
| `MemoryMixin` | `collect_heap_graph_stats()` | `android.memory.heap_graph.heap_graph_stats` | 堆图摘要：总/可达对象数量、堆大小、Native 分配、OOM score、RSS/Swap、dmabuf | 内存用量概览：评估应用整体内存健康度，OOM 风险判断 |
| `MemoryMixin` | `collect_heap_class_aggregation()` | `android.memory.heap_graph.heap_graph_class_aggregation` | 按类聚合堆内存 Top 20：对象数量、self size、reachable size、dominated size、native size | 定位内存大户：找出哪些类占用了最多堆空间，区分可达 vs 不可达对象 |
| `MemoryMixin` | `collect_heap_dominator_tree()` | `android.memory.heap_graph.dominator_tree` | 堆支配树 Top 50：对象 ID、immediate dominator、dominated set 大小、retained size、深度 | 追踪内存泄漏：通过支配树找到 retain 最多内存的对象链路，定位泄漏根节点 |

#### P1 — 增强模块

| Mixin | 方法 | Perfetto Stdlib (`INCLUDE`) | 采集数据 | 解决的性能问题 |
|-------|------|---------------------------|---------|---------------|
| `LockMixin` | `collect_lock_contention()` | `android.monitor_contention` | Java 锁竞争事件 Top 20：blocked/blocking 方法、线程名、是否主线程、等待者数量 + 阻塞线程状态分解 | 定位主线程阻塞：识别导致主线程等待的锁竞争，区分 IO 等待 vs CPU 等待 |
| `BinderMixin` | `collect_binder_txns()` | `android.binder` | 同步 Binder 事务 Top 30：client/server 进程线程、AIDL 方法名、是否主线程、client/server 耗时 | 定位跨进程调用瓶颈：找出最耗时的 IPC 调用，识别主线程上的同步 binder 等待 |
| `BinderMixin` | `collect_binder_breakdown()` | `android.binder`<br>`android.binder_breakdown` | Binder 延迟分解 Top 50：client/server 侧的 reason (flush/sched/wait 等)、reason_type | 深入分析 Binder 延迟：区分 binder 调用中的调度延迟、flush 等待、事务执行等各阶段耗时 |
| `GcMixin` | `collect_garbage_collection()` | `android.garbage_collection` | GC 事件 Top 20：wall duration、CPU 时间分解 (running/runnable/io_wait/non_io_wait)、GC 类型、reclaimed 大小、heap 范围 | 评估 GC 影响：识别 GC Stop-The-World 对主线程的实际影响，区分 concurrent vs stop-the-world GC |
| `AnrMixin` | `collect_anrs()` | `android.anrs` | ANR 事件：进程/组件、ANR 类型、subject、timer_delay + ANR 窗口内主线程 Top 10 切片 | ANR 根因分析：识别 ANR 发生时主线程正在执行的耗时操作 |
| `SliceEnhancedMixin` | `collect_slice_cpu_time()` | `slices.cpu_time` | SI$ 切片真实 CPU 时间 Top 20：cpu_time、total_dur、cpu_ratio、线程名 | 区分真实计算 vs 等待：切片耗时长不代表 CPU 慢，cpu_ratio 低说明瓶颈在 IO/锁/调度而非计算 |
| `SliceEnhancedMixin` | `collect_slice_time_in_state()` | `slices.time_in_state`<br>`sched.states` | SI$ 切片线程状态分布 Top 10：Running/Sleeping/Runnable/IO Wait 等状态占比、blocked_function | 诊断切片耗时根因：识别切片时间花在了 CPU 运行、IO 等待、调度等待还是休眠上 |
| `InputMixin` | `collect_input_latency()` | `android.input` | 输入事件延迟 Top 20：dispatch/handling/ACK 三阶段耗时、end-to-end 延迟、事件类型 | 追踪触控响应延迟：区分输入事件在 dispatch、处理、ACK 哪个阶段耗时最长 |
| `SchedLatencyMixin` | `collect_sched_latency()` | `sched.latency` | 线程调度延迟 Top 20：per-thread 的 runnable→running 等待次数、总/平均/最大等待时间 | 识别调度瓶颈：线程 ready 后多久才被 CPU 执行，高调度延迟意味着 CPU 资源竞争激烈 |
| `OomMixin` | `collect_oom_rss_swap()` | `android.memory.process`<br>`android.memory.lmk` | OOM score 变迁 + 内存快照 (anon_rss/file_rss/shmem_rss/swap) + LMK kill 事件 | 内存压力分析：追踪进程 OOM score 变化与内存占用的关系，识别被 LMK 杀死的进程 |
| `CpuUtilizationMixin` | `collect_process_cpu_utilization()` | `linux.cpu.utilization.process` | 进程级 CPU 利用率：频率加权 millicycles/megacycles、runtime、min/max/avg freq、per-second utilization | 精确 CPU 利用率：传统 CPU 使用率不含频率信息，频率加权利用率更准确反映真实 CPU 消耗 |
| `CpuUtilizationMixin` | `collect_thread_cpu_utilization()` | `linux.cpu.utilization.thread` | 线程级 CPU 利用率 Top 15：per-thread 的 millicycles/megacycles、频率信息、per-second utilization | 定位 CPU 热线程：找出哪些线程消耗了最多 CPU 周期，结合频率信息判断是否因低频运行导致性能差 |
| `SurfaceFlingerMixin` | `collect_surfaceflinger_timeline()` | `android.surfaceflinger` | App-SF 帧时间线匹配 Top 200：app/SF 帧时间戳、耗时、期望 deadline、匹配类型 (on_time/late) | 端到端帧延迟分析：从 App 渲染到 SurfaceFlinger 合成的完整链路，识别帧是在 App 端还是 SF 端延迟 |

**Perfetto config**:
- Default categories: sched, freq, idle, power, memreclaim, gfx, view, input, dalvik, am, wm
- `atrace_apps: "*"` — captures app-level atrace markers
- `ftrace/print` — captures Trace.beginSection() output
- Buffer: 64MB primary + 4MB secondary

---

## Android TraceHook Library

### Architecture

```
Application.onCreate()
    └── TraceHook.init(context)          # singleton, thread-safe
        ├── HookConfigManager.load(context)   # 从 SP 加载配置
        ├── registerConfigReceiver(context)   # 注册 broadcast receiver (debug)
        └── doInit()                          # 根据 config 选择性 hook
            ├── hookActivityLifecycle()
            ├── hookFragmentLifecycle()
            ├── hookRecyclerView()
            ├── hookLayoutInflate()            # 可选
            ├── hookViewTraverse()             # 可选
            ├── hookHandlerDispatch()          # 可选
            └── hookExtraClasses()             # 自定义 hook 点
```

### Hook Points

**内置 hook 点（配置开关控制）**：

| Hook ID | 默认 | Hook 点 | Tag 格式 |
|---------|------|---------|---------|
| `activity_lifecycle` | true | Activity: onCreate/onStart/onResume/onPause/onStop/onDestroy/onWindowFocusChanged | `SI$Activity.onResume` |
| `fragment_lifecycle` | true | Fragment (AndroidX+app): onCreate/onCreateView/onViewCreated/onResume/onPause/onDestroyView/onHiddenChanged/setUserVisibleHint | `SI$Fragment.onViewCreated` |
| `rv_pipeline` | true | RV: onDraw/onScrollStateChanged/dispatchLayoutStep1-3/setAdapter/setLayoutManager/GapWorker.prefetch | `SI$RV#id#Adapter.method` |
| `rv_adapter` | true | Adapter (dynamic): onCreateViewHolder/onBindViewHolder/onViewRecycled/onViewAttachedToWindow/onViewDetachedFromWindow | `SI$Adapter.method` |
| `layout_inflate` | false | LayoutInflater: inflate | `SI$inflate#[layout_name]#[parent_class]` |
| `view_traverse` | false | View: measure/layout/draw (非RV) | `SI$view#[class].[method]` |
| `handler_dispatch` | false | Handler: dispatchMessage (主线程) | `SI$handler#[msg_class]` |

**自定义 hook 点（extra_hooks 配置）**：

```json
{
  "extra_hooks": [
    {
      "class": "com.fdzq.smartinvest.util.DataManager",
      "methods": ["loadData", "formatPrice"],
      "enabled": true
    }
  ]
}
```

Tag 格式：`SI$[SimpleClassName].[method]`

### Trace Tag Format

**SI$ 前缀规则**：所有 TraceHook 注入的 `Trace.beginSection()` 统一加 `SI$` 前缀。系统/框架的 atrace tag 无此前缀。下游通过 `startswith("SI$")` 区分用户代码与系统代码。

```
TraceHook tag → Perfetto slice name

用户代码（SI$ 前缀，可归因到源码）：
  SI$StockDetailActivity.onResume
  SI$StockFragment.onViewCreated
  SI$RV#rv_list#StockListAdapter.onBindViewHolder
  SI$RV#rv_chart#ChartAdapter.dispatchLayoutStep2
  SI$inflate#item_stock_list#FrameLayout
  SI$DataManager.loadData

系统代码（无 SI$ 前缀，不可归因）：
  Choreographer.doFrame
  RecyclerView.onDraw
  performTraversals
  measure / layout / draw
```

### SDK Build Variants

| 变体 | 内容 | 用途 |
|------|------|------|
| **debug** | 完整 hook + 配置面板 + BroadcastReceiver + WS 客户端 | 开发/测试 |
| **release** | 所有 public API 为空实现 (no-op)，无 WS 依赖，无 Pine 依赖 | 生产环境，零开销 |

目标 app 接入代码无需判断：

```java
// debug 和 release 代码一致
TraceHook.init(this);

// 打开配置面板（debug 有实现，release 为空方法）
TraceHook.openConfig(context);
```

编译器会内联 release 变体的空方法，运行时零开销。

### Hook 配置系统

**配置通道**：`am broadcast`（唯一外部入口）

```bash
# 开关内置 hook 点
adb shell am broadcast -a com.smartinspector.HOOK_CONFIG \
  --es config '{"layout_inflate":true,"view_traverse":true}'

# 新增自定义 hook
adb shell am broadcast -a com.smartinspector.HOOK_CONFIG \
  --es config '{"extra_hooks":[{"class":"com.fdzq.DataManager","methods":["loadData"]}]}'
```

**持久化**：`SharedPreferences`

```
BroadcastReceiver 收到配置
    → 解析 JSON
    → 更新内存配置 (HookConfigManager)
    → 写入 SP 持久化
    → 下次 init() 自动加载
```

**运行时行为**：Pine 不支持 unhook，"关闭" hook 点通过 `beforeCall` 里检查配置实现：

```java
@Override public void beforeCall(Pine.CallFrame cf) {
    if (!HookConfigManager.isEnabled("layout_inflate")) return;  // 跳过
    Trace.beginSection("SI$inflate#...");
}
```

关闭的 hook 点方法仍被 Pine 拦截（极低开销），但不执行 `Trace.beginSection`。零开销需使用 release 变体。

**配置面板（debug）**：`HookConfigActivity` — 可嵌入目标 app 的调试面板（如 DoraemonKit、Flipper）

```
┌─────────────────────────────────┐
│  SmartInspector Hook Config     │
├─────────────────────────────────┤
│                                 │
│  内置 Hook 点                   │
│    Activity 生命周期       [✓]  │
│    Fragment 生命周期       [✓]  │
│    RV 管线                [✓]  │
│    RV Adapter             [✓]  │
│    LayoutInflater          [ ]  │
│    View measure/layout     [ ]  │
│    Handler.dispatch         [ ]  │
│                                 │
│  自定义 Hook 点                 │
│    DataManager.loadData    [✓]  │
│    ImageLoader.load        [ ]  │
│    [+ 添加自定义 Hook]          │
│                                 │
│  ─────────────────────────────  │
│  当前状态                       │
│    已 hook 类: 5                │
│    已 hook 方法: 23             │
│                                 │
│  [开始采集 10s trace]            │
│    → 采集完成后展示摘要：        │
│      jank 帧: 78                │
│      最慢 slice: 82.3ms         │
│      [查看详细分析]              │
│                                 │
│  [恢复默认配置]                  │
└─────────────────────────────────┘
```

功能：
- **内置 hook 开关**：列表展示所有预定义 hook 点，Switch 实时切换，立即写 SP 生效
- **自定义 hook 管理**：显示 extra_hooks 列表，支持添加（输入全限定类名 + 方法名）和删除
- **状态面板**：从 TraceHook 内部 Set 统计获取已 hook 的类数量和方法数量
- **一键采集**：触发 Perfetto trace 采集（内部调用 `PerfettoCollector.pull_trace_from_device`），采集完成后展示简要摘要
- **恢复默认**：清除 SP，恢复所有内置 hook 默认值，清空 extra_hooks

集成方式（目标 app 无需关心 debug/release）：
```java
// 方式 1：直接启动 Activity
TraceHook.openConfig(context);

// 方式 2：获取 Fragment 嵌入到 DoraemonKit 等调试面板
Fragment configFragment = TraceHook.getConfigFragment();
debugPanel.addTab("SmartInspector", configFragment);

// 方式 3：adb 命令远程打开（无需改目标 app 代码）
// tracelib debug 的 AndroidManifest.xml 通过 manifest merger 自动合并
// 声明了 exported Activity + intent-filter
```

**adb 远程打开配置页面**：

tracelib debug 变体通过 Gradle manifest merger 在 `AndroidManifest.xml` 中声明：
```xml
<!-- tracelib/src/debug/AndroidManifest.xml -->
<activity
    android:name="com.smartinspector.tracelib.HookConfigActivity"
    android:exported="true"
    android:theme="@style/Theme.AppCompat.Light.Dialog"
    android:taskAffinity="com.smartinspector.tracelib"
    android:excludeFromRecents="true">
    <intent-filter>
        <action android:name="com.smartinspector.ACTION_HOOK_CONFIG"/>
        <category android:name="android.intent.category.DEFAULT"/>
    </intent-filter>
</activity>
```

打开方式：
```bash
# 隐式 Intent（不需要知道目标 app 包名）
adb shell am start -a com.smartinspector.ACTION_HOOK_CONFIG

# 显式 Intent（指定目标 app）
adb shell am start -n com.target.package/com.smartinspector.tracelib.HookConfigActivity

# Agent 端自动打开（android_expert_node 内部调用）
subprocess.run(["adb", "shell", "am", "start", "-a", "com.smartinspector.ACTION_HOOK_CONFIG"])
```

release 变体不声明此 Activity — adb 启动无效果，不会报错（`am start` 静默失败）。

### Pine Framework Notes

- Version: `top.canyie.pine:core:0.3.0`
- Cannot hook abstract methods → dynamic hook via setAdapter/setLayoutManager interceptors
- `PineConfig.debuggable = false` — works on non-debuggable apps
- `enhances` module excluded (incompatible with Android 15)
- Uses `Set<Class<?>>` to track already-hooked concrete classes

---

## Data Flow: End-to-End Analysis

### Full Analysis Pipeline (full_analysis route)

```
User: "全面分析列表滑动性能"
  │
  ▼
[orchestrator] → route: full_analysis
  │
  ▼
[collector_node] ─ graph/nodes/collector.py
  ├─ Reads Perfetto params from WS config cache
  ├─ PerfettoCollector.pull_trace_from_device(duration_ms, buffer_size_kb, target_process)
  │   ├─ WS 已连接: 下发 start_trace → App 采集 → WS 上报 .pb
  │   └─ WS 未连接: fallback adb shell perfetto → adb pull .pb
  ├─ PerfettoCollector(trace_path).summarize()
  │   ├─ collect_sched()
  │   ├─ collect_cpu_hotspots()
  │   ├─ collect_frame_timeline()
  │   ├─ collect_memory()
  │   ├─ collect_view_slices()  ← SI$ prefix filtering, rv_instances grouping
  │   ├─ collect_block_events()  ← WS 结构化 JSON + SQL atrace 合并（非覆盖）
  │   └─ Stdlib Mixin modules (via INCLUDE PERFETTO MODULE):
  │       ├─ collect_frame_metrics()        ← android.frames.*
  │       ├─ collect_startup_metrics()      ← android.startup.*
  │       ├─ collect_startup_breakdown()    ← android.startup.startup_breakdowns
  │       ├─ collect_heap_graph_stats()     ← android.memory.heap_graph.heap_graph_stats
  │       ├─ collect_heap_class_aggregation() ← android.memory.heap_graph.heap_graph_class_aggregation
  │       ├─ collect_heap_dominator_tree()  ← android.memory.heap_graph.dominator_tree
  │       ├─ collect_lock_contention()      ← android.monitor_contention
  │       ├─ collect_binder_txns()          ← android.binder
  │       ├─ collect_binder_breakdown()     ← android.binder + android.binder_breakdown
  │       ├─ collect_garbage_collection()   ← android.garbage_collection
  │       ├─ collect_anrs()                 ← android.anrs
  │       ├─ collect_slice_cpu_time()       ← slices.cpu_time
  │       ├─ collect_slice_time_in_state()  ← slices.time_in_state + sched.states
  │       ├─ collect_input_latency()        ← android.input
  │       ├─ collect_sched_latency()        ← sched.latency
  │       ├─ collect_oom_rss_swap()         ← android.memory.process + android.memory.lmk
  │       ├─ collect_process_cpu_utilization() ← linux.cpu.utilization.process
  │       ├─ collect_thread_cpu_utilization()  ← linux.cpu.utilization.thread
  │       └─ collect_surfaceflinger_timeline() ← android.surfaceflinger
  └─ State: perf_summary = "{...json...}", _trace_path = "/tmp/xxx.pb"
  │
  ▼
[analyzer_node] ─ graph/nodes/analyzer.py
  ├─ Reads perf_summary JSON
  ├─ LLM analysis with perf-analyzer prompt
  ├─ Outputs: P0/P1/P2 problems with specific SI$ slice names
  └─ State: perf_analysis = "..."
  │
  ▼
[attributor_node] ─ graph/nodes/attributor.py
  ├─ extract_attributable_slices(perf_json)
  │   ├─ 过滤: startswith("SI$") → 可归因列表
  │   ├─ 两层过滤排除系统类: FQN包名匹配 + 短类名模式匹配
  │   └─ 解析: class name + method name + viewId
  ├─ run_attribution(attributable)
  │   ├─ Glob 定位文件
  │   ├─ Grep 定位类定义
  │   ├─ Read 读取方法实现
  │   └─ LLM 分析代码与性能问题的关系
  └─ State: attribution_data = "[...]", attribution_result = "[...]"
  │
  ▼
[reporter_node] ─ graph/nodes/reporter/
  ├─ formatter.py: 构建报告 sections
  │   ├─ compute_hints() 确定性预计算
  │   ├─ format_perf_sections() 性能数据表格
  │   └─ format_attribution_section() 归因结果
  ├─ generator.py: LLM streaming 生成报告正文
  ├─ persistence.py: 保存到 ./reports/perf_report_YYYYMMDD_HHMMSS.md
  └─ State: messages = [AIMessage(content=complete_report)]
  │
  ▼
END (report displayed + saved to file)
```

### Single-Step Routes

**Android Expert** (`android` route):
```
orchestrator → android_expert → (perf_summary? → analyzer : END)
```

**Standalone Analysis** (`analyze` route):
```
orchestrator → perf_analyzer → END
```

**Code Explorer** (`explorer` route):
```
orchestrator → explorer → END
```

**Trace Only** (`/trace` command):
```
orchestrator → collector → analyzer → END
```

## Key Design Decisions

1. **Graph-based pipeline** — `graph.py` refactored into `graph/` package with `builder.py` (graph construction), `cli.py` (REPL loop), `state.py` (state + routing), `streaming.py` (execution). Pipeline nodes (collector → analyzer → attributor → reporter) are first-class LangGraph nodes, not CLI-loop orchestration.
2. **SI$ prefix** — TraceHook 注入的 tag 统一加 `SI$` 前缀，区分用户代码与系统代码，下游无需硬编码类名规则
3. **Raw trace stays local** — Only ~2KB structured JSON summary is sent to LLM
4. **Streaming first** — reporter streams tokens in real-time; other nodes process silently
5. **Lazy hook** — RV Adapter/LayoutManager hooked dynamically when set; extra_hooks configured at init
6. **Command registry** — Slash commands refactored into `commands/` package with registry pattern (`SLASH_COMMANDS` dict + `handle_slash_command`). Each command file is self-contained.
7. **Programmatic extraction** — Class/method names extracted from JSON by code, search strategy by AI
8. **CS architecture** — Agent (WS server) ↔ App (WS client)，按需懒加载，每个平台 expert 独立管理自己的 WS server
9. **debug/release variants** — Zero-cost abstraction: release = pure no-op stubs (no WS, no hooks), same API surface
10. **Configurable models** — `SI_MODEL` for all agents, `SI_ATTRIBUTOR_MODEL` override for attribution (code understanding)
11. **Reporter sub-module** — Report generation split into formatter (pure), generator (LLM), persistence (IO) for testability
12. **MemorySaver state** — `graph.get_state(config)` replaces manual state merging; `node_error_handler` decorator for unified error handling
13. **Token efficiency** — Route LLM uses max_tokens=5; message window trimming for attributor; reporter input token estimation and truncation; fallback filters Human/AI only
14. **SDK safety** — Trace nesting depth protection; Tag truncation at 127 bytes; BlockMonitor capacity limit; system widget filtering; BuildConfig.DEBUG guards
15. **WS reliability** — Ping/pong heartbeat; startup exception propagation; dynamic port via get_ws_port(); config msg_id + ACK; hook config persistence to local file; ready event to prevent startup race condition
16. **Configurable limits** — Hardcoded values (tool timeout, read limits, report tokens, WS ping timeout) centralized in `config.py` with `SI_*` environment variable overrides
17. **Thread-safe singletons** — LLM client singletons in agents use double-checked locking pattern (`threading.Lock`) for thread-safe lazy initialization
18. **Shared path validation** — Tools share `path_utils.validate_search_path()` to prevent directory traversal attacks

---

## CS Architecture: Agent-App WebSocket Communication

### Overview

类似 React Native 调试的 C/S 架构 — SmartInspector Agent 是服务端，目标 App 是客户端。

**关键设计：WS server 按需懒加载**。WS server 不是全局启动的，而是在 orchestrator 路由到某个平台 expert 时才启动。每个平台 expert 管理自己的 WS 实例。

```
SmartInspector Agent
  │
  ├─ orchestrator → route: android
  │     └── android_expert_node
  │           └── AndroidWSServer :9876 (按需启动，懒加载)
  │
  ├─ orchestrator → route: harmony (未来)
  │     └── harmony_expert_node
  │           └── HarmonyWSServer :9877 (按需启动)
  │
  ├─ orchestrator → route: ios (未来)
  │     └── ios_expert_node
  │           └── iOSWSServer :9878 (按需启动)
  │
  └─ 每个平台 expert 各自管理:
      ├─ WS server 生命周期
      ├─ 通信协议 (不同平台不同)
      └─ 数据格式 (Perfetto / hitrace / Instruments)
```

### Android Connection Flow

```
┌─────────────────────────┐         ┌──────────────────────────┐
│  目标 App (WS Client)    │         │  SmartInspector Agent     │
│                          │         │                          │
│  TraceHook.init()        │  WS     │                          │
│    └── WSClient connect ─┼────────►│  AndroidWSServer :9876   │
│                          │         │    ├── config handler     │
│  TraceHook hooks         │◄────────┼──── config_update        │
│  HookConfigManager (SP)  │  WS     │    ├── trace trigger     │
│  ConfigPanel UI          │         │    ├── status poll        │
│                          │         │    └── message bridge     │
└─────────────────────────┘         │                          │
                                     │  LangGraph Agent         │
                                     │  PerfettoCollector       │
                                     │  Code Explorer           │
                                     └──────────────────────────┘
```

### WS Server Lifecycle (Lazy Init)

```
User: "采集trace分析列表滑动"
  │
  ▼
[orchestrator] → route: android
  │
  ▼
[android_expert_node 启动]
  ├─ 检查: AndroidWSServer 是否已启动？
  │   ├─ 否 → 启动 WS server ws://0.0.0.0:9876
  │   │       → adb forward tcp:9876 tcp:9876
  │   │       → 等待 App 连接（带超时）
  │   └─ 是 → 复用已有连接
  │
  ├─ App 已连接:
  │   └─ 通过 WS 下发 start_trace → App 采集 → 上报 .pb
  │
  ├─ App 未连接 (超时):
  │   └─ fallback 到 adb shell perfetto (旧流程)
  │
  └─ 后续 flow: perf_analyzer → explorer → final conclusion
```

**懒加载的好处**：
- 不使用某个平台时零资源占用
- 每个平台 expert 完全解耦，独立演进
- 新增平台只需新增 expert agent + WS server，不影响其他平台

### Connection Handshake

```
1. orchestrator 路由到 android_expert
   → AndroidWSServer 启动 (首次)
   → adb forward tcp:9876 tcp:9876

2. App 端 (设备上):
   → TraceHook.init(context)
   → WSClient 异步连接 ws://localhost:9876 (通过 adb forward 到电脑)
   → 连接失败静默降级 — hook 正常工作，只是无法远程控制

3. 连接建立:
   → App 上报: {"type": "hello", "package": "com.fdzq.smartinvest", "hooks": {...}}
   → Agent 显示: "设备已连接: com.fdzq.smartinvest"

4. 断线重连:
   → App 侧指数退避重连 (1s, 2s, 4s, ...max 30s)
   → Agent 侧感知断线，CLI 提示
```

### Protocol

**Agent → App** (指令下发)：

| type | 用途 | payload |
|------|------|---------|
| `config_update` | 下发 hook 配置 | `{"hooks": {...}, "extra_hooks": [...]}` |
| `start_trace` | 触发 Perfetto 采集 | `{"duration_ms": 10000}` |
| `get_status` | 查询当前 hook 状态 | — |
| `add_extra_hook` | 新增自定义 hook 点 | `{"class": "...", "methods": [...]}` |
| `remove_extra_hook` | 删除自定义 hook 点 | `{"class": "..."}` |

**App → Agent** (数据上报)：

| type | 用途 | payload |
|------|------|---------|
| `hello` | 连接握手 | `{"package": "...", "hooks": {...}, "hooked_count": {"classes": 5, "methods": 23}}` |
| `status` | 响应 get_status | `{"hooked_classes": [...], "hooked_methods": 23}` |
| `trace_result` | trace 采集完成 | `{"data": "<base64 .pb>", "duration_ms": 10000, "size_kb": 1024}` |
| `config_changed` | 用户在 ConfigPanel 改了配置 | `{"hooks": {...}}` |
| `log` | 实时日志推送 | `{"level": "info", "message": "hooked StockListAdapter"}` |

### Integration with Agent Workflow

```
User: "采集一个trace"
  │
  ▼
[orchestrator] → route: android
  │
  ▼
[android_expert_node]
  ├─ 检查 WS 连接状态
  ├─ 连接已建立:
  │   ├─ 通过 WS 下发 start_trace (duration_ms=10000)
  │   ├─ App 内 Perfetto 采集
  │   ├─ App 通过 WS 上报 trace_result (base64 .pb)
  │   ├─ Agent 收到 .pb → PerfettoCollector.summarize()
  │   └─ 返回 JSON summary
  │
  ├─ 连接未建立 (fallback):
  │   ├─ 走旧流程: adb shell perfetto -c ... (textproto stdin pipe)
  │   ├─ adb pull .pb
  │   └─ PerfettoCollector.summarize()
  │
  └─ 后续 flow 不变: perf_analyzer → explorer → final conclusion
```

### Config Sync Flow

```
┌─────────────────┐                    ┌─────────────────┐
│  ConfigPanel UI │                    │  Agent CLI      │
│  (App 内)       │                    │                 │
│                 │                    │                 │
│  用户关闭 RV   ─┼── config_changed ─►│  收到，显示提示 │
│                 │                    │                 │
│                 │◄── config_update ──┼─ LLM 建议开启   │
│  自动刷新 UI    │                    │  inflate hook   │
│                 │                    │                 │
└─────────────────┘                    └─────────────────┘

双向同步，后到者优先 (last-write-wins)
App 侧持久化到 SP，Agent 侧保存在内存 (session 级)
```

### Implementation Points

**Agent 端 (Python)**：
- WebSocket server 使用 `websockets` 或 `aiohttp` 库
- 启动时自动 `adb forward tcp:9876 tcp:9876`
- 支持 adb 设备断开/重连时重新 forward
- `collect_android_trace` tool 增加优先走 WS 通道的路径，fallback 到 adb shell

**App 端 (tracelib debug)**：
- WebSocket client 使用 `OkHttp` 或 Java NIO
- `TraceHook.init()` 时异步连接，不阻塞启动
- 连接失败静默降级 — hook 正常工作，只是无法远程控制
- 收到 `config_update` → 更新内存配置 + 写 SP + 刷新 ConfigPanel UI
- 收到 `start_trace` → 调用 `PerfettoCollector` 采集 → base64 编码推送

**tracelib release**：
- WSClient 不存在 — `TraceHook.init()` 只有空实现
- 不含任何 WebSocket 依赖
- 不含 OkHttp 等网络库引用
