# SI 归因系统改进方案：调用栈精确归因

## 1. 现状分析

### 1.1 当前归因流程

```
Perfetto trace (.pb)
    ↓ collector/perfetto.py :: collect_view_slices()
    ↓ SQL 查询 slice 表，获取 id/name/ts/dur/depth/parent_id
view_slices JSON
    ↓ commands/attribution.py :: extract_attributable_slices()
    ↓ 提取 SI$ 前缀 → 解析 class_name + method_name
attributable 列表 [{class_name, method_name, dur_ms, ...}]
    ↓ agents/attributor.py :: run_attribution() → _search_group()
    ↓ LLM 调用 Glob → Grep → Read 工具
源码归因结果 [{file_path, line_start, line_end, source_snippet}]
```

### 1.2 关键缺陷

**问题一：调用点歧义**

当一个方法（如 `Adapter.onBindViewHolder`）在多个文件中被调用，或同一文件中多处调用时：
- 系统只能定位方法定义位置，无法确定是**哪次调用**导致性能问题
- 归因会列出所有匹配位置，LLM 需要猜测哪个是热点
- 多余的搜索结果浪费 token

**问题二：上下文缺失**

LLM 搜索 `Adapter.onBindViewHolder` 时不知道：
- 这个调用发生在哪个 Activity/Fragment 的上下文中
- 是哪个 RecyclerView 实例触发的（如果有多个 RV）
- 在 doFrame 的哪个子阶段（measure/layout/draw）

**问题三：数据已采集但未利用**

`collector/perfetto.py` 的 `collect_view_slices()` 已经：
- 查询了每个 slice 的 `parent_id`（第 607 行）
- 构建了 `children_map` 父子关系映射（第 759-765 行）
- 构建了 `call_chains` 调用链（第 805-821 行）
- 甚至向上回溯了祖父节点（第 650-664 行）

但 `extract_attributable_slices()` 完全没有使用这些调用栈信息。

### 1.3 当前数据流中的信息断裂点

| 数据阶段 | 调用栈信息 | 是否传递给下游 |
|----------|-----------|---------------|
| `collect_view_slices()` SQL 查询 | `parent_id` 已获取 | ✅ 存入 slice dict |
| `collect_view_slices()` 调用链构建 | `call_chains` 已构建 | ✅ 写入 view_slices JSON |
| `extract_attributable_slices()` | **未读取** `call_chains` | ❌ 断裂点 |
| `_build_group_prompt()` | **未包含** 父链上下文 | ❌ 断裂点 |
| `_compute_call_chain_distribution()` | 读取了 `call_chains` | ✅ 但仅用于预计算提示 |
| `prompts/attributor.txt` | **无** 调用栈搜索指引 | ❌ 断裂点 |

## 2. 改进方案设计

### 2.1 核心思路

**利用 Perfetto slice 的 parent_id 链构建调用栈上下文，将"方法定义级别搜索"升级为"调用点级别搜索"。**

具体策略：
1. 在 `extract_attributable_slices()` 阶段，利用 `view_slices.call_chains` 和 `slowest_slices` 中的 `parent_id`，为每个可归因 slice 提取完整的父链上下文
2. 将父链上下文编码为 `call_context` 字段，传递给 attributor agent
3. Agent 搜索时利用上下文缩小搜索范围（如知道父 Activity 名、RV 实例 ID 等）
4. 对无法通过上下文区分的场景，提供优先级排序而非全部列出

### 2.2 技术方案

#### 改动一：`extract_attributable_slices()` 增加调用栈提取

**文件**: `src/smartinspector/commands/attribution.py`

**当前代码**（第 552-707 行）只处理 `slowest_slices`、`summary`、`rv_instances`、`block_events`，未读取 `call_chains`。

**改进**：在处理 `slowest_slices` 时，利用 `call_chains` 数据构建每个 slice 的调用上下文。

