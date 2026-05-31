# SmartInspector Skill 渐进式加载方案调研

## 1. 当前加载机制分析

### 1.1 调用链

```
load_skills_for_dimensions(perf_json)     # prompts.py:50
  ├── 解析 perf_json JSON
  ├── 遍历 DimensionRegistry.all()        # 7 个维度
  │   └── 检查 dimensions[dim.name] 是否有数据（非空、非 error）
  │       └── 有数据 → 加入 skill_names 列表
  ├── 额外检查 frame_timeline (jank) → ui-jank
  ├── 额外检查 startup_metrics → startup
  ├── 去重后遍历 unique_skills
  │   └── load_skill(name) → 读完整文件（带缓存）
  └── 拼接所有 skill 全文 → 返回
```

### 1.2 调用点（4 个 Agent）

| Agent | 文件 | 行号 | 用途 |
|-------|------|------|------|
| perf_analyzer | `agents/perf_analyzer.py:101` | 分析 trace 数据时注入 |
| frame_analyzer | `agents/frame_analyzer.py:118` | 帧级分析时注入 |
| attributor | `agents/attributor.py:135` | 源码归因时注入 |
| reporter | `graph/nodes/reporter/__init__.py:32` | 生成报告时注入 |

### 1.3 当前问题

**全量加载**: 只要维度有数据就加载完整 skill 文件，不区分数据是否异常。

| 维度 Skill 文件 | 大小 (bytes) | 内容结构 |
|----------------|-------------|---------|
| lock-contention.md | 19,176 | 数据源→领域知识→数据解读→SQL→严重度→关联→优化→深入 |
| cpu-scheduling.md | 17,197 | 数据源→领域知识→数据解读→SQL→严重度→关联→优化→PELT→容量模型→渲染管线 |
| gc-analysis.md | 17,676 | 数据源→领域知识→数据解读→SQL→严重度→关联→优化→ART算法→分配热定位 |
| io-analysis.md | 20,363 | 数据源→领域知识→数据解读→SQL→严重度→关联→优化→页缓存→f2fs→mmap |
| memory-analysis.md | 18,807 | 数据源→领域知识→数据解读→SQL→严重度→关联→优化→zRAM→LMKD→Heap |
| binder-ipc.md | 12,872 | 数据源→领域知识→数据解读→SQL→严重度→关联→优化→缓冲区→线程饥饿 |
| cpu-throttling.md | 13,357 | 数据源→领域知识→数据解读→SQL→严重度→关联→优化→Trip Point→Governor |
| ui-jank.md | 15,308 | 数据源→领域知识→数据解读→SQL→严重度→关联→优化→SF合成→BufferQueue |
| startup.md | 16,312 | 数据源→领域知识→数据解读→SQL→严重度→关联→优化→Baseline→dex2oat |
| **总计** | **~151 KB** | — |

假设 7 个维度全部有数据 + ui-jank + startup = 9 个 skill 全部注入，约 **151 KB** 文本注入到 LLM prompt 中。按中文约 1.5 token/byte 估算，约 **~75K tokens**，远超大部分模型的 context window 有效利用范围。

## 2. 各维度 compute_hint() 异常判定标准

每个维度的 `compute_hint()` 方法已内置了异常判定逻辑，当数据正常时返回空字符串 `""`：

| 维度 | 异常判定条件 | 正常条件（不触发 hint） |
|------|------------|---------------------|
| **lock_contention** | main 线程 max_wait > 5ms，或其他线程 max_wait > 10ms | 所有线程锁等待在阈值内 |
| **sched_latency** | avg_runnable_ms > frame_budget × 0.5 | 调度延迟低于帧预算一半 |
| **gc_events** | max_pause > 10ms | 所有 GC 暂停 < 10ms |
| **file_io** | main 线程有 IO 事件 total_ms > 5ms | 主线程无显著 IO 阻塞 |
| **memory_trend** | delta_pct > 20%（RSS 增长超 20%） | RSS 增长 < 20% |
| **binder_ipc** | main 线程 max_wait > 10ms | 主线程 Binder 等待 < 10ms |
| **cpu_throttling** | 存在 avg/max < 50% 的核心（throttle_pct > 50%） | 无核心降频 |

**关键洞察**: `compute_hint()` 已经具备"是否异常"的判定能力。当返回空字符串时，说明该维度数据正常，不需要深度分析知识。

## 3. 渐进式加载方案设计

### 3.1 Skill 文件分隔符格式

在每个 skill 文件的合适位置插入分隔符，将内容分为两部分：

```markdown
# GC 事件分析

## 数据源
（简要数据源描述...约 200-500 bytes）

## 领域知识
（简要领域知识概述...约 300-800 bytes）

## 数据解读
（Metric 字段说明 + 决策树...约 500-1000 bytes）

<!-- SKILL_DEEP_DIVE -->
## 严重度标准
（完整严重度标准...）

## Perfetto SQL 深入查询
（完整 SQL 查询模式...）

## 常见误判
（完整误判表...）

## 与其他维度的关联
（完整关联分析...）

## 优化方向
（完整优化方案...）

## [深度专题章节]
（ART 算法演进、锁升级策略等深度内容...）
```

