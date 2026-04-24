# SmartInspector 架构改进规范

> 审查日期: 2026-04-24
> 审查范围: src/smartinspector/ 全部 45 个 Python 源文件
> 审查人: 架构审查 Agent

---

## 1. 现状评估

### 1.1 架构健康度总评分: 7.2 / 10

项目整体架构设计合理，LangGraph pipeline 模式清晰，模块职责划分得当。deterministic pre-computation 层是一个亮点设计，有效减少了 LLM token 消耗。但在 SQL 查询性能、资源管理、错误处理一致性等方面存在改进空间。

### 1.2 各模块质量评分

| 模块 | 评分 | 说明 |
|------|------|------|
| `graph/state.py` | 9/10 | 简洁清晰的 AgentState 定义，`_pass_through` 和 `node_error_handler` 设计得当 |
| `agents/deterministic.py` | 9/10 | 纯计算层设计优秀，与 LLM 职责分离明确 |
| `graph/builder.py` | 9/10 | 图构建清晰，路由映射完备 |
| `collector/perfetto.py` | 7/10 | 功能完整但有 SQL 注入风险、N+1 查询问题、资源管理不完善 |
| `agents/attributor.py` | 7/10 | fast-path 优化到位，但 LLM 单例管理混乱，_structured_ok 全局可变状态 |
| `graph/nodes/collector.py` | 8/10 | WS 集成良好，但缺少对 collector_node 的 `@node_error_handler` 装饰 |
| `graph/nodes/reporter/` | 7/10 | 截断策略合理，但 formatter 和 generator 共享 LLM 实例设计不当 |
| `commands/attribution.py` | 8/10 | SI$ tag 解析完备，匿名内部类处理周到 |
| `ws/server.py` | 7/10 | 单例模式正确，但缺少重连/心跳超时后的自动恢复 |
| `ws/bridge_server.py` | 6/10 | 全局可变状态过多，trace path 管理不够健壮 |
| `config.py` | 8/10 | 简洁实用，env var 解析一致 |
| `token_tracker.py` | 9/10 | 线程安全，API 设计清晰 |

### 1.3 做得好的地方

1. **Deterministic pre-computation** (`agents/deterministic.py`): 将算术和分类逻辑从 LLM 中剥离，大幅减少 token 消耗，同时提高了结论的准确性。这是一个非常值得肯定的架构决策。

2. **Attributor fast-path** (`agents/attributor.py:137-327`): 对于简单的 Java 类搜索，绕过 LLM 直接执行 Glob→Grep→Read，既节省 token 又提高速度。partial fallback 机制（部分命中 fast-path，其余走 LLM）设计精巧。

3. **`node_error_handler` decorator** (`graph/state.py:62-84`): 统一的节点错误处理模式，避免任何单个节点的异常导致整个 pipeline 崩溃。

4. **SI$ tag system**: 完整的 tag 解析体系（`commands/attribution.py`），涵盖 RV、block、inflate、view、handler 等多种模式，匿名内部类的处理尤其周到。

5. **Trace collection degradation** (`collector/perfetto.py:1750-1831`): stdin-pipe → cat-pipe → cmdline 三级降级策略，覆盖了 SELinux 限制等设备兼容性问题。

6. **LRU file cache** (`agents/attributor.py:63-98`): 跨 group 共享的文件缓存，避免 attributor 多轮迭代中重复读取相同文件。

### 1.4 关键技术债务清单

| # | 技术债 | 影响 | 严重度 |
|---|--------|------|--------|
| T1 | SQL 注入风险: f-string 拼接 SQL | 安全隐患 | 高 |
| T2 | thread_state N+1 查询 | 性能瓶颈 | 高 |
| T3 | `_structured_ok` 全局可变状态竞态 | 可靠性 | 中 |
| T4 | TraceProcessor 未在所有路径 close | 资源泄漏 | 高 |
| T5 | LLM 实例管理碎片化 | 维护性 | 中 |
| T6 | bridge_server 全局状态管理 | 可维护性 | 中 |
| T7 | 部分节点缺少 `@node_error_handler` | 可靠性 | 中 |
| T8 | `_walk_call_chain` 逐行查询 | 性能 | 中 |

