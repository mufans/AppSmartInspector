
# SmartInspector — Project Rules

## Project Overview

AI-powered Android performance analysis CLI. Collects Perfetto traces from device, analyzes with LLM agents, and generates source-attributed performance reports.

## Project Structure

```
src/smartinspector/          # Main Python package (installed via hatchling)
  agents/                    # LLM agent logic (attribution, analysis, frame analysis)
    deterministic.py         #   Pre-computed hints (no LLM) — severity, call chain, thread state
  commands/                  # Slash command handlers
    trace.py                 #   /trace, /record, /analyze, /frame, /open, /close
    orchestrate.py           #   /full, /report
    hook.py                  #   /config, /hooks, /hook, /debug
    device.py                #   /devices, /connect, /status, /disconnect
    session.py               #   /help, /clear, /summary, /tokens
  collector/                 # Perfetto trace collection & SQL analysis
    perfetto.py              #   PerfettoCollector — 13 collect_*() methods
  graph/                     # LangGraph orchestration
    nodes/                   #   Graph nodes (orchestrator, collector, attributor, reporter, ...)
      reporter/              #   Reporter sub-package
        formatter.py         #     format_perf_sections(), format_attribution_section()
        generator.py         #     LLM report generation
        persistence.py       #     Markdown report file output
    state.py                 #   AgentState TypedDict, _pass_through(), node_error_handler()
  tools/                     # File search tools (glob, grep, read) — used by agents
  ws/                        # WebSocket server for app communication
  config.py                  # Runtime configuration (env vars: SI_*)
  debug_log.py               # Debug logging utility → reports/debug_*.log
  perfetto_compat.py         # macOS IPv4 fix for perfetto trace_processor
  prompts.py                 # Prompt loader (reads prompts/*.txt)
prompts/                     # LLM prompt text files
  attributor.txt             #   Source attribution instructions
  report-generator.txt       #   Report generation instructions
  perf-analyzer.txt          #   Performance analysis instructions
  frame-analyzer.txt         #   Frame analysis instructions
  android-expert.txt         #   Android domain knowledge
  code-explorer.txt          #   Code exploration instructions
platform/android/            # Android test app (Kotlin/Java hook layer)
perfetto-plugin/             # SI Bridge Perfetto UI plugin source (TypeScript)
perfetto-build/              # Forked Perfetto repo with plugin built in
reports/                     # Generated output: debug_*.log + perf_report_*.md
docs/                        # Design documents
bin/                         # trace_processor_shell binary (used by PerfettoCollector)
```

## Python Conventions

### Language & Tooling

- Python >= 3.12 (uses `X | Y` union syntax, `list[dict]` generics)
- Package manager: hatchling (`pyproject.toml`)
- Test framework: pytest
- Entry point: `smartinspector = "smartinspector.graph:main"`

### Import Order

1. Standard library (`import os`, `from pathlib import Path`)
2. Third-party (`from langchain_core.messages import AIMessage`)
3. Local (`from smartinspector.config import get_model`)

### Naming

- Files: `snake_case.py`
- Classes: `PascalCase`
- Functions/methods: `snake_case`
- Constants: `UPPER_CASE`
- Private: `_leading_underscore`

### Type Hints

- All public functions must have type hints
- Use `X | None` (not `Optional[X]`)
- Use `list[dict]` (not `List[Dict]`)
- State uses `TypedDict` with `Annotated[list, operator.add]` for list merging

### Docstrings

- Google style with `Args:` / `Returns:` sections
- Module-level docstring: single-line description

## Logging Rules (IMPORTANT)

Three logging mechanisms, each with distinct purpose:

| Mechanism | Usage | Output |
|-----------|-------|--------|
| `debug_log(category, msg)` | Pipeline data inspection | `reports/debug_*.log` |
| `print(f"  [node] msg", flush=True)` | User-facing progress | Console (stdout) |
| `logger.debug(msg)` | Internal library logging | Python logging (not in debug log files) |

**Rule**: When adding observability to collector/agent/reporter code that needs to appear in `reports/debug_*.log`, use `debug_log()` — NOT `logger.debug()`.

Enable debug mode: `SI_DEBUG=1` or `--debug` flag.

