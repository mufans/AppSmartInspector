# AppSmartInspector 功能架构分析报告

> 审查日期: 2026-05-06
> 审查范围: `src/smartinspector/` 全部 62 个 Python 源文件，约 13,287 行代码
> 审查人: 资深架构师

---

## 一、项目定位与现状总结

AppSmartInspector 是一个 AI 驱动的移动端性能分析 CLI 工具，核心能力是：
- 从 Android 设备采集 Perfetto trace
- 通过 LLM Agent 分析性能瓶颈
- 将性能热点归因到具体源码位置
- 生成 Markdown/JSON 结构化报告

当前仅支持 Android 平台，HarmonyOS 和 iOS 在规划中。

### 量化概览

| 指标 | 数值 |
|------|------|
| Python 源文件数 | 62 |
| 总代码行数 | ~13,287 |
| 测试文件数 | 4 |
| 测试代码行数 | ~1,208 |
| 测试覆盖率 | 约 9% (1208/13287) |
| 最大单文件 | `collector/perfetto.py` (2,345 行) |
| collect_* 方法数 | 14 |
| graph 节点数 | 10 (orchestrator + 9 业务节点) |
| Agent 数 | 6 (attributor, perf_analyzer, frame_analyzer, explorer, android, deterministic) |

---

## 二、当前架构分析

### 2.1 整体架构评分: 7.5 / 10

项目整体架构设计合理，LangGraph pipeline 模式清晰，deterministic pre-computation 层是亮点。但在模块粒度、扩展性、测试覆盖等方面存在明显改进空间。

### 2.2 架构分层

```
┌─────────────────────────────────────────────────────────┐
│                    CLI Entry Points                       │
│  cli.py (argparse)  │  graph/cli.py (REPL)  │ headless  │
├─────────────────────────────────────────────────────────┤
│                  Commands Layer (Slash)                   │
│  trace │ orchestrate │ hook │ device │ session │ compare │
├─────────────────────────────────────────────────────────┤
│               LangGraph Orchestration                     │
│  builder.py ← state.py ← streaming.py                   │
│  Nodes: orchestrator → collector → analyzer →            │
│         attributor → reporter → startup → metric_qa      │
├─────────────────────────────────────────────────────────┤
│                    Agent Layer                            │
│  attributor │ perf_analyzer │ frame_analyzer │           │
│  explorer   │ android       │ deterministic (纯计算)      │
│  verifier (质量验证)                                      │
├─────────────────────────────────────────────────────────┤
│                   Collector Layer                         │
│  perfetto.py (PerfettoCollector - 14 methods)            │
│  startup.py (StartupAnalyzer)  │  memory.py (MemoryAnalyzer) │
├─────────────────────────────────────────────────────────┤
│                Infrastructure Layer                       │
│  config.py │ debug_log.py │ prompts.py │ token_tracker   │
│  ws/server │ ws/bridge_server │ perfetto_compat          │
│  tools (grep/glob/read/perfetto) │ storage/store         │
└─────────────────────────────────────────────────────────┘
```

### 2.3 各模块质量评估

| 模块 | 行数 | 评分 | 评价 |
|------|------|------|------|
| `graph/state.py` | 88 | 9/10 | 简洁的 AgentState 定义，`_pass_through` 和 `node_error_handler` 设计得当 |
| `graph/builder.py` | 108 | 9/10 | 图构建清晰，路由映射完备，条件边设计合理 |
| `agents/deterministic.py` | 822 | 9/10 | 纯计算层，与 LLM 职责分离明确，是架构亮点 |
| `agents/verifier.py` | ~150 | 8.5/10 | 0 token 验证，L1+L2 分层设计优秀 |
| `graph/nodes/orchestrator.py` | 263 | 8/10 | 路由逻辑完备，metric_qa 处理合理，但 prompt 内联过长 |
| `graph/streaming.py` | 74 | 8/10 | 流式处理简洁，get_state() 复用得当 |
| `config.py` | ~170 | 8/10 | 集中管理配置，支持环境变量覆盖，但有重复模式 |
| `token_tracker.py` | ~80 | 9/10 | 线程安全，API 设计清晰 |
| `collector/perfetto.py` | 2,345 | 6.5/10 | 功能完整但文件过大，存在 SQL 安全风险、静默异常吞没 |
| `agents/attributor.py` | 1,027 | 7/10 | fast-path 优化到位，但 LLM 单例管理混乱 |
| `commands/attribution.py` | 1,164 | 7/10 | SI$ tag 解析完备，但大量重复解析逻辑 |
| `ws/bridge_server.py` | 436 | 6/10 | 全局可变状态过多，trace path 管理不够健壮 |
| `headless.py` | 169 | 7.5/10 | 复用 LangGraph pipeline，但 JSON 序列化冗余 |
| `storage/store.py` | ~200 | 7/10 | 基线管理功能完整，但与 reporter 耦合较紧 |