---

## 2. 性能优化建议

### P2-1: thread_state 分析的 N+1 查询问题

**问题描述**: `collect_thread_state()` 在 `__intrinsic_thread_state` 主路径中，对每个 slice 单独执行一次 SQL 查询（per-slice loop, `collector/perfetto.py:1264-1278`），且每次还需要额外查询 waker name（`collector/perfetto.py:1317-1324`）。20 个 slice 最多产生 20 + 20 = 40 次 SQL 查询。

**影响范围**: `collector/perfetto.py:1203-1338`

**具体方案**:

1. 将 per-slice 查询改为批量 CTE 查询，一次 SQL 获取所有 slice 的 thread_state 分布：

```sql
WITH slices AS (
  SELECT name, ts, dur, ts + dur AS end_ts
  FROM slice
  WHERE name LIKE 'SI$%' AND dur > 1000000
  ORDER BY dur DESC LIMIT 20
)
SELECT
  s.name AS slice_name,
  its.state,
  SUM(its.dur) AS total_ns,
  its.blocked_function,
  its.io_wait,
  its.waker_utid
FROM slices s
JOIN __intrinsic_thread_state its
  ON its.utid = :main_utid
  AND its.ts < s.end_ts
  AND its.ts + its.dur > s.ts
GROUP BY s.name, its.state, its.blocked_function, its.io_wait, its.waker_utid
ORDER BY s.name, total_ns DESC
```

2. waker name 批量解析：收集所有 unique `waker_utid`，一次查询获取所有 name。

**预估收益**: SQL 查询次数从 ~40 降低到 2（主查询 + waker 批量查询），thread_state 分析耗时减少 80%+。

**实施复杂度**: 中等。需要重构 Python 端的结果聚合逻辑。

### P2-2: `_walk_call_chain` 的逐行查询

**问题描述**: `perfetto.py:2104-2133` 中的 `_walk_call_chain` 对每个 parent_id 单独执行 SQL 查询，最深 20 层。在 `query_frame_slices` 中最多 10 个 slice 调用，总计可能 200 次 SQL 查询。

**影响范围**: `collector/perfetto.py:2104-2133`

**具体方案**: 预加载所有相关 slice 到内存 map，然后在 Python 中遍历 parent 链（与 `collect_view_slices` 中 `_build_chain` 的做法一致）。

```python
def _walk_call_chain_cached(tp, slice_id: int, slice_map: dict, seen: set) -> dict:
    """Walk call chain using pre-loaded slice map."""
    chain_items = []
    current_id = slice_id
    for _ in range(20):
        if current_id is None or current_id in seen or current_id not in slice_map:
            break
        r = slice_map[current_id]
        seen.add(current_id)
        chain_items.append({...})
        current_id = r.get("parent_id")
    ...
```

在 `query_frame_slices` 中，先一次性查询所有涉及的 slice（按 parent_id 递归），构建 map，然后遍历。

**预估收益**: `query_frame_slices` 中 SQL 查询从 ~200 降低到 ~5。

**实施复杂度**: 低。

### P2-3: PerfSummary.to_json() 的重复 JSON 序列化

**问题描述**: `PerfSummary.to_json()` 使用 `json.dumps(self.__dict__, indent=2)`。随后在 `collector_node` 中，这个 JSON 字符串又在 `formatter.py` 中被 `json.loads()` 解析，又被 `deterministic.py` 中的 `compute_hints` 再次 `json.loads()`。同一段数据被反复序列化/反序列化 3-4 次。

**影响范围**: `collector/perfetto.py:80-81`, `graph/nodes/reporter/__init__.py`, `graph/nodes/reporter/formatter.py`, `agents/deterministic.py`