```python
# 新增函数
def _build_parent_contexts(view_slices: dict) -> dict[int, str]:
    """为每个 slowest_slice 构建 parent chain 上下文摘要。

    利用 collect_view_slices() 已构建的 call_chains 数据和 slice 的 parent_id，
    生成精简的调用上下文字符串，用于辅助 attributor agent 精确定位。

    Returns:
        dict: slice_name → context_string 映射
    """
    slices_data = view_slices.get("slowest_slices", [])
    call_chains = view_slices.get("call_chains", [])

    # 从 call_chains 提取 name → chain 映射
    chain_map: dict[str, list[str]] = {}
    for cc in call_chains:
        name = cc.get("name", "")
        chain = cc.get("chain", [])
        if name and chain:
            chain_map[name] = chain

    # 从 slices 数据构建 parent_id → slice_name 映射
    # 需要从原始 slice 数据获取 parent_id（slowest_slices 中有 parent_id）
    slice_by_id: dict[int, dict] = {}
    for s in slices_data:
        sid = s.get("id")
        if sid is not None:
            slice_by_id[sid] = s

    contexts: dict[str, str] = {}
    for s in slices_data:
        name = s.get("name", "")
        if not name.startswith("SI$"):
            continue

        # 策略1: 使用 call_chains 中的预构建链
        if name in chain_map:
            chain = chain_map[name]
            # chain 是 [root, ..., leaf]，取倒数2-4层作为上下文
            context_parts = _extract_context_from_chain(chain)
            if context_parts:
                contexts[name] = " → ".join(context_parts)
                continue

        # 策略2: 从 parent_id 向上回溯（call_chains 未覆盖的 slice）
        parent_chain = _walk_parent_chain(s, slice_by_id, max_depth=5)
        if parent_chain:
            context_parts = _extract_context_from_chain(parent_chain)
            if context_parts:
                contexts[name] = " → ".join(context_parts)

    return contexts


def _extract_context_from_chain(chain: list[str]) -> list[str]:
    """从调用链中提取有意义的上下文节点。

    过滤掉系统标签（doFrame, Choreographer 等），保留 SI$ 自定义标签和
    关键系统标签（作为阶段标识）。
    """
    # 阶段标识：doFrame → performMeasure/performLayout/performDraw
    STAGE_KEYWORDS = {
        "doFrame": "帧渲染",
        "performMeasure": "measure阶段",
        "performLayout": "layout阶段",
        "performDraw": "draw阶段",
        "Choreographer": "vsync",
    }

    context_parts = []
    for item in chain:
        # chain item 格式: "slice_name [XX.XXms]" 或 "slice_name"
        name = item.split(" [")[0] if " [" in item else item

        if name.startswith("SI$"):
            # SI$ 标签：提取关键信息
            context_parts.append(_summarize_si_tag(name))
        else:
            # 系统标签：只保留阶段标识
            for keyword, label in STAGE_KEYWORDS.items():
                if keyword in name:
                    context_parts.append(f"[{label}]")
                    break

    return context_parts


def _summarize_si_tag(tag: str) -> str:
    """将 SI$ 标签转换为可读的上下文摘要。"""
    body = tag[3:] if tag.startswith("SI$") else tag

    if body.startswith("RV#"):
        # SI$RV#viewId#Adapter.method → "RV(viewId, Adapter.method)"
        parts = body.split("#")
        if len(parts) >= 3:
            view_id = parts[1]
            fqn_method = parts[2]
            _, method = _split_fqn_method(fqn_method)
            adapter = fqn_method.rsplit(".", 1)[0].rsplit(".", 1)[-1]
            return f"RV#{view_id}#{adapter}.{method or '?'}"
        return body

    if body.startswith("inflate#"):
        parts = body[8:].split("#")
        layout = parts[0] if parts else "?"
        return f"inflate({layout})"

    if body.startswith("view#"):
        fqn, method = _split_fqn_method(body[5:])
        cls = fqn.rsplit(".", 1)[-1] if fqn else "?"
        return f"{cls}.{method or '?'}"

    if body.startswith("handler#"):
        fqn_part = body[8:].split("#")[0]
        fqn, method = _split_fqn_method(fqn_part)
        cls = fqn.rsplit(".", 1)[-1] if fqn else fqn_part
        return f"handler({cls}.{method or '?'})"

    if body.startswith("Activity.lifecycle"):
        return "Activity生命周期"

    if body.startswith("Fragment.lifecycle"):
        return "Fragment生命周期"

    # 默认
    fqn, method = _split_fqn_method(body)
    cls = fqn.rsplit(".", 1)[-1] if fqn else body
    return f"{cls}.{method or '?'}"


def _walk_parent_chain(slice_data: dict, slice_by_id: dict, max_depth: int = 5) -> list[str]:
    """从 slice 数据沿 parent_id 向上回溯，构建调用链。

    Returns:
        调用链 [root, ..., leaf]，每项格式 "name [dur_ms]"
    """
    chain = []
    visited = set()
    current = slice_data

    for _ in range(max_depth):
        sid = current.get("id")
        if sid is None or sid in visited:
            break
        visited.add(sid)

        name = current.get("name", "")
        dur_ms = current.get("dur_ms", 0)
        chain.append(f"{name} [{dur_ms:.2f}ms]")

        parent_id = current.get("parent_id")
        if not parent_id or parent_id not in slice_by_id:
            break
        current = slice_by_id[parent_id]

    chain.reverse()  # root → leaf
    return chain
```