**分隔符**: `<!-- SKILL_DEEP_DIVE -->`

- **分隔符之前**（触发条件层）: 包含数据源、领域知识概述、Metric 字段、决策树。约 1-2 KB。足够 LLM 判断该维度是否有问题、了解基本分析框架。
- **分隔符之后**（详情层）: 包含完整 SQL 查询、误判表、关联分析、优化方案、深度专题。约 10-18 KB。仅在确认需要深度分析时注入。

### 3.2 加载流程

```
Agent 请求 skill 知识
  │
  ├── load_skill_trigger(name)              # 新函数：只读触发条件层
  │   └── 读文件 → 按 <!-- SKILL_DEEP_DIVE --> 分割 → 返回前半部分
  │
  ├── 判定是否需要深度知识
  │   ├── compute_hint() 返回空 → 数据正常 → 仅保留触发条件层
  │   └── compute_hint() 非空 → 数据异常 → 调用 load_skill_detail(name)
  │
  └── load_skill_detail(name)               # 新函数：只读详情层
      └── 读文件 → 按 <!-- SKILL_DEEP_DIVE --> 分割 → 返回后半部分
```

### 3.3 框架级自动判定（推荐方案）

不需要让 LLM 自行判断，而是复用已有的 `compute_hint()` 结果：

```python
def load_skills_for_dimensions(
    perf_json: str,
    hints: dict[str, str] | None = None,
) -> str:
    """渐进式加载 dimension skill 知识。

    Args:
        perf_json: PerfSummary JSON string.
        hints: 预计算的 hint 字典 {dim_name: hint_text}。
               有 hint 的维度加载完整 skill，无 hint 的只加载触发条件层。
    """
    ...
    for dim in DimensionRegistry.all():
        dim_data = dimensions_data.get(dim.name)
        if not dim_data or is_empty(dim_data):
            continue

        # 判定是否需要深度知识
        hint = (hints or {}).get(dim.name, "")
        if hint:
            # 有 hint → 数据异常 → 加载完整 skill
            content = load_skill(dim.skill_name)  # 全文
        else:
            # 无 hint → 数据正常 → 只加载触发条件层
            content = load_skill_trigger(dim.skill_name)

        if content:
            parts.append(f"\n\n# Knowledge: {dim.skill_name}\n\n{content}")
```

### 3.4 新增 prompts.py API

```python
# 缓存触发条件层和详情层
_trigger_cache: dict[str, str] = {}
_detail_cache: dict[str, str] = {}

_SPLIT_MARKER = "<!-- SKILL_DEEP_DIVE -->"

def _split_skill(content: str) -> tuple[str, str]:
    """将 skill 内容分割为触发条件层和详情层。"""
    if _SPLIT_MARKER in content:
        trigger, detail = content.split(_SPLIT_MARKER, 1)
        return trigger.strip(), detail.strip()
    # 无分隔符的旧格式：全文作为详情层，取前 500 字符作为触发条件层
    return content[:500], content

def load_skill_trigger(name: str, category: str = "dimensions") -> str:
    """加载 skill 触发条件层（分隔符之前的内容）。"""
    cache_key = f"trigger:{category}/{name}"
    if cache_key in _trigger_cache:
        return _trigger_cache[cache_key]
    full = load_skill(name, category)
    trigger, _ = _split_skill(full)
    _trigger_cache[cache_key] = trigger
    return trigger

def load_skill_detail(name: str, category: str = "dimensions") -> str:
    """加载 skill 详情层（分隔符之后的内容）。"""
    cache_key = f"detail:{category}/{name}"
    if cache_key in _detail_cache:
        return _detail_cache[cache_key]
    full = load_skill(name, category)
    _, detail = _split_skill(full)
    _detail_cache[cache_key] = detail
    return detail
```

## 4. 各 Agent 差异化需求分析

不同 Agent 对 skill 知识的深度需求不同：

| Agent | 需要的 skill 深度 | 理由 |
|-------|-----------------|------|
| **perf_analyzer** | 触发条件层 + **异常维度详情层** | 分析性能数据，需要完整的分析框架来识别和解释异常 |
| **attributor** | **仅触发条件层**（甚至可以更精简） | 归因只关心"哪个 SI$ slice 属于哪个维度"，不需要深度分析知识 |
| **reporter** | 触发条件层 + **异常维度详情层** | 生成报告需要完整的优化建议和误判识别 |
| **frame_analyzer** | 触发条件层 + **相关维度详情层** | 帧分析只关心与该帧相关的维度深度知识 |

### 4.1 Agent 级别策略建议