**具体方案**: 在 pipeline 内部传递 `dict` 而非 JSON 字符串，仅在边界（state 存储和 LLM prompt 输入）进行 JSON 序列化。

- `PerfettoCollector.summarize()` 返回 `dict` 而非调用 `to_json()`
- `collector_node` 存储到 state 时序列化一次
- `formatter.py` 和 `deterministic.py` 直接接收 `dict`

**预估收益**: 减少不必要的 CPU 消耗和内存分配，对大 trace 尤为明显。

**实施复杂度**: 中等。需要修改 state 中 `perf_summary` 的类型约定。

### P2-4: LLM token 效率 - perf_analyzer 截断策略

**问题描述**: `perf_analyzer.py:49` 将 `perf_json[:3000]` 截断后发送给 LLM，但 `compute_hints` 已经预计算了所有结论。原始 JSON 仅作为"参考"出现，3000 字符的限制过于武断，可能截断关键数据（如 thread_state），也可能包含大量无用数据（如 cpu_idle_samples 的时间序列）。

**影响范围**: `agents/perf_analyzer.py:46-49`

**具体方案**: 根据 `hints` 的内容智能选择补充数据，而非盲截。例如：
- 如果 `_classify_severity` 输出非空，附上 view_slices 的 slowest_slices
- 如果 `_analyze_thread_state` 输出非空，附上 thread_state 完整数据
- 始终附上 frame_timeline 汇总（FPS, jank count）

**预估收益**: LLM 输入 token 减少 30-50%（去掉无用的时间序列数据），同时分析质量提升（保留关键数据）。

**实施复杂度**: 低。

### P2-5: reporter 重复调用 compute_hints

**问题描述**: `compute_hints(perf_json)` 在 pipeline 中被调用两次：
1. `perf_analyzer.py:43` — analyzer 阶段
2. `formatter.py:17` — reporter 阶段（通过 `format_perf_sections`）

同一段 JSON 被解析和计算两次，完全浪费。

**影响范围**: `agents/perf_analyzer.py:43`, `graph/nodes/reporter/formatter.py:17`

**具体方案**: 将第一次 `compute_hints` 的结果存入 state（新增 `perf_hints` 字段），reporter 直接复用。

**预估收益**: 避免重复 JSON 解析 + 6 个分析函数的重复计算。

**实施复杂度**: 低。

---

## 3. 架构改进建议

### A1: SQL 注入风险修复

**当前问题**: 多处 SQL 查询使用 f-string 拼接用户可控输入：

- `collector/perfetto.py:130`: `WHERE name = '{package_name}'`
- `collector/perfetto.py:149-153`: `WHERE package_name = '{package_name}'`
- `collector/perfetto.py:166`: `WHERE uid = {uid}`
- `collector/perfetto.py:743`: `WHERE id IN ({id_list})`
- `collector/perfetto.py:965-971`: track_id IN 查询
- `collector/perfetto.py:1183-1199`: thread_state 多处 f-string

虽然 Perfetto trace_processor 是本地进程且数据来自设备 trace，不涉及网络攻击面，但 `package_name` 和 `target_process` 来自 CLI 输入/WS 配置，理论上有注入可能。

**目标架构**: 使用参数化查询或至少进行输入验证。

**迁移路径**:
1. 在 `PerfettoCollector.__init__` 中验证 `target_process` 格式（仅允许 `[a-zA-Z0-9._]`）
2. 对于 `id_list` 类查询，确保所有 ID 都是整数（`int()` 转换）
3. 对于 `utid` 等，已在内部生成，风险较低，但仍建议使用占位符

**风险评估**: 低风险修改，不影响功能。

### A2: TraceProcessor 资源管理

**当前问题**: `query_frame_slices()` (`collector/perfetto.py:1934-2039`) 创建了 `TraceProcessor` 实例并在 `finally` 中 close，这是正确的。但 `PerfettoCollector` 的 `_open()` 方法只在 `close()` 中释放，而 `summarize()` 调用链中如果中途异常，`_tp` 可能不会被关闭。

