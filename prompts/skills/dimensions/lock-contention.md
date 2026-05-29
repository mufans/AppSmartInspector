# 锁竞争分析

## 数据源

- SQL 表: `__intrinsic_thread_state` (WHERE `blocked_function GLOB '*futex*'` AND `dur > 100000`)
- 需要 `sched_switch` ftrace 事件支持（`__intrinsic_thread_state` 虚拟表依赖它）
- 关联表: `thread` (通过 `utid` JOIN 获取线程名)

## 领域知识

### futex 机制

futex (Fast Userspace Mutex) 是 Linux 内核提供的用户空间互斥锁原语。当锁无竞争时，加锁/解锁完全在用户空间完成（快速路径，无需系统调用）。当发生竞争时，线程通过 `futex_wait` 系统调用进入内核等待队列，被唤醒时通过 `futex_wake` 返回用户空间。

在 Perfetto trace 中，futex 竞争表现为线程状态为 `S` (Sleeping)，`blocked_function` 包含以下值之一：

| blocked_function | 含义 | 典型场景 |
|-----------------|------|---------|
| `futex_wait_queue_me` | 线程被加入 futex 等待队列 | Java `synchronized` / `ReentrantLock` 竞争 |
| `futex_wait` | 通用 futex 等待 | `Object.wait()`、`Condition.await()` |
| `do_futex` | futex 系统调用入口 | 较少见，通常包含在前两者中 |
| `futex_lock_pi` | Priority Inheritance futex | Java `synchronized` 在 ART 中的实现 |

### Android 锁实现映射

| Java API | 内核原语 | blocked_function |
|----------|---------|-----------------|
| `synchronized` | ART Monitor → futex | `futex_wait_queue_me` |
| `ReentrantLock` | AQS → `LockSupport.park()` → futex | `futex_wait_queue_me` |
| `Object.wait()` | ART Monitor Wait → futex | `futex_wait` / `futex_wait_queue_me` |
| `CountDownLatch.await()` | AQS → `LockSupport.park()` → futex | `futex_wait_queue_me` |
| `Condition.await()` | AQS → `LockSupport.park()` → futex | `futex_wait_queue_me` |

### 主线程锁竞争的影响

主线程（thread_name = "main"）上的锁竞争是 ANR 和 jank 的直接原因：

- **ANR 触发**: Input 事件 5 秒内未处理完毕、BroadcastReceiver 10 秒超时、Service 20 秒超时
- **Jank 帧**: 锁等待时间超过帧预算（16.67ms@60Hz, 8.33ms@120Hz）导致帧被丢弃
- **输入延迟**: 主线程被阻塞时无法响应触摸事件，导致触摸到响应延迟（input latency）

## 数据解读

### Metric 字段

```
lock_contention.threads[]:
  thread_name        # 线程名，"main" 表示主线程
  futex_wait_count   # futex 等待总次数（反映竞争频率）
  total_wait_ms      # 总等待时间 ms（反映累计影响）
  max_wait_ms        # 最大单次等待 ms（反映最严重卡顿）
  avg_wait_ms        # 平均等待 ms（反映竞争严重程度）

lock_contention.contention_hotspots[]:
  thread_name        # 线程名
  blocked_function   # 阻塞函数名
  occurrences        # 发生次数
  total_ms           # 该热点总阻塞时间
```

### 分析决策树

```
1. 检查主线程 (thread_name == "main") 的锁竞争数据
   ├─ max_wait_ms > 帧预算 → P0，主线程被长时间阻塞，直接导致 jank
   ├─ max_wait_ms > 5ms → P1，主线程有锁竞争但尚未超过帧预算
   └─ futex_wait_count > 100 且 avg_wait_ms < 1ms → 大量轻量级竞争，关注优化空间

2. 检查其他线程的锁竞争
   ├─ max_wait_ms > 50ms → P2，可能影响后台任务完成时间
   └─ total_wait_ms 占 trace 时长 > 10% → 该线程效率严重受限于锁

3. 检查 content_hotspots
   ├─ 同一 blocked_function 出现多次 → 定位到具体锁对象
   └─ 跨多个线程的热点 → 全局锁，需考虑锁粒度拆分
```

### 关键指标组合解读

| 模式 | 含义 | 优化方向 |
|------|------|---------|
| count 高, avg 低, max 低 | 频繁轻量竞争 | 减少临界区操作、考虑无锁方案 |
| count 低, max 高 | 偶发长时间阻塞 | 检查临界区内是否有 IO 或长耗时操作 |
| main 线程 count=1, max > 帧预算 | 单次严重锁等待 | 检查持锁线程在做什么，移出主线程 |
| 多个线程竞争同一函数 | 热点锁 | 拆分锁粒度或使用分段锁 |