Categories: `collector`, `attributor`, `reporter`, `ws`, `full`.

### debug_log API

```python
from smartinspector.debug_log import debug_log
debug_log("collector", f"thread_state: {name} running={running_ms:.1f}ms")
```

- Thread-safe (serialized via threading.Lock)
- No-op when `SI_DEBUG` is not set
- Auto-creates `reports/` directory and log file on first call

## Architecture

### LangGraph Pipeline

```
orchestrator → collector → attributor → reporter
                    ↓
              perf_analyzer (for /analyze)
```

### Graph Node Pattern

Every node follows this pattern:

```python
from smartinspector.graph.state import AgentState, _pass_through, node_error_handler

@node_error_handler("my_node")
def my_node(state: AgentState) -> dict:
    # ... processing ...
    return {
        "messages": [AIMessage(content="...")],
        "my_field": new_value,
        **_pass_through(state),  # forward unchanged state fields
    }
```

### Agent Pattern

Agents are separate from graph nodes. They contain the business logic:

- `agents/attributor.py` — Source code attribution (fast-path + LLM fallback)
- `agents/deterministic.py` — Pure computation hints (no LLM): severity, call chain distribution, RV hotspot ranking, jank frame correlation, CPU hotspot identification, thread state analysis
- `agents/frame_analyzer.py` — Frame-level analysis
- `agents/perf_analyzer.py` — Performance analysis
- `agents/explorer.py` — Code exploration

### AgentState Fields

| Field | Type | Description |
|-------|------|-------------|
| `messages` | `Annotated[list, operator.add]` | Accumulated conversation messages |
| `perf_summary` | `str` | JSON from PerfettoCollector (PerfSummary.to_json()) |
| `perf_analysis` | `str` | Markdown from analysis agent |
| `attribution_data` | `str` | JSON: list of attributable SI$ slices |
| `attribution_result` | `str` | JSON: source attribution results with snippets |
| `trace_duration_ms` | `int` | CLI override: trace duration |
| `trace_target_process` | `str` | CLI override: target process name |
| `skip_wait` | `bool` | CLI flag: skip waiting for app connection |
| `_route` | `str` | Internal: RouteDecision value |
| `_trace_path` | `str` | Internal: trace file path (set by /full <trace.pb>) |

### RouteDecision

```
full_analysis  → collector → attributor → reporter
android        → android_expert
analyze        → collector → perf_analyzer
explorer       → explorer
trace          → collector → perf_analyzer
end            → END
```

## Commands

### /full (Main Entry Point)

```
/full [--no-wait] [--debug] [duration_ms] [package_name]
/full <trace.pb> [--debug] [package_name]
```

- `--no-wait`: Start trace immediately without waiting for app (useful for cold start profiling)
- `--debug`: Enable debug logging to `reports/debug_*.log`
- `<trace.pb>`: Analyze existing trace file, skip device collection (Mode 1)
- Without `.pb`: Pull new trace from device (Mode 2)

### Other Commands

| Command | Description |
|---------|-------------|
| `/trace <package>` | Record and load a trace |
| `/record [duration]` | Start perfetto recording on device |
| `/analyze` | Analyze loaded trace |
| `/frame ts=X dur=Y` | Frame-level analysis |
| `/open` | Start Perfetto UI bridge server + browser |
| `/close` | Stop bridge server |
| `/report` | Re-generate report from existing analysis |
| `/config [key=val]` | Set/get config values (model, source_dir) |
| `/hooks` | List available trace hooks |
| `/hook <name>` | Toggle a specific hook |
| `/debug [on\|off]` | Toggle debug logging |
| `/devices` | List connected Android devices |
| `/connect <addr>` | Connect to device via ADB |
| `/status` | Show device and session status |
| `/summary` | Show current trace summary |
| `/tokens` | Show token usage stats |

## Configuration

Environment variables with `SI_` prefix:

| Variable | Default | Purpose |
|----------|---------|---------|
| `SI_MODEL` | `deepseek-chat` | Default LLM model |
| `SI_BASE_URL` | `https://api.deepseek.com` | API base URL |
| `SI_API_KEY` | — | API key (fallback: `OPENAI_API_KEY`) |
| `SI_ATTRIBUTOR_MODEL` | — | Model override for attributor role |
| `SI_DEBUG` | — | Enable debug logging (`1`/`true`/`yes`) |
| `SI_WS_PORT` | `9876` | WebSocket server port |
| `SI_WS_PING_TIMEOUT` | `30` | WebSocket ping timeout (seconds) |
| `SI_REPORT_MAX_TOKENS` | `4000` | Max input tokens for report generation |
| `SI_TOOL_TIMEOUT` | `30` | Timeout for tool subprocess calls (seconds) |
| `SI_READ_MAX_LINES` | `2000` | Max lines returned by read tool |
| `SI_READ_MAX_BYTES` | `51200` | Max bytes returned by read tool |
| `SI_READ_MAX_LINE_LENGTH` | `2000` | Max characters per line in read output |

Configuration via `.env` file at project root (auto-loaded by `python-dotenv`).

## Perfetto Trace Collection

### Trace Config

Trace config in `PerfettoCollector.pull_trace_from_device()`:
- Default atrace categories: `sched, freq, idle, power, memreclaim, gfx, view, input, dalvik, am, wm`
- Includes `sched/sched_switch` ftrace events (required for `thread_state` table)
- Additional data sources: `linux.perf` (CPU sampling), `linux.process_stats` (process memory), `android.java_hprof` (heap dump), `android.log` (logcat)
- Buffer: 65536 KB default

### trace_processor_shell