更重要的是，`PerfettoCollector` 用作 context manager 时如果 `summarize()` 抛异常（如 `collect_cpu_hotspots` 失败），`__exit__` 能正确清理。但在 `collector_node` 中（`graph/nodes/collector.py:184`）：

```python
collector = PerfettoCollector(trace_path, target_process=target_process)
summary = collector.summarize()
```

没有使用 `with` 语句，`close()` 从未被调用。这意味着 `trace_processor_shell` 子进程在 `summarize()` 完成后仍然运行。

**目标架构**: 使用 context manager 确保资源释放。

**迁移路径**:
```python
with PerfettoCollector(trace_path, target_process=target_process) as collector:
    summary = collector.summarize()
```

**风险评估**: 安全修改，`summarize()` 内部的 try/except 已保证即使异常也有结果返回。

### A3: 统一 LLM 实例管理

**当前问题**: LLM 实例分散在多处，管理模式不一致：

| 位置 | 模式 | 问题 |
|------|------|------|
| `orchestrator.py:39-47` | 全局 `_route_llm` | 单例，但被 reporter 的 `generate_report` 复用（不合理） |
| `attributor.py:100-130` | 全局 + Lock + 探测 | `_structured_ok` 全局变量，线程不安全 |
| `perf_analyzer.py:17-25` | 全局 + Lock | 独立实例 |
| `frame_analyzer.py:21-29` | 全局 + Lock | 独立实例 |
| `explorer.py:19-33` | 全局 + Lock | `create_agent` API（可能已废弃） |
| `android.py:16-31` | 全局 + Lock | 同上 |

**目标架构**: 创建 `LLMFactory` 类集中管理 LLM 实例。

```python
# config.py 或新的 llm_factory.py
class LLMFactory:
    _instances: dict[str, ChatOpenAI] = {}
    _lock = threading.Lock()

    @classmethod
    def get(cls, role: str = "default", **kwargs) -> ChatOpenAI:
        key = f"{role}:{frozenset(kwargs.items())}"
        if key not in cls._instances:
            with cls._lock:
                if key not in cls._instances:
                    cls._instances[key] = ChatOpenAI(**get_llm_kwargs(role=role, **kwargs))
        return cls._instances[key]
```

**迁移路径**: 逐个替换现有全局变量。注意 `attributor.py` 的 `_structured_ok` 探测逻辑需要特殊处理。

**风险评估**: 低风险，但需要测试各 LLM 实例的 temperature/max_tokens 配置是否保持一致。

### A4: agent 层 API 不一致

**当前问题**:
- `explorer.py:36` 和 `android.py:16` 使用 `langchain.agents.create_agent` — 这个 API 在 LangChain 中可能已废弃或不存在于当前版本
- `attributor.py` 使用手动的 tool-call loop（`llm.bind_tools` + 手动 dispatch）
- `perf_analyzer.py` 和 `frame_analyzer.py` 使用单次 `llm.invoke`

三种不同的 agent 实现模式增加了维护负担。

**目标架构**: 统一使用 `llm.bind_tools` + 手动 dispatch 模式（attributor 的模式已被验证最可控），或统一使用 LangGraph 的 `create_react_agent`。

**迁移路径**: 长期演进。当前优先修复 `create_agent` 的导入问题。

**风险评估**: 中等。需要验证 LangChain 版本兼容性。

### A5: 缺少 `@node_error_handler` 的节点

**当前问题**: 以下节点缺少 `@node_error_handler` 装饰：

- `graph/nodes/collector.py:107` — `collector_node`: 内部有 try/except 但不返回 `_pass_through` 字段，可能导致下游节点读到空值
- `graph/nodes/attributor.py:33` — `attributor_node`: 完全没有异常处理
- `graph/nodes/reporter/__init__.py:21` — `reporter_node`: 完全没有异常处理