### 2.4 做得好的地方（架构亮点）

#### 1. Deterministic Pre-computation 层
`agents/deterministic.py` 将算术和分类逻辑从 LLM 中完全剥离。`compute_hints()` 提供 8 个纯计算模块（severity、call_chain、RV hotspot、jank correlation、CPU hotspot、thread_state、SQL summarizer、perf JSON compression），大幅减少 LLM token 消耗，同时提高结论准确性。

**这是项目最有价值的架构决策**，将"确定性计算"与"LLM 理解"清晰分离。

#### 2. Attributor Fast-path
`agents/attributor.py` 对简单 Java 类搜索走 Glob→Grep→Read 快路径，绕过 LLM 调用。Partial fallback 机制（部分命中 fast-path，其余走 LLM）设计精巧，兼顾效率和覆盖。

#### 3. SI$ Tag System
完整的 tag 解析体系（`commands/attribution.py`），涵盖 RV、block、inflate、view、handler、compose、net、db、img 等 12+ 种模式。匿名内部类处理尤其周到（`_extract_method_from_anonymous`）。

#### 4. `node_error_handler` Decorator
统一的节点错误处理模式，`@node_error_handler("node_name")` 确保任何节点异常不会导致 pipeline 崩溃，返回安全状态 + AIMessage 错误提示。

#### 5. Pipeline Architecture Rule
CLAUDE.md 中明确规定"所有新功能必须复用 LangGraph pipeline 链路"，避免独立执行路径。这个约定保证了架构一致性。

#### 6. Trace Collection Degradation
PerfettoCollector 的 stdin-pipe → cat-pipe → cmdline 三级降级策略，覆盖 SELinux 限制等设备兼容性问题。

### 2.5 核心数据流

```
用户输入 → orchestrator_node (LLM 路由)
              │
              ├─ full_analysis/startup → collector_node
              │    │
              │    └─ PerfettoCollector.summarize()
              │       → 14 collect_*() 方法 → PerfSummary JSON
              │       → perf_summary (str) 存入 AgentState
              │
              ├─ analyzer_node
              │    │
              │    └─ compute_hints(perf_json)  ← 确定性预计算
              │    └─ perf_analyzer agent (LLM) ← 生成分析
              │       → perf_analysis (str) 存入 AgentState
              │
              ├─ attributor_node
              │    │
              │    └─ extract_attributable_slices(perf_json)
              │    └─ fast-path: Glob→Grep→Read (无 LLM)
              │    └─ slow-path: attributor agent (LLM + tools)
              │       → attribution_result (str) 存入 AgentState
              │
              └─ reporter_node
                   │
                   └─ compute_hints(perf_json)  ← 第二次计算（重复！）
                   └─ format_perf_sections(perf_json)
                   └─ LLM report generation (流式)
                      → Markdown/JSON 报告
```

---

## 三、识别的核心问题

### 3.1 P0 — 架构级问题（影响可维护性和扩展性）

#### P0-1: `collector/perfetto.py` 巨型文件（2,345 行）

**问题**: PerfettoCollector 单文件包含 14 个 `collect_*()` 方法，每个方法包含独立的 SQL 查询、数据转换和异常处理逻辑。文件行数是第二大文件（`commands/attribution.py` 1,164 行）的两倍。

