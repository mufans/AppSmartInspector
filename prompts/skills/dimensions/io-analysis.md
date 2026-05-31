# 文件 IO 分析

## 数据源

- SQL 表: `__intrinsic_thread_state` (WHERE `io_wait = 1` AND `dur > 100000`)
- `io_wait` 字段标识线程是否在等待 IO 完成
- `blocked_function` 指示具体的内核阻塞函数

## 领域知识

### IO 阻塞的内核层面

当线程发起文件 IO 请求时，如果数据不在页缓存（page cache）中，线程会进入 `D` (DiskSleep/Uninterruptible) 状态，在 Perfetto 中通过 `io_wait = 1` 标识。

常见 `blocked_function` 及其含义：

| blocked_function | 含义 | 典型场景 |
|-----------------|------|---------|
| `folio_wait_bit_common` | 等待文件页从磁盘读入 | 首次读取文件、冷启动读配置 |
| `wait_on_page_bit` | 等待页 IO 完成（旧内核） | 同上，内核版本差异 |
| `vfs_read` / `vfs_write` | 虚拟文件系统读写 | 通用文件操作 |
| `do_writepages` | 回写脏页到磁盘 | 大量写入后触发回写 |
| `ext4_file_read_iter` | ext4 文件系统读取 | 读取 ext4 分区上的文件 |
| `f2fs_file_read_iter` | f2fs 文件系统读取 | 多数 Android 设备使用 f2fs |
| `io_schedule` | IO 调度等待 | 通用 IO 等待入口 |
| `blkdev_issue_flush` | 等待存储设备 flush | fsync / commit 操作 |

### Android 常见 IO 场景

| 场景 | IO 类型 | 典型耗时 | 是否可避免 |
|------|---------|---------|-----------|
| SharedPreferences.commit() | 同步写入 XML | 5-50ms | 改用 apply() 或 DataStore |
| SharedPreferences 读取 | 同步读取 XML | 1-10ms | 首次读取无法避免 |
| SQLite 查询 | 同步文件锁 + 磁盘读取 | 1-100ms | 使用 Room + 异步查询 |
| Asset 文件读取 | 直接读取 APK 内文件 | 1-20ms | 预加载到内存 |
| Dex 文件加载 | mmap 读取 dex2oat 产物 | 10-200ms | 冷启动阶段无法避免 |
| Bitmap 解码 | 读取图片文件 | 5-100ms | 子线程解码 + 缓存 |
|日志写入 | 追加写入日志文件 | 1-10ms | 使用 BufferOutputStream |
| Protobuf / JSON 解析 | 读取 + 解析 | 5-50ms | 子线程 + 缓存解析结果 |

### eMMC vs UFS 存储性能

| 存储 | 随机读 IOPS | 随机写 IOPS | 顺序读带宽 |
|------|-----------|-----------|-----------|
| eMMC 5.1 | ~5000 | ~3000 | ~250 MB/s |
| UFS 2.1 | ~15000 | ~10000 | ~600 MB/s |
| UFS 3.1 | ~50000 | ~40000 | ~1200 MB/s |
| UFS 4.0 | ~100000 | ~70000 | ~2400 MB/s |

低端设备（eMMC）上，即使是少量 IO 操作也可能产生显著延迟。

## 数据解读

### Metric 字段

```
file_io.blocking_events[]:
  blocked_function   # 内核阻塞函数名（定位 IO 类型）
  thread_name        # 线程名（"main" 表示主线程 IO 问题）
  occurrences        # 该类阻塞发生次数
  total_ms           # 总阻塞时间 ms
  max_ms             # 最大单次阻塞 ms

file_io.main_thread_total_ms  # 主线程 IO 阻塞总时间（关键指标）
```

### 分析决策树