这意味着如果这些节点抛出未预期的异常，LangGraph 会直接终止整个 pipeline，用户看到的是原始 traceback 而非友好错误信息。

**目标架构**: 所有 graph node 使用 `@node_error_handler` 装饰。

**迁移路径**: 逐个添加装饰器，确保每个节点的异常都被捕获并转换为 AIMessage。

**风险评估**: 低风险。

### A6: bridge_server 全局状态管理

**当前问题**: `bridge_server.py:314-317` 使用模块级全局变量管理状态：

```python
_active_bridge: BridgeServer | None = None
_active_trace_server = None
_cached_perf_summary: str = ""
_cached_attribution_result: str = ""
```

`start_bridge()` 中还局部变量覆盖了这些全局（`_perf_summary_cache` vs `_cached_perf_summary`），容易混淆。

**目标架构**: 将状态封装到 `BridgeManager` 类中，或至少统一变量命名。

**迁移路径**: 短期：统一变量命名。长期：封装到类。

**风险评估**: 低风险。

---

## 4. 可靠性改进

### R1: TraceProcessor 连接超时处理

**问题**: `PerfettoCollector._open()` (`collector/perfetto.py:100-108`) 设置了 `load_timeout=10` 秒，但没有重试机制。对于大 trace 文件（>100MB），首次加载可能超时。

**方案**: 添加重试逻辑，或根据文件大小动态调整超时。

```python
def _open(self) -> TraceProcessor:
    if self._tp is not None:
        return self._tp
    file_size_mb = Path(self.trace_path).stat().st_size / 1024 / 1024
    timeout = max(10, int(file_size_mb / 10))  # 100MB -> 10s, 1GB -> 100s
    config = TraceProcessorConfig(bin_path=self.shell_path, load_timeout=timeout)
    self._tp = TraceProcessor(trace=self.trace_path, config=config)
    return self._tp
```

**文件**: `collector/perfetto.py:100-108`

### R2: WS 连接断开后的状态同步

**问题**: `ws/server.py:278-283` 的 `_handler` 在连接关闭时从 `_connections` 中移除，但没有清理 `_pending_acks`。如果 app 在 ACK 到达前断开，等待 ACK 的线程会永久阻塞直到 timeout。

**方案**: 在连接关闭时，set 所有 pending acks（标记为失败）。

```python
async def _handler(self, ws) -> None:
    self._connections.add(ws)
    try:
        async for raw in ws:
            ...
    except websockets.exceptions.ConnectionClosed:
        pass
    finally:
        self._connections.discard(ws)
        # Release any pending ACKs for this connection
        for msg_id, event in list(self._pending_acks.items()):
            if not event.is_set():
                event.set()  # Will return False from send_config/send_start_trace
```

**文件**: `ws/server.py:264-283`

### R3: attributor 的 `_structured_ok` 竞态条件

**问题**: `attributor.py:55` 的 `_structured_ok` 是模块级全局变量，在 `_get_llm()` 中初始化，在 `_search_group()` 中修改为 `False`。如果有两个 attributor 调用并发执行（虽然当前 CLI 是单线程），一个修改 `_structured_ok` 可能影响另一个。

**当前风险评估**: 低。CLI 是单线程调用 attributor，但 frame_analyzer 的 bridge_server 调用在 executor 线程中，理论上可能并发。

**方案**: 将 `_structured_ok` 移入 `_get_llm()` 的 Lock 保护范围内，或在初始化时一次性探测并缓存。

**文件**: `agents/attributor.py:55, 863`

### R4: subprocess 资源泄漏

**问题**: `TraceServer` (`collector/perfetto.py:1877`) 使用 `Popen` 启动 `trace_processor_shell`，如果 BridgeServer 异常退出，`TraceServer.stop()` 可能不被调用。`bridge_server.py:419-427` 的 `stop_bridge()` 虽然会清理，但依赖调用方正确调用。