**影响**:
- 代码审查困难，单个 PR 难以覆盖完整变更
- 不同 collect 方法之间的公共逻辑（SQL 查询模板、结果转换）被复制
- 无法独立测试单个 collect 方法（需 mock 整个 PerfettoCollector）

**根因**: 缺少 collector 方法级别的模块化拆分。

#### P0-2: AgentState 中 `perf_summary` 为 JSON 字符串

**问题**: `perf_summary: str` 在 pipeline 内部以 JSON 字符串形式传递。每个消费节点（analyzer、attributor、reporter、metric_qa）都需要 `json.loads()` 解析，产生 3-4 次重复反序列化。

**影响**:
- 不必要的 CPU 消耗和内存分配
- 类型不安全：消费方无法获得类型提示
- `compute_hints()` 在 analyzer 和 reporter 阶段被调用两次（完全重复计算）

**根因**: LangGraph 的 `TypedDict` state 要求可序列化类型，最初用 JSON string 是最简单方案，但缺乏演进。

#### P0-3: LLM 实例管理碎片化

**问题**: 6 个 Agent 模块各自维护独立的 LLM 单例，管理模式不一致：

| 模块 | 模式 | 问题 |
|------|------|------|
| `orchestrator.py` | 全局 `_route_llm` | 被 reporter generator 不合理复用 |
| `attributor.py` | 全局 + Lock + `_structured_ok` 探测 | 全局可变状态竞态风险 |
| `perf_analyzer.py` | 全局 + Lock | 独立实例 |
| `frame_analyzer.py` | 全局 + Lock | 独立实例 |
| `explorer.py` | 全局 + Lock | 使用可能废弃的 `create_agent` API |
| `android.py` | 全局 + Lock | 同上 |

**影响**:
- 修改 LLM 配置（如 temperature、model）需要改 6 个文件
- Agent 实例化模式不一致（`bind_tools` + 手动 dispatch vs `create_agent` vs 单次 invoke）
- 无法统一管理 token 配额和限流

#### P0-4: 测试覆盖率极低（~9%）

**问题**: 仅 4 个测试文件，1,208 行测试代码。核心业务逻辑（SI$ tag 解析、deterministic hints、SQL 查询、归因逻辑）缺乏测试保护。

**影响**:
- 重构风险极高：无法验证变更不引入回归
- 架构改进难以落地：缺乏安全网
- 关键业务逻辑（severity 分类、hotspot 排名）的正确性依赖人工验证

### 3.2 P1 — 设计缺陷（影响代码质量和可靠性）

#### P1-1: SI$ Tag 解析逻辑重复

**问题**: `commands/attribution.py` (1,164 行) 中 `extract_class()`、`extract_method()`、`extract_fqn()` 三个函数对同一个 SI$ tag 分别独立解析，每个函数都有相同的 `if body.startswith("block#"):` 等分支结构，大量重复代码。

**影响**: 修改 tag 格式（如新增 `SI$compose#`）需要在 3+ 个函数中同步修改，容易遗漏。

#### P1-2: SQL 注入风险

**问题**: `collector/perfetto.py` 多处 SQL 查询使用 f-string 拼接：
- `WHERE name = '{package_name}'` (用户输入)
- `WHERE uid = {uid}` (内部生成但未验证)
- `WHERE id IN ({id_list})` (列表拼接)

虽然 trace_processor 是本地进程，但 `package_name` 来自 CLI/WS 配置，理论上有注入可能。

#### P1-3: `compute_hints()` 重复调用

**问题**: `compute_hints(perf_json)` 在 pipeline 中被调用两次：
1. `perf_analyzer.py` — analyzer 阶段
2. `formatter.py` — reporter 阶段（通过 `format_perf_sections`）

同一段 JSON 被解析和计算两次，完全浪费。

#### P1-4: Bridge Server 全局状态管理

**问题**: `bridge_server.py` 使用 4 个模块级全局变量管理状态：
```python
_active_bridge: BridgeServer | None = None
_active_trace_server = None
_cached_perf_summary: str = ""
_cached_attribution_result: str = ""
```

