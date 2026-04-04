# SmartInspector

AI 驱动的跨平台移动端性能分析 CLI 工具。通过自然语言交互，自动采集设备性能 trace，分析性能瓶颈，并将热点归因到源码。

当前已实现 **Android** 平台完整支持，**HarmonyOS** 和 **iOS** 平台支持规划中。

## 快速开始

```bash
# 安装依赖
uv sync

# 配置 LLM（复制示例配置并填入 API Key）
cp .env.example .env
# 编辑 .env: SI_API_KEY=your-api-key

# 启动 CLI（自动启动 WS server + adb reverse）
uv run smartinspector --source-dir /path/to/your/app/source

# 交互式使用
you> 全面分析列表滑动性能
you> 采集一个 10s trace 分析卡顿
you> 搜索源码中 LazyForEach 的用法
```

## 架构概览

```
用户自然语言 → LangGraph Orchestrator → 路由到专用 Agent / Pipeline → 报告输出
                    │
        ┌───────────┼───────────────┬──────────────┐
        ▼           ▼               ▼              ▼
  Android Expert  Perf Analyzer  Explorer     Full Pipeline
  (adb+Perfetto)  (LLM解读JSON)  (grep/glob)  (collector→analyzer→attributor→reporter)
```

全量分析流水线（LangGraph 图节点编排）：

```
collector (设备 trace 采集) → analyzer (LLM 性能解读) → attributor (源码归因) → reporter (生成 Markdown 报告)
```

详细架构见 [ARCHITECTURE.md](ARCHITECTURE.md)。

## 平台支持

| 平台 | 状态 | Trace 采集 | 方法 Hook | 源码归因 |
|------|------|-----------|----------|---------|
| Android | **已实现** | Perfetto + adb | Pine AOP | 支持 |
| HarmonyOS | 规划中 | hdc + hiperf/hitrace | — | — |
| iOS | 规划中 | Instruments + Xcode | — | — |

### Android

- Trace 采集：Perfetto (ftrace + atrace + CPU callstack + Java heap)
- 方法 Hook：Pine AOP 框架，运行时 hook Activity/Fragment/RecyclerView 等框架方法
- 卡顿检测：BlockMonitor (BlockCanary-style)，监测主线程每条 Message 耗时
- 通信：WebSocket (adb reverse)，CLI ↔ App 实时配置同步 + 数据传输

### HarmonyOS (规划)

- Trace 采集：hdc + hiperf/hitrace
- 方法 Hook：待定
- 已有 prompt 模板：`reference-hdc-commands.txt`

## 项目结构

```
smartinspector/
├── src/smartinspector/              # Python CLI + Agent
│   ├── cli.py                      #   CLI 入口 (argparse)
│   ├── graph/                      #   LangGraph 编排 (模块化包)
│   │   ├── __init__.py             #     公共导出 (create_graph, run_graph, main)
│   │   ├── builder.py              #     LangGraph 图构建 (节点+边+条件路由)
│   │   ├── cli.py                  #     CLI REPL 主循环 (prompt_toolkit)
│   │   ├── state.py                #     AgentState + RouteDecision + pass-through
│   │   ├── streaming.py            #     图流式执行 (_stream_run)
│   │   └── nodes/                  #     LangGraph 图节点
│   │       ├── orchestrator.py     #       路由分类 + fallback
│   │       ├── android.py          #       Android Expert (trace 采集+分析)
│   │       ├── analyzer.py         #       性能分析 (perf_analyzer_node + analyzer_node)
│   │       ├── explorer.py         #       源码搜索 (grep/glob/read)
│   │       ├── collector.py        #       设备 trace 采集 (PerfettoCollector)
│   │       ├── attributor.py       #       源码归因 (SI$ slice → 源码定位)
│   │       └── reporter/           #       报告生成
│   │           ├── __init__.py     #         reporter_node 入口
│   │           ├── generator.py    #         LLM 报告生成 (流式+重试)
│   │           ├── formatter.py    #         数据格式化 (perf+归因→Markdown)
│   │           └── persistence.py  #         报告文件保存
│   │
│   ├── agents/                     #   Agent 定义 (LLM + Tools)
│   │   ├── android.py              #     Android Expert Agent
│   │   ├── explorer.py             #     Code Explorer Agent
│   │   ├── perf_analyzer.py        #     Perf Analyzer (单次 LLM 调用)
│   │   ├── attributor.py           #     源码归因 Agent (run_attribution)
│   │   └── deterministic.py        #     确定性预计算 (减少 LLM token)
│   │
│   ├── collector/perfetto.py       #   PerfettoCollector (adb→SQL→JSON)
│   ├── commands/                   #   Slash 命令 (注册表模式)
│   │   ├── __init__.py             #     命令注册表 (handle_slash_command)
│   │   ├── attribution.py          #     SI$ tag 解析 + 归因提取
│   │   ├── device.py               #     设备管理 (/devices, /connect)
│   │   ├── hook.py                 #     Hook 配置 (/config, /hooks)
│   │   ├── orchestrate.py          #     编排命令 (/full, /report)
│   │   ├── session.py              #     会话管理 (/help, /clear)
│   │   └── trace.py                #     Trace 采集 (/trace, /record)
│   │
│   ├── tools/                      #   LangChain 工具 (grep/glob/read/perfetto)
│   ├── ws/server.py                #   WebSocket Server (CLI↔App 通信)
│   ├── prompts.py                  #   Prompt 文件加载器
│   ├── config.py                   #   全局配置 (LLM 模型, source dir)
│   ├── token_tracker.py            #   LLM Token 使用量追踪
│   └── perfetto_compat.py          #   macOS IPv4 兼容修复
│
├── platform/                       # 平台 SDK
│   └── android/tracelib/           #   Android SDK (AAR)
│       └── src/main/java/.../tracelib/
│           ├── TraceHook.java          # Pine AOP 方法 hook 入口
│           ├── BlockMonitor.java       # 主线程卡顿检测
│           ├── HookConfig.java         # 配置模型 (JSON 序列化)
│           └── HookConfigManager.java  # 配置管理 (SP 持久化)
│
├── prompts/                        # LLM Prompt 模板
├── bin/                            # trace_processor_shell
├── reports/                        # 生成的性能报告 (Markdown)
└── tests/                          # 单元测试
```

