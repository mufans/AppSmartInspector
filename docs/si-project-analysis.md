# SmartInspector 项目架构分析

## 项目概述

SmartInspector 是一个 AI 驱动的移动端性能分析 CLI 工具。核心能力：通过自然语言交互，自动从 Android 设备采集 Perfetto trace，分析性能瓶颈，并将热点精确归因到应用源码位置。

**目标用户**：Android 开发者、性能优化工程师
**核心价值**：将传统的 trace 采集 → 手动分析 → 代码定位 流程自动化

## 目录结构

```
src/smartinspector/
├── graph/                    # LangGraph 编排层（核心入口）
│   ├── __init__.py           #   公共导出: create_graph, run_graph, main
│   ├── builder.py            #   图构建：节点注册 + 条件边
│   ├── cli.py                #   REPL 主循环 (prompt_toolkit)
│   ├── state.py              #   AgentState TypedDict + RouteDecision 枚举
│   ├── streaming.py          #   流式执行 + MemorySaver
│   └── nodes/                #   图节点（每个节点映射一个处理步骤）
│       ├── orchestrator.py   #     LLM 路由分类
│       ├── collector.py      #     设备 trace 采集
│       ├── analyzer.py       #     性能分析 (perf_analyzer + analyzer)
│       ├── attributor.py     #     源码归因
│       ├── reporter/         #     报告生成 (formatter → generator → persistence)
│       ├── startup.py        #     冷启动分析
│       ├── metric_qa.py      #     自然语言指标查询
│       ├── android.py        #     Android Expert
│       └── explorer.py       #     源码搜索
├── agents/                   # Agent 业务逻辑（LLM + Tools）
│   ├── attributor.py         #   源码归因 Agent (Glob→Grep→Read 搜索)
│   ├── perf_analyzer.py      #   性能分析 Agent (LLM + 验证重试)
│   ├── frame_analyzer.py     #   帧分析 Agent (Perfetto UI 交互)
│   ├── deterministic.py      #   确定性预计算 (无 LLM，纯 Python)
│   ├── verifier.py           #   分析质量验证 (L1+L2, 0 token)
│   ├── android.py            #   Android Expert Agent
│   └── explorer.py           #   Code Explorer Agent
├── collector/                # 采集层
│   ├── perfetto.py           #   PerfettoCollector (adb→SQL→JSON)
│   ├── startup.py            #   冷启动阶段分析器
│   └── memory.py             #   内存分配分析器
├── commands/                 # Slash 命令处理
│   ├── trace.py, orchestrate.py, hook.py, device.py, session.py, ...
├── tools/                    # LangChain 工具 (glob/grep/read/perfetto)
├── ws/                       # WebSocket 通信 (CLI↔App, Bridge Server)
├── storage/                  # 报告存储层 (历史对比)
├── config.py                 # 全局配置 (SI_* 环境变量)
├── headless.py               # Headless/CI 非交互式运行器
└── prompts.py                # Prompt 文件加载器
```

## 模块职责

| 模块 | 职责 |
|------|------|
| `graph/builder.py` | 构建 LangGraph StateGraph，定义节点和条件路由 |
| `graph/nodes/orchestrator.py` | LLM 路由：将用户自然语言分类到 6 种路由决策 |
| `graph/nodes/collector.py` | Trace 采集：adb shell perfetto → pull .pb → SQL 查询 |
| `graph/nodes/analyzer.py` | 调用 perf_analyzer Agent，LLM 解读 perf JSON |
| `graph/nodes/attributor.py` | 提取 SI$ 切片，搜索源码，定位到文件+行号 |
| `graph/nodes/reporter/` | 格式化数据 → LLM 生成报告 → 保存到文件 |
| `collector/perfetto.py` | PerfettoCollector：14 个 collect_*() 方法，查询 Perfetto SQL |
| `agents/deterministic.py` | 纯计算分析（严重度分级、调用链分布、热点排名等） |
| `agents/attributor.py` | LangChain Agent：用 Glob/Grep/Read 工具搜索源码 |
| `agents/verifier.py` | L1 格式检查 + L2 一致性验证，0 token |
| `ws/server.py` | WebSocket Server：CLI↔App 双向通信 |
| `ws/bridge_server.py` | Perfetto UI Bridge：自托管 UI + WS 桥接 |

## 核心数据流

### 全量分析流水线（主路径）

