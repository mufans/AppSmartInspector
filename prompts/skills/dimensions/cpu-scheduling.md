# CPU 调度延迟分析

## 数据源

- Perfetto 标准库模块: `INCLUDE PERFETTO MODULE sched.runnable`
- 虚拟表: `sched_runnable`（由标准库模块自动构建）
- 过滤: `HAVING runnable_count > 5`，排除偶发调度事件
- 排序: `ORDER BY avg_runnable_ms DESC`，关注调度延迟最严重的线程

## 领域知识

### Linux CFS 调度器

Android 使用 Linux Completely Fair Scheduler (CFS) 作为默认 CPU 调度器。CFS 通过红黑树维护所有 runnable 线程的虚拟运行时间（vruntime），每次选择 vruntime 最小的线程投入运行。

线程从 sleeping/blocked 状态唤醒后进入 runnable 队列，等待 CFS 分配 CPU 时间。这段时间就是**调度延迟**（scheduling latency），即从线程变为 runnable 到实际获得 CPU 的时间差。

### 影响调度延迟的因素

| 因素 | 影响程度 | 说明 |
|------|---------|------|
| CPU 核心数量 | 高 | 8 核设备调度延迟远低于 4 核 |
| runnable 线程数量 | 高 | 线程越多，等待时间越长 |
| 线程优先级 (nice) | 中 | 低 nice 值（高优先级）线程更早获得 CPU |
| cgroup 分配 | 中 | 前台进程组优先于后台进程组 |
| CPU 频率 | 低 | 低频时每个线程执行慢，间接延长调度等待 |
| 大小核架构 | 高 | 小核调度延迟通常更高（竞争更激烈）|

### Android 线程调度特性

| 线程类型 | 默认优先级 | 调度行为 |
|---------|-----------|---------|
| main (UI) | THREAD_PRIORITY_DEFAULT (0) | 前台 cgroup，正常调度 |
| RenderThread | THREAD_PRIORITY_DISPLAY (-4) | 前台 cgroup，高于普通线程 |
| Binder 线程 | THREAD_PRIORITY_DEFAULT | 按需创建，最多 16 个 |
| AsyncTask 线程 | THREAD_PRIORITY_BACKGROUND (10) | 后台 cgroup，被抑制 |
| Executors 线程 | 取决于配置 | 默认 THREAD_PRIORITY_DEFAULT |
| kernel 线程 | 各不相同 | 不受 cgroup 控制 |

### 大小核（big.LITTLE）架构影响

现代 ARM 处理器通常采用大小核设计：
- **大核**: 高性能、高频率，适合 UI 线程和计算密集型任务
- **小核**: 低功耗、低频率，适合后台任务

当 CPU 被小核调度时，即使调度延迟不高，执行时间也会显著增加。Android 的 HMP（Heterogeneous Multi-Processing）调度器会根据负载动态迁移线程。

## 数据解读

### Metric 字段

```
sched_latency.threads[]:
  thread_name        # 线程名
  runnable_count     # runnable 次数（被唤醒后等待 CPU 的次数）
  avg_runnable_ms    # 平均调度延迟 ms（核心指标）
  max_runnable_ms    # 最大调度延迟 ms（最差情况）

sched_latency.summary:
  total_threads      # 有调度延迟数据的线程总数
  worst_thread       # 调度延迟最严重的线程名
```

### 分析决策树

```
1. 检查 main 线程的调度延迟
   ├─ avg > 帧预算 50% → P0，UI 线程被严重延迟
   ├─ avg > 4ms → P1，调度延迟明显
   └─ max > 帧预算 → 即使 avg 正常，单次延迟也可能导致 jank

2. 检查 RenderThread 调度延迟
   ├─ avg > 4ms → 渲染管线被延迟，帧提交延迟
   └─ max > 16ms → 可能导致双缓冲翻转延迟

3. 检查全局调度压力
   ├─ total_threads > 50 → 线程数过多，调度器压力大
   └─ 多个线程 avg > 4ms → 系统 CPU 资源不足

4. 检查 runnable_count 异常
   ├─ main 线程 runnable_count > 500 → 频繁休眠/唤醒，检查是否有不必要的锁/IO
   └─ 后台线程 runnable_count > 1000 → 后台线程过于活跃
```