#### 改动二：在 `extract_attributable_slices()` 中注入上下文

**文件**: `src/smartinspector/commands/attribution.py`

在函数末尾（约第 707 行），去重之后、排序之前，注入 `call_context`：

```python
def extract_attributable_slices(perf_summary_json: str, min_dur_ms: float = 1.0) -> list[dict]:
    # ... 现有逻辑不变 ...

    # ── 新增：注入调用栈上下文 ──
    parent_contexts = _build_parent_contexts(view_slices)

    for entry in seen.values():
        raw_name = entry.get("raw_name", "")
        if raw_name in parent_contexts:
            entry["call_context"] = parent_contexts[raw_name]

        # 对 RV 实例方法，补充 RV 上下文
        if entry.get("instance"):
            # instance 格式: RV#viewId#AdapterName
            entry["call_context"] = f"RV实例: {entry['instance']}"

    return sorted(seen.values(), key=lambda x: -x["dur_ms"])
```

#### 改动三：`_build_group_prompt()` 传递上下文给 LLM

**文件**: `src/smartinspector/agents/attributor.py`

修改 `_build_group_prompt()` 函数（第 390-428 行），在 prompt 中加入调用栈上下文：

```python
def _build_group_prompt(group: list[dict]) -> str:
    """Build a search prompt for one group of issues."""
    from smartinspector.config import get_source_dir

    source_dir = get_source_dir()

    lines = [
        f"源码目录: {source_dir}\n",
    ]

    for i, issue in enumerate(group, 1):
        search_type = issue.get("search_type", "java")
        cn = issue["class_name"]
        line = f"{i}. {cn}.{issue['method_name']} ({issue['dur_ms']:.2f}ms, {search_type}"
        if issue.get("count"):
            line += f", count={issue['count']}"

        # ── 新增：调用栈上下文 ──
        call_ctx = issue.get("call_context", "")
        if call_ctx:
            line += f", 调用链: {call_ctx}"

        # BlockMonitor 堆栈（保留原有逻辑）
        if issue.get("stack_trace"):
            line += f", 堆栈:{issue['stack_trace'][0]}"

        # 内部类提示（保留原有逻辑）
        if "$" in cn:
            outer = cn.split("$")[0]
            line += f", 内部类:用Glob搜索外部类 {outer}"
            line += f", RESULT行请用完整类名: {cn}.{issue['method_name']}"

        # XML 布局提示（保留原有逻辑）
        if search_type == "xml":
            line += f", xml布局:Glob **/{cn}.xml → Read完整文件, RESULT行请用: {cn}.{issue['method_name']}"

        line += ")"
        lines.append(line)

    # ... 后续代码不变 ...
```

