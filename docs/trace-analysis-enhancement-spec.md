# SI Trace 分析能力增强 & Prompt Skill 化改造 SPEC

> 生成日期: 2026-05-21
> 对照基准: `~/.claude/skills/perfetto-trace-analysis/SKILL.md` + `~/.claude/skills/perfetto-sql/SKILL.md`

---

## Part 1: 分析能力补充

### 1.1 SI 当前能力清单

| # | 能力 | 数据源 | Collector 方法 | Deterministic Hint | 报告输出 |
|---|------|--------|---------------|-------------------|----------|
| 1 | CPU 调度统计 | `sched` | `collect_sched()` | `[CPU热点]` | 热点线程 + blocked_reasons |
| 2 | CPU 函数采样 | `perf_sample` + `stack_profile_*` | `collect_cpu_hotspots()` | `[CPU热点]` | 火焰图数据 + callchain |
| 3 | CPU 使用率 | `sched` + `trace_bounds` | `collect_cpu_usage()` | `[CPU热点]` | 每进程/线程 CPU% |
| 4 | 帧时间线 | `actual/expected_frame_timeline_slice` | `collect_frame_timeline()` | `[卡顿帧关联]` + `[严重度分类]` | FPS、jank、最慢帧 |
| 5 | View 切片 | `slice` (SI$ 前缀) | `collect_view_slices()` | `[RV热点排名]` + `[调用链时间分布]` | 调用链、RV 实例分组 |
| 6 | 主线程阻塞 | `slice` (SI$block#) + `android_logs` | `collect_block_events()` | — | BlockMonitor 堆栈 |
| 7 | IO 切片 | `slice` (SI$net/db/img#) | `collect_io_slices()` | `[IO分析]` | 按类型聚合 |
| 8 | 输入事件 | `slice` (SI$touch#) | `collect_input_events()` | `[卡顿帧关联]` | 触摸事件 + jank 关联 |
| 9 | 线程状态 | `__intrinsic_thread_state` / `thread_state` | `collect_thread_state()` | `[线程状态分析]` | Running/Sleeping/DiskSleep |
| 10 | 堆内存 | `heap_graph_*` | `collect_memory()` → `memory.py` | `[内存分配分析]` | 堆对象、泄漏嫌疑 |
| 11 | 进程内存 | `process_counter_track` | `collect_process_memory()` | `[内存分配分析]` | RSS/anon 趋势 |
| 12 | Compose 重组 | `slice` (SI$compose#) | `collect_compose_slices()` | `[Compose重组分析]` | 首次/重组计数 |
| 13 | 冷启动 | startup SQL | `startup.py` | — | 阶段拆分 + 瓶颈 |
| 14 | 系统统计 | `cpu_counter_track` + `counter` | `collect_sys_stats()` | — | CPU idle、频率、fork |
| 15 | 源码归因 | 代码搜索 | `attributor.py` agent | — | 文件路径 + 代码片段 |

**总计: 15 个分析维度**

### 1.2 缺失能力清单（Skill 中有但 SI 没有）

对比 `perfetto-trace-analysis/SKILL.md` 中定义的 6 大分析维度和 Perfetto 标准 SQL 能力：

| # | 缺失能力 | 优先级 | 分析价值 | 实现难度 |
|---|---------|--------|---------|---------|
| **A1** | **CPU 调度延迟分析** | P0 | 高 | 中 |
| **A2** | **锁竞争分析** | P0 | 高 | 低 |
| **A3** | **GC 事件分析** | P0 | 高 | 低 |
| **A4** | **文件 I/O 延迟分析** | P1 | 高 | 中 |
| **A5** | **内存增长趋势（时序）** | P1 | 高 | 低 |
| **A6** | **Binder IPC 分析** | P1 | 中 | 中 |
| **A7** | **网络请求分析** | P2 | 中 | 高 |
| **A8** | **电源/Wakelock 分析** | P2 | 中 | 低 |
| **A9** | **SharedPreferences 分析** | P2 | 中 | 低 |
| **A10** | **CPU 频率降频检测** | P1 | 中 | 低 |
| **A11** | **线程等待链分析** | P2 | 中 | 高 |
| **A12** | **DALVIK/ART 分析** | P2 | 低 | 高 |

### 1.3 每个缺失能力的补充方案

#### A1: CPU 调度延迟分析 [P0]

**现状**: `collect_sched()` 只统计 switches 和 total_dur，不计算调度延迟。

**缺失**: 从线程变为 runnable 到实际获得 CPU 运行的时间差（scheduling latency），是判断"线程被抢占"的关键指标。

**方案**:

```python
def collect_sched_latency(self) -> dict:
    """Analyze scheduling latency: time from runnable to running."""
    tp = self._open()
    # 使用 sched 表中相邻事件计算唤醒延迟
    rows = tp.query("""
        SELECT
            t.name AS thread_name,
            COUNT(*) AS latency_events,
            AVG(s2.ts - s1.ts) / 1e6 AS avg_latency_ms,
            MAX(s2.ts - s1.ts) / 1e6 AS max_latency_ms,
            SUM(CASE WHEN (s2.ts - s1.ts) > 1e6 THEN 1 ELSE 0 END) AS over_1ms
        FROM sched s1
        JOIN sched s2 ON s1.utid = s2.utid
            AND s2.ts > s1.ts
            AND s2.ts < s1.ts + 50000000  -- 50ms 窗口
        JOIN thread t ON s1.utid = t.utid
        WHERE s1.end_state = 'R'  -- 上次让出 CPU 时是 runnable
        GROUP BY t.name
        HAVING latency_events > 5
        ORDER BY avg_latency_ms DESC
        LIMIT 15
    """)
```

**Perfetto SQL 替代方案** (更准确，使用标准库):
```sql
INCLUDE PERFETTO MODULE sched.runnable;
SELECT
  thread_name,
  COUNT(*) AS runnable_count,
  AVG(runnable_dur) / 1e6 AS avg_runnable_ms,
  MAX(runnable_dur) / 1e6 AS max_runnable_ms
FROM sched_runnable
GROUP BY thread_name
ORDER BY avg_runnable_ms DESC
LIMIT 15;
```

**数据结构**:
```python
{
    "sched_latency": {
        "threads": [
            {
                "thread_name": "main",
                "runnable_count": 150,
                "avg_runnable_ms": 2.3,
                "max_runnable_ms": 45.0,
                "over_8ms": 12  # 超过帧预算的次数
            }
        ],
        "summary": {
            "total_over_budget": 50,
            "worst_thread": "main"
        }
    }
}
```

**Deterministic Hint**: `[调度延迟]` — 标记调度延迟超过帧预算 50% 的线程。

---

#### A2: 锁竞争分析 [P0]

**现状**: `_analyze_thread_state()` 能识别 futex_wait 但无锁竞争全貌。

**方案**:

```python
def collect_lock_contention(self) -> dict:
    """Analyze lock contention from futex wait events."""
    tp = self._open()
    rows = tp.query("""
        SELECT
            t.name AS thread_name,
            COUNT(*) AS wait_count,
            SUM(ts_end - ts) / 1e6 AS total_wait_ms,
            MAX(ts_end - ts) / 1e6 AS max_wait_ms,
            AVG(ts_end - ts) / 1e6 AS avg_wait_ms
        FROM (
            SELECT
                utid,
                ts,
                LEAD(ts) OVER (PARTITION BY utid ORDER BY ts) AS ts_end,
                state
            FROM thread_state
        ) sub
        JOIN thread t ON sub.utid = t.utid
        WHERE state = 'S'
          AND ts_end IS NOT NULL
          AND (ts_end - ts) > 100000  -- > 0.1ms
        GROUP BY t.name
        ORDER BY total_wait_ms DESC
        LIMIT 15
    """)
```

**更精确的 Perfetto SQL** (使用 `__intrinsic_thread_state`):
```sql
SELECT
  t.name AS thread_name,
  COUNT(*) AS futex_wait_count,
  SUM(dur) / 1e6 AS total_wait_ms,
  MAX(dur) / 1e6 AS max_wait_ms
FROM __intrinsic_thread_state its
JOIN thread t ON its.utid = t.utid
WHERE its.blocked_function GLOB '*futex*'
  AND its.dur > 100000
GROUP BY t.name
ORDER BY total_wait_ms DESC
LIMIT 15;
```

**数据结构**:
```python
{
    "lock_contention": {
        "threads": [
            {
                "thread_name": "main",
                "futex_wait_count": 35,
                "total_wait_ms": 250.0,
                "max_wait_ms": 45.0,
                "avg_wait_ms": 7.1
            }
        ],
        "contention_hotspots": [
            {
                "blocked_function": "futex_wait_queue_me",
                "thread_name": "main",
                "occurrences": 20,
                "total_ms": 150.0
            }
        ]
    }
}
```

**Deterministic Hint**: `[锁竞争]` — 标记主线程 futex 等待超过 5ms 的事件。

---

#### A3: GC 事件分析 [P0]

**现状**: 完全缺失。无法分析 GC pause 对主线程的影响。

**方案**:

```python
def collect_gc_events(self) -> dict:
    """Analyze GC events from atrace/dalvik slices."""
    tp = self._open()
    rows = tp.query("""
        SELECT
            name,
            ts,
            dur,
            EXTRACT_ARG(arg_set_id, 'reason') AS gc_reason,
            EXTRACT_ARG(arg_set_id, 'gc_type') AS gc_type
        FROM slice
        WHERE name GLOB '*GC*'
           OR name GLOB '*gc*'
           OR name GLOB '*GarbageCollector*'
           OR name GLOB '*ConcurrentGC*'
        ORDER BY dur DESC
        LIMIT 20
    """)
```

**数据结构**:
```python
{
    "gc_events": {
        "total_count": 15,
        "total_pause_ms": 120.0,
        "max_pause_ms": 35.0,
        "main_thread_pause_ms": 80.0,  # 影响主线程的 GC
        "events": [
            {
                "name": "GC: Wait For Concurrent",
                "ts_ns": 123456789,
                "dur_ms": 35.0,
                "gc_reason": "Alloc",
                "gc_type": "Concurrent"
            }
        ]
    }
}
```

**Deterministic Hint**: `[GC分析]` — 标记 GC pause 超过 10ms 的主线程停顿，关联 jank 帧。

---

#### A4: 文件 I/O 延迟分析 [P1]

**现状**: `collect_thread_state()` 能识别 DiskSleep 和 `folio_wait_bit_common`，但没有独立的文件 I/O 分析。

**方案**:

```python
def collect_file_io(self) -> dict:
    """Analyze file I/O latency from kernel ftrace events."""
    tp = self._open()
    # 查询 ftrace 中的文件系统事件
    rows = tp.query("""
        SELECT
            t.name AS thread_name,
            its.blocked_function,
            COUNT(*) AS occurrences,
            SUM(its.dur) / 1e6 AS total_blocked_ms,
            MAX(its.dur) / 1e6 AS max_blocked_ms
        FROM __intrinsic_thread_state its
        JOIN thread t ON its.utid = t.utid
        WHERE its.io_wait = 1
          AND its.dur > 100000  -- > 0.1ms
        GROUP BY t.name, its.blocked_function
        ORDER BY total_blocked_ms DESC
        LIMIT 15
    """)
```

**数据结构**:
```python
{
    "file_io": {
        "main_thread_io": {
            "total_blocked_ms": 150.0,
            "blocking_events": [
                {
                    "blocked_function": "folio_wait_bit_common",
                    "occurrences": 8,
                    "total_ms": 120.0,
                    "max_ms": 45.0
                }
            ]
        },
        "sp_commit": {  # SharedPreferences 同步提交检测
            "occurrences": 3,
            "total_ms": 80.0
        }
    }
}
```

**Deterministic Hint**: `[主线程IO]` — 标记主线程上的文件 I/O 阻塞事件。

---

#### A5: 内存增长趋势（时序） [P1]

**现状**: `collect_process_memory()` 只返回 avg/max，没有时序趋势。无法检测内存泄漏的渐进增长。

**方案**:

```python
def collect_memory_trend(self) -> dict:
    """Collect memory usage trend over time (time series)."""
    tp = self._open()
    rows = tp.query("""
        SELECT
            p.name AS process_name,
            c.ts,
            c.value AS rss_kb
        FROM process_counter_track pct
        JOIN counter c ON c.track_id = pct.id
        JOIN process p ON pct.upid = p.upid
        WHERE pct.name = 'mem.rss'
          AND p.name = ?
        ORDER BY c.ts ASC
    """)
    # 计算趋势: 线性回归斜率、阶段性跳跃
```

**数据结构**:
```python
{
    "memory_trend": {
        "process_name": "com.example.app",
        "samples": 50,
        "start_rss_mb": 120.0,
        "end_rss_mb": 180.0,
        "delta_mb": 60.0,
        "trend_slope_mb_per_s": 6.0,
        "anomaly": "内存持续增长，疑似泄漏",
        "jumps": [
            {"ts_ns": 123456789, "rss_mb": 150.0, "delta_mb": 30.0}
        ]
    }
}
```

**Deterministic Hint**: `[内存趋势]` — RSS 增长 > 20% 标记为异常。

---

#### A6: Binder IPC 分析 [P1]

**现状**: 只在 `BLOCKED_FN_MEANING` 中映射了 `binder_thread_read` 的含义，无独立分析。

**方案**:

```python
def collect_binder_latency(self) -> dict:
    """Analyze Binder IPC latency."""
    tp = self._open()
    rows = tp.query("""
        SELECT
            t.name AS thread_name,
            COUNT(*) AS binder_waits,
            SUM(its.dur) / 1e6 AS total_wait_ms,
            MAX(its.dur) / 1e6 AS max_wait_ms
        FROM __intrinsic_thread_state its
        JOIN thread t ON its.utid = t.utid
        WHERE its.blocked_function = 'binder_thread_read'
          AND its.dur > 100000
        GROUP BY t.name
        ORDER BY total_wait_ms DESC
        LIMIT 10
    """)
```

**数据结构**:
```python
{
    "binder_ipc": {
        "main_thread_waits": {
            "count": 12,
            "total_ms": 180.0,
            "max_ms": 35.0
        }
    }
}
```

---

#### A7: 网络请求分析 [P2]

**现状**: 只有 `SI$net#` hook 切片，依赖应用层打点。

**方案**: 从 `slice` 表提取 OkHttp/HttpURLConnection 相关系统切片。

```python
def collect_network_slices(self) -> dict:
    """Analyze network-related system slices."""
    tp = self._open()
    rows = tp.query("""
        SELECT name, ts, dur
        FROM slice
        WHERE name GLOB '*OkHttp*'
           OR name GLOB '*HttpEngine*'
           OR name GLOB '*network*'
        ORDER BY dur DESC
        LIMIT 20
    """)
```

**注意**: 此项依赖 atrace tag 配置，可能不是所有设备都有数据。优先级 P2。

---

#### A8: 电源/Wakelock 分析 [P2]

**方案**:

```python
def collect_wakelock(self) -> dict:
    """Analyze wakelock holds from slice data."""
    tp = self._open()
    rows = tp.query("""
        SELECT
            name,
            ts,
            dur / 1e6 AS dur_ms,
            EXTRACT_ARG(arg_set_id, 'tag') AS wl_tag
        FROM slice
        WHERE name GLOB '*WakeLock*'
        ORDER BY dur DESC
        LIMIT 20
    """)
```

**数据结构**:
```python
{
    "wakelock": {
        "total_holds": 15,
        "total_ms": 5000.0,
        "long_holds": [
            {"name": "WakeLock acquire", "dur_ms": 3000.0, "tag": "MyService"}
        ]
    }
}
```

---

#### A9: SharedPreferences 分析 [P2]

**方案**: 从 `__intrinsic_thread_state` 中识别主线程 `futex_wait` + `commit` 关联的 SharedPreferences 同步写入。

```python
def collect_sp_commits(self) -> dict:
    """Detect main-thread SharedPreferences commit blocks."""
    tp = self._open()
    # 搜索 SharedPreferencesImpl.commit 相关 slice
    rows = tp.query("""
        SELECT name, ts, dur / 1e6 AS dur_ms
        FROM slice
        WHERE name GLOB '*SharedPreferences*'
           OR name GLOB '*sp_commit*'
        ORDER BY dur DESC
        LIMIT 10
    """)
```

---

#### A10: CPU 频率降频检测 [P1]

**现状**: `collect_sys_stats()` 已采集 `cpu_freq_by_core`，但没有分析逻辑。

**方案**: 添加 deterministic hint，检测 CPU 频率是否降至最低运行频率（thermal throttling）。

```python
def _detect_cpu_throttling(self, data: dict) -> str:
    """Detect CPU thermal throttling from frequency data."""
    sys_stats = data.get("sys_stats") or {}
    freq_by_core = sys_stats.get("cpu_freq_by_core", {})
    # 检查是否有核心长时间运行在最低频率
    # ...
```

**Deterministic Hint**: `[CPU降频]` — 标记降频超过 50% 的核心和持续时间。

---

#### A11: 线程等待链分析 [P2]

**方案**: 利用 `__intrinsic_thread_state.waker_utid` 构建线程唤醒链，找到阻塞根因。

```python
def collect_wait_chain(self) -> dict:
    """Build thread wait chain from waker_utid."""
    # Thread A 等待 Thread B (waker_utid) → Thread B 等待 Thread C → ...
    # 找到链末端（阻塞根因）
```

---

### 1.4 优先级排序与实施路径

```
Phase 1 (P0 — 核心缺失能力):
├── A1: CPU 调度延迟     → 新增 collect_sched_latency()
├── A2: 锁竞争分析       → 新增 collect_lock_contention()
└── A3: GC 事件分析      → 新增 collect_gc_events()

Phase 2 (P1 — 高价值增强):
├── A4: 文件 I/O 延迟    → 新增 collect_file_io()
├── A5: 内存增长趋势     → 增强 collect_process_memory()
├── A6: Binder IPC 分析  → 新增 collect_binder_latency()
└── A10: CPU 降频检测    → 新增 _detect_cpu_throttling()

Phase 3 (P2 — 扩展能力):
├── A7: 网络请求分析     → 新增 collect_network_slices()
├── A8: Wakelock 分析    → 新增 collect_wakelock()
├── A9: SP 分析          → 新增 collect_sp_commits()
└── A11: 等待链分析      → 新增 collect_wait_chain()
```

---

## Part 2: Prompt Skill 化改造

### 2.1 现有 Prompt 文件分析

| 文件 | 行数 | 内容分类 | 知识内容（可提取） | 运行时指令（保留） |
|------|------|---------|-----------------|-----------------|
| `attributor.txt` | 105 | 源码归因 | SI$ 前缀格式说明、Java/Kotlin 搜索策略、Layout XML 搜索策略 | 输出格式 RESULT: 行、约束（40行/3文件限制）、工作流程步骤 |
| `report-generator.txt` | 55 | 报告生成 | 线程状态分析解读知识（blocked_function 含义）、问题分级标准 | 输出规则（不重复头部）、问题格式模板、约束 |
| `perf-analyzer.txt` | 79 | 性能分析 | 数据解读知识（scheduling/cpu_hotspots/frame_timeline/view_slices/memory 各字段含义） | 工具描述、输出 JSON 格式、约束 |
| `frame-analyzer.txt` | 66 | 帧分析 | SI$ 前缀格式（重复）、分析步骤描述 | 输出格式模板、约束 |
| `android-expert.txt` | 8 | Android 采集 | 核心能力描述、工具说明 | 采集流程、关键规则 |
| `code-explorer.txt` | 53 | 代码探索 | 搜索策略表格（问题类型→搜索关键词） | 工作流程、输出格式 |
| `metric-qa.txt` | 11 | 指标问答 | 无（纯模板） | 回答要求 |
| `compaction.txt` | 50 | 上下文压缩 | 必须保留/可丢弃列表 | 输出格式 |
| `monkey-driver.txt` | 80 | Monkey 测试 | 场景翻译规则表、操作类型表 | 输出 JSON 格式、约束 |
| `reference-hdc-commands.txt` | 124 | 参考文档 | **纯知识** — HarmonyOS 命令参考 | 无 |
| `reference-opencode-prompts.txt` | 176 | 参考文档 | **纯知识** — OpenCode prompt 设计参考 | 无 |

### 2.2 知识 vs 指令分离原则

```
知识 (Knowledge) → 可提取为 skill reference:
  - SI$ 标签系统格式定义
  - Perfetto SQL 表结构 / 查询模式
  - Android 性能分析领域知识（blocked_function 含义、GC 类型等）
  - 搜索策略（Java/Kotlin 文件搜索策略）
  - 问题分类标准（P0/P1/P2 定义）
  - 数据解读指南（各 metric 字段含义）
  - 命令参考（hdc 命令、Perfetto 命令）

指令 (Instructions) → 保留在 prompt 中:
  - 角色/身份定义（"你是一个..."）
  - 输出格式模板
  - 工作流程步骤
  - 约束条件（字数、文件数限制等）
  - 工具调用规则
  - 格式化要求
```

### 2.3 Skill 目录结构设计

```
prompts/
├── skills/                          # 知识性内容（索引式按需加载）
│   ├── SKILL.md                     # Skill 索引文件
│   ├── references/                  # 参考文档
│   │   ├── si-tag-system.md         # SI$ 标签格式定义
│   │   ├── android-perf-knowledge.md  # Android 性能分析领域知识
│   │   │                             (blocked_function、GC、锁竞争等)
│   │   ├── perfetto-sql-tables.md   # Perfetto 关键表结构和查询模式
│   │   ├── search-strategy.md       # Java/Kotlin/布局搜索策略
│   │   ├── severity-classification.md  # P0/P1/P2 分级标准
│   │   ├── metric-field-guide.md    # 各 metric 字段解读指南
│   │   ├── hdc-commands.md          # HarmonyOS hdc 命令参考
│   │   └── opencode-design.md       # OpenCode prompt 设计参考
│   └── templates/                   # 输出格式模板（按需加载）
│       ├── attribution-result.txt   # RESULT: 行格式
│       ├── report-problem.txt       # 报告问题格式
│       └── frame-analysis.txt       # 帧分析输出格式
│
├── attributor.txt                   # 精简后：纯指令
├── report-generator.txt             # 精简后：纯指令
├── perf-analyzer.txt                # 精简后：纯指令
├── frame-analyzer.txt               # 精简后：纯指令
├── android-expert.txt               # 精简后：纯指令
├── code-explorer.txt                # 精简后：纯指令
├── metric-qa.txt                    # 保持不变（已是纯模板）
├── compaction.txt                   # 保持不变
└── monkey-driver.txt                # 精简后：纯指令
```

### 2.4 SKILL.md 索引文件设计

```markdown
# SmartInspector Knowledge Base

## 按需加载机制
每个 agent prompt 可通过 `load_skill_reference(name)` 加载所需知识。
知识文件仅在需要时加载，不占用基础 prompt token。

## 可用 References

| Reference | 描述 | 使用场景 |
|-----------|------|---------|
| `si-tag-system` | SI$ 标签格式定义和解析规则 | attributor, perf-analyzer, frame-analyzer |
| `android-perf-knowledge` | Android 性能分析领域知识 | report-generator, perf-analyzer |
| `perfetto-sql-tables` | Perfetto SQL 表结构和查询模式 | collector 新增方法开发 |
| `search-strategy` | Java/Kotlin/XML 源码搜索策略 | attributor, code-explorer |
| `severity-classification` | P0/P1/P2 分级标准和帧预算计算 | report-generator |
| `metric-field-guide` | 各 metric JSON 字段含义解读 | perf-analyzer, metric-qa |
| `hdc-commands` | HarmonyOS hdc 命令参考 | android-expert (HarmonyOS) |
```

### 2.5 Skill Loader 代码改造方案

#### 当前 `prompts.py`:

```python
def load_prompt(name: str) -> str:
    path = _PROMPTS_DIR / f"{name}.txt"
    return path.read_text(encoding="utf-8")
```

#### 改造后 `prompts.py`:

```python
"""Load prompt instructions and skill references on demand."""

from pathlib import Path

_PROMPTS_DIR = Path(__file__).resolve().parent.parent.parent / "prompts"
_SKILLS_DIR = _PROMPTS_DIR / "skills"
_REFERENCES_DIR = _SKILLS_DIR / "references"

# Skill reference 缓存（懒加载，加载后缓存）
_reference_cache: dict[str, str] = {}


def load_prompt(name: str) -> str:
    """Load a prompt instruction file (prompts/{name}.txt).

    Args:
        name: Prompt file name without .txt extension.

    Returns:
        The prompt text content.
    """
    path = _PROMPTS_DIR / f"{name}.txt"
    return path.read_text(encoding="utf-8")


def load_skill_reference(name: str) -> str:
    """Load a skill reference file (prompts/skills/references/{name}.md).

    Skill references are knowledge documents loaded on demand by agents.
    They are cached after first load.

    Args:
        name: Reference file name without .md extension,
              e.g. "si-tag-system", "android-perf-knowledge".

    Returns:
        The reference text content, or empty string if not found.
    """
    if name in _reference_cache:
        return _reference_cache[name]

    path = _REFERENCES_DIR / f"{name}.md"
    if not path.exists():
        return ""

    content = path.read_text(encoding="utf-8")
    _reference_cache[name] = content
    return content


def load_prompt_with_skills(name: str, *skill_names: str) -> str:
    """Load a prompt file and append selected skill references.

    This is the primary API for agent initialization:
    - Loads the instruction prompt (always)
    - Appends requested skill references (on demand)
    - Separates instruction from knowledge in the final prompt

    Args:
        name: Prompt instruction file name.
        *skill_names: Skill reference names to append.

    Returns:
        Combined prompt text with instructions + knowledge sections.
    """
    parts = [load_prompt(name)]

    for skill_name in skill_names:
        ref = load_skill_reference(skill_name)
        if ref:
            parts.append(f"\n\n# Reference: {skill_name}\n\n{ref}")

    return "\n".join(parts)
```

### 2.6 各 Agent 适配方案

#### attributor agent

**当前**:
```python
_prompt = load_prompt("attributor")  # 105 行，含 SI$ 格式 + 搜索策略
```

**改造后**:
```python
# prompts/attributor.txt 精简到 ~60 行（纯指令）
# SI$ 格式 + 搜索策略提取到 references/si-tag-system.md + references/search-strategy.md
_prompt = load_prompt_with_skills("attributor", "si-tag-system", "search-strategy")
```

**Token 节省**: 当 attributor 被重复调用时，SI$ 格式可从缓存获取（~0 token 成本）。
但实际 attributor 只调用一次，所以 token 节省主要来自：搜索策略可按需加载（如检测到 Kotlin 代码才加载 Kotlin 搜索策略部分）。

#### report-generator agent

**当前**:
```python
# report-generator.txt 内联了 blocked_function 含义解读
```

**改造后**:
```python
# blocked_function 含义 → references/android-perf-knowledge.md
# P0/P1/P2 标准 → references/severity-classification.md
_prompt = load_prompt_with_skills("report-generator", "android-perf-knowledge")
```

#### perf-analyzer agent

**当前**:
```python
# perf-analyzer.txt 内联了各 metric 字段解读（~30 行知识）
```

**改造后**:
```python
# 字段解读 → references/metric-field-guide.md
_prompt = load_prompt_with_skills("perf-analyzer", "metric-field-guide", "si-tag-system")
```

#### frame-analyzer agent

**当前**:
```python
# frame-analyzer.txt 重复了 SI$ 格式（~10 行）
```

**改造后**:
```python
# SI$ 格式从 references/si-tag-system.md 按需加载
_prompt = load_prompt_with_skills("frame-analyzer", "si-tag-system")
```

#### metric-qa agent

**当前**: 已是纯模板（11 行），无需改造。

#### android-expert agent

**当前**: 已很精简（8 行），无需改造。但 HarmonyOS 支持时需要 `references/hdc-commands.md`。

### 2.7 Before/After Token 节省估算

#### 各文件 Token 估算

| 文件 | Before (行) | 知识行数 | After 指令行数 | 节省行数 | 节省 Token |
|------|-----------|---------|-------------|---------|-----------|
| `attributor.txt` | 105 | ~45 | 60 | 45 | ~600 |
| `report-generator.txt` | 55 | ~15 | 40 | 15 | ~200 |
| `perf-analyzer.txt` | 79 | ~35 | 44 | 35 | ~450 |
| `frame-analyzer.txt` | 66 | ~15 | 51 | 15 | ~200 |
| `code-explorer.txt` | 53 | ~10 | 43 | 10 | ~130 |
| **合计** | **358** | **~120** | **238** | **120** | **~1580** |

#### 关键收益

1. **去重**: SI$ 标签格式在 4 个文件中重复出现（attributor, perf-analyzer, frame-analyzer, report-generator），提取为单一 reference 后只需加载一份。

2. **按需加载**: 不是每个 agent 都需要全部知识。例如 attributor 不需要 metric 字段解读，frame-analyzer 不需要搜索策略。

3. **可扩展性**: 新增分析能力（如 GC 分析）只需添加 `references/android-perf-knowledge.md` 中的新 section，不需要修改每个 prompt 文件。

4. **维护性**: 知识更新只需改 reference 文件，不影响 prompt 指令逻辑。

---

## Part 3: 实施计划

### Phase 1: P0 分析能力补充

**目标**: 新增 CPU 调度延迟、锁竞争、GC 事件分析

**文件改动清单**:

| 文件 | 改动 |
|------|------|
| `src/smartinspector/collector/perfetto.py` | 新增 `collect_sched_latency()`, `collect_lock_contention()`, `collect_gc_events()` |
| `src/smartinspector/collector/perfetto.py` | `PerfSummary` 新增字段 `sched_latency`, `lock_contention`, `gc_events` |
| `src/smartinspector/collector/perfetto.py` | `summarize()` 中调用新方法 |
| `src/smartinspector/agents/deterministic.py` | 新增 `_analyze_sched_latency()`, `_analyze_lock_contention()`, `_analyze_gc_events()` |
| `src/smartinspector/agents/deterministic.py` | `compute_hints()` 中调用新 helper |
| `src/smartinspector/graph/nodes/reporter/formatter.py` | `format_perf_sections()` 新增调度延迟/锁竞争/GC section |
| `src/smartinspector/graph/nodes/metric_qa.py` | `METRIC_DATA_MAP` 新增 `sched_latency`, `lock_contention`, `gc` |
| `tests/test_deterministic.py` | 新增对应测试 |
| `prompts/report-generator.txt` | 新增线程状态解读知识更新 |
| `CLAUDE.md` | 更新 Collector 分析方法表和 Metric QA 指标表 |

### Phase 2: P1 分析能力补充

**目标**: 文件 I/O 延迟、内存增长趋势、Binder IPC、CPU 降频检测

**文件改动清单**:

| 文件 | 改动 |
|------|------|
| `src/smartinspector/collector/perfetto.py` | 新增 `collect_file_io()`, `collect_binder_latency()` |
| `src/smartinspector/collector/perfetto.py` | 增强 `collect_process_memory()` 返回时序数据 |
| `src/smartinspector/collector/perfetto.py` | 增强 `collect_sys_stats()` 分析降频 |
| `src/smartinspector/agents/deterministic.py` | 新增对应 helper |
| `src/smartinspector/graph/nodes/reporter/formatter.py` | 新增 section |
| `src/smartinspector/graph/nodes/metric_qa.py` | 新增 metric ID |

### Phase 3: Prompt Skill 化改造

**目标**: 建立 skill reference 体系，精简 prompt 文件

**文件改动清单**:

| 文件 | 改动 |
|------|------|
| `src/smartinspector/prompts.py` | 新增 `load_skill_reference()`, `load_prompt_with_skills()` |
| `prompts/skills/SKILL.md` | 新建：索引文件 |
| `prompts/skills/references/si-tag-system.md` | 新建：从 attributor/perf-analyzer/frame-analyzer 提取 |
| `prompts/skills/references/android-perf-knowledge.md` | 新建：从 report-generator 提取 + 新增 GC/锁竞争知识 |
| `prompts/skills/references/perfetto-sql-tables.md` | 新建：从 perfetto-sql skill 提取 |
| `prompts/skills/references/search-strategy.md` | 新建：从 attributor 提取 |
| `prompts/skills/references/severity-classification.md` | 新建：从 deterministic 提取 |
| `prompts/skills/references/metric-field-guide.md` | 新建：从 perf-analyzer 提取 |
| `prompts/skills/references/hdc-commands.md` | 从 `reference-hdc-commands.txt` 迁移 |
| `prompts/attributor.txt` | 精简：移除 SI$ 格式和搜索策略（→ references） |
| `prompts/perf-analyzer.txt` | 精简：移除字段解读（→ references） |
| `prompts/frame-analyzer.txt` | 精简：移除 SI$ 格式（→ references） |
| `prompts/report-generator.txt` | 精简：移除 blocked_function 含义（→ references） |
| `prompts/code-explorer.txt` | 精简：移除搜索策略表格（→ references） |
| Agent 文件 | 适配 `load_prompt_with_skills()` 调用 |

### Phase 4: P2 扩展能力

**目标**: 网络、Wakelock、SP、等待链分析

**改动与 Phase 1/2 类似，按需扩展。**

---

## 附录: 关键设计决策

### A1: 为什么不直接使用 Perfetto SQL skill 替换当前 Collector？

Perfetto SQL skill 的设计目标是**交互式 SQL 查询**（用户提问 → 生成 SQL → 执行），而 SI 的 Collector 是**批量预计算**（一次 trace → 14+ SQL 查询 → 结构化 JSON）。两者职责不同：

- Collector: 固定 SQL 查询，结果结构化，用于 LLM 消费
- SQL Skill: 动态 SQL 生成，用于深入探索特定问题

**建议**: Collector 保持当前架构，新增方法时参考 SQL skill 中的标准库模块（如 `sched.runnable`、`intervals.overlap`）提升查询准确性。

### A2: Skill reference 缓存策略

- 首次加载后缓存到内存（`_reference_cache`）
- 不设过期策略（知识性内容不频繁变化）
- 支持开发模式热重载（`SI_DEBUG=1` 时每次从文件读取）

### A3: 与 Deterministic Hint 的关系

新增分析能力应遵循已有模式：
1. Collector 方法 → SQL 查询 → 结构化 JSON
2. Deterministic helper → 纯 Python 计算 → 中文文本 hint
3. Formatter → 格式化 markdown section
4. LLM → 组织语言 + 因果分析

新增的分析维度（调度延迟、锁竞争、GC）都应该在 Step 2 中有对应的 deterministic hint，避免让 LLM 做算术。

### A4: `__intrinsic_thread_state` 依赖

A1、A2、A4、A6 四个新分析都依赖 `__intrinsic_thread_state` 表。该表在较新版本的 Perfetto 中可用，但旧版本 trace 不支持。**所有新方法都应有 fallback 到 `thread_state` / `sched_blocked_reason` 的降级路径**。
