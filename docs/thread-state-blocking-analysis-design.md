# thread_state 阻塞原因分析 — 改造设计文档

> 生成日期：2026-04-23
> 状态：方案设计，待实施

---

## 一、问题背景

### 1.1 当前 thread_state 的局限

`collect_thread_state` 目前基于 `sched` 表手动推算线程状态分布：

1. 计算 `sched` 表中与 slice 窗口重叠的 Running 时间
2. 用 `slice_duration - running_time` 推算 blocked 时间
3. 用最近的 `sched.end_state` 分类为 Sleeping 或 DiskSleep

**问题**：只输出 `{Running: 100%}` 或 `{Sleeping: 100%}`，无法回答「为什么阻塞」。

### 1.2 用户反馈

> "thread_state 的作用是什么，我从报告中没有感受到"

测试 trace 中所有 SI$ 慢切片都是 CPU 密集型（100% Running），thread_state 的结论与源码归因结论完全重复。即使遇到真正的阻塞切片，当前实现也只能给出笼统的 "Sleeping" 标签，无法提供可操作的优化建议。

---

## 二、Perfetto 中可用的阻塞详情数据

通过实际查询 trace 文件，确认 `__intrinsic_thread_state` 表包含以下关键字段：

| 字段 | 类型 | 说明 | 示例 |
|------|------|------|------|
| `state` | TEXT | 线程状态 | `Running`, `S`, `D`, `R+` |
| `blocked_function` | TEXT | **内核阻塞函数名** | `folio_wait_bit_common` |
| `io_wait` | INTEGER | **是否在等待IO** | `1` = IO等待 |
| `waker_utid` | INTEGER | **唤醒者的线程ID** | 可关联 `thread.name` |
| `irq_context` | INTEGER | 是否在中断上下文被唤醒 | `0`/`1` |
| `ucpu` | INTEGER | 用户态CPU时间 | nanoseconds |

### 2.1 实际数据验证

以测试 trace（20s, com.smartinspector.hook）为例：

**blocked_function 分布（跨所有线程）：**

| blocked_function | 含义 | 出现次数 | 总耗时 |
|-----------------|------|---------|--------|
| `worker_thread` | 工作线程等待 | 11177 | 745.5s |
| `rcu_gp_fqs_loop` | RCU内核周期 | 3879 | 15.1s |
| `msleep` | 内核主动睡眠 | 6 | 10.3s |
| `sde_encoder_helper_wait_for_irq` | 等待显示硬件中断 | 1017 | 3.5s |
| `_sde_encoder_cesta_update` | 显示编码器更新 | 1372 | 3.1s |
| `spi_geni_transfer_one` | SPI总线传输（触控IC） | 489 | 633ms |
| `folio_wait_bit_common` | **等待磁盘IO页缓存** | 1130 | 282ms |
| `rpmh_write_batch` | 硬件资源电源管理 | — | — |

**waker（唤醒关系）：**

| 唤醒者 | 被唤醒者 | 次数 |
|--------|---------|------|
| Jit thread pool | Profile Saver | 49286 |
| swapper | TPP_MAIN | 10893 |
| surfaceflinger | RelBufCB | 1957 |
| tinspector.hook | logd.writer | 1794 |
| TimerDispatch | app (主线程) | 1312 |
| tinspector.hook | SI-BlockWatchdo | 1195 |

**主线程 D 状态（DiskSleep）：**
```
RenderThread: blocked_function=rpmh_write_batch (0.04ms)
  → 等待硬件资源电源管理器完成写入
```

### 2.2 核心洞察

当前 `collect_thread_state` 用 `sched` 表推算，**丢弃了 `blocked_function`、`waker_utid`、`io_wait` 三个关键字段**。改造后可以直接回答：

- **为什么阻塞？** → `folio_wait_bit_common` = 等待磁盘IO页缓存
- **阻塞了多久？** → 45ms
- **谁唤醒的？** → `binder:1801_3` (Binder IPC 调用方)
- **是IO等待吗？** → `io_wait=1` 确认

---

## 三、改造方案

### 3.1 数据层：`collect_thread_state` 改用 `__intrinsic_thread_state`

**位置**：`src/smartinspector/collector/perfetto.py` 的 `collect_thread_state()` 方法

#### 当前实现（sched 推算）

```python
# 从 sched 表手动计算 Running 时间
# blocked_time = slice_duration - running_time
# 无法获取 blocked_function, waker, io_wait
```

#### 改造后实现