#### 改动四：更新 attributor prompt，增加调用链搜索指引

**文件**: `prompts/attributor.txt`

在"核心原则"部分之后增加调用链上下文的使用指引：

```
## 调用链上下文（call_context）

某些热点会附带调用链上下文信息，格式如：
  `调用链: [帧渲染] → RV#recycler_orders#OrderAdapter.onCreateViewHolder`

调用链的作用：
1. **缩小搜索范围**：如果调用链包含具体的 Activity/Fragment 名称，
   说明热点发生在该 UI 组件上下文中，优先搜索该组件相关的类
2. **区分同名方法**：如果多个 Adapter 有同名方法（如 onBindViewHolder），
   调用链中的 RV 实例 ID 和 Adapter 名称可以精确定位到具体的 Adapter 类
3. **理解性能阶段**：调用链中的 [measure阶段]/[layout阶段]/[draw阶段]
   标识说明热点发生在 UI 渲染的哪个子阶段

使用策略：
- 调用链是辅助信息，不需要额外搜索调用链中提到的类
- 当 Glob 搜索到多个同名文件时，优先选择与调用链上下文匹配的文件
- 当 Grep 搜索到方法在多个位置出现时，优先选择与调用链描述一致的调用点
```

#### 改动五：`build_attribution_prompt()` 传递上下文给 explorer

**文件**: `src/smartinspector/commands/attribution.py`

在 `build_attribution_prompt()` 函数（第 744-792 行）中增加调用链上下文展示：

```python
def build_attribution_prompt(attributable: list[dict]) -> str:
    # ... 现有逻辑 ...

    for i, s in enumerate(attributable[:15], 1):
        lines.append(f"### {i}. {s['class_name']}.{s['method_name']}")
        lines.append(f"   - 耗时: {s['dur_ms']:.2f}ms")
        if s.get("instance"):
            lines.append(f"   - 实例: {s['instance']}")
        if s.get("count"):
            lines.append(f"   - 调用次数: {s['count']}")
        if s.get("total_ms"):
            lines.append(f"   - 总耗时: {s['total_ms']:.1f}ms")

        # ── 新增：调用链上下文 ──
        if s.get("call_context"):
            lines.append(f"   - 调用链上下文: {s['call_context']}")

        if s.get("stack_trace"):
            lines.append(f"   - 堆栈采样 (BlockMonitor):")
            for frame in s["stack_trace"][:12]:
                lines.append(f"     {frame}")
        # ... 后续逻辑不变 ...
```

### 2.3 数据流设计

改进后的完整数据流：

```
┌─────────────────────────────────────────────────────────────────┐
│  Perfetto trace (.pb)                                          │
│  slice 表: id, name, ts, dur, depth, parent_id                 │
└──────────────────────────┬──────────────────────────────────────┘
                           │
                           ▼
┌─────────────────────────────────────────────────────────────────┐
│  collect_view_slices()                                          │
│  ① SQL 查询所有 SI$ slices + parent_id                         │
│  ② 回溯缺失的 parent/ grandparent slices                        │
│  ③ 构建 slice_by_id 映射 (id → slice dict)                     │
│  ④ 构建 children_map (parent_id → children list)               │
│  ⑤ 构建 call_chains: top 10 slowest 的 parent chain            │
│                                                                 │
│  输出 view_slices JSON:                                         │
│  {                                                              │
│    "slowest_slices": [...],     ← 每项含 id + parent_id        │
│    "summary": [...],                                           │
│    "rv_instances": [...],                                      │
│    "call_chains": [...]         ← 已构建的调用链                │
│  }                                                              │
└──────────────────────────┬──────────────────────────────────────┘
                           │
                           ▼
