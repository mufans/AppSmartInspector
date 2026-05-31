# 从一把梭 SQL 到维度注册：性能分析采集的工程化之路

> 开源项目 SmartInspector：[GitHub - mufans/AppSmartInspector](https://github.com/mufans/AppSmartInspector)
>
> AI 驱动的 Android 性能分析 CLI 工具，采集 Perfetto trace，用 LLM agent 生成带源码归因的性能报告。

## 硬编码 SQL 的困境

之前我在做 SmartInspector 的 Perfetto trace 分析时，每次想加一个新的分析维度，比如 GC 事件检测，流程是这样的：

1. 在 `perfetto.py` 的 `PerfettoCollector` 里加一个 `collect_gc_events()` 方法，写 SQL
2. 在 `PerfSummary` 数据类里加字段
3. 在 `deterministic.py` 里加一个 `_analyze_gc_events()` hint 函数
4. 在 `formatter.py` 里加一个 format section
5. 在 `metric_qa.py` 里注册触发词
6. 写对应的 prompt 知识

一个问题：**加一个维度要改 6 个文件**。每个文件里的代码跟这个维度紧密耦合，改漏了就是 bug。

更要命的是，这些代码分散在完全不同的模块里——collector 负责采集，deterministic 负责预计算，formatter 负责展示，metric_qa 负责查询。你要加一个维度，得在四个完全不同的系统里各写一段。这不是工程，这是搬砖。

我当时就想，能不能**一个维度 = 一个文件**？把采集 SQL、预计算逻辑、格式化输出全部封装在一起，然后通过一个注册表自动发现和调度？

这就是维度注册体系（Dimension Registry）的由来。

## AnalysisDimension 基类：一个维度该长什么样

先看基类设计：

```python
# src/smartinspector/collector/dimensions/base.py

class AnalysisDimension(ABC):
    """分析维度基类。每个维度自包含 collect + hint + format + metric 逻辑。"""

    @property
    @abstractmethod
    def name(self) -> str:
        """维度唯一标识，如 'sched_latency'。"""

    @property
    def description(self) -> str:
        """维度中文描述。"""
        return self.name

    @property
    def perf_summary_key(self) -> str:
        """在 PerfSummary JSON 中的 key。"""
        return self.name

    @property
    def metric_triggers(self) -> list[str]:
        """Metric QA 自然语言触发词列表。"""
        return []

    @property
    def skill_name(self) -> str:
        """对应的 dimension skill 文件名。"""
        return self.name

    @abstractmethod
    def collect(self, tp) -> dict:
        """执行 SQL 查询，返回结构化数据。"""

    def compute_hint(self, data: dict, context: HintContext) -> str:
        """计算 deterministic hint（纯 Python，不调 LLM）。"""
        return ""

    def format_section(self, data: dict) -> str:
        """格式化为 Markdown section。"""
        return ""

    def metric_filter(self, data: dict) -> dict:
        """Metric QA 数据过滤。默认直接透传。"""
        return data
```

设计理念很简单：**一个维度的所有逻辑封装在一个类里**。核心有三个方法：

- `collect(tp)` — 接收 TraceProcessor 实例，跑 SQL，返回结构化 dict
- `compute_hint(data, context)` — 纯 Python 预计算，不花 LLM token
- `format_section(data)` — 把数据格式化成 Markdown 表格

加上 `HintContext` 提供全局上下文（帧预算、目标进程、trace 时长），让 hint 的阈值判断能适配不同设备刷新率（60Hz vs 120Hz）。

## DimensionRegistry：自动发现 + 注册

注册表的设计更简单——类装饰器 + 包扫描：

```python
# src/smartinspector/collector/dimensions/__init__.py

class DimensionRegistry:
    _dimensions: dict[str, AnalysisDimension] = {}

    @classmethod
    def register(cls, dim: AnalysisDimension) -> None:
        cls._dimensions[dim.name] = dim

    @classmethod
    def all(cls) -> list[AnalysisDimension]:
        return list(cls._dimensions.values())

    @classmethod
    def discover(cls) -> None:
        """自动发现 dimensions/ 包下的所有维度模块。"""
        from smartinspector.collector import dimensions as pkg
        for _, module_name, _ in pkgutil.iter_modules(pkg.__path__):
            importlib.import_module(
                f"smartinspector.collector.dimensions.{module_name}"
            )

def register_dimension(cls_or_dim):
    """类装饰器：@register_dimension → 自动实例化并注册。"""
    if isinstance(cls_or_dim, type):
        dim = cls_or_dim()
        DimensionRegistry.register(dim)
        return cls_or_dim
    else:
        DimensionRegistry.register(cls_or_dim)
        return cls_or_dim
```

关键点在于 `discover()` 方法——它用 `pkgutil.iter_modules` 扫描 `dimensions/` 目录下的所有 `.py` 文件，自动 import。而每个模块里的类都用了 `@register_dimension` 装饰器，import 时就自动注册了。

这意味着：**新增维度只需要在 `dimensions/` 目录下新建一个文件，写好类，加上装饰器，完事**。不需要改任何其他文件。

## 七个维度的实现

目前注册了 7 个维度，覆盖了 Android 性能分析中几个高频但又容易遗漏的领域。

### LockContentionDimension：futex 锁竞争

这个维度分析 `__intrinsic_thread_state` 表中的 futex 等待事件。futex（Fast Userspace Mutex）是 Linux 用户空间互斥锁，主线程锁竞争直接导致 ANR 和卡顿。

```python
@register_dimension
class LockContentionDimension(AnalysisDimension):
    @property
    def name(self) -> str:
        return "lock_contention"

    @property
    def metric_triggers(self) -> list[str]:
        return ["锁竞争", "lock", "futex", "锁"]

    def collect(self, tp) -> dict:
        rows = tp.query("""
            SELECT
              t.name AS thread_name,
              COUNT(*) AS futex_wait_count,
              SUM(its.dur) / 1e6 AS total_wait_ms,
              MAX(its.dur) / 1e6 AS max_wait_ms,
              AVG(its.dur) / 1e6 AS avg_wait_ms
            FROM __intrinsic_thread_state its
            JOIN thread t ON its.utid = t.utid
            WHERE its.blocked_function GLOB '*futex*'
              AND its.dur > 100000
            GROUP BY t.name
            ORDER BY total_wait_ms DESC
            LIMIT 15
        """)
        # ... 结构化处理 ...
```

SQL 查询设计的核心思路：用 `blocked_function GLOB '*futex*'` 过滤 futex 等待，`dur > 100000`（100 微秒）排除噪声，按线程分组统计总等待、最大等待、平均等待。

它还额外跑了一个 hotspot 查询，按 `blocked_function + thread_name` 分组，找到哪个锁函数最耗时。

### GcEventsDimension：GC 暂停检测

GC 事件对帧率的影响是 Android 性能分析的经典课题。这个维度从 `slice` 表查 GC 相关事件：

```python
def collect(self, tp) -> dict:
    rows = tp.query("""
        SELECT name, ts,
               IIF(dur = -1, 0, dur) AS dur,
               EXTRACT_ARG(arg_set_id, 'reason') AS gc_reason,
               EXTRACT_ARG(arg_set_id, 'gc_type') AS gc_type
        FROM slice
        WHERE name GLOB '*GC*'
           OR name GLOB '*GarbageCollector*'
           OR name GLOB '*ConcurrentGC*'
        ORDER BY dur DESC
        LIMIT 20
    """)
```

有意思的是 `compute_hint()` 里的主线程影响判断——只统计 `"Wait For Concurrent"` 和 `"GC: Alloc"` 两种类型对主线程的暂停时间，因为这两种才是真正阻塞主线程的 GC 事件。

还有一个细节：hint 会跟帧预算对比。如果 GC 暂停超过一帧的时间（16.67ms@60Hz），就直接标记"可能导致 jank"。

### SchedLatencyDimension：调度延迟

这个维度用了 Perfetto 的标准 SQL 库模块 `sched.runnable`：

```python
def collect(self, tp) -> dict:
    rows = tp.query("""
        INCLUDE PERFETTO MODULE sched.runnable;

        SELECT thread_name,
               COUNT(*) AS runnable_count,
               AVG(runnable_dur) / 1e6 AS avg_runnable_ms,
               MAX(runnable_dur) / 1e6 AS max_runnable_ms
        FROM sched_runnable
        GROUP BY thread_name
        HAVING runnable_count > 5
        ORDER BY avg_runnable_ms DESC
        LIMIT 15
    """)
```

`INCLUDE PERFETTO MODULE sched.runnable` 加载 Perfetto 内置模块，直接用 `sched_runnable` 视图计算从 runnable 到 running 的延迟。比自己拼 sched 表做 JOIN 靠谱多了。

### MemoryTrendDimension：内存泄漏检测

这个维度不是查一次快照，而是查 RSS 时序数据，算增长趋势：

```python
# 计算 slope（增长斜率）和跳跃事件
slope = delta_mb / duration_s

jumps = []
for i in range(1, len(samples)):
    jump = curr_mb - prev_mb
    if jump > 10:  # 单次跳跃 > 10MB
        jumps.append(...)
```

增长超过 20% 就标记为"建议关注"，超过 50% 直接标记"疑似泄漏"。还会检测单次跳跃超过 10MB 的异常分配事件。

### 其他三个维度

- **FileIODimension**：查 `__intrinsic_thread_state` 的 `io_wait = 1` 字段，找主线程文件 IO 阻塞。只标记主线程 `total_ms > 5ms` 的事件。
- **BinderIPCDimension**：查 `blocked_function = 'binder_thread_read'`，分析跨进程调用延迟。只标记主线程 `max > 10ms` 的 Binder 等待。
- **CpuThrottlingDimension**：查 `cpu_counter_track` 的频率数据，检测平均频率降到最高频率 50% 以下的核心。这是 thermal throttling 的典型信号。

七个维度的共同设计模式：**SQL 查数据 → 纯 Python 算阈值 → 输出中文结论**。LLM 只负责最后的语言组织和因果分析，不需要做算术。

## Prompt Skill 系统：给 Agent 装上领域知识

维度注册解决了数据采集的问题。但数据采集完，LLM Agent 分析的时候怎么知道怎么解读这些数据？

这就是 Prompt Skill 系统的作用。每个分析维度对应一个 skill 文件，定义了该维度的领域知识、严重度标准、优化方向。

### 维度 Skill 文件

比如 `lock-contention.md`：

```markdown
# 锁竞争分析

## 数据源
- SQL 表: `__intrinsic_thread_state` (blocked_function GLOB '*futex*')

## 领域知识
- futex (Fast Userspace Mutex): Linux 用户空间互斥锁
- 主线程锁竞争直接导致 ANR 和 jank

## 严重度标准
- P0: 主线程 futex 等待 max > 帧预算
- P1: 主线程 futex 等待 max > 5ms
- P2: 其他线程 futex 等待 max > 10ms

## 优化方向
- 减小锁粒度: 使用细粒度锁替代全局锁
- 避免主线程持锁: 将耗时操作移到子线程
```

每个 skill 文件回答四个问题：数据从哪来？领域知识是什么？什么算严重？怎么优化？

### 共享 Skill

除了维度专属的知识，还有共享 skill：

- `si-tag-system.md` — SI$ 标签格式定义和解析规则
- `search-strategy.md` — Java/Kotlin/XML 源码搜索策略

这些是多个 Agent 都需要的公共知识，通过 `shared:` 前缀按需加载。

### Skill 加载机制

```python
# src/smartinspector/prompts.py

def load_prompt_with_skills(name: str, *skill_names: str) -> str:
    """加载 prompt 指令 + 按需追加 skill 知识。"""
    parts = [load_prompt(name)]  # 基础指令

    for skill_name in skill_names:
        if skill_name.startswith("shared:"):
            ref = load_skill(skill_name[7:], category="shared")
        else:
            ref = load_skill(skill_name, category="dimensions")
        if ref:
            parts.append(f"\n\n# Knowledge: {skill_name}\n\n{ref}")

    return "\n".join(parts)
```

各 Agent 按需加载自己需要的 skill：

```python
# attributor 加载 SI$ 标签 + 搜索策略
_system_prompt = load_prompt_with_skills("attributor", "shared:si-tag-system", "shared:search-strategy")

# frame-analyzer 加载 UI jank 知识 + SI$ 标签
_prompt = load_prompt_with_skills("frame-analyzer", "ui-jank", "shared:si-tag-system")

# perf-analyzer 只加载基础指令
_prompt = load_prompt_with_skills("perf-analyzer")
```

好处很明显：**指令和知识分离**。指令（输出格式、约束、工作流）稳定不变，知识（领域知识、数据解读）按需加载。SI$ 标签格式定义只需要维护一份，不用在 4 个 prompt 文件里各写一遍。

## 全 Pipeline 集成

改造后的完整数据流是这样的：

```
                    ┌─────────────────────────────┐
                    │      DimensionRegistry       │
                    │  discover() 自动发现 7 个维度  │
                    └──────────┬──────────────────┘
                               │
  ┌────────────────────────────┼────────────────────────────┐
  │                            │                            │
  ▼                            ▼                            ▼
Collector                  Deterministic                 Formatter
(采集层)                   (预计算层)                    (展示层)
│                          │                            │
│ dim.collect(tp)          │ dim.compute_hint(          │ dim.format_section(
│  → SQL 查询               │   data, context)           │   data)
│  → 结构化 dict            │  → 纯 Python 阈值判断       │  → Markdown 表格
│  → 写入 perf_summary      │  → 中文结论文本             │  → 拼入 LLM prompt
│                          │                            │
└──────────────────────────┴────────────────────────────┘
                               │
                    ┌──────────┴──────────┐
                    │                     │
                    ▼                     ▼
               Metric QA              LLM Agent
               (查询层)              (分析层)
               │                     │
               │ dim.metric_filter()  │ load_prompt_with_skills()
               │ 自然语言→维度匹配    │ 按需加载维度知识
               └─────────────────────┘
```

在 collector 层，集成代码只有 10 行：

```python
# perfetto.py 的 summarize() 方法里
from smartinspector.collector.dimensions import DimensionRegistry
DimensionRegistry.discover()
for dim in DimensionRegistry.all():
    try:
        dim_data = dim.collect(tp)
        summary.dimensions[dim.perf_summary_key] = dim_data
    except Exception as e:
        summary.dimensions[dim.perf_summary_key] = {"error": str(e)}
```

在 deterministic 层：

```python
# deterministic.py 的 compute_hints() 方法里
dimensions_data = data.get("dimensions", {})
if dimensions_data:
    DimensionRegistry.discover()
    ctx = HintContext(frame_budget_ms=frame_budget_ms, ...)
    for dim in DimensionRegistry.all():
        dim_data = dimensions_data.get(dim.perf_summary_key)
        if dim_data and not dim_data.get("error"):
            hint = dim.compute_hint(dim_data, ctx)
            if hint:
                sections.append(hint)
```

在 formatter 层：

```python
# formatter.py 的 format_perf_sections() 里
dimensions_data = perf_data.get("dimensions", {})
if dimensions_data:
    DimensionRegistry.discover()
    for dim in DimensionRegistry.all():
        dim_data = dimensions_data.get(dim.perf_summary_key)
        if dim_data and not dim_data.get("error"):
            section = dim.format_section(dim_data)
            if section:
                user_parts.append(section)
```

三个环节的代码结构完全一样：`discover()` → 遍历 `all()` → 调对应方法。任何新维度注册进去，三个环节自动集成，零改动。

## 小结

改造前，加一个分析维度要改 6 个文件、4 个模块，每个模块里的代码风格和调用约定还不一样。

改造后，**一个维度 = 一个 Python 文件 + 一个 Skill 知识文件**。写好 `collect()`、`compute_hint()`、`format_section()` 三个方法，加上 `@register_dimension` 装饰器，整个 Pipeline 自动集成。

从一把梭 SQL 到工程化的维度注册，说到底就是把「关注点分离」这个老生常谈的原则用对了地方。

---

> 项目地址：[GitHub - mufans/AppSmartInspector](https://github.com/mufans/AppSmartInspector)
>
> 如果觉得有用，欢迎 Star。下篇聊一聊怎么用 LLM Agent 做源码级性能归因——从 trace 数据一路追踪到具体的代码行。