```python
def collect_thread_state(self) -> list[dict]:
    tp = self._open()

    # 获取主线程 utid
    main_utid = self._resolve_main_utid(tp)
    if main_utid is None:
        return []

    # 获取 SI$ 慢切片
    slice_rows = tp.query("""
        SELECT name, ts, dur
        FROM slice
        WHERE name LIKE 'SI$%'
          AND name NOT LIKE 'SI$net#%'
          AND name NOT LIKE 'SI$db#%'
          AND dur > 1000000
        ORDER BY dur DESC
        LIMIT 20
    """)

    results = []
    for sr in slice_rows:
        slice_ts = sr.ts
        slice_end = sr.ts + sr.dur
        slice_name = sr.name
        dur_ms = round(sr.dur / 1e6, 2)

        # 查询 __intrinsic_thread_state 获取阻塞详情
        state_rows = tp.query(f"""
            SELECT
                state,
                SUM(dur) AS total_ns,
                blocked_function,
                io_wait,
                waker_utid,
                CASE WHEN waker_utid IS NOT NULL
                     THEN (SELECT name FROM thread WHERE utid = waker_utid)
                     ELSE NULL END AS waker_name
            FROM __intrinsic_thread_state
            WHERE utid = {main_utid}
              AND ts < {slice_end}
              AND ts + dur > {slice_ts}
            GROUP BY state, blocked_function, io_wait, waker_utid
            ORDER BY total_ns DESC
        """)

        # 如果没有 thread_state 覆盖，推断为 Running
        # （sleeping 线程无法执行产生 slice 的代码）
        state_entries = list(state_rows)
        if not state_entries:
            results.append({
                "slice_name": slice_name,
                "dur_ms": dur_ms,
                "state_distribution": {"Running": 100.0},
                "dominant_state": "Running",
                "blocked_function": None,
                "io_wait": False,
                "waker_name": None,
            })
            continue

        total_ns = sum(r.total_ns for r in state_entries)
        pct_dist = {}
        blocked_fn = None
        io_wait = False
        waker_name = None

        for r in state_entries:
            # 映射状态名
            state_label = _map_state_label(r.state)
            pct = round(r.total_ns / total_ns * 100, 1)
            pct_dist[state_label] = pct_dist.get(state_label, 0) + pct

            # 记录第一个非 Running 状态的阻塞详情
            if state_label != "Running" and blocked_fn is None:
                blocked_fn = r.blocked_function
                io_wait = bool(r.io_wait)
                waker_name = r.waker_name

        dominant = max(pct_dist, key=pct_dist.get)
        results.append({
            "slice_name": slice_name,
            "dur_ms": dur_ms,
            "state_distribution": pct_dist,
            "dominant_state": dominant,
            "blocked_function": blocked_fn,
            "io_wait": io_wait,
            "waker_name": waker_name,
        })

    return results
```

#### 状态映射函数

```python
def _map_state_label(raw_state: str) -> str:
    """Map kernel thread state to human-readable label."""
    mapping = {
        "Running": "Running",
        "R": "Running",
        "R+": "Running",
        "S": "Sleeping",
        "D": "DiskSleep",
        "D+": "DiskSleep",
        "T": "Stopped",
        "t": "Traced",
        "X": "Dead",
        "Z": "Zombie",
    }
    return mapping.get(raw_state, raw_state)
```

#### 新增返回字段

```python
{
    "slice_name": "SI$RV#...",
    "dur_ms": 73.0,
    "state_distribution": {"Running": 80.0, "Sleeping": 20.0},
    "dominant_state": "Running",
    # ↓ 新增字段
    "blocked_function": "futex_wait_queue_me",  # 阻塞的内核函数
    "io_wait": False,                            # 是否IO等待
    "waker_name": "WorkerThread-2",              # 谁唤醒了此线程
}
```

### 3.2 预计算层：`_analyze_thread_state` 增加阻塞原因解读

**位置**：`src/smartinspector/agents/deterministic.py` 的 `_analyze_thread_state()`

#### 改造内容

在现有 Running/Sleeping/DiskSleep 分类基础上，增加 `blocked_function` 解读：

```python
# blocked_function 到人类可读原因的映射
BLOCKED_FN_MEANING = {
    "futex_wait_queue_me": "等待锁释放 (futex)",
    "futex_wait": "等待锁释放 (futex)",
    "folio_wait_bit_common": "等待磁盘IO (页缓存)",
    "wait_woken": "等待被唤醒",
    "msleep": "内核主动睡眠",
    "rpmh_write_batch": "等待硬件资源电源管理",
    "sde_encoder_helper_wait_for_irq": "等待显示硬件中断",
    "spi_geni_transfer_one": "等待SPI总线传输",
    "do_writepages": "等待磁盘写入",
    "journal_commit": "等待文件系统日志提交",
    "bio_wait": "等待块IO完成",
    "pipe_wait": "等待管道数据",
    "unix_stream_recvmsg": "等待Unix Socket数据",
    " binder_thread_read": "等待Binder IPC回复",
}

IO_WAIT_MEANING = {
    True: "IO等待 (磁盘/网络/设备)",
    False: "",  # 非IO等待时省略
}
```