变量命名不一致（`_perf_summary_cache` vs `_cached_perf_summary`），状态清理依赖调用方正确执行。

#### P1-5: Agent API 不一致

**问题**: 三种不同的 Agent 实现模式并存：
1. `explorer.py` / `android.py` — `langchain.agents.create_agent`（可能已废弃）
2. `attributor.py` — `llm.bind_tools` + 手动 tool dispatch
3. `perf_analyzer.py` / `frame_analyzer.py` — 单次 `llm.invoke`

增加新 Agent 时没有统一模式可参考。

### 3.3 P2 — 优化项（影响性能和开发体验）

#### P2-1: `perf_analyzer.py` 武断截断

`perf_json[:3000]` 硬截断后发送给 LLM，可能截断关键数据（如 thread_state），也可能包含无用数据（如 cpu_idle_samples 时间序列）。

#### P2-2: config.py 重复模式

6 个 `get_*()` 函数完全同构（`try: return int(os.environ.get(...)) except: return default`），应提取为 `_env_int()` 辅助函数。

#### P2-3: 函数内部 import

`formatter.py` 在函数内部 `from smartinspector.agents.deterministic import compute_hints`，虽然 Python 缓存模块，但代码组织不规范。

#### P2-4: perf_analyzer 智能截断

应根据 `compute_hints` 结果智能选择补充数据，而非盲截。

---

## 四、功能架构改进方案

### 4.1 改进一：Collector 模块化拆分

**目标**: 将 2,345 行的 `perfetto.py` 拆分为模块化结构。

**方案**:

```
collector/
├── __init__.py
├── perfetto.py          # PerfettoCollector 基类（open/close/summarize 公共逻辑）
├── sched.py             # collect_sched(), collect_thread_state()
├── cpu.py               # collect_cpu_hotspots(), collect_cpu_usage()
├── frame.py             # collect_frame_timeline(), collect_view_slices()
├── memory.py            # collect_process_memory(), collect_memory()  (已存在，整合)
├── sys.py               # collect_sys_stats(), collect_threads()
├── io.py                # collect_io_slices(), collect_input_events()
├── block.py             # collect_block_events()
├── compose.py           # collect_compose_slices()
├── startup.py           # StartupAnalyzer (已存在，保持)
├── sql_utils.py         # 公共 SQL 查询工具（参数化、批量查询、CTE 模板）
└── types.py             # PerfSummary dataclass, 公共类型定义
```

**关键设计**:
- `PerfettoCollector` 保留为入口类，通过 Mixin 或组合模式组合各模块方法
- 每个 `collect_*()` 方法可独立测试（只需 mock `TraceProcessor.query()`）
- `sql_utils.py` 集中管理 SQL 模板，统一参数化查询

**预期收益**:
- 单文件代码量从 2,345 行降到 ~300 行（核心类） + 各子模块 100-200 行
- 可独立测试每个 collect 方法
- SQL 安全修复可集中在一个文件
- 新增 collect 方法只需新建文件 + 注册到主类

**实施复杂度**: 中等。主要是代码搬迁 + import 调整。

**优先级**: P0

---

### 4.2 改进二：AgentState 数据类型优化

**目标**: 消除 pipeline 内部 JSON 字符串的反复序列化/反序列化。

**方案**:

```python
# graph/state.py
class AgentState(TypedDict):
    messages: Annotated[list, operator.add]
    perf_summary_raw: dict          # ← 内部传递 dict（替代 str）
    perf_summary: str               # ← 仅在边界序列化（LLM prompt 输入）
    perf_hints: str                 # ← 新增：缓存 compute_hints 结果
    perf_analysis: str
    attribution_data: str
    attribution_result: str
    trace_duration_ms: int
    trace_target_process: str
    skip_wait: bool
    _route: str
    _trace_path: str
```