### 关键指标组合解读

| 模式 | 含义 | 优化方向 |
|------|------|---------|
| main avg 高, max 正常 | 持续轻度调度压力 | 减少后台线程 CPU 占用 |
| main avg 正常, max 极高 | 偶发严重调度延迟 | 检查是否有 CPU 突发竞争 |
| 所有线程 avg 都高 | 系统 CPU 过载 | 减少总线程数和 CPU 负载 |
| 特定线程 max > 100ms | 极端调度延迟 | 可能被后台 cgroup 抑制 |
| RenderThread 延迟高 | 渲染管线瓶颈 | 减少 GPU 等待、降低渲染复杂度 |

### cgroup 与线程调度

Android 使用 cgroup v1/v2 管理进程组调度优先级：

| cgroup | nice 范围 | CPU 份额 | 适用场景 |
|--------|----------|---------|---------|
| `/dev/cggrp/cpu/` (foreground) | -20 ~ 0 | 高 | 前台 Activity |
| `/dev/cggrp/cpu/bg_non_interactive` | 0 ~ 19 | 低 | 后台进程 |
| `sys_bg` (system background) | 0 ~ 19 | 极低 | 系统后台服务 |

当应用从前台切到后台时，所有线程被迁移到 `bg_non_interactive` cgroup，调度权重大幅降低。这解释了为什么后台线程的调度延迟（avg_runnable_ms）可能极高。

### sched_wakeup 与调度延迟的因果关系

调度延迟的完整因果链：

```
线程 A 持有锁 → 释放锁 → futex_wake 唤醒线程 B
                                    ↓
                          线程 B 进入 runnable 状态
                                    ↓
                          CFS 选择线程 B 投入运行（调度延迟）
                                    ↓
                          线程 B 获取锁并继续执行
```

调度延迟不是孤立问题，它会沿着因果链放大：调度延迟 → 锁持有时间延长 → 下游等待线程阻塞时间延长。因此，优化调度延迟对整个锁竞争链都有改善效果。

## Perfetto SQL 深入查询

### 调度延迟与 jank 帧关联

```sql
-- 查找 main 线程调度延迟与 jank 帧的时间重叠
INCLUDE PERFETTO MODULE sched.runnable;

SELECT
  sr.thread_name,
  sr.ts,
  sr.runnable_dur / 1e6 AS runnable_ms,
  frame.name AS frame_slice,
  frame.dur / 1e6 AS frame_ms
FROM sched_runnable sr
JOIN slice frame ON frame.track_id IN (
  SELECT id FROM thread_track WHERE utid = (SELECT utid FROM thread WHERE name = 'main'))
WHERE sr.thread_name = 'main'
  AND sr.runnable_dur > 4e6
  AND frame.ts < sr.ts + sr.runnable_dur AND frame.ts + frame.dur > sr.ts
  AND frame.dur > 16e6
ORDER BY runnable_ms DESC LIMIT 10;
```

### 线程 runnable 次数与 CPU 使用率对比

```sql
-- 找出 runnable 次数高但 CPU 时间少的线程（调度效率低）
INCLUDE PERFETTO MODULE sched.runnable;

SELECT
  sr.thread_name,
  sr.runnable_count,
  sr.avg_runnable_dur / 1e6 AS avg_runnable_ms,
  CAST(sc.cpu_time_ms AS FLOAT) / sr.runnable_count AS cpu_per_wake_ms
FROM (
  SELECT thread_name, COUNT(*) AS runnable_count,
         AVG(runnable_dur) AS avg_runnable_dur
  FROM sched_runnable
  GROUP BY thread_name
) sr
LEFT JOIN (
  SELECT thread.name AS thread_name, SUM(dur) / 1e6 AS cpu_time_ms
  FROM sched JOIN thread USING (utid)
  GROUP BY thread_name
) sc ON sr.thread_name = sc.thread_name
WHERE sr.runnable_count > 10
ORDER BY avg_runnable_ms DESC LIMIT 15;
```

## 严重度标准