`PerfettoCollector` uses a local `trace_processor_shell` binary (not the Python pip package's bundled one):
- Binary location: `bin/trace_processor_shell`
- Configured via `TraceProcessorConfig(bin_path=str(SHELL_BIN))`
- macOS IPv4 fix applied via `perfetto_compat.patch()` (forces `127.0.0.1` instead of `localhost`)

### Collector Analysis Methods

Each `collect_*()` method queries Perfetto SQL tables and returns structured data:

| Method | Returns | SQL Tables |
|--------|---------|------------|
| `collect_sched()` | Scheduling stats, hot threads, blocked reasons | `sched`, `thread` |
| `collect_cpu_hotspots()` | CPU flame graph data with callchain reconstruction | `perf_sample`, `stack_profile_callsite`, `stack_profile_frame` |
| `collect_thread_state()` | Running/Sleeping/DiskSleep per SI$ slice | `sched` (see note below) |
| `collect_frame_timeline()` | Frame timing + jank detection | `actual_frame_timeline_slice`, `expected_frame_timeline_slice` |
| `collect_cpu_usage()` | CPU usage per-core over time | `counter`, `cpu` |
| `collect_sys_stats()` | System stats (memory, CPU freq) | `counter` |
| `collect_process_memory()` | Per-process memory (RSS, anon) | `process_memory_snapshot` |
| `collect_memory()` | Aggregated memory summary | `process_memory_snapshot` |
| `collect_threads()` | Thread list for target process | `thread`, `process` |
| `collect_view_slices()` | View system slices (doFrame, measure, layout, draw, RV) with parent chains | `slice`, `args` |
| `collect_io_slices()` | IO-related slices (net/db/img) | `slice` |
| `collect_input_events()` | Touch/input event data | `slice` |
| `collect_block_events()` | SI$block slices merged with logcat SIBlock stack traces | `slice`, `android_logs` |

**Note on `collect_thread_state`**: Currently uses `sched` table overlap calculation. Planned upgrade to use `__intrinsic_thread_state` table for `blocked_function`, `waker_utid`, and `io_wait` data (see `docs/thread-state-blocking-analysis-design.md`).

### SI$ Custom Tag System

Android hook layer emits custom Perfetto slices with `SI$` prefix:

| Tag Pattern | Description | Example |
|-------------|-------------|---------|
| `SI$RV#[viewId]#[Adapter].[method]` | RecyclerView pipeline | `SI$RV#recycler_view#DemoAdapter.onBindViewHolder` |
| `SI$block#[stack]#[duration]` | Main thread block (detected by BlockMonitor) | `SI$block#worker.CpuBurnWorker$1#129ms` |
| `SI$inflate#[layout]#[parent]` | LayoutInflater inflate | `SI$inflate#item_complex#recycler_view` |
| `SI$view#[class].[method]` | View traverse (measure/layout/draw) | `SI$view#HeavyDrawView.onDraw` |
| `SI$handler#[msg_class]` | Handler message dispatch | `SI$handler#ScrollRunnable` |
| `SI$Activity.[lifecycle]` | Activity lifecycle | `SI$Activity.onResume` |
| `SI$Fragment.[lifecycle]` | Fragment lifecycle | `SI$Fragment.onCreateView` |
| `SI$db#...` | Database operations (excluded from main analysis) | |
| `SI$net#...` | Network operations (excluded from main analysis) | |
| `SI$img#...` | Image operations (excluded from main analysis) | |
| `SI$touch#...` | Touch events (excluded from thread state analysis) | |

## Reporter Pipeline

### Data Flow

```
perf_summary (JSON)
  → formatter.format_perf_sections()      # Build LLM prompt sections
  → deterministic.compute_hints()          # Pre-computed conclusions (no LLM)
  → LLM (report-generator prompt)         # Generate markdown report
  → persistence.save_report()             # Write to reports/perf_report_*.md
```

### Report Sections (Priority Order)

Sections are ordered by priority to survive truncation at `SI_REPORT_MAX_TOKENS`:

1. **Attribution results** — must not be truncated (source code locations)
2. **Perf sections**:
   - 预计算结论 (deterministic hints)
   - 线程状态分析 (thread state)
   - 帧时间线 (frame timeline)
   - 自定义切片统计 (view slices summary)
3. **Report header** (summary tables)
4. **Performance analysis** (LLM-generated)

### Key Formatter Functions

- `format_perf_sections(perf_json)` → `list[str]`: Formats raw perf JSON into markdown sections for LLM prompt
- `format_attribution_section(attribution_result)` → `list[str]`: Formats source attribution results with file paths, line numbers, and source snippets

### Deterministic Pre-computation

`agents/deterministic.py` provides 6 analysis modules (all pure Python, no LLM):

1. `_classify_severity()` — P0/P1/P2 severity based on device frame budget
2. `_compute_call_chain_distribution()` — Call chain time distribution with percentages
3. `_rank_rv_hotspots()` — RecyclerView hotspot ranking by max/avg duration
4. `_correlate_jank_frames()` — Frame ↔ Slice ↔ InputEvent three-way correlation
5. `_identify_cpu_hotspots()` — CPU function sampling hotspot identification
6. `_analyze_thread_state()` — Running vs Sleeping/DiskSleep classification per slice

## Android App Conventions

- Package: `com.smartinspector.hook`
- Language: Mix of Kotlin and Java
- Location: `platform/android/app/src/main/`
- Naming: PascalCase for classes/fragments/adapters
- Package structure mirrors component type: `adapter/`, `worker/`, `ui/`, `model/`, `repository/`

### Hook Layer

The Android app injects trace hooks that emit `SI$` prefixed slices into Perfetto traces:
- `TraceHook` — Base hook class, uses `android.os.Trace.beginSection()` / `endSection()`
- `BlockMonitor` — Detects main thread blocks, posts stack traces to logcat as `SIBlock` messages
- Hooks configured via `/config` command at runtime

## Git Conventions

- Main branch: `master`
- Branch naming: `feat/`, `fix/`, `hotfix/` prefixes
- Commit format: Conventional commits (`feat(scope): description`, `fix(scope): description`)

## Known Issues & Design Notes

### Perfetto `thread_state` Virtual Table Limitation

The `thread_state` virtual table depends on `sched_switch` events. When a thread runs CPU-bound code without context switches, no new `sched_switch` fires, so the table incorrectly inherits the last state (typically `S`/Sleeping). The `__intrinsic_thread_state` table has the same underlying data but includes additional fields (`blocked_function`, `waker_utid`, `io_wait`) that provide actionable blocking context.

### Reporter Truncation

Content exceeding `SI_REPORT_MAX_TOKENS` (default 4000) is truncated at paragraph (`\n\n`) boundaries. Section ordering determines survival — sections placed earlier are more likely to survive.

### Anonymous Inner Class Naming

SI$ slices from anonymous inner classes follow the pattern `OuterClass$innerMethod$1`. The attribution pipeline extracts the enclosing method name via `_extract_method_from_anonymous()`. When `context_method == method_name`, the display avoids redundant duplication (e.g., showing `startMainThreadWork` instead of `startMainThreadWork$startMainThreadWork`).

## Perfetto UI Plugin System

- Must fork google/perfetto — no side-loading of plugins
- Plugins go in `ui/src/plugins/<id>/`
- Auto-discovered by `generateImports()` → `ui/src/gen/all_plugins.ts`
- Register in `ui/src/core/embedder/default_plugins.ts` as string array
- Plugin API: `trace.selection.registerAreaSelectionTab()` for area tabs
- `AreaSelection.start/end` are `time` type (branded bigint), not number
- `render()` must return `ContentWithLoadingFlag | undefined` (not m.Children)
- Build: `perfetto-plugin/build.sh` (auto-removes Android NDK from PATH to avoid strip conflict)

### Bridge Architecture

```
Perfetto UI Plugin (WS client)
  → ws://127.0.0.1:9877/bridge
  → BridgeServer (bridge_server.py)
  → frame_analyzer agent → LLM → results back
```

- BridgeServer uses `websockets` lib with `process_request` hook for HTTP static files
- Static files served from `perfetto-build/ui/out/dist/`

### Build Notes

- `build.sh` auto-removes Android NDK from PATH (strip conflict on macOS)
- WASM build requires emscripten (auto-installed by `tools/install-build-deps`)
- For proxy: set `http_proxy`/`https_proxy` before running build
- `~/.curlrc` with `--http1.1` needed if proxy causes HTTP/2 errors

---

## Logging Standard

### 规则

1. **统一使用标准 `logging` 模块**，禁止使用 `print()` 输出日志
2. 每个模块文件顶部初始化：`logger = logging.getLogger(__name__)`
3. 日志级别规范：
   - `logger.debug()` — 调试信息（SQL查询失败、fallback触发、内部状态变化）
   - `logger.info()` — 关键流程节点（采集开始/完成、报告生成完成、设备连接）
   - `logger.warning()` — 可恢复的异常（API降级、fallback、重试）
   - `logger.error()` — 严重错误（采集失败、报告生成失败）
4. **日志格式统一**：`[模块名] 消息内容`，通过 logging formatter 配置，不要在消息中手动加 `[tag]`
5. **用户面向的进度输出**（流式token、进度条等）可以继续用 `print()`，但必须标注 `# noqa: LOG` 注释说明原因
6. **禁止裸 `print()`**：所有 print 必须改为 logger 调用，除非有明确注释说明原因

### 当前需要改造的文件

- `src/smartinspector/graph/nodes/collector.py` — 多处 `print("  [collector] ...")`
- `src/smartinspector/graph/nodes/analyzer.py` — `print("  [analyzer] ...")`
- `src/smartinspector/graph/nodes/reporter/__init__.py` — 多处 `print("  [reporter] ...")`
- `src/smartinspector/graph/nodes/reporter/persistence.py` — `print("  [reporter] ...")`
- `src/smartinspector/graph/nodes/reporter/generator.py` — `print("  [reporter] ...")`（流式输出除外）

### logging 配置

在 CLI 入口（`cli.py`）统一配置 logging：

```python
import logging

logging.basicConfig(
    level=logging.INFO,
    format='%(asctime)s [%(name)s] %(levelname)s: %(message)s',
    datefmt='%H:%M:%S',
)
# debug 模式通过 --debug 参数降低到 DEBUG 级别
```

### 示例

```python
# ✅ 正确
import logging
logger = logging.getLogger(__name__)

logger.info("Starting trace collection")
logger.debug("process table lookup failed: %s", e)
logger.warning("__intrinsic_thread_state not available, falling back to sched")

# ❌ 错误
print("  [collector] Starting trace collection...")  # 改用 logger.info()
print(f"  [reporter] Failed to save report: {e}")     # 改用 logger.error()

# ✅ 允许的 print（用户面向的流式输出）
print(token, end="", flush=True)  # noqa: LOG — streaming LLM tokens to user
```