**迁移路径**:
1. `collector_node` 中 `PerfettoCollector.summarize()` 返回 `dict`，同时生成 JSON string
2. `perf_summary_raw` 存 dict，`perf_summary` 存 str（向后兼容）
3. analyzer / attributor / reporter 优先读 `perf_summary_raw`
4. `compute_hints()` 结果存入 `perf_hints`，reporter 直接复用

**预期收益**:
- 消除 3-4 次重复 `json.loads()` 调用
- `compute_hints()` 只调用一次
- 消费方获得类型提示
- 对大 trace 文件（perf JSON > 100KB）性能提升明显

**实施复杂度**: 中等。需要修改 state 定义和所有消费节点。

**优先级**: P1

---

### 4.3 改进三：统一 LLM 实例管理（LLMFactory）

**目标**: 集中管理所有 LLM 实例的创建和配置。

**方案**:

```python
# llm_factory.py（新文件）
import threading
from langchain_openai import ChatOpenAI
from smartinspector.config import get_llm_kwargs

class LLMFactory:
    """Centralized LLM instance management."""
    _instances: dict[str, ChatOpenAI] = {}
    _lock = threading.Lock()

    @classmethod
    def get(cls, role: str = "default", **overrides) -> ChatOpenAI:
        """Get or create an LLM instance for the given role.

        Args:
            role: "default" | "attributor" | "router" | "streaming"
            **overrides: Additional kwargs passed to ChatOpenAI
        """
        key = f"{role}:{frozenset(overrides.items())}"
        if key not in cls._instances:
            with cls._lock:
                if key not in cls._instances:
                    kwargs = get_llm_kwargs(role=role if role != "default" else None)
                    kwargs.update(overrides)
                    cls._instances[key] = ChatOpenAI(**kwargs)
        return cls._instances[key]

    @classmethod
    def get_with_tools(cls, role: str, tools: list, **overrides):
        """Get LLM with bound tools."""
        return cls.get(role, **overrides).bind_tools(tools)

    @classmethod
    def get_structured(cls, role: str, schema, **overrides):
        """Get LLM with structured output."""
        return cls.get(role, **overrides).with_structured_output(schema)

    @classmethod
    def reset(cls):
        """Clear all instances (for testing)."""
        with cls._lock:
            cls._instances.clear()
```

**迁移路径**:
1. 创建 `llm_factory.py`
2. 逐个替换 6 个 Agent 的 LLM 初始化代码
3. `attributor.py` 的 `_structured_ok` 探测逻辑改为初始化时一次性完成
4. 统一 Agent 实现模式为 `bind_tools` + 手动 dispatch（attributor 的模式）

**预期收益**:
- LLM 配置变更只需改一处
- 统一 temperature、max_tokens 等参数管理
- Token 配额和限流可集中管理
- 测试可轻松 mock LLM

**实施复杂度**: 中低。逐个文件替换，无功能变更。

**优先级**: P1

---

### 4.4 改进四：SI$ Tag 统一解析

**目标**: 消除 `commands/attribution.py` 中的重复解析逻辑。

**方案**:

```python
# commands/attribution.py
from dataclasses import dataclass

@dataclass
class SITag:
    """Structured representation of an SI$ tag."""
    tag_type: str        # "block", "RV", "inflate", "view", "handler", "compose", "net", "db", "img"
    class_name: str      # 短类名
    method_name: str     # 方法名
    fqn: str             # 完全限定名（可能为空）
    search_type: str     # "java", "xml", "system"
    io_type: str | None  # "network" | "database" | "image" | None
    raw_name: str        # 原始 tag
    extras: dict         # 额外字段（view_id, layout, duration 等）

def parse_si_tag(name: str) -> SITag | None:
    """Single-pass SI$ tag parser.

    替代 extract_class() + extract_method() + extract_fqn() 三次独立解析。
    """
    ...  # 单次遍历，一次解析所有字段
```

**预期收益**:
- 代码量从 ~1,164 行降至 ~400 行
- 新增 tag 类型只需添加一个分支
- 类型安全：消费方通过 `SITag` dataclass 获取字段
- 可独立测试

**实施复杂度**: 低。纯重构，无功能变更。

**优先级**: P1

---

### 4.5 改进五：测试基础设施搭建