┌─────────────────────────────────────────────────────────────────┐
│  extract_attributable_slices()  [改动一 + 改动二]               │
│  ① 从 slowest_slices/summary/rv_instances 提取 class+method    │
│  ② 从 block_events 合并 stack_trace                            │
│  ③ [新增] _build_parent_contexts():                            │
│     - 利用 call_chains 提取预构建的调用链                       │
│     - 对未覆盖的 slice，用 parent_id 回溯构建链                 │
│     - 过滤系统标签，提取 SI$ 上下文节点                         │
│     - 为 RV 实例方法附加 RV#viewId#Adapter 上下文              │
│  ④ 将 call_context 注入每个 attributable entry                  │
│                                                                 │
│  输出 attributable 列表:                                        │
│  [{class_name, method_name, dur_ms, call_context, ...}]         │
└──────────────────────────┬──────────────────────────────────────┘
                           │
                      ┌────┴────┐
                      │         │
                      ▼         ▼
┌──────────────────┐  ┌──────────────────────────────────────────┐
│ deterministic.py │  │  agents/attributor.py                    │
│ compute_hints()  │  │  _build_group_prompt() [改动三]           │
│                  │  │  ① 在每个 issue 行末附加 call_context    │
│ 使用 call_chains │  │  ② LLM 看到调用链上下文                 │
│ 构建调用链分布   │  │                                          │
│ (已有逻辑不变)   │  │  prompts/attributor.txt [改动四]          │
│                  │  │  ① 增加调用链上下文使用指引              │
│                  │  │  ② 增加同名方法区分策略                  │
└──────────────────┘  │                                          │
                      │  输出归因结果:                            │
                      │  [{file_path, line_start, line_end,      │
                      │    source_snippet, call_context}]         │
                      └──────────────────────────────────────────┘
```

### 2.4 调用链上下文的典型输出示例

**场景 A：RecyclerView onBindViewHolder 热点**

```
输入: SI$RV#recycler_list#com.example.app.OrderAdapter.onBindViewHolder
当前: class_name=OrderAdapter, method_name=onBindViewHolder
     → Agent 只知道搜索 OrderAdapter.onBindViewHolder

改进后: call_context = "RV#recycler_list#OrderAdapter.onBindViewHolder"
     → Agent 知道是 recycler_list 这个 RV 实例的 OrderAdapter
     → 如果项目中有多个 OrderAdapter（不同包名），可优先匹配
```

**场景 B：View.measure 热点嵌套在 doFrame 中**

```
输入: SI$view#com.example.app.DetailView.measure
当前: class_name=DetailView, method_name=measure
     → Agent 不知道发生在哪个渲染阶段

改进后: call_context = "[帧渲染] → [measure阶段] → DetailView.measure"
     → Agent 知道是 measure 阶段的性能问题
     → 搜索时可以关联到 layout 布局分析
```

**场景 C：Activity 生命周期中的耗时操作**

```
输入: SI$Activity.lifecycle
当前: class_name=Activity, method_name=lifecycle → system_class
改进后: call_context = "[帧渲染] → HomeActivity.onCreate"
     → 可以精确定位到 HomeActivity 的 onCreate 方法
```

**场景 D：Block 事件的堆栈补充**

```
输入: SI$block#com.example.app.Worker$1.run#250ms
当前: class_name=Worker, method_name=run, stack_trace=[at Worker$1.run(Worker.java:42)]
改进后: call_context = "handler(Worker.startWork)"（如果 parent 是 handler dispatch）
     → Agent 知道是 startWork 方法中启动的匿名类