```
1. 检查 main_thread_total_ms
   ├─ > 帧预算 → P0，主线程 IO 直接导致 jank，必须修复
   ├─ > 5ms → P1，主线程有 IO 阻塞，需优化
   └─ ≈ 0 → 主线程 IO 正常，检查子线程

2. 分析 blocked_function 分布
   ├─ folio_wait_bit_common / wait_on_page_bit → 文件读取阻塞
   │   └─ 可能原因: 冷启动读配置、首次加载资源文件
   ├─ vfs_write / do_writepages → 文件写入阻塞
   │   └─ 可能原因: SharedPreferences.commit()、日志写入
   └─ ext4/f2fs 相关 → 文件系统层阻塞
       └─ 可能原因: 存储设备性能差、并发 IO 竞争

3. 检查阻塞频率与时长组合
   ├─ occurrences 高 + avg 低 → 频繁小 IO，可能是日志或频繁查询
   ├─ occurrences 低 + max 高 → 偶发大 IO，可能是读取大文件
   └─ occurrences 高 + max 高 → 严重 IO 问题，存储设备瓶颈
```

### 关键模式识别

| 模式 | 数据特征 | 根因推测 |
|------|---------|---------|
| 主线程 folio_wait 集中 | main 线程 occurrences 多 | 首次读取配置/资源文件 |
| 主线程 vfs_write 阻塞 | main 线程 write 阻塞 | SharedPreferences.commit() |
| 多线程 IO 阻塞 | 多个线程均有 IO 等待 | 存储设备带宽不足 |
| Binder 线程 IO 阻塞 |Binder 线程出现 IO 等待 | ContentProvider 查询走同步 IO |

### Linux IO 栈与 Perfetto 追踪点

```
用户空间 (read/write/fsync)
    ↓
VFS (虚拟文件系统层)        ← vfs_read/vfs_write
    ↓
Page Cache                  ← folio_wait_bit_common (cache miss)
    ↓
文件系统 (ext4/f2fs)        ← ext4_file_read_iter / f2fs_file_read_iter
    ↓
Block Layer                 ← io_schedule (IO 调度队列等待)
    ↓
存储驱动 (UFS/eMMC)         ← 设备硬件延迟
```

每一层的阻塞都会在 `__intrinsic_thread_state` 中以不同的 `blocked_function` 出现。通过分析 `blocked_function` 的层级，可以精确定位 IO 瓶颈在哪一层。

### Page Cache 行为

Page Cache 命中时，read 操作直接从内存返回，不会产生 `io_wait`。只有 cache miss 才导致磁盘 IO：

| 场景 | Page Cache | io_wait | 典型 blocked_function |
|------|-----------|---------|----------------------|
| 首次读文件 | Miss | 有 | `folio_wait_bit_common` |
| 重复读同一文件 | Hit | 无 | — |
| 冷启动读配置 | Miss | 有 | `folio_wait_bit_common` / `f2fs_file_read_iter` |
| fsync/commit | N/A | 有 | `blkdev_issue_flush` |

### Android IO 调度策略

| 调度器 | 特点 | 适用场景 |
|--------|------|---------|
| `bfq` | 按比例分配 IO 带宽，前台进程优先 | 默认配置，对交互友好 |
| `none` | FIFO 顺序处理，无调度开销 | 高端设备 UFS 3.1+ |
| `mq-deadline` | 请求超时保证，防止饥饿 | 部分设备配置 |

## Perfetto SQL 深入查询

### 主线程 IO 阻塞与 jank 帧关联

```sql
-- 查找主线程 IO 阻塞与 jank 帧的时间重叠
SELECT
  its.blocked_function,
  its.dur / 1e6 AS io_wait_ms,
  frame.dur / 1e6 AS frame_ms
FROM __intrinsic_thread_state its
JOIN thread t ON its.utid = t.utid
JOIN slice frame ON frame.track_id IN (
  SELECT id FROM thread_track WHERE utid = (SELECT utid FROM thread WHERE name = 'main'))
WHERE t.name = 'main' AND its.io_wait = 1 AND its.dur > 1e6
  AND frame.ts < its.ts + its.dur AND frame.ts + frame.dur > its.ts
  AND frame.dur > 16e6
ORDER BY io_wait_ms DESC LIMIT 10;
```

### IO 阻塞函数层级分布