**目标**: 建立核心模块的测试保护网，目标覆盖率 50%+。

**方案（按优先级排序）**:

| 优先级 | 模块 | 测试类型 | 理由 |
|--------|------|----------|------|
| P0 | `agents/deterministic.py` | 单元测试 | 纯函数，最容易测试，覆盖核心业务逻辑 |
| P0 | `commands/attribution.py` (parse_si_tag) | 单元测试 | 纯函数，tag 解析是最核心的业务逻辑 |
| P1 | `graph/state.py` (_pass_through, node_error_handler) | 单元测试 | 确保状态传递和错误处理正确 |
| P1 | `agents/verifier.py` | 单元测试 | 确保验证逻辑正确 |
| P1 | `graph/builder.py` | 集成测试 | 验证图构建和路由正确性 |
| P2 | `collector/perfetto.py` (各 collect_*) | 单元测试 (mock SQL) | 需要 mock TraceProcessor |
| P2 | `agents/attributor.py` (fast-path) | 单元测试 (mock tools) | 验证归因逻辑 |

**测试基础设施**:
- 使用 `pytest` + `pytest-mock`
- 创建 `tests/conftest.py` 提供公共 fixture（mock TraceProcessor、sample perf JSON、sample SI$ tags）
- CI 中运行 `pytest` 作为 gate check

**预期收益**:
- 重构和架构改进有安全网
- 回归检测自动化
- 新功能开发可先写测试

**实施复杂度**: 低到中。纯函数测试容易，mock 测试需要设计 fixture。

**优先级**: P0

---

### 4.6 改进六：扩展性改进 — 平台抽象层

**目标**: 为 HarmonyOS / iOS 平台扩展建立清晰的架构基础。

**当前问题**: 所有平台相关逻辑（adb、Perfetto、Android SDK hook）硬编码在 collector/ 和 agents/ 中。添加 HarmonyOS 支持（hdc、hitrace）需要在多处添加 if/else 分支。

**方案**:

```
collector/
├── base.py              # BaseCollector 抽象基类
│   └── abstract methods:
│       ├── pull_trace() → str
│       ├── get_target_process() → str
│       └── get_device_info() → dict
├── android/
│   ├── perfetto.py      # PerfettoCollector(BaseCollector)
│   ├── sched.py
│   ├── cpu.py
│   └── ...
├── harmonyos/
│   ├── hitrace.py       # HitraceCollector(BaseCollector)
│   └── ...
└── types.py             # PerfSummary dataclass (平台无关)
```

**关键设计原则**:
1. `BaseCollector` 定义标准采集接口和输出格式（`PerfSummary`）
2. 平台特有逻辑封装在各自的 collector 子类中
3. `PerfSummary` dataclass 是平台无关的中间表示
4. 下游的 analyzer/attributor/reporter 不关心数据来自哪个平台

**实施路径**: 长期演进，当前可在代码中预留接口（添加 TODO 标记），不急于实现。

**优先级**: P2（长期演进方向）

---

### 4.7 改进七：Orchestrator 路由 Prompt 外置

**目标**: 将 orchestrator 的路由 prompt 从 Python 代码内联移到 `prompts/` 目录。

**当前问题**: `orchestrator.py` 中的 `_ROUTE_PROMPT`（65 行）和 `_FALLBACK_SYSTEM`（10 行）直接内联在 Python 代码中。按项目规则（CLAUDE.md "Prompt 管理规则"），超过 3 行的 prompt 必须抽取到 `prompts/` 目录。

**方案**:
- 创建 `prompts/route-classification.txt`
- 创建 `prompts/fallback-system.txt`
- `orchestrator.py` 中使用 `load_prompt("route-classification")` 加载

**预期收益**:
- 符合项目 Prompt 管理规则
- 路由 prompt 可独立编辑和版本管理
- A/B 测试不同路由策略时更方便

**实施复杂度**: 极低。

**优先级**: P2

---

## 五、实施路线图

### Phase 1: 基础加固（建议立即开始）

