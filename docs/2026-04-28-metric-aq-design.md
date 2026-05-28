# Metric QA — 自然语言指标问答设计

## 背景

当前 SmartInspector 的分析模式是全量采集 + 全量分析（`/full`），用户无法针对单个指标追问。用户在跑完一次完整分析后，常需要针对具体指标做深入了解，例如"CPU 占用率怎么样？"、"内存有没有泄漏？"。

本设计新增 `metric_qa` 节点，允许用户在已有 `perf_summary` 数据的基础上，用自然语言追问具体性能指标。

## 约束

- **前置条件**：用户必须先通过 `/full`、`/trace`、`/analyze` 等命令完成分析，`state["perf_summary"]` 中有数据。若无数据，提示用户先采集。
- **触发场景**：仅在交互式 CLI 模式中使用，不涉及 headless/CI。
- **指标范围**：固定 20 个预定义指标，不做开放问答。
- **回答深度**：数据提取 + LLM 解读（包含优化建议）。

## 指标定义

### 指标映射表

共 6 大类、20 个细粒度指标，每个指标映射到 `perf_summary` 中的数据段：

#### 一、CPU & 调度类

| ID | 指标名 | 中/英触发词 | perf_summary 数据段 |
|---|---|---|---|
| `cpu` | CPU 占用率 | cpu占用 / cpu usage / cpu使用率 | `cpu_usage.overall_cpu_pct` + `per_thread` |
| `cpu_hotspot` | CPU 热点函数 | cpu热点 / hot function / 火焰图 | `cpu_hotspots[].function, samples, pct, callchain` |
| `sched` | 线程调度 | 调度 / 上下文切换 / context switch | `scheduling.hot_threads` (switches, dominant_state) |
| `blocked` | 主线程阻塞 | 阻塞 / 卡住 / block / ANR | `block_events[].dur_ms, stack_trace` |

#### 二、内存类

| ID | 指标名 | 中/英触发词 | perf_summary 数据段 |
|---|---|---|---|
| `memory` | 内存占用 | 内存 / memory / RSS | `process_memory[].rss_kb, avg_rss_kb` |
| `heap` | 堆分析 / 对象分布 | 堆 / heap / 对象 / leak / 内存泄漏 | `memory.heap_graph_classes` (obj_count, total_size_kb) |

#### 三、UI & 渲染类

| ID | 指标名 | 中/英触发词 | perf_summary 数据段 |
|---|---|---|---|
| `frame` | 帧率 / 卡顿 | 帧率 / fps / 卡顿 / jank / 掉帧 | `frame_timeline.fps, jank_frames, slowest_frames` |
| `rv` | RecyclerView | 滚动 / recycler / list / 列表 | `view_slices.rv_instances[].methods, total_ms` |
| `view` | View 绘制 | 绘制 / draw / measure / layout / view | `view_slices.slowest_slices` (measure/layout/draw) |
| `compose` | Compose 重组 | compose / 重组 / recompose | `compose_slices.composables` (recompose_count, total_ms) |
| `inflate` | 布局加载 | 布局加载 / inflate / 布局 | `view_slices` 中 `SI$inflate#` 前缀 slices |
| `startup` | 冷启动 | 启动 / 冷启动 / startup / cold start | startup phases + bottlenecks |

#### 四、IO 类

| ID | 指标名 | 中/英触发词 | perf_summary 数据段 |
|---|---|---|---|
| `io` | IO 总览 | io / 磁盘 | `io_slices.summary` |
| `network` | 网络请求 | 网络 / network / 请求 / okhttp | `io_slices` 中 `io_type: "network"` |
| `db` | 数据库查询 | 数据库 / db / query / sql | `io_slices` 中 `io_type: "database"` |
| `image` | 图片加载 | 图片 / image / glide / coil | `io_slices` 中 `io_type: "image"` |

#### 五、线程 & 系统类

| ID | 指标名 | 中/英触发词 | perf_summary 数据段 |
|---|---|---|---|
| `thread_state` | 线程状态分布 | 线程状态 / running / sleeping | `thread_state[].state_distribution` |
| `sys` | 系统状态 | 系统状态 / cpu频率 / cpu freq | `sys_stats.cpu_freq_by_core, cpu_idle_samples` |
| `input` | 输入事件 | 触摸 / touch / input | `input_events` |

#### 六、总览

| ID | 指标名 | 中/英触发词 | perf_summary 数据段 |
|---|---|---|---|
| `overview` | 性能总览 | 性能怎么样 / overall / summary | 聚合所有关键指标 |

## 架构设计

### 流程

```
用户输入 "cpu占用率怎么样？"
  → orchestrator 节点（LLM 意图分类）
  → RouteDecision.metric_qa + metric_id="cpu"
  → metric_qa 节点
     1. 检查 state["perf_summary"] 是否存在
     2. 根据 metric_id 提取对应数据段
     3. 调用 LLM 做专门解读（专用 prompt）
     4. 返回 messages + 解读结果
```

### RouteDecision 扩展

在 `RouteDecision` 枚举中新增 `metric_qa` 值。orchestrator 路由到 `metric_qa` 节点。

### AgentState 扩展

无需新增 state 字段。metric_qa 节点通过 orchestrator 传递的 `_route` 值识别指标类型。

具体来说，`_route` 的格式扩展为：
- 原有：`"metric_qa"`
- 新增约定：`"metric_qa:cpu"` — 冒号后跟 metric_id

### 新增文件

| 文件 | 说明 |
|---|---|
| `src/smartinspector/graph/nodes/metric_qa.py` | metric_qa 节点实现 |
| `prompts/metric-qa.txt` | 指标解读 LLM prompt |