- **P0**: 平均调度延迟 > 帧预算的 50% (>8.33ms@60Hz, >4.17ms@120Hz)
- **P1**: 平均调度延迟 > 4ms
- **P2**: 最大调度延迟 > 8ms

## 常见误判

| 数据现象 | 易误判为 | 实际可能是 |
|---------|---------|-----------|
| main 线程 runnable_count 很高 | 调度问题严重 | 可能是正常的事件驱动模式（input → doFrame → sleep 循环），每次唤醒都有 runnable 等待 |
| avg_runnable_ms > 4ms 但帧率正常 | 需要优化调度 | 如果 jank_count = 0，说明调度延迟没有实际影响帧率，优先级不高 |
| 后台线程 max_runnable_ms > 100ms | 系统调度异常 | 后台 cgroup 正常行为，系统优先保障前台进程 |
| RenderThread runnable_ms 高 | 渲染瓶颈 | RenderThread 可能被 GPU 等待阻塞（dequeueBuffer），不是调度问题 |

## 与其他维度的关联

| 关联维度 | 关联模式 | 分析方法 |
|---------|---------|---------|
| CPU 降频 (cpu-throttling) | 降频延长每个线程的执行时间，间接增加调度延迟 | 低 CPU 频率 + 高调度延迟 |
| 锁竞争 (lock-contention) | 持锁线程被调度延迟拖慢，锁等待时间更长 | 持锁线程 runnable 时间长 + 其他线程 futex 等待长 |
| 帧时间线 (ui-jank) | main/RenderThread 调度延迟直接导致 jank | 调度延迟峰值时间戳与 jank 帧时间戳对齐 |
| Binder IPC (binder-ipc) | binder 线程调度延迟导致 IPC 响应变慢 | binder 线程的 avg_runnable_ms 偏高 |
| 文件 IO (io-analysis) | IO 完成后线程唤醒，调度延迟延长 IO 总时间 | IO 阻塞结束后的 runnable 等待时间 |

## 优化方向

### 减少调度压力

1. **精简线程数**: 合并功能相近的后台线程，使用共享线程池
2. **使用协程**: Kotlin 协程比线程更轻量，协程挂起不占用 CPU 调度资源
3. **WorkManager**: 后台任务使用 WorkManager 而非常驻线程

### 优先级调整

1. **UI 线程优先级**: 保持 `THREAD_PRIORITY_DEFAULT`，避免人为降低
2. **后台线程降级**: `Process.setThreadPriority(Process.THREAD_PRIORITY_BACKGROUND)` 确保不抢占 UI
3. **关键线程提升**: 对音频、实时渲染等线程使用 `THREAD_PRIORITY_URGENT_DISPLAY`

### 绑核与亲和性

1. **UI 线程绑定大核**: 通过 `sched_setaffinity` 将 UI 线程绑定到高性能核心
2. **后台线程限制小核**: 后台任务限制在小核运行，避免干扰 UI
3. **Android S+ 的性能提示**: 使用 `Window.setSustainedPerformanceMode()` 告知系统保持性能

### 典型调度优化模式

```kotlin
// 反模式: 大量线程池
val pool1 = Executors.newFixedThreadPool(4)   // 网络
val pool2 = Executors.newFixedThreadPool(4)   // 数据库
val pool3 = Executors.newFixedThreadPool(4)   // 图片
// 12 个线程 + main + RenderThread + Binder = 大量 runnable 线程

// 推荐: 共享线程池 + 协程
val ioScope = CoroutineScope(Dispatchers.IO)  // 共享 IO 线程池
// 网络和数据库操作使用同一 IO 调度器
```

## PELT 信号与负载追踪

Linux 使用 Per-Entity Load Tracking (PELT) 在任务级别和 CPU 级别追踪负载。PELT 为每个 sched_entity 维护一组衰减信号（runnable、running、util），调度器据此做出负载均衡和频率调节决策。

### PELT 核心机制