```sql
-- 分析 IO 阻塞在内核哪个层级（VFS/FS/Block）
SELECT
  CASE
    WHEN blocked_function GLOB '*vfs*' THEN 'VFS层'
    WHEN blocked_function GLOB '*ext4*' OR blocked_function GLOB '*f2fs*' THEN '文件系统层'
    WHEN blocked_function GLOB '*folio*' OR blocked_function GLOB '*page*' THEN 'Page Cache层'
    WHEN blocked_function GLOB '*blk*' OR blocked_function GLOB '*io_schedule*' THEN 'Block层'
    ELSE '其他: ' || blocked_function
  END AS io_layer,
  COUNT(*) AS events,
  SUM(dur) / 1e6 AS total_ms
FROM __intrinsic_thread_state
WHERE io_wait = 1 AND dur > 100000
GROUP BY io_layer
ORDER BY total_ms DESC;
```

---

## 严重度标准

- **P0**: 主线程 IO 阻塞 > 帧预算 (16.67ms@60Hz, 8.33ms@120Hz)
- **P1**: 主线程 IO 阻塞 > 5ms
- **P2**: 任何线程 IO 阻塞 > 10ms

## 常见误判

| 数据现象 | 易误判为 | 实际可能是 |
|---------|---------|-----------|
| 主线程短时间 io_wait | 应用 IO 问题 | dex 文件 mmap page fault，冷启动正常行为 |
| Binder 线程 IO 等待 | 应用 IO 问题 | ContentProvider 查询的正常行为 |
| 大量 `io_schedule` | 存储设备慢 | 可能是并发 IO 过多导致排队，非设备性能问题 |
| 无 io_wait 但 IO 操作慢 | 无 IO 问题 | IO 可能命中 Page Cache（不产生 io_wait），但 CPU 处理耗时 |

## 与其他维度的关联

| 关联维度 | 关联模式 | 分析方法 |
|---------|---------|---------|
| 帧时间线 (ui-jank) | 主线程 IO 导致 jank 帧 | IO 阻塞时间戳与 jank 帧时间戳对齐 |
| 锁竞争 (lock-contention) | 持锁期间执行 IO 放大阻塞 | 锁等待 + IO 等待在同一时间窗口 |
| 冷启动 (startup) | 启动阶段大量 IO 读取 | startup phases 中 IO 事件密集 |
| Binder IPC (binder-ipc) | ContentProvider 查询触发 IO | binder 线程的 IO 等待 |
| CPU 降频 (cpu-throttling) | 降频使 IO 调度变慢 | 低 CPU 频率期间 IO 延迟增加 |

## 优化方向

### 主线程 IO 消除（最高优先级）

1. **SharedPreferences → DataStore**: `commit()` 是同步写入，改用 Jetpack DataStore（基于协程 + protobuf）
2. **异步文件读取**: 使用 Kotlin 协程 `withContext(Dispatchers.IO)` 替代同步读取
3. **预加载**: 在 Application.onCreate 或后台线程预加载首屏需要的文件数据
4. **mmap**: 对大文件使用 `MappedByteBuffer`（内存映射文件，减少系统调用开销）

### 数据库 IO 优化

1. **Room + 协程**: 所有数据库操作使用 `suspend` 函数，确保不在主线程执行
2. **WAL 模式**: `PRAGMA journal_mode=WAL` 减少写锁竞争
3. **索引优化**: 确保 WHERE 条件有索引覆盖，减少磁盘扫描
4. **批量操作**: 使用事务批量插入/更新，减少 fsync 次数

### 网络与图片 IO

1. **缓存策略**: 内存缓存 → 磁盘缓存 → 网络，减少重复下载
2. **图片解码**: 子线程解码 + 降采样 (`inSampleSize`)，避免主线程解码大图
3. **OkIO 缓冲**: 使用 `BufferedSink` / `BufferedSource` 减少系统调用

### 典型 IO 优化模式

```kotlin
// 反模式: 主线程同步 IO
fun loadData(): Config {
    val json = File(context.filesDir, "config.json").readText()  // 主线程同步读
    return parseJson(json)
}

// 推荐: 协程异步 IO
suspend fun loadData(): Config = withContext(Dispatchers.IO) {
    val json = File(context.filesDir, "config.json").readText()
    parseJson(json)
}

// 反模式: SharedPreferences.commit()
prefs.edit().putString("key", "value").commit()  // 同步写入

// 推荐: apply() 或 DataStore
prefs.edit().putString("key", "value").apply()   // 异步写入
```