### 修改文件

| 文件 | 修改内容 |
|---|---|
| `src/smartinspector/graph/state.py` | RouteDecision 枚举新增 `metric_qa` |
| `src/smartinspector/graph/nodes/orchestrator.py` | 意图分类 prompt 新增 metric_qa 路由 + metric_id 提取 |
| `src/smartinspector/graph/__init__.py`（或 graph 构建文件） | 注册 metric_qa 节点 + 条件边 |

## metric_qa 节点设计

```python
@node_error_handler("metric_qa")
def metric_qa(state: AgentState) -> dict:
    # 1. 解析 _route 获取 metric_id
    route = state.get("_route", "")
    metric_id = route.split(":")[1] if ":" in route else "overview"

    # 2. 检查 perf_summary 是否存在
    perf_summary = state.get("perf_summary", "")
    if not perf_summary:
        return {
            "messages": [AIMessage(content="请先运行 /full 或 /trace 采集数据后再查询指标。")],
            **_pass_through(state),
        }

    # 3. 提取对应指标数据
    data = extract_metric_data(perf_summary, metric_id)

    # 4. 调用 LLM 解读
    metric_name = METRIC_NAMES.get(metric_id, "性能总览")
    prompt = load_prompt("metric-qa").format(metric_name=metric_name, data=data)
    result = llm.invoke(prompt)

    # 5. 返回
    return {
        "messages": [result],
        **_pass_through(state),
    }
```

### extract_metric_data() 函数

将 metric_id 映射到 perf_summary 的 JSON 路径，提取对应数据段。对于组合指标（如 `cpu` = `cpu_usage` + `cpu_hotspots`），合并多段数据。

映射关系（metric_id → perf_summary keys）：

```python
METRIC_DATA_MAP: dict[str, list[str]] = {
    "cpu":           ["cpu_usage"],
    "cpu_hotspot":   ["cpu_hotspots"],
    "sched":         ["scheduling"],
    "blocked":       ["block_events"],
    "memory":        ["process_memory"],
    "heap":          ["memory"],
    "frame":         ["frame_timeline"],
    "rv":            ["view_slices"],        # 过滤 rv_instances
    "view":          ["view_slices"],         # 过滤 slowest_slices
    "compose":       ["compose_slices"],
    "inflate":       ["view_slices"],         # 过滤 SI$inflate# 前缀
    "startup":       [],                      # 来自 startup analyzer（单独数据段）
    "io":            ["io_slices"],
    "network":       ["io_slices"],           # 过滤 io_type=network
    "db":            ["io_slices"],           # 过滤 io_type=database
    "image":         ["io_slices"],           # 过滤 io_type=image
    "thread_state":  ["thread_state"],
    "sys":           ["sys_stats"],
    "input":         ["input_events"],
    "overview":      [],                      # 聚合所有
}
```

对于需要过滤的指标（`rv`、`view`、`inflate`、`network`、`db`、`image`），在提取后做二次过滤，只保留相关子集传给 LLM。

## Orchestrator 意图分类扩展

在现有路由 prompt 中新增 `metric_qa` 分类。示例 prompt 片段：

```
metric_qa — 用户追问某个具体性能指标。关键词：
  cpu占用率、cpu usage、cpu热点、火焰图 → metric_qa:cpu_hotspot
  内存、memory、RSS → metric_qa:memory
  内存泄漏、heap、leak → metric_qa:heap
  帧率、fps、卡顿、jank、掉帧 → metric_qa:frame
  滚动、recycler、列表 → metric_qa:rv
  绘制、draw、measure、layout → metric_qa:view
  compose、重组、recompose → metric_qa:compose
  布局加载、inflate → metric_qa:inflate
  网络、network、请求 → metric_qa:network
  数据库、db、query → metric_qa:db
  图片加载、glide、coil → metric_qa:image
  启动、冷启动、startup → metric_qa:startup
  阻塞、卡住、block、ANR → metric_qa:blocked
  线程状态、sleeping → metric_qa:thread_state
  性能怎么样、overall → metric_qa:overview
  调度、上下文切换 → metric_qa:sched
  io、磁盘 → metric_qa:io
  系统状态、cpu频率 → metric_qa:sys
  触摸、touch → metric_qa:input
```

LLM 返回格式保持现有约定（单标签），只新增 `metric_qa:<id>` 格式。

## Prompt 设计

`prompts/metric-qa.txt` 结构：

```
你是 Android 性能分析专家。用户正在查看一份 Perfetto trace 分析报告，针对「{metric_name}」指标追问。

以下是该指标的原始数据：
{data}

请用中文回答用户的问题，要求：
1. 先给出当前指标的数值概要（如"CPU 占用率 45%"）
2. 如果数据中有异常值，指出并解释
3. 给出 1-2 条具体优化建议
4. 如果用户问了具体问题，直接回答
5. 控制在 200 字以内
```

## 错误处理

| 场景 | 处理 |
|---|---|
| `perf_summary` 为空 | 返回提示："请先运行 /full 或 /trace 采集数据" |
| metric_id 对应的数据段为空 | 返回："该 trace 中没有采集到 {metric_name} 相关数据" |
| 无法识别具体指标 | fallback 到 `overview`（性能总览） |
| startup 数据不在 perf_summary 中 | 从 state 中查找 startup 分析结果 |

## 不做的事

- 不做开放问答——只支持 20 个预定义指标
- 不做自动采集——必须有已分析数据
- 不改 headless/CI 模式——仅交互式 CLI
- 不新增 AgentState 字段——复用 `_route` 传递 metric_id