**方案**: 为 `TraceServer` 添加 `__del__` 方法作为最后保障，或使用 `atexit` 注册清理。

```python
import atexit

class TraceServer:
    def __init__(self, ...):
        atexit.register(self.stop)
```

**文件**: `collector/perfetto.py:1848-1919`

### R5: JSON 解析失败的静默吞没

**问题**: 多处 `json.loads()` 在 try/except 中静默吞没错误，返回空值：

- `perf_analyzer.py:46`: `compute_hints` 内 `json.loads` 失败返回 `""`
- `formatter.py:22`: `json.loads` 失败返回 `{}`
- `frame_analyzer.py:79`: `json.loads` 失败截断到前 2000 字符

这些静默失败可能隐藏重要错误，使得调试困难。

**方案**: 至少用 `logger.warning` 记录解析失败，不要完全静默。

**文件**: 多处

### R6: node_error_handler 中的 print

**问题**: `graph/state.py:78` 的 `node_error_handler` 使用 `print()` 输出错误信息，而非 `logger.error()`。按照项目的 logging 标准，应该使用 logger。

```python
# 当前
print(f"  [{node_name}] ERROR: {e}", flush=True)

# 应改为
import logging
logging.getLogger(__name__).error("[%s] %s", node_name, e)
```

**文件**: `graph/state.py:78`

---

## 5. 代码质量改进

### Q1: extract_class / extract_method 的重复解析模式

**问题**: `commands/attribution.py` 中 `extract_class()`, `extract_method()`, `extract_fqn()` 三个函数对同一个 SI$ tag 分别解析一次，且解析逻辑高度重复（每個函数都有相同的 `if body.startswith("block#"):` 等分支）。

**方案**: 创建统一的 `_parse_si_tag()` 函数，一次解析返回结构化结果：

```python
@dataclass
class SITag:
    tag_type: str        # "block", "RV", "inflate", "view", "handler", ...
    class_name: str
    method_name: str
    fqn: str
    search_type: str     # "java", "xml", "system"
    raw_name: str

def parse_si_tag(name: str) -> SITag:
    """Single-pass SI$ tag parser."""
    ...
```

**文件**: `commands/attribution.py:15-401`

### Q2: formatter.py 中 import 放在函数内部

**问题**: `graph/nodes/reporter/formatter.py:17` 在函数内部 `from smartinspector.agents.deterministic import compute_hints`，每次调用都执行 import 查找（虽然 Python 会缓存）。

**方案**: 移到文件顶部。如果存在循环依赖问题，说明模块拆分不合理。

**文件**: `graph/nodes/reporter/formatter.py:17`, `agents/perf_analyzer.py:42`

### Q3: SIServer 单例模式的可测试性

**问题**: `ws/server.py` 的 `SIServer` 使用类级 `_instance` 和 `_lock` 实现单例。这使得单元测试难以注入 mock server。

**方案**: 添加 `reset()` 类方法用于测试，或使用依赖注入。

**文件**: `ws/server.py:58-63`

### Q4: 测试覆盖

**问题**: 项目目前没有测试文件（`tests/` 目录为空或不存在）。

**建议的测试优先级**:

1. **P0**: `commands/attribution.py` 的 SI$ tag 解析 — 纯函数，容易测试，覆盖最核心的业务逻辑
2. **P0**: `agents/deterministic.py` 的 `compute_hints` — 纯函数，验证预计算逻辑正确性
3. **P1**: `collector/perfetto.py` 的 SQL 查询逻辑（用 mock TraceProcessor）
4. **P1**: `graph/state.py` 的 `_pass_through` 和 `node_error_handler`
5. **P2**: 各 graph node 的输入/输出契约

### Q5: 代码重复 — block stack trace 关联