#### 输出格式

**阻塞切片（Sleeping/DiskSleep 主导）：**

```
[线程状态分析]
  以下切片主要处于阻塞状态：

  SharedPreferencesImpl.awaitLoadedLocked (45.2ms):
    Sleeping 100% | 原因: futex_wait_queue_me (等待锁释放)
    唤醒者: main

  SQLiteDatabase.query (120.5ms):
    DiskSleep 85%, Running 15% | 原因: folio_wait_bit_common (等待磁盘IO)
    IO等待: 是 | 唤醒者: binder:1801_3
```

**Running 切片（不变，但补充「无阻塞」说明）：**

```
  以下切片主要在执行用户代码（无IO/锁阻塞）：

  DemoAdapter.onBindViewHolder (73.0ms): Running 100%
  CpuBurnWorker.startMainThreadWork (129.0ms): Running 100%
```

### 3.3 报告格式层：`format_perf_sections` 增加阻塞详情展示

**位置**：`src/smartinspector/graph/nodes/reporter/formatter.py` 的 `format_perf_sections()`

#### 改造内容

```python
thread_states = perf_data.get("thread_state", [])
if thread_states:
    ts_lines = ["## 线程状态分析\n"]
    ts_lines.append("区分\"代码慢\"(Running)和\"被阻塞\"(Sleeping/DiskSleep)：")

    # 分两组展示：先阻塞，后 Running
    blocked = [ts for ts in thread_states
               if ts.get("dominant_state") in ("Sleeping", "DiskSleep")]
    running = [ts for ts in thread_states
               if ts.get("dominant_state") == "Running"]

    if blocked:
        ts_lines.append("")
        ts_lines.append("**阻塞切片**（线程被IO/锁挂起，优化方向不是代码本身）：")
        for ts in blocked[:5]:
            short = ts["slice_name"].replace("SI$", "")
            dist_str = ", ".join(f"{k} {v:.0f}%" for k, v in ts["state_distribution"].items())
            ts_lines.append(f"- {short} ({ts['dur_ms']:.1f}ms): {dist_str}")
            # 新增：阻塞原因
            bf = ts.get("blocked_function")
            if bf:
                meaning = BLOCKED_FN_MEANING.get(bf, bf)
                ts_lines.append(f"  阻塞原因: {meaning}")
            if ts.get("io_wait"):
                ts_lines.append(f"  类型: IO等待")
            if ts.get("waker_name"):
                ts_lines.append(f"  唤醒者: {ts['waker_name']}")

    if running:
        ts_lines.append("")
        ts_lines.append("**Running切片**（代码执行慢，需优化算法或异步化）：")
        for ts in running[:5]:
            short = ts["slice_name"].replace("SI$", "")
            ts_lines.append(f"- {short} ({ts['dur_ms']:.1f}ms): Running 100%")

    user_parts.append("\n".join(ts_lines))
```

### 3.4 报告生成层：LLM Prompt 增加 blocked_function 上下文

**位置**：`src/smartinspector/graph/nodes/reporter/` 的 prompt 模板

在预计算结论 section 中，blocked_function 信息将帮助 LLM 生成**针对性的建议**，而非模板化建议：

| blocked_function | LLM 应生成的建议 |
|-----------------|----------------|
| `futex_wait_queue_me` | 「主线程在等待锁释放，检查是否有后台线程持有锁（如 SharedPreferences 同步提交）」 |
| `folio_wait_bit_common` | 「等待磁盘IO完成，考虑使用异步IO或增加缓存命中率」 |
| `binder_thread_read` | 「等待 Binder IPC 回复，考虑将同步 Binder 调用改为异步」 |
| `spi_geni_transfer_one` | 「等待SPI总线传输（通常为触控IC），属于硬件瓶颈，应用层无法优化」 |
| `NULL` + Running 100% | 「代码执行慢，需优化算法复杂度或将耗时操作移至后台线程」 |

---

## 四、涉及文件清单

| 文件 | 改动类型 | 说明 |
|------|---------|------|
| `src/smartinspector/collector/perfetto.py` | **重写** | `collect_thread_state` 改用 `__intrinsic_thread_state` 表 |
| `src/smartinspector/agents/deterministic.py` | **增强** | `_analyze_thread_state` 增加 `blocked_function` 解读和分组展示 |
| `src/smartinspector/graph/nodes/reporter/formatter.py` | **增强** | `format_perf_sections` 展示阻塞原因、IO等待、唤醒者 |
| `src/smartinspector/graph/nodes/reporter/prompts.py` | **微调** | Reporter prompt 增加阻塞原因相关的分析指引（可选） |