### ART Monitor 内部机制

ART 虚拟机的 `synchronized` 通过 Monitor 实现，Monitor 内部维护三个队列：

| 队列 | 用途 | 线程状态 |
|------|------|---------|
| Entry Set | 等待获取 Monitor 的线程 | BLOCKED (Java) / futex_wait_queue_me (内核) |
| Wait Set | 调用 Object.wait() 的线程 | WAITING/TIMED_WAITING (Java) / futex_wait (内核) |
| Owner | 当前持有 Monitor 的线程 | RUNNABLE (Java) / Running (内核) |

Monitor 竞争传递链：线程 A 持锁 → 线程 B 等待线程 A 释放 → 线程 C 等待线程 B 持有的另一把锁。这种传递链在 trace 中表现为多个线程同时 futex 等待，需通过时间窗口对齐分析因果关系。

### WaitForGcToComplete 与锁竞争

ART GC 期间会请求获取 heap 锁（`mutator_lock_`），应用线程分配对象时也需要该锁。当 GC 正在进行时，分配线程会进入 `WaitForGcToComplete` 等待。在 trace 中表现为：
- GC 切片（如 `GC: Concurrent`）正在执行
- 同时主线程出现 `futex_wait_queue_me`（等待 GC 完成）
- 这是 GC 间接导致锁竞争的典型模式

## Perfetto SQL 深入查询

### 锁竞争与 jank 帧时序关联

```sql
-- 查找主线程 futex 等待与 jank 帧的时间重叠
SELECT
  f.thread_name, f.ts, f.dur / 1e6 as wait_ms,
  j.name as jank_slice, j.dur / 1e6 as jank_ms
FROM __intrinsic_thread_state f
JOIN slice j ON f.thread_name = 'main'
  AND j.track_id IN (SELECT id FROM thread_track WHERE utid = (SELECT utid FROM thread WHERE name = 'main'))
  AND f.ts < j.ts + j.dur AND f.ts + f.dur > j.ts
WHERE f.blocked_function GLOB '*futex*' AND f.dur > 1e6
  AND j.dur > 16e6
ORDER BY wait_ms DESC LIMIT 10;
```

### 锁竞争热点与持锁线程分析

```sql
-- 分析哪些线程在被 futex 阻塞时，其他线程在做什么（定位持锁者）
SELECT
  waiter.name AS waiter_thread,
  blocker.name AS active_thread,
  COUNT(*) AS coincidences
FROM __intrinsic_thread_state its_wait
JOIN thread waiter ON its_wait.utid = waiter.utid
JOIN __intrinsic_thread_state its_run ON
  its_run.ts < its_wait.ts + its_wait.dur AND its_run.ts + its_run.dur > its_wait.ts
JOIN thread blocker ON its_run.utid = blocker.utid
WHERE its_wait.blocked_function GLOB '*futex*' AND its_wait.dur > 5e6
  AND its_run.state = 'R' AND waiter.utid != blocker.utid
GROUP BY waiter_thread, active_thread
ORDER BY coincidences DESC LIMIT 10;
```

## 严重度标准

- **P0**: 主线程 max_wait_ms > 帧预算 (16.67ms@60Hz, 8.33ms@120Hz)
- **P1**: 主线程 max_wait_ms > 5ms
- **P2**: 其他线程 max_wait_ms > 10ms

## 常见误判

| 数据现象 | 易误判为 | 实际可能是 |
|---------|---------|-----------|
| main 线程短时间 futex_wait | 锁竞争 | `Object.wait()` 超时等待，非竞争 |
| Binder 线程 futex_wait | 应用锁问题 | Binder 驱动等待客户端数据，正常行为 |
| futex_wait_count 极高但 total_wait_ms 很低 | 严重锁竞争 | 锁快速获取/释放，可能是 `synchronized` 的 fast path 失败后立即获取 |
| GC 线程 futex_wait | GC 自身有锁问题 | GC 等待 mutator 暂停（`CAS mutator_lock_`），是正常 GC 流程 |

## 与其他维度的关联