**问题**: `collect_block_events()` (`collector/perfetto.py:1169-1201`) 和 `_correlate_block_stacks_from_logcat()` (`collector/perfetto.py:2042-2101`) 实现了几乎相同的 bisect-based 时间戳关联逻辑。

**方案**: 提取公共函数 `_correlate_by_timestamp(sql_events, log_entries, match_window_ns)`。

**文件**: `collector/perfetto.py:1169-1201` 和 `collector/perfetto.py:2042-2101`

### Q6: config.py 的 get_* 函数重复模式

**问题**: `config.py` 中 `get_tool_timeout()`, `get_read_max_lines()`, `get_read_max_bytes()`, `get_read_max_line_length()`, `get_report_max_tokens()`, `get_ws_ping_timeout()` 六个函数完全同构：

```python
def get_xxx() -> int:
    try:
        return int(os.environ.get("SI_XXX", default))
    except (ValueError, TypeError):
        return default
```

**方案**: 提取 `_env_int(key, default)` 辅助函数。

```python
def _env_int(key: str, default: int) -> int:
    try:
        return int(os.environ.get(key, str(default)))
    except (ValueError, TypeError):
        return default

def get_tool_timeout() -> int:
    return _env_int("SI_TOOL_TIMEOUT", 30)
```

**文件**: `config.py:122-170`

---

## 6. 实施路线图

### P0（紧急）— 影响用户体验的问题

| # | 项目 | 涉及文件 | 说明 | 状态 |
|---|------|----------|------|------|
| P0-1 | TraceProcessor 资源泄漏 | `graph/nodes/collector.py:184` | 使用 `with` 语句确保 close | ✅ 已完成 |
| P0-2 | 缺少 `@node_error_handler` | `graph/nodes/attributor.py:33`, `graph/nodes/reporter/__init__.py:21` | 添加装饰器 | ✅ 已完成 |
| P0-3 | `_walk_call_chain` 性能 | `collector/perfetto.py:2104-2133` | 预加载 slice map 替代逐行查询 | ✅ 已完成 |

> **P0 功能改进已于 2026-04-24 完成**，新增了以下功能：
> - IO Hooks 默认启用（`SI$net#`/`SI$db#`/`SI$img#`），IO 切片独立收集和归因
> - 冷启动专项分析模式（`collector/startup.py` + `graph/nodes/startup.py`）
> - Headless/CI 模式（`headless.py` + CLI `--ci` 参数）
> - JSON 报告格式（`graph/nodes/reporter/json_formatter.py`）

### P1（重要）— 架构层面的改进

| # | 项目 | 涉及文件 | 说明 |
|---|------|----------|------|
| P1-1 | SQL 注入风险修复 | `collector/perfetto.py` 多处 | 输入验证 + 参数化 |
| P1-2 | thread_state N+1 查询 | `collector/perfetto.py:1203-1338` | 批量 CTE 查询 |
| P1-3 | 统一 LLM 实例管理 | 多文件 | 创建 LLMFactory |
| P1-4 | reporter 重复调用 compute_hints | `formatter.py:17`, `perf_analyzer.py:43` | state 中缓存 hints |
| P1-5 | block stack 关联代码重复 | `collector/perfetto.py` | 提取公共函数 |
| P1-6 | SI$ tag 统一解析 | `commands/attribution.py` | 创建 `parse_si_tag()` |
| P1-7 | node_error_handler print → logger | `graph/state.py:78` | 改用 logger.error |

### P2（优化）— 性能和代码质量提升

| # | 项目 | 涉及文件 | 说明 |
|---|------|----------|------|
| P2-1 | JSON 反复序列化/反序列化 | pipeline 多处 | 内部传递 dict |
| P2-2 | perf_analyzer 智能截断 | `agents/perf_analyzer.py:46-49` | 基于 hints 选择补充数据 |
| P2-3 | config.py 重复模式 | `config.py:122-170` | 提取 `_env_int` |
| P2-4 | formatter.py 内部 import | `graph/nodes/reporter/formatter.py:17` | 移到文件顶部 |
| P2-5 | bridge_server 全局状态 | `ws/bridge_server.py:314-317` | 封装到类 |
| P2-6 | _structured_ok 竞态 | `agents/attributor.py:55` | Lock 保护 |
| P2-7 | WS 连接断开清理 | `ws/server.py:278-283` | 清理 pending_acks |