```
用户输入 → orchestrator (LLM 路由, max_tokens=5)
  → collector (adb perfetto → .pb → trace_processor_shell SQL → PerfSummary JSON)
  → analyzer (perf_analyzer Agent: LLM 解读 + deterministic hints + 验证重试)
  → attributor (提取 SI$ 切片 → Glob/Grep/Read 源码搜索 → LLM 归因)
  → reporter (format → LLM 流式生成 Markdown → save to file)
```

### 路由决策

`orchestrator_node` 通过 LLM 将用户请求分类为：
- `full_analysis` → collector → analyzer → attributor → reporter
- `startup` → collector → analyzer → startup → attributor → reporter
- `android` → android_expert (采集 trace)
- `analyze` → perf_analyzer (解读现有数据)
- `explorer` → explorer (源码搜索)
- `metric_qa:<id>` → metric_qa (指标追问)
- `end` → fallback (通用问答)

### 状态流转

`AgentState` (TypedDict) 是唯一的状态载体，在节点间传递：
- `messages`：累积的对话消息
- `perf_summary`：PerfettoCollector 输出的 JSON
- `perf_analysis`：LLM 分析结果
- `attribution_data`/`attribution_result`：源码归因数据
- `_route`：路由决策（驱动条件边）

## 关键依赖

| 依赖 | 用途 |
|------|------|
| `langgraph` | 图编排框架，定义节点+条件边的 DAG |
| `langchain` + `langchain-openai` | LLM 调用 (OpenAI 兼容 API) |
| `perfetto` | trace_processor Python SDK，SQL 查询 trace |
| `prompt_toolkit` | CLI REPL (Tab 补全、交互) |
| `websockets` | CLI↔App 双向通信 |
| `python-dotenv` | 环境变量管理 |

LLM 提供商可配置：默认 DeepSeek，支持 Claude/OpenAI（通过 `SI_MODEL`/`SI_BASE_URL` 环境变量切换）。

## 输入输出格式

**输入**：
- 交互式：自然语言 + Slash 命令 (`/full`, `/trace`, `/analyze` 等)
- CI 模式：`--ci --trace trace.pb --target com.xxx --format json`

**中间数据**：
- `PerfSummary` JSON（~2-10KB）：包含 frame_timeline、cpu_usage、view_slices、io_slices、thread_state 等 14 个维度
- `SI$` 自定义切片：Android Hook 层注入的 Perfetto slice，格式 `SI$<tag>#<class>.<method>`

**输出**：
- Markdown 报告（`reports/perf_report_*.md`）：P0/P1/P2 分级问题 + 源码位置 + 优化建议
- JSON 报告（CI 模式）：结构化 issues 数组
- 冷启动报告：4 阶段时序分析 + 瓶颈识别

## AI 诊断核心逻辑

### 1. 确定性预计算 (`agents/deterministic.py`)
纯 Python，不调用 LLM，提供 8 个分析模块：
- 严重度分级（基于设备帧预算，120Hz = 8.33ms）
- 调用链时间分布
- RecyclerView 热点排名
- 帧↔切片↔输入事件三路关联
- CPU 热点识别
- 线程状态分析（Running/Sleeping/DiskSleep）
- SQL 结果压缩（统计摘要 + 异常采样，降低 60-80% token）
- Perf JSON 压缩

### 2. LLM 分析 (`agents/perf_analyzer.py`)
- 输入：压缩后的 perf JSON + deterministic hints
- 输出：Markdown 格式性能分析
- 验证：L1 格式检查（数值/方法名/长度/分级）+ L2 一致性验证（P0 覆盖/数值±20%/热点覆盖）
- L2 失败自动重试一次

### 3. 源码归因 (`agents/attributor.py`)
- 从 perf JSON 提取 SI$ 切片
- 过滤系统/框架类（FQN 包名匹配 + 短类名模式匹配）
- LangChain Agent 调用 Glob→Grep→Read 工具在源码中定位
- LLM 生成源码归因摘要（含文件路径、行号、代码片段）

### 4. SI$ Hook 体系 (Android SDK)
- Pine AOP 框架 hook 框架方法
- TraceHook 发射 `SI$` 前缀的 `Trace.beginSection` 标记
- 支持 12 种 Hook 类型：Activity/Fragment 生命周期、RV 管线、Layout Inflate、View Traverse、Handler、Block Monitor、IO (net/db/img)、Compose 重组