```python
# 抽象为策略枚举
class SkillLoadStrategy:
    TRIGGER_ONLY = "trigger_only"     # 只加载触发条件层
    ANOMALY_FULL = "anomaly_full"     # 异常维度加载全文，正常维度只加载触发条件层
    ALL_FULL = "all_full"             # 全量加载（当前行为，向后兼容）

# 各 Agent 使用的策略
AGENT_STRATEGIES = {
    "perf_analyzer": SkillLoadStrategy.ANOMALY_FULL,
    "attributor": SkillLoadStrategy.TRIGGER_ONLY,
    "reporter": SkillLoadStrategy.ANOMALY_FULL,
    "frame_analyzer": SkillLoadStrategy.ANOMALY_FULL,
}
```

## 5. Token 节省估算

### 场景：7 个维度有数据，其中 3 个异常

| 方案 | 加载内容 | 估算 Token |
|------|---------|-----------|
| 当前（全量） | 9 个 skill 完整文件 | ~75K tokens |
| 渐进式（anomaly_full） | 3 个异常完整 + 6 个触发条件层 | ~30K tokens（节省 60%） |
| 渐进式（trigger_only） | 9 个触发条件层 | ~9K tokens（节省 88%） |

### 场景：7 个维度有数据，全部正常

| 方案 | 加载内容 | 估算 Token |
|------|---------|-----------|
| 当前（全量） | 9 个 skill 完整文件 | ~75K tokens |
| 渐进式（anomaly_full） | 9 个触发条件层 | ~9K tokens（节省 88%） |

## 6. 扩展性：新增维度自动适配

### 6.1 框架级保证

新增维度时只需遵循以下约定：

1. 创建 `src/smartinspector/collector/dimensions/<name>.py`
2. 实现 `compute_hint()` 方法（已有要求）
3. 创建 `prompts/skills/dimensions/<skill_name>.md`，在合适位置放置 `<!-- SKILL_DEEP_DIVE -->`
4. 框架自动通过 `DimensionRegistry.discover()` 发现新维度
5. `load_skills_for_dimensions()` 自动根据 hint 判定加载策略

### 6.2 向后兼容

- 无分隔符的旧 skill 文件：`_split_skill()` 会将全文作为 detail 层，取前 500 字符作为 trigger 层
- 逐步迁移：可以先改造异常频率高的维度（gc、lock、io），其余维度后续跟进

## 7. 实施步骤

### Phase 1: 框架改造

1. `prompts.py` 新增 `load_skill_trigger()` / `load_skill_detail()` / `_split_skill()`
2. `prompts.py` 修改 `load_skills_for_dimensions()` 增加 `hints` 参数和加载策略
3. 确定向后兼容：无 hints 参数时保持当前全量加载行为

### Phase 2: Skill 文件改造

按优先级逐个添加分隔符：

| 优先级 | 维度 | 理由 |
|-------|------|------|
| P0 | gc-analysis.md | 最常见异常维度，内容最长 (17KB) |
| P0 | lock-contention.md | 内容最长 (19KB) |
| P0 | io-analysis.md | 内容最长 (20KB) |
| P1 | cpu-scheduling.md | 内容长 (17KB) |
| P1 | memory-analysis.md | 内容长 (19KB) |
| P2 | binder-ipc.md | 内容较短 (13KB) |
| P2 | cpu-throttling.md | 内容较短 (13KB) |
| P2 | ui-jank.md | 非 dimension 维度 |
| P2 | startup.md | 非 dimension 维度 |

### Phase 3: Agent 策略配置

为每个 Agent 配置合适的加载策略，attributor 使用 TRIGGER_ONLY 可以立即节省大量 token。

## 8. 分隔符位置建议

每个 skill 文件应在 **"数据解读"（含决策树）之后、"严重度标准"之前** 插入分隔符。

理由：
- 触发条件层需要：数据源（知道数据从哪来）、领域知识概述（理解基本概念）、Metric 字段+决策树（知道怎么判断异常）
- 详情层包含：SQL 深入查询（分析时用）、误判识别（避免误报）、关联分析（跨维度关联）、优化方案（给用户的建议）

示例（gc-analysis.md）：

```
# GC 事件分析
## 数据源              ← 触发条件层开始
## 领域知识
## 数据解读
<!-- SKILL_DEEP_DIVE -->
## 严重度标准          ← 详情层开始
## 常见误判
## 与其他维度的关联
## 优化方向
## ART GC 算法演进
## GC 暂停预算计算
## GC 触发链分析
## 对象分配热定位
## Perfetto SQL 查询模式
## 误报识别
```

## 9. 风险与注意事项

1. **trigger 层内容充分性**: 触发条件层必须包含足够的信息让 LLM 正确理解维度含义。如果截取过少，LLM 可能无法正确分析。
2. **分隔符维护成本**: 新增内容时需要注意添加在分隔符的正确一侧。
3. **缓存一致性**: 改造后需要清理 `_skill_cache` / `_trigger_cache` / `_detail_cache` 避免读到旧内容（当前设计是进程级缓存，重启自动生效）。
4. **hints 传递**: `compute_hint()` 在 `deterministic.py` 中调用，需要在 reporter node 等调用点将 hints 传入 `load_skills_for_dimensions()`。需要确认 hints 的数据流路径。