### 页缓存 (Page Cache) 分析

Linux 页缓存将文件内容缓存在内存中，避免重复磁盘读取。理解页缓存行为对 IO 分析至关重要：

#### 页缓存命中与缺失

| 场景 | 页缓存状态 | io_wait | blocked_function |
|------|-----------|---------|-----------------|
| 首次读文件 | Miss (数据不在内存) | 有 | `folio_wait_bit_common` |
| 重复读同一文件 | Hit (数据在内存) | 无 | — |
| 内存压力下重新读取 | Miss (页面被回收) | 有 | `folio_wait_bit_common` |
| 写入新数据 | N/A (写缓存) | 可能 | `do_writepages` (回写时) |

#### 高频页缓存 Miss 的根因分析

`folio_wait_bit_common` / `wait_on_page_bit` 出现频率高表明数据不在内存中，可能原因：

1. **冷启动读取大量不同文件** — 预期行为，应用首次启动需要加载配置、资源等
2. **读取大于可用页缓存的文件** — 大型媒体文件或数据文件无法完全缓存在内存中
3. **内存压力导致页缓存被回收** — 交叉验证 memory_trend 维度，检查 RSS 增长和系统内存压力

#### 脏页回写分析

`do_writepages` 表示脏页回写到磁盘，频繁回写可能原因：

1. **大量写入** — 日志写入频繁、数据库大量提交
2. **VM 压力触发提前回写** — 系统内存不足时，内核提前触发脏页回写释放内存
3. **显式 fsync/fdatasync** — 应用调用 `FileDescriptor.sync()` 或 SQLite PRAGMA fullfsync 强制回写

#### 页缓存与内存压力关联

当 memory_trend 显示 RSS 持续增长时，系统可能回收页缓存页面来为应用腾出内存。这会导致后续文件读取变为 cache miss，形成 "内存增长 → 页缓存缩减 → IO 增加 → 性能下降" 的恶性循环。

### IO 调度器影响

Android 设备使用不同的 IO 调度器，对 IO 等待时间有显著影响：

#### IO 调度器类型

| 调度器 | 策略 | 交互 IO 延迟 | 适用设备 |
|--------|------|------------|---------|
| CFQ (Completely Fair Queuing) | 按比例公平分配 IO 带宽 | 较高（公平性优先） | 旧设备（Android 7 以前） |
| BFQ (Budget Fair Queuing) | 基于预算的公平调度，前台进程优先 | 低（交互友好） | 现代中端设备 |
| none (noop) | FIFO 顺序处理，零调度开销 | 最低（依赖硬件队列） | 高端设备 UFS 3.1+ |
| mq-deadline | 请求截止时间保证，防止饥饿 | 中等 | 部分设备配置 |

#### IO 调度器对 trace 数据的影响

- 同一存储设备上，CFQ 的 `io_wait` 时间可能比 none 调度器高 2-3 倍
- BFQ 在多线程并发 IO 时对前台线程有优先调度，主线程 IO 等待通常较短
- 多线程同时 IO 时，调度器可能串行化请求，表现为多个线程同时显示 `io_wait = 1`

#### IO 总线竞争检测

```
检测模式:
  在同一时间戳 (ts) 附近，多个线程同时出现 io_wait = 1
  → IO 总线竞争

影响:
  单线程 IO 延迟 = 设备延迟
  多线程竞争 IO 延迟 = 设备延迟 + 调度排队等待

优化:
  1. 合并小 IO 为批量 IO
  2. 错开 IO 操作的时间窗口
  3. 使用异步 IO (io_uring) 减少线程阻塞
```

### mmap vs read/write 性能特征

mmap (内存映射文件) 与传统 read/write 在 IO 表现上有本质区别：

#### mmap 工作原理