- 指数衰减公式：`load = load * y + new_load`，其中 y ≈ 0.981（半衰期约 32ms）
- 这意味着一个线程如果近期是 CPU 密集型的，即使短暂进入 idle 状态，仍会获得优先调度
- CFS 使用 PELT 信号进行跨核负载均衡和频率调节（schedutil governor）决策
- PELT 的 runnable 信号反映线程等待 CPU 的"权重"，running 信号反映实际占用 CPU 的"权重"

### 分析应用

- 如果线程 `runnable_count` 高但 `avg_runnable_ms` 低，可能是 PELT 负载高的线程频繁被定时器唤醒执行短任务（timer-driven work）
- `sched_runnable` 表显示的是 runnable→running 的时间，但 PELT load 决定了 CFS vruntime 推进速率——PELT 负载高的线程 vruntime 增长更快，会被更早调度
- 对于 UI 线程：如果 doFrame 期间 PELT load 突然升高（前几帧有大量计算），后续帧的调度延迟会降低，因为 CFS 会优先调度 PELT 负载高的线程

### 关键结论

| 场景 | PELT 行为 | 对调度延迟的影响 |
|------|----------|---------------|
| 线程刚完成长计算 | PELT load 高 | 后续唤醒调度延迟低（优先调度） |
| 线程长期 idle | PELT load 衰减至接近 0 | 唤醒后调度延迟可能较高 |
| 后台线程频繁唤醒 | PELT load 维持中等 | 与 UI 线程竞争 CPU 时间 |
| 新创建的线程 | PELT load = 0 | 初始调度延迟较高，需积累负载 |

## CPU 容量模型

ARM big.LITTLE 架构的 CPU 核心具有不同的"容量"值（CPU capacity），范围 0-1024：

| 核心类型 | 典型容量值 | 代表 SoC |
|---------|-----------|---------|
| Cortex-X2 (超大核) | 1024 | Snapdragon 8 Gen 1 |
| Cortex-A78 (大核) | 768 | Snapdragon 888 |
| Cortex-A55 (小核) | 256 | 通用 ARM SoC |
| Cortex-A510 (小核) | 285 | Snapdragon 8 Gen 1 |

### 容量对调度延迟的影响

- 调度延迟受线程运行在哪种核心类型上影响
- 如果 UI 线程持续被调度到小核上，即使调度延迟很低，执行时间也会显著增加
- 有效延迟公式：`effective_delay = sched_latency + (execution_time * max_freq / actual_freq)`
- 交叉验证：cpu_throttling 数据展示每核频率。如果 UI 线程运行在 avg_mhz 低且 sched latency 高的核心上，两者的效果是乘法叠加的

### 关键分析模式

```
main 线程 avg_runnable_ms < 2ms 但 doFrame 仍 > 20ms
  → 可能是线程被调度到了慢速核心
  → 检查 main 线程实际运行的核心的 CPU 频率
  → 如果该核心 avg_mhz 远低于 max_mhz，说明核心性能不足
```

| 现象 | 可能原因 | 验证方法 |
|------|---------|---------|
| main 调度延迟低但帧耗时长 | 线程在小核执行 | 检查 main 线程运行的 CPU 核心频率 |
| main 调度延迟高且帧耗时长 | 小核 + CPU 过载 | 检查核心类型 + runnable 线程数量 |
| 降频期间调度延迟剧增 | 低频延长执行 → 队列堆积 | cpu_throttling 数据交叉验证 |

## 调度延迟与帧渲染流水线

帧渲染涉及 3 个调度敏感点，任何一个点的调度延迟都会导致帧延迟：

### 帧渲染调度链

```
Vsync 信号到达
  → 1. Main 线程唤醒（Choreographer callback 调度延迟）
      → doFrame 执行（measure + layout + draw commands）
        → 2. RenderThread 唤醒（flush DrawCommands 调度延迟）
            → drawFrame 执行（GPU 命令提交）
              → queueBuffer
                → 3. SurfaceFlinger 唤醒（compose + present 调度延迟）
                    → 合成并提交显示
```

### 端到端帧延迟组成

用户感知的总帧延迟 = `input_dispatch + main_runnable + doFrame + RT_runnable + drawFrame + SF_runnable + compose`

其中每个 `_runnable` 环节都是一个潜在的调度延迟瓶颈。

### 关键指标对比