### P1 Feature Roadmap — 规划中的功能改进

| # | 项目 | 说明 | 涉及模块 |
|---|------|------|----------|
| P1-1 | Compose 重组追踪 | 追踪 Jetpack Compose 重组次数和耗时，定位不必要的 recomposition | Android SDK + collector |
| P1-2 | 内存分配分析 | 基于 `android.java_hprof` 数据源分析内存分配热点，定位内存抖动和泄漏 | collector + 新 agent |
| P1-3 | 历史对比与趋势 | 多次分析结果对比，生成 before/after 报告和性能趋势图 | reporter + persistence |
| P1-4 | 智能一键分析 | 基于历史数据和 device profile 自动选择最佳分析策略 | orchestrator |
| P1-5 | ExtraHook 参数自动推断 | 分析代码结构自动推荐 Hook 配置，减少手动配置 | agents + commands |

### P3（可选）— 长期演进方向

| # | 项目 | 说明 |
|---|------|------|
| P3-1 | 统一 agent 实现模式 | explorer/android 使用 `create_agent`，改为 `bind_tools` + 手动 dispatch |
| P3-2 | 添加测试覆盖 | 从 attribution.py 和 deterministic.py 开始 |
| P3-3 | TraceProcessor 连接池 | 对于高频查询场景（bridge），复用 HTTP 模式的 TraceServer |
| P3-4 | 流式 attributor | 将 attributor 的 LLM 调用改为流式，减少用户等待感知 |
| P3-5 | 可插拔 LLM 后端 | 支持本地模型（Ollama）和不同 API 格式，减少 DeepSeek 依赖 |
| P3-6 | SIServer 可测试性重构 | 添加 reset() 方法或使用依赖注入 |

---

## 附录: 文件级问题索引

| 文件 | 行号 | 问题 | 严重度 |
|------|------|------|--------|
| `collector/perfetto.py` | 100-108 | TraceProcessor 超时不可配置 | P1 |
| `collector/perfetto.py` | 130 | SQL f-string 拼接 | P1 |
| `collector/perfetto.py` | 743 | SQL IN clause f-string | P1 |
| `collector/perfetto.py` | 1264-1278 | N+1 SQL 查询 | P1 |
| `collector/perfetto.py` | 1877-1919 | TraceServer 无 atexit 清理 | P2 |
| `collector/perfetto.py` | 2104-2133 | 逐行查询 call chain | P0 |
| `graph/state.py` | 78 | print 而非 logger | P1 |
| `graph/nodes/collector.py` | 184 | 缺少 `with` context manager | P0 |
| `graph/nodes/attributor.py` | 33 | 缺少 `@node_error_handler` | P0 |
| `graph/nodes/reporter/__init__.py` | 21 | 缺少 `@node_error_handler` | P0 |
| `graph/nodes/reporter/formatter.py` | 17 | 函数内部 import | P2 |
| `graph/nodes/reporter/generator.py` | 19 | 复用 orchestrator 的 `_get_route_llm()` | P1 |
| `agents/attributor.py` | 55 | `_structured_ok` 全局可变状态 | P2 |
| `agents/perf_analyzer.py` | 49 | 武断截断 perf_json[:3000] | P2 |
| `commands/attribution.py` | 全文件 | 重复的 tag 解析逻辑 | P1 |
| `ws/server.py` | 278-283 | 连接断开未清理 pending_acks | P2 |
| `ws/bridge_server.py` | 314-317 | 模块级全局状态 | P2 |
| `config.py` | 122-170 | 重复的 get_* 函数模式 | P2 |