```
mmap 系统调用:
  1. 内核建立虚拟地址 → 文件偏移的映射关系
  2. 不立即读取文件数据
  3. 进程访问映射地址时触发 page fault
  4. 内核在 page fault handler 中读取文件数据到页缓存
  5. 后续访问同一页面直接从页缓存返回（无系统调用）
```

#### mmap vs read/write 在 trace 中的差异

| 特征 | mmap | read/write |
|------|------|-----------|
| 首次访问 | major page fault (不显示为 io_wait) | `io_wait = 1` + `folio_wait_bit_common` |
| 后续访问 | 直接内存访问（无系统调用） | 系统调用 → 页缓存命中（无 io_wait） |
| blocked_function | 通常不出现（page fault 不计入 io_wait） | `vfs_read`/`vfs_write`/`folio_wait_bit_common` |
| 线程状态 | 可能是 Running（处理 page fault） | D (DiskSleep, io_wait = 1) |

#### Android 中 mmap 的使用场景

Android 系统广泛使用 mmap：

| 场景 | mmap 对象 | IO 表现 |
|------|----------|---------|
| APK 读取 | APK 内文件通过 mmap 访问 | page fault（不显示为 io_wait） |
| dex 文件加载 | dex2oat 产物 (ODEX/VDEX) 通过 mmap | 冷启动时 page fault 密集 |
| 共享库 (.so) | `dlopen` → mmap 共享库代码段 | 首次调用函数时 page fault |
| SharedPreferences (Android 9+) | XML 文件 mmap | 读取不显示为 io_wait |

#### 分析影响

如果主线程有 IO 等待但应用使用了 mmap 访问这些文件，IO 可能来自：

1. **非 mmap 路径** — SharedPreferences (`commit()` 写入)、直接 `FileInputStream` 读取
2. **mmap 文件的页面被回收** — 内存压力导致 mmap 页面被回收，再次访问时触发 page fault
3. **Dex 加载** — `dex2oat` 产物通过 mmap 加载，冷启动的 dex 加载 IO 表现为 page fault 而非 io_wait

关键洞察：如果 trace 显示主线程 IO 阻塞很低但应用仍有性能问题，可能是 mmap page fault 导致的开销。需要通过 `perf` 采样数据（major fault 统计）来分析，而非 `io_wait`。

### f2fs 文件系统特性

大多数 Android 设备使用 f2fs (Flash-Friendly File System)，理解其特性有助于准确分析 IO 数据：

#### f2fs 核心机制

| 机制 | 说明 | 对 IO 的影响 |
|------|------|------------|
| 追加日志 (Append-only Logging) | 数据写入日志区域而非原地更新 | 写入延迟稳定，较少随机写 |
| 多头日志 (Multi-head Logging) | 热/温/冷数据分别写入不同日志区域 | 减少热点数据碎片化 |
| 垃圾回收 (GC) | 回收日志中过期的数据块 | GC 期间写入操作可能被阻塞 |
| 后台清理 (Background Cleaning) | 空闲时主动整理碎片 | 正常情况下对前台 IO 无影响 |

#### f2fs GC 对性能的影响

f2fs 垃圾回收是导致 IO 延迟突刺的主要原因之一：

```
触发条件:
  1. 可用空间低于阈值（通常 < 10%）
  2. 碎片化严重（大量无效块分散在段中）

影响:
  → f2fs GC 需要读取有效数据 → 移动到新位置 → 回收旧段
  → 此过程中前台写操作被阻塞
  → 在 trace 中表现为: 写入 IO 的 io_wait 突然增加

检测模式:
  blocked_function 包含 f2fs 相关函数
  + 写入 IO 延迟突增（正常 < 5ms, GC 期间 > 50ms）
  + 仅在写入操作中出现，读取不受影响
```

#### f2fs 在 trace 中的标识

| blocked_function | IO 操作 | 分析意义 |
|-----------------|---------|---------|
| `f2fs_file_read_iter` | f2fs 文件读取 | 正常读取路径 |
| `f2fs_file_write_iter` | f2fs 文件写入 | 正常写入路径 |
| `f2fs_write_begin` + `f2fs_write_end` | 写入操作对 | 如果两函数之间 `dur` 长，说明写入被阻塞 |
| `f2fs_gc` | f2fs 垃圾回收 | 文件系统 GC 进行中，会阻塞写入 |