---

## 五、降级策略

`__intrinsic_thread_state` 表可能在某些 Perfetto 版本中不可用。需要 fallback：

```python
# 1. 先尝试 __intrinsic_thread_state
try:
    state_rows = tp.query("SELECT 1 FROM __intrinsic_thread_state LIMIT 1")
    has_intrinsic_ts = True
except:
    has_intrinsic_ts = False

# 2. 如果不可用，回退到当前 sched 推算逻辑（保持现有行为）
if not has_intrinsic_ts:
    return self._collect_thread_state_fallback()
```

---

## 六、预期效果

### 改造前报告

```
### P0 [DemoAdapter.onBindViewHolder]

现象：onBindViewHolder 耗时 73ms。

原因：同步解码 Bitmap 导致主线程阻塞。

建议：
1. 使用异步图片加载库
2. 在后台线程预解码
3. 增大缓存容量
```

→ 所有建议都是模板化的「通用优化方案」，无法区分问题类型。

### 改造后报告

**场景 A：Running 100%（代码慢）**

```
### P0 [DemoAdapter.onBindViewHolder]

现象：onBindViewHolder 耗时 73ms。
线程状态：Running 100%（纯代码执行，无IO/锁阻塞）

原因：BitmapFactory.decodeResource 同步解码 200x200 图片，在主线程执行IO密集操作。
虽然 thread_state 显示 Running，但 decodeResource 底层调用了磁盘读取，
只是因为读取速度快未触发 D 状态。建议改用异步图片加载库。

建议：
1. 使用 Coil/Glide 异步加载，主线程只负责设置 placeholder
2. 增大 bitmapCache 容量至可见 item 数量的 2 倍
```

**场景 B：Sleeping 主导（锁等待）— 改造后新增的识别能力**

```
### P0 [SharedPreferencesImpl.awaitLoadedLocked]

现象：awaitLoadedLocked 耗时 45ms。
线程状态：Sleeping 100%
阻塞原因：futex_wait_queue_me（等待锁释放）
唤醒者：SharedPreferencesImpl.writeThread

原因：主线程在 awaitLoadedLocked 中通过 futex 等待 SharedPreferences 文件加载完成。
writeThread 在后台加载 XML 文件，但主线程的 getSharedPreferences() 调用发起了同步等待。
在启动阶段，如果 SP 文件较大（>100KB），这个等待可能超过一帧（16ms）。

建议：
1. 使用 commit() 替代 apply() 避免阻塞（已废弃）
2. 将 SP 读取移到 Application.onCreate 之前（Multidex 期间并行加载）
3. 考虑迁移到 DataStore（基于 Protocol Buffer，支持异步）
```

**场景 C：DiskSleep 主导（磁盘IO）— 改造后新增的识别能力**

```
### P0 [SQLiteDatabase.query]

现象：query 耗时 120ms。
线程状态：DiskSleep 85%, Running 15%
阻塞原因：folio_wait_bit_common（等待磁盘IO页缓存）
IO等待：是
唤醒者：kworker/2:1

原因：SQL 查询触发了磁盘页缓存缺失，需要从闪存读取数据。
120ms 中 102ms 花在等待磁盘IO，仅 18ms 在执行查询逻辑。
属于磁盘IO瓶颈而非查询语句效率问题。

建议：
1. 增加 WAL 模式的 checkpoint 频率，减少读阻塞
2. 对热点表建立索引减少扫描量
3. 考虑使用 Room 的分页查询（Paging 3）避免一次加载过多数据
```

---

## 七、实施计划

| 步骤 | 内容 | 预计改动量 |
|------|------|-----------|
| 1 | `perfetto.py` 重写 `collect_thread_state`，新增 `_map_state_label` | ~80 行 |
| 2 | `deterministic.py` 增强 `_analyze_thread_state`，新增 `BLOCKED_FN_MEANING` 映射 | ~40 行 |
| 3 | `formatter.py` 增强 thread_state 展示，增加阻塞原因/IO等待/唤醒者 | ~25 行 |
| 4 | 添加 fallback 逻辑（`__intrinsic_thread_state` 不可用时回退到 sched 推算） | ~15 行 |
| 5 | 用现有 trace 验证 Running 100% 场景不变，验证降级逻辑 | 测试 |
| 6 | 构造 Sleeping/DiskSleep 测试场景验证新增数据路径 | 测试 |