- **核心指标**：jank 帧期间 main 线程 `avg_runnable_ms` vs 正常帧期间的 `avg_runnable_ms`
- 如果 jank 帧的调度延迟是正常帧的 3 倍以上，调度就是瓶颈
- RenderThread 调度延迟高通常与 GPU 繁忙有关（dequeueBuffer 等待），需结合 GPU 维度分析

### 调度延迟在渲染管线中的传播效应

```
Main runnable 延迟 +3ms
  → doFrame 延迟完成 +3ms
    → RT 唤醒延迟 +3ms（因为 doFrame 完成更晚）
      → queueBuffer 延迟 +3ms
        → SF 唤醒延迟 +3ms
          → 最终帧延迟 +12ms（远超原始 3ms 调度延迟）
```

调度延迟会在渲染管线中累积放大，一个 3ms 的调度延迟可能导致最终 12ms 的帧延迟。

## Perfetto SQL 查询模式

### UI 关键线程调度延迟统计

```sql
INCLUDE PERFETTO MODULE sched.runnable;

-- UI 关键线程的调度延迟统计
SELECT
  thread_name,
  COUNT(*) as runnable_count,
  AVG(runnable_dur) / 1e6 as avg_runnable_ms,
  MAX(runnable_dur) / 1e6 as max_runnable_ms,
  SUM(runnable_dur) / 1e6 as total_runnable_ms
FROM sched_runnable
WHERE thread_name IN ('main', 'RenderThread', 'mqt_ui')
  AND runnable_dur > 50000  -- > 0.05ms
GROUP BY thread_name
ORDER BY avg_runnable_ms DESC;
```

### 主线程调度延迟直方图

```sql
INCLUDE PERFETTO MODULE sched.runnable;

-- 主线程 runnable 延迟分布
SELECT
  CASE
    WHEN runnable_dur < 500000 THEN '<0.5ms'
    WHEN runnable_dur < 1000000 THEN '0.5-1ms'
    WHEN runnable_dur < 2000000 THEN '1-2ms'
    WHEN runnable_dur < 5000000 THEN '2-5ms'
    WHEN runnable_dur < 10000000 THEN '5-10ms'
    ELSE '>10ms'
  END as bucket,
  COUNT(*) as count
FROM sched_runnable
WHERE thread_name = 'main'
GROUP BY bucket
ORDER BY CASE bucket
  WHEN '<0.5ms' THEN 1
  WHEN '0.5-1ms' THEN 2
  WHEN '1-2ms' THEN 3
  WHEN '2-5ms' THEN 4
  WHEN '5-10ms' THEN 5
  ELSE 6
END;
```

## 误报识别

### Idle 唤醒（误报：频繁唤醒看似严重）

- 线程频繁唤醒但 runnable 时间极短（<0.1ms），通常是在执行定时器/监控类工作
- 高 `runnable_count` + 极低 `avg_runnable_ms` 不是需要关注的问题
- 典型线程：`Timer-`、`Watchdog`、`FinalizerDaemon`

### Binder 线程调度（误报：新建线程延迟高）

- Binder 线程（`Binder:<pid>_X`）由内核按需创建
- 新的 Binder 请求到达时，新线程被创建后必须被调度
- 新建 Binder 线程的初始调度延迟比已有线程更高，这是正常行为
- 只有当已建立的 Binder 线程持续出现高调度延迟时才需关注

### SMP 迁移延迟（误报：负载均衡导致延迟）

- 线程从一个 CPU 迁移到另一个 CPU 时（负载均衡），会有短暂的调度延迟
- 这是 CFS 正常行为，除非 UI 线程频繁迁移（>10% 的唤醒涉及迁移），否则不需要优化
- 可通过检查 `sched` 表中同一线程的 `cpu` 字段变化来检测迁移

### cgroup 调整延迟（误报：生命周期切换延迟）

- 应用在前台/后台 cgroup 之间切换时，会有短暂的调度延迟上升期
- 这发生在 Activity 生命周期切换期间（如 `onPause`→`onStop`），属于预期行为
- 不应将生命周期切换期间的短暂调度延迟上升诊断为性能问题