#### f2fs 性能退化因素

| 因素 | 影响 | 数据库密集型应用影响 |
|------|------|-------------------|
| 高碎片化（大量小文件） | GC 频率增加，写入延迟上升 | SQLite WAL 文件碎片化导致写入变慢 |
| 存储空间接近满 | GC 触发更频繁 | 长期运行的数据库应用受影响更大 |
| 并发写入过多 | f2fs 日志区域竞争 | 多线程数据库写入互相阻塞 |

### Perfetto SQL 查询模式

```sql
-- IO 阻塞按线程和 blocked_function 分组统计
SELECT
  thread.name as thread_name,
  its.blocked_function,
  COUNT(*) as wait_count,
  SUM(its.dur) / 1e6 as total_wait_ms,
  MAX(its.dur) / 1e6 as max_wait_ms,
  AVG(its.dur) / 1e6 as avg_wait_ms
FROM __intrinsic_thread_state its
JOIN thread ON its.utid = thread.id
WHERE its.io_wait = 1
  AND its.dur > 100000  -- > 0.1ms
GROUP BY thread.name, its.blocked_function
ORDER BY total_wait_ms DESC;

-- 主线程 IO 阻塞时间线 (按 IO 类型分类)
SELECT
  ts,
  dur / 1e6 as wait_ms,
  blocked_function,
  CASE WHEN blocked_function LIKE '%folio%' OR blocked_function LIKE '%page%'
       THEN 'page_cache_miss'
       WHEN blocked_function LIKE '%write%' OR blocked_function LIKE '%writepages%'
       THEN 'write_io'
       WHEN blocked_function LIKE '%f2fs%' OR blocked_function LIKE '%ext4%'
       THEN 'filesystem'
       ELSE 'other' END as io_category
FROM __intrinsic_thread_state
JOIN thread USING (utid)
WHERE thread.name = 'main'
  AND io_wait = 1
  AND dur > 500000  -- > 0.5ms
ORDER BY dur DESC;

-- IO 总线竞争检测: 同一时间窗口多线程 IO 等待
SELECT
  ts,
  COUNT(DISTINCT utid) AS concurrent_io_threads,
  GROUP_CONCAT(DISTINCT thread.name) AS threads,
  SUM(dur) / 1e6 AS total_wait_ms,
  MAX(dur) / 1e6 AS max_single_wait_ms
FROM __intrinsic_thread_state its
JOIN thread ON its.utid = thread.id
WHERE its.io_wait = 1 AND its.dur > 500000
GROUP BY ts / 1000000  -- 1ms 时间窗口分组
HAVING concurrent_io_threads > 1
ORDER BY total_wait_ms DESC
LIMIT 20;
```

### 误报识别

| 误报类型 | 数据特征 | 判断依据 |
|---------|---------|---------|
| **冷启动 IO** | 进程创建后的前 1-2 秒内出现主线程 IO 阻塞 | 首次文件读取必然是页缓存 Miss。主线程在启动前 1-2 秒内的 IO（读配置、dex 加载等）是预期行为 |
| **mmap 页缺失** | IO 阻塞数据很低但应用性能仍差 | mmap 文件访问不显示为 io_wait。如果 IO 阻塞低但应用慢，问题可能是 mmap 文件的 page fault，需要不同的分析方法（perf 采样数据） |
| **系统线程 IO** | 非应用线程的 IO 等待 | 部分系统服务在应用进程内运行（如 WebView 资源加载）。检查 thread_name — 系统线程的 IO 不可通过应用代码优化 |
| **生命周期写入 IO** | `onPause`/`onStop` 回调中出现短暂写入 IO (< 5ms) | 生命周期回调中 `SharedPreferences` 写入或状态持久化是正常行为。仅当写入阻塞 > 5ms 时才需关注 |
| **日志系统 IO** | 主线程出现短暂 `vfs_write` (< 2ms) | `Log.d()` / `android.util.Log` 在某些实现中会触发短暂 IO。高频日志可能累积影响，但单次不应视为问题 |