```

## 3. 对现有代码的改动范围

| 文件 | 改动类型 | 改动内容 | 估计行数 |
|------|---------|---------|---------|
| `commands/attribution.py` | 新增函数 | `_build_parent_contexts()`, `_extract_context_from_chain()`, `_summarize_si_tag()`, `_walk_parent_chain()` | ~120 行 |
| `commands/attribution.py` | 修改函数 | `extract_attributable_slices()` 末尾注入 call_context | ~10 行 |
| `commands/attribution.py` | 修改函数 | `build_attribution_prompt()` 增加 call_context 展示 | ~3 行 |
| `agents/attributor.py` | 修改函数 | `_build_group_prompt()` 增加 call_context 传递 | ~4 行 |
| `prompts/attributor.txt` | 新增章节 | 调用链上下文使用指引 | ~25 行 |
| **总计** | | | **~160 行** |

### 3.1 无需改动的部分

- `collector/perfetto.py`：**无需改动**。已完整采集 parent_id 和 call_chains
- `agents/deterministic.py`：**无需改动**。其 `_compute_call_chain_distribution()` 逻辑独立，不受影响
- `graph/nodes/attributor.py`：**无需改动**。它是 orchestrator 层的胶水代码，数据透传
- `tools/perfetto.py`：**无需改动**

### 3.2 向后兼容性

所有改动都是**增量式**的：
- `call_context` 字段是可选的（`issue.get("call_context", "")`）
- 没有 `call_context` 时，行为与当前完全一致
- 不影响 `deterministic.py`、`frame_analyzer.py` 等其他消费者

## 4. Token 节省预估

### 4.1 减少无效搜索

| 场景 | 当前 token 消耗 | 改进后 token 消耗 | 节省 |
|------|---------------|-----------------|------|
| 同名 Adapter 的 onBindViewHolder | Glob 返回 3 个文件，Read 3 次 | Glob 返回 3 个文件，利用上下文只 Read 1 次 | ~2000 tokens/次 |
| 同名方法多处调用 | Grep 返回 5+ 行，Read 5 次 | 利用上下文只 Read 1 次 | ~4000 tokens/次 |
| 系统类误搜索 | 搜索后才发现是系统类 | 调用链标识了阶段，跳过系统类搜索 | ~1500 tokens/次 |

### 4.2 减少歧义消除的 LLM 推理

当前 LLM 在搜索到多个候选位置后，需要消耗 token 进行推理判断哪个是热点。改进后：
- 调用链上下文直接消除了歧义
- 减少 LLM "猜测" 的 token 消耗

### 4.3 总体预估

假设一次典型归因有 5-8 个可归因 slice，其中 2-3 个存在调用歧义：
- **每次歧义消除节省**: ~1500-4000 tokens
- **单次归因节省**: ~3000-8000 tokens
- **占归因总 token 的比例**: 约 15%-30%

增加的上下文信息 token 开销：
- 每个 slice 的 call_context: ~20-50 tokens
- 8 个 slice: ~160-400 tokens
- prompt 增加的指引: ~200 tokens（一次性）

**净节省: ~2500-7000 tokens/次归因**

## 5. 风险和边界情况

### 5.1 风险分析

#### 风险 1：call_chains 覆盖率不足

**现象**: `collect_view_slices()` 只为 top 10 slowest custom slices 构建 call_chains，但 `extract_attributable_slices()` 可能提取更多 slices。

**缓解**: 改动一中的 `_build_parent_contexts()` 设计了策略2（parent_id 回溯），即使 call_chains 未覆盖，也能从 slowest_slices 中已有的 parent_id 信息向上回溯。

**边界**: 如果 slice 的 parent_id 对应的 slice 不在 slowest_slices 中（因为 top 30 截断），回溯链会不完整。这是可接受的降级——此时 call_context 为空，退回到当前行为。

#### 风险 2：parent_id 在 JSON 传递中丢失

**现象**: `slowest_slices` 在构建时（`collect_view_slices()` 第 691-696 行）保留了 `id` 和 `parent_id`，但 `summary` 统计中没有这些字段。

**缓解**: 只对 `slowest_slices` 中的 slice 提取 parent_context。`summary` 和 `rv_instances` 中的 slice 不需要调用链（它们是聚合数据，不是具体的单次调用）。

#### 风险 3：调用链信息过长

**现象**: 如果调用链很长（>5层），传递给 LLM 的 context 字符串过长，反而浪费 token。

**缓解**: `_extract_context_from_chain()` 只保留 SI$ 自定义标签和阶段标识，过滤掉中间的系统标签。限制最大深度为 5 层。

#### 风险 4：LLM 误用调用链信息

**现象**: LLM 可能尝试搜索调用链中的其他类（如 parent chain 中的 Activity），导致额外的 Glob/Grep/Read 调用。

**缓解**: 在 `prompts/attributor.txt` 中明确说明"调用链是辅助信息，不需要额外搜索调用链中提到的类"。

### 5.2 边界情况

| 边界情况 | 处理方式 |
|---------|---------|
| slice 没有 parent_id | call_context 为空，退回当前行为 |
| parent_id 对应的 slice 不在结果集中 | 回溯链截断，只使用已知的链段 |
| call_chains 为空（trace 无 SI$ 数据） | `_build_parent_contexts()` 返回空 dict |
| atrace 截断了 SI$ tag 名称 | 调用链中可能出现不完整的 tag，_summarize_si_tag() 做了防御处理 |
| 多个 slowest_slice 有相同 name | context 以 name 为 key，后出现的会覆盖（可接受：相同 name 的调用链结构通常相似） |
| RV 实例方法的 instance 字段已包含上下文 | 优先使用 instance 作为上下文（见改动二中的优先级逻辑） |

### 5.3 性能影响

- `_build_parent_contexts()` 的计算开销可忽略（O(n) 遍历 + O(n*log(n)) 回溯）
- 不增加任何 SQL 查询或文件 I/O
- 增加的 JSON 字段约 100-500 bytes，对 token 预算影响极小

## 6. 实施计划

### Phase 1: 基础设施（改动一 + 改动二）

在 `commands/attribution.py` 中实现 `_build_parent_contexts()` 及其辅助函数，修改 `extract_attributable_slices()` 注入 call_context。

验证方式：构造包含 call_chains 的 view_slices JSON，确认 _build_parent_contexts() 输出正确。

### Phase 2: Agent 集成（改动三 + 改动四）

修改 `_build_group_prompt()` 和 `prompts/attributor.txt`，让 LLM 利用调用链上下文。

验证方式：使用真实 trace 数据运行归因，检查 LLM 是否利用了调用链信息、是否减少了无效搜索。

### Phase 3: Explorer 集成（改动五）

修改 `build_attribution_prompt()`，将上下文传递给 explorer agent。

验证方式：运行完整的 orchestrate 流程，检查最终报告是否包含更精确的归因信息。

### Phase 4: 效果评估

对比改进前后的：
1. 归因精确度（能否定位到具体调用点而非方法定义）
2. Token 消耗（归因阶段的 input/output token 总量）
3. 搜索效率（Glob/Grep/Read 调用次数）

## 7. 后续优化方向

### 7.1 短期（本次改动基础上）

- **调用链去重聚合**: 如果多个 slowest_slice 共享相同的 parent chain 前缀，在 prompt 中合并展示，避免重复
- **上下文感知的 Grep 模式**: 当有 call_context 时，让 LLM 在 Grep 时使用更精确的 pattern（如类名+方法名+邻近上下文关键词）

### 7.2 中期（需要额外数据采集）

- **增强 collect_view_slices() 的 parent 回溯深度**: 当前只回溯到 grandparent（第 650-664 行），可考虑递归回溯到根节点（doFrame 级别）
- **利用 track_id 区分线程上下文**: 当前只关注 main thread 的 slices，可扩展到分析 worker thread 的调用上下文

### 7.3 长期（架构级改进）

- **预计算调用链到源码行号的映射**: 在 deterministic.py 中预先匹配调用链中的 SI$ tag 到源码位置，减少 LLM 的搜索负担
- **增量归因**: 对同一 RV 实例的多次调用，只搜索一次源码，后续复用结果