| 关联维度 | 关联模式 | 分析方法 |
|---------|---------|---------|
| 帧时间线 (ui-jank) | 主线程锁等待导致 jank 帧 | 对比锁等待时间戳与 jank 帧时间戳，看是否有时序重叠 |
| GC 事件 (gc-analysis) | GC pause 持有 heap 锁导致其他线程等待 | "GC: Wait For Concurrent" 期间，其他线程的 futex 等待增加 |
| Binder IPC (binder-ipc) | binder 调用在服务端持锁导致客户端阻塞 | 主线程 binder_thread_read 等待 + 同一时刻其他线程 futex 等待 |
| 文件 IO (io-analysis) | 持锁期间执行 IO 操作放大阻塞时间 | 检查锁等待时长是否与 IO 阻塞时长线性相关 |
| CPU 调度 (cpu-scheduling) | 高调度延迟使持锁线程唤醒慢，间接延长锁等待 | 持锁线程的 runnable 时间长 → 锁持有时间长 → 等待线程等待时间长 |

## 优化方向

### 主线程锁竞争（最高优先级）

1. **移出主线程**: 将耗时的同步操作移到子线程，主线程通过回调/协程获取结果
2. **减小锁粒度**: 用细粒度锁替代全局锁（如 `ConcurrentHashMap` 分段锁替代 `synchronized(map)`）
3. **避免锁嵌套**: 锁嵌套容易导致死锁，且持锁时间成倍增加
4. **使用无锁结构**: `ConcurrentHashMap`、`AtomicInteger`、`CopyOnWriteArrayList` 等并发容器

### 长时间持锁

1. **检查临界区**: 持锁期间不应执行 IO、网络请求、数据库操作
2. **读写分离**: 读多写少场景使用 `ReentrantReadWriteLock`
3. **双缓冲**: 数据更新使用双缓冲策略，读操作无锁访问旧缓冲区

### 典型 Android 锁反模式

```java
// 反模式: 主线程等待子线程持锁
synchronized (cache) {          // 主线程等待
    cache.get(key);             // 临界区不应包含 IO
}

// 推荐: 使用 ConcurrentHashMap
concurrentCache.get(key);       // 无锁读取

// 反模式: 全局锁
class GlobalState {
    synchronized void update() { ... }  // 所有调用者竞争同一把锁
}

// 推荐: 分段锁或独立状态
class SegmentState {
    private final Object[] locks = new Object[16];
    void update(int segment) {
        synchronized (locks[segment]) { ... }  // 竞争分散到 16 个锁
    }
}
```

### ART Monitor 锁升级与 PI Futex 机制

ART 虚拟机的 `synchronized` 实现采用三层锁升级策略，从轻量到重量级逐级递进：

| 阶段 | 实现 | 条件 | 开销 |
|------|------|------|------|
| Thin Lock (偏斜锁) | 原子 CAS 操作 | 无竞争，单线程访问 | 极低（用户空间 CAS） |
| Inflated Monitor (轻量级锁) | 自旋等待 | 短时间竞争，持锁线程正在执行 | 低（CPU 自旋） |
| Inflated Monitor (重量级锁) | `futex_lock_pi` 内核等待 | 持锁线程不在运行或自旋超时 | 高（系统调用 + 内核调度） |

锁升级是不可逆的：一旦 Monitor 被膨胀（inflate），即使后续无竞争也不会回退到 thin lock。这意味着首次竞争会导致永久性的性能开销增加。

#### Monitor Enter 序列

```
synchronized(obj) 调用路径:
  art::Monitor::Enter(obj)
    → 尝试 thin lock (CAS)
      → 成功: 直接返回（快速路径，无系统调用）
      → 失败: 检查是否为自己重入（锁重入计数 +1）
        → 是: 直接返回
        → 否: 尝试自旋等待（有限次数）
          → 成功: 获取锁
          → 失败: 膨胀 Monitor
            → art::Monitor::MonitorEnterHelper(monitor)
              → futex_lock_pi(monitor->lock_, FUTEX_WAIT_PRIVATE)
              → 内核 PI futex 等待（Sleeping 状态）
```

#### Monitor Wait 序列

```
obj.wait() 调用路径:
  art::Monitor::Wait(obj, timeout)
    → 检查当前线程是否持有 Monitor（否则抛 IllegalMonitorStateException）
    → 将当前线程加入 Wait Set
    → 释放 Monitor（唤醒 Entry Set 中的一个线程）
    → futex_wait(monitor->lock_, timeout)
      → 超时或被 notify/notifyAll 唤醒
    → 重新竞争获取 Monitor
```

#### `ReentrantLock` 的 AQS 路径

`ReentrantLock` 不使用 ART Monitor，而是基于 `java.util.concurrent.locks.AbstractQueuedSynchronizer` (AQS)：