## Hook 体系 (Android)

SDK 通过 Pine AOP 框架 hook 框架方法，用 `SI$` 前缀的 `Trace.beginSection` 标记用户代码调用，Perfetto atrace 采集后由 CLI 分析。

### Hook 类别

| Hook | 默认 | Tag 格式 | 说明 |
|------|------|----------|------|
| Activity Lifecycle | ON | `SI$ActivityClass.onCreate` | Activity 生命周期 |
| Fragment Lifecycle | ON | `SI$FragmentClass.onCreateView` | Fragment 生命周期 (AndroidX + app) |
| RV Pipeline | ON | `SI$RV#[viewId]#[Adapter].dispatchLayoutStep2` | RecyclerView 管线 |
| RV Adapter | ON | `SI$RV#[viewId]#[Adapter].onBindViewHolder` | Adapter 数据绑定 |
| Layout Inflate | OFF | `SI$inflate#[layout]#[parent]` | 布局加载 |
| View Traverse | OFF | `SI$view#[ViewClass].measure` | View measure/layout/draw |
| Handler Dispatch | OFF | `SI$handler#[msgClass]` | Handler 消息分发 |
| Block Monitor | ON | `SI$block#[MsgClass]#[dur]ms` | 主线程卡顿检测 (≥100ms) |
| Network IO | OFF | `SI$net#[Class].execute` | OkHttp / HttpURLConnection |
| Database IO | OFF | `SI$db#[Class].query#[table]` | SQLiteDatabase / Room |
| Image Load | OFF | `SI$img#[Class].into` | Glide / Coil |

**IO Hook 说明**：Network/DB/Image hook 在所有线程执行，使用独立前缀 (`SI$net#`/`SI$db#`/`SI$img#`)，Python 端单独收集到 `io_slices`，不污染主线程 `view_slices` 分析。

### 源码归因流程

```
Trace → SI$ slices → 过滤系统类 → 提取 class+method → Glob→Grep→Read 搜索源码 → LLM 归因
```

归因系统通过两层过滤排除系统/框架代码：
1. **FQN 包名匹配**：`android.*`、`androidx.*`、`java.*` 等
2. **短类名模式匹配**：`Choreographer`、`FragmentManager`、`ViewRootImpl` 等（Perfetto atrace 截断 FQN 时）

## CLI 命令

### Slash 命令

| 命令 | 说明 |
|------|------|
| `/full` | 全量分析流水线 (采集→分析→归因→报告) |
| `/trace [duration]` | 采集 Perfetto trace |
| `/analyze` | 分析已有 perf_summary |
| `/report` | 生成性能报告 |
| `/config [key] [value]` | 查看或修改配置 |
| `/config source_dir <path>` | 设置源码目录 |
| `/hooks` | 查看 hook 配置 |
| `/hook add <class> <method>` | 添加自定义 hook |
| `/devices` | 列出已连接设备 |
| `/connect` | 连接 WS 服务 |
| `/status` | 查看 WS 状态 |
| `/summary` | 查看 perf_summary 摘要 |
| `/tokens` | 查看 token 使用量 |
| `/clear` | 清除会话状态 |
| `/help` | 帮助信息 |

### 自然语言路由

Orchestrator 通过 LLM 分类将用户请求路由到对应 Agent：

- **全面分析** (`full_analysis`): "全面分析列表滑动性能" → collector → analyzer → attributor → reporter
- **平台采集** (`android`): "采集 trace 分析 FPS" → Platform Expert Agent
- **性能解读** (`analyze`): "解读这份数据" → Perf Analyzer
- **源码搜索** (`explorer`): "搜索 XXX 类源码" → Code Explorer
- **通用问答** (`end`): "什么是卡顿" → Fallback 回复