| # | 项目 | 涉及文件 | 预期收益 |
|---|------|----------|----------|
| 1 | 测试基础设施 + deterministic 测试 | 新建测试文件 | 重构安全网 |
| 2 | SI$ Tag 统一解析 (parse_si_tag) | `commands/attribution.py` | 代码量减少 60%+ |
| 3 | config.py 提取 `_env_int()` | `config.py` | 消除 6 个重复函数 |
| 4 | Orchestrator prompt 外置 | `orchestrator.py` → `prompts/` | 符合项目规范 |
| 5 | formatter.py 内部 import 移到顶部 | `formatter.py` | 代码规范 |

### Phase 2: 架构改进（基础加固完成后）

| # | 项目 | 涉及文件 | 预期收益 |
|---|------|----------|----------|
| 6 | LLMFactory 统一管理 | 新建 `llm_factory.py` + 6 个 Agent 文件 | 统一 LLM 实例管理 |
| 7 | Collector 模块化拆分 | `collector/perfetto.py` → 10 个子模块 | 单文件从 2345 行降至 ~300 行 |
| 8 | AgentState 数据类型优化 | `state.py` + 所有消费节点 | 消除重复 JSON 序列化 |
| 9 | SQL 参数化查询 | `collector/sql_utils.py` | 消除 SQL 注入风险 |
| 10 | compute_hints 缓存 | `state.py` + `perf_analyzer.py` + `formatter.py` | 消除重复计算 |

### Phase 3: 扩展性建设（长期演进）

| # | 项目 | 涉及文件 | 预期收益 |
|---|------|----------|----------|
| 11 | 平台抽象层（BaseCollector） | `collector/base.py` | 多平台扩展基础 |
| 12 | Bridge Server 类封装 | `ws/bridge_server.py` | 消除全局状态 |
| 13 | Agent API 统一（bind_tools） | `explorer.py` + `android.py` | 统一 Agent 模式 |
| 14 | perf_analyzer 智能截断 | `agents/perf_analyzer.py` | LLM token 减少 30-50% |

---

## 六、风险与注意事项

### 6.1 重构风险控制

1. **先测试后重构**: Phase 2 的每项改进都应在 Phase 1 的测试基础上进行
2. **增量变更**: 每个改进独立一个 commit，不混合多个改进
3. **向后兼容**: AgentState 新增字段而非修改现有字段，确保现有功能不受影响
4. **CI 验证**: 每次变更后运行 `uv run smartinspector --help` 验证入口正常

### 6.2 架构约束

1. **Pipeline Architecture Rule**: 所有新功能必须通过 LangGraph graph 执行
2. **Logging Standard**: 禁止 `import logging`，统一使用 `info_log()` / `debug_log()`
3. **Prompt 管理**: 超过 3 行的 prompt 必须外置到 `prompts/` 目录
4. **文档同步**: 新增命令/功能必须同步更新 CLAUDE.md

### 6.3 不建议做的事

1. **不建议引入新的编排框架**: LangGraph 已经满足需求，引入新框架增加复杂度
2. **不建议将 Collector 改为异步**: 当前同步模型简单可靠，异步改造收益不大
3. **不建议过早抽象平台层**: 等 HarmonyOS 需求明确后再做，避免过度设计
4. **不建议合并 Agent 和 Node 层**: 当前两层分离（Node 负责状态管理，Agent 负责业务逻辑）是合理的

---

## 七、总结

AppSmartInspector 的核心架构（LangGraph pipeline + deterministic pre-computation + fast-path attribution）设计合理，是一个在"LLM 效率"和"分析准确性"之间取得良好平衡的系统。

**最需要改进的三个领域**:
1. **测试覆盖**（从 9% → 50%+）：是所有其他改进的基础
2. **Collector 模块化**（2,345 行 → 多模块）：是可维护性的关键瓶颈
3. **LLM 实例统一管理**（6 处碎片化 → LLMFactory）：是扩展性的前提

**最具价值的改进路径**: 先建立测试 → 再重构 Collector → 再统一 Agent 管理。这条路径确保每一步都有安全网，每一步都使代码更易于维护和扩展。