- AQS 使用 `volatile int state` + CAS 实现锁状态管理
- 竞争时调用 `LockSupport.park()` → `Unsafe.park()` → `futex_wait_queue_me`
- 与 `synchronized` 的区别：AQS 不使用 PI futex，因此不支持优先级继承
- AQS 支持公平/非公平模式：公平模式按 FIFO 排队，非公平模式允许插队

#### `contended_monitor` 字段

ART 线程结构（`art::Thread`）中的 `contended_monitor` 字段记录当前线程正在等待的 Monitor 对象。结合 `__intrinsic_thread_state` 的时间戳和 `waker_utid`，可以精确定位：
- 哪个线程持有了目标 Monitor（通过 `waker_utid` 追踪唤醒者）
- 等待持续了多长时间（通过 `dur` 字段）
- 等待发生在哪个调用路径上（通过关联的 stack trace）

### 优先级反转检测

优先级反转（Priority Inversion）是锁竞争中最危险的性能问题之一：

#### 经典优先级反转

```
时间线:
  高优先级 (UI 主线程, nice=-10) ──── 等待锁 ────────── 获取锁 ──
                                  ^                    ^
  低优先级 (后台线程, nice=10)  ── 持锁 ──────────── 释放锁 ──
                                        ^
  中优先级 (Binder 线程, nice=0) ─── 抢占低优先级线程 ────────────
```

中优先级线程抢占低优先级的锁持有者，导致高优先级线程间接被中优先级线程阻塞。

#### ART PI Futex 缓解

ART 使用 Priority Inheritance futex (`futex_lock_pi`) 实现 `synchronized`：
- 当高优先级线程在 `futex_lock_pi` 上等待时，内核自动将锁持有者的优先级提升到等待者中最高优先级
- 效果：锁持有者（低优先级后台线程）临时获得 UI 线程优先级，不会被中优先级线程抢占
- 限制：PI 只提升直接锁持有者的优先级，如果锁持有者本身在等待另一把锁（锁链），PI 不传递

#### 检测模式

在 Perfetto trace 中识别优先级反转：

```
优先级反转信号:
  1. 主线程 (nice <= 0) 出现 futex_lock_pi 等待
  2. 锁持有线程为后台线程 (nice > 0, THREAD_PRIORITY_BACKGROUND)
  3. max_wait_ms > 50ms（正常 PI 应在 <10ms 内完成优先级提升和锁传递）

无法缓解的场景 (unbounded priority inversion):
  1. 主线程等待 futex_lock_pi
  2. 锁持有者被提升优先级后，又进入另一个 futex_wait（锁链）
  3. 锁链中的下一跳没有 PI 保护（如 ReentrantLock 的 AQS 路径）

数据模式:
  main_thread.max_wait_ms > 50ms
  + 锁持有线程 nice > 0
  + 锁持有线程在同一时间窗口内也出现 futex_wait
  -> 疑似锁链导致的优先级反转
```

### 锁等待图分析 (Wait Graph)

通过 `__intrinsic_thread_state` 表中的 `waker_utid` 字段构建锁等待图，识别锁竞争的传递链。

#### 等待图构建

```
Wait Graph 节点: 每个线程 (utid)
Wait Graph 边: waiter_utid -> waker_utid (等待关系)

含义: waiter_utid 线程在等待 waker_utid 线程持有的锁
      waker_utid 是唤醒 waiter_utid 的线程（通常是锁持有者释放锁时唤醒等待者）
```

#### 关键分析方法

```sql
-- 构建锁等待图: 找出所有 futex 等待关系
SELECT
  waiter.name AS waiter_thread,
  waker.name AS waker_thread,
  COUNT(*) AS wait_count,
  SUM(its.dur) / 1e6 AS total_wait_ms,
  MAX(its.dur) / 1e6 AS max_wait_ms
FROM __intrinsic_thread_state its
JOIN thread waiter ON its.utid = waiter.id
LEFT JOIN thread waker ON its.waker_utid = waker.id
WHERE its.blocked_function GLOB '*futex*'
  AND its.dur > 500000  -- > 0.5ms
GROUP BY waiter_thread, waker_thread
ORDER BY total_wait_ms DESC;
```

#### 锁链识别

```
锁链模式 (Transitive Contention):

  main -> Binder:123_1 -> background_worker

  第一跳: main 线程等待 Binder:123_1 持有的锁
  第二跳: Binder:123_1 等待 background_worker 持有的锁

  根因: background_worker 持锁时间过长
  影响: main 线程的总等待时间 = 第一跳等待时间（远大于 background_worker 的持锁时间）

识别方法:
  1. 找到主线程的 waiter -> waker 关系 (第一跳)
  2. 检查 waker 线程是否同时也在 futex_wait 状态
  3. 如果 waker 也在等待，递归追踪直到找到最终锁持有者
  4. 最终锁持有者的持锁行为才是需要优化的根因
```