## 报告示例

全量分析流水线（`/full` 或自然语言触发 `full_analysis`）会生成 Markdown 性能报告，保存到 `reports/` 目录。以下为实际生成的报告摘要：

### 测试概要

```
| 项目 | 内容 |
|------|------|
| 应用 | com.smartinspector.hook |
| 时长 | 10.0s |
| 日期 | 2026-04-04 09:30 |
```

### 性能总览

```
| 指标       | 数值     | 评价 |
|------------|----------|------|
| 平均 FPS   | 27.8     | 差   |
| 卡顿次数   | 5        |      |
| CPU 峰值   | 50.3%    | 良   |
| 内存峰值   | 993MB    | 差   |
```

### 问题列表（源码归因）

报告会通过 SI$ tag 将性能热点归因到具体源码位置，并给出优化建议：

```
### P0 RecyclerView 布局与数据绑定严重卡顿

现象：DemoAdapter.dispatchLayoutStep2 单次耗时 221.24ms，超出帧预算 15.5 倍。
原因：onBindViewHolder 中存在 Thread.sleep、同步数据加载、主线程图片解码。
位置：platform/android/app/.../DemoAdapter.java:40-64

建议：
1. 移除 Thread.sleep(20ms)
2. 将 loadItemsSync 改为异步加载
3. 使用 Glide/Coil 异步图片加载
4. 使用 DiffUtil 增量更新
```

完整报告示例见 [reports/perf_report_20260404_093038.md](reports/perf_report_20260404_093038.md)。

## 技术栈

| 组件 | 技术 |
|------|------|
| Agent 编排 | LangGraph + LangChain |
| LLM | DeepSeek / Claude / OpenAI (通过 SI_MODEL 配置) |
| Android Trace | Perfetto + atrace |
| HarmonyOS Trace | hiperf + hitrace (规划) |
| 方法 Hook (Android) | Pine AOP Framework |
| CLI 交互 | prompt_toolkit |
| 通信 | WebSocket (CLI ↔ App) |
| Trace 分析 | trace_processor_shell (SQL) |

## LLM 配置

通过 `.env` 文件或环境变量配置 LLM 提供商：

```bash
cp .env.example .env
```

| 变量 | 说明 | 默认值 |
|------|------|--------|
| `SI_MODEL` | 全局默认模型 | `deepseek-chat` |
| `SI_BASE_URL` | API Base URL (OpenAI 兼容) | `https://api.deepseek.com` |
| `SI_API_KEY` | API Key (回退到 `OPENAI_API_KEY`) | — |
| `SI_ATTRIBUTOR_MODEL` | 归因 Agent 模型覆盖 (代码理解) | 同 `SI_MODEL` |

**切换到 Claude 示例：**
```bash
SI_MODEL=claude-sonnet-4-20250514
SI_BASE_URL=https://api.anthropic.com
SI_API_KEY=sk-ant-xxx
```

**归因用更强模型示例：**
```bash
SI_MODEL=deepseek-chat
SI_ATTRIBUTOR_MODEL=claude-sonnet-4-20250514
```

归因环节需要代码理解能力，建议使用更强的模型。其他环节（路由、报告）用 DeepSeek 即可。

## 环境要求

- Python 3.12+
- macOS (trace_processor_shell 为 arm64 二进制)
- **Android**: 设备 API 28+，开启 USB 调试，adb 已加入 PATH
- **HarmonyOS**: hdc 已加入 PATH (规划)
- **iOS**: Xcode + Instruments (规划)

## Todo

### 高优先级

- [ ] `collect_view_slices` 调用链限制从 10 提升到 20
- [ ] `collect_block_events` 处理 atrace 截断的 block name fallback
- [ ] 帧严重度阈值区分刷新率 (120Hz 设备帧预算 8.33ms)
- [ ] 输入事件关联 (touch event → frame jank 因果)
- [ ] 系统类模式补充: `WindowCallback`, `IdleHandler`, Jetpack Compose 类

### 中优先级

- [ ] RV Instance 区分 create vs bind 开销
- [ ] attributor agent 内部类 `$数字` 跳过 Glob 直接 grep 外部类
- [ ] Perfetto `android.surfaceflinger.frame` 维度 (CPU vs GPU 瓶颈)
- [ ] 自适应阈值 (基于设备能力动态调整)
- [ ] 报告缺少对比基线 (before/after)

### 平台扩展

- [ ] HarmonyOS collector (hdc + hiperf/hitrace)
- [ ] iOS Instruments 集成
- [ ] Jetpack Compose 性能 hook
- [ ] Native C/C++ 代码覆盖
- [ ] 内存分配热点追踪 (当前仅 RSS)

### 工程优化

- [ ] LRU 文件缓存减少重复 Read
- [ ] 工具结果截断 (10K 字符上限)
- [ ] 更多复杂 trace 测试 (Kotlin、多文件)
- [ ] CI/CD 集成