#### 典型锁链场景

| 锁链 | 场景 | 根因 |
|------|------|------|
| main -> binder线程 -> 后台线程 | 主线程调用服务端方法，服务端持锁等待后台任务 | 服务端不应在 RPC 方法中持锁 |
| main -> GC线程 | 主线程 WaitForGcToComplete | GC 问题，非锁竞争 |
| main -> HandlerThread | 主线程等待 Handler 结果，Handler 持锁 | Handler 任务内有 IO 或长耗时操作 |
| main -> DefaultPool-worker | 主线程等待协程/线程池结果 | 线程池任务执行过慢 |

### Perfetto SQL 查询模式

```sql
-- 主线程 futex 等待按 blocked_function 分组统计
SELECT
  blocked_function,
  COUNT(*) as wait_count,
  SUM(dur) / 1e6 as total_wait_ms,
  MAX(dur) / 1e6 as max_wait_ms,
  AVG(dur) / 1e6 as avg_wait_ms
FROM __intrinsic_thread_state
JOIN thread USING (utid)
WHERE thread.name = 'main'
  AND blocked_function GLOB '*futex*'
  AND dur > 100000
GROUP BY blocked_function
ORDER BY total_wait_ms DESC;

-- 锁竞争时间线 (与 jank 帧关联)
SELECT
  ts,
  dur / 1e6 as wait_ms,
  blocked_function,
  thread.name as waiter,
  waker.name as waker_thread
FROM __intrinsic_thread_state its
JOIN thread ON its.utid = thread.id
LEFT JOIN thread waker ON its.waker_utid = waker.id
WHERE its.blocked_function GLOB '*futex*'
  AND its.dur > 500000  -- > 0.5ms
ORDER BY its.dur DESC
LIMIT 30;

-- 锁链检测: 找到同时等待和被等待的线程（锁链中间节点）
SELECT
  mid_thread.name AS chain_midpoint,
  waiter.name AS waiting_for_mid,
  waker.name AS mid_waiting_for,
  mid_wait.dur / 1e6 AS mid_wait_ms,
  outer_wait.dur / 1e6 AS outer_wait_ms
FROM __intrinsic_thread_state outer_wait
JOIN thread waiter ON outer_wait.utid = waiter.id
JOIN __intrinsic_thread_state mid_wait ON outer_wait.waker_utid = mid_wait.utid
JOIN thread mid_thread ON mid_wait.utid = mid_thread.id
LEFT JOIN thread waker ON mid_wait.waker_utid = waker.id
WHERE outer_wait.blocked_function GLOB '*futex*' AND outer_wait.dur > 1e6
  AND mid_wait.blocked_function GLOB '*futex*' AND mid_wait.dur > 500000
  AND outer_wait.ts < mid_wait.ts + mid_wait.dur
  AND outer_wait.ts + outer_wait.dur > mid_wait.ts
ORDER BY outer_wait.dur DESC
LIMIT 10;
```

### 误报识别

| 误报类型 | 数据特征 | 判断依据 |
|---------|---------|---------|
| **App 启动 futex 等待** | 进程初始化阶段 (前 500ms) 出现短时间 futex_wait (< 2ms) | 进程初始化涉及 synchronized class loading，短时间锁等待是预期行为，不可操作 |
| **空闲 futex_wait** | `futex_wait` (非 `futex_wait_queue_me`) 且线程为空闲工作线程 (如 `IdleConnectionPool`, `TimerThread`) | `Object.wait()` 带超时产生 `futex_wait`，空闲线程池等待任务是正常行为 |
| **Concurrent GC 锁** | 主线程出现短暂 futex_wait，同时存在 "GC: Concurrent" 切片 | "GC: Wait For Concurrent" 期间主线程短暂等待 GC 锁，根因是 GC 问题而非锁竞争，应关联 gc_events 分析 |
| **锁自旋快速获取** | `futex_wait_count` 极高但 `avg_wait_ms` < 0.5ms | CAS 快速路径失败后进入 brief futex wait 后立即重试成功，这是良性竞争。除非 `max_wait_ms` 也很高，否则不需要优化 |
| **JNI 全局锁** | futex_wait 的 blocked_function 包含 `JNI` 相关 | JNI 全局引用表锁在频繁 JNI 调用时短暂竞争，属正常开销 |
