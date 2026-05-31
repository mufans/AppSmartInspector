# 内存分析

## 数据源

- SQL 表: `process_counter_track` + `counter` (WHERE `name = 'mem.rss'`)
- 关联: `process` 表获取目标进程名
- 时序数据: RSS 值按时间排序，计算趋势斜率
- 跳跃检测: 相邻样本增长 > 10MB 标记为 memory jump

## 领域知识

### Android 内存模型

Android 应用的内存由多个区域组成：

| 内存区域 | 内容 | 监控方式 |
|---------|------|---------|
| Java Heap | Java/Kotlin 对象 | Debug.getMemoryInfo()、heap dump |
| Native Heap | malloc/mmap 分配的 Native 内存 | /proc/pid/smaps |
| RSS (Resident Set Size) | 进程实际占用的物理内存 | process_counter_track (mem.rss) |
| PSS (Proportional Set Size) | 按比例分摊共享库内存后的 RSS | dumpsys meminfo |
| GPU Memory | OpenGL/Vulkan 纹理、帧缓冲 | GPU Profiler |
| ashmem / dmabuf | 共享内存、DMA 缓冲区 | /proc/pid/smaps |

### RSS 增长模式

| 模式 | 特征 | 可能原因 | 严重度 |
|------|------|---------|--------|
| 锯齿形（正常） | RSS 在 GC 后回落，波动幅度稳定 | 正常的分配-GC 循环 | 无 |
| 线性增长 | RSS 持续上升，GC 后不回落 | 内存泄漏（Activity/Fragment 未释放） | 高 |
| 阶梯增长 | RSS 在特定时间点跳跃 | 大对象分配（Bitmap、JSON 缓存） | 中 |
| 指数增长 | RSS 增速越来越快 | 集合类无限增长（List/Map 未清理） | 高 |
| 突增后稳定 | RSS 突然跳高后不再增长 | 初始化阶段加载大量资源 | 低 |

### Android 内存压力信号

| 信号 | 阈值 | 系统行为 |
|------|------|---------|
| `onTrimMemory(TRIM_MEMORY_RUNNING_LOW)` | 可用内存低 | 建议释放非必要缓存 |
| `onTrimMemory(TRIM_MEMORY_UI_HIDDEN)` | UI 不可见 | 释放 UI 相关缓存 |
| `onTrimMemory(TRIM_MEMORY_BACKGROUND)` | 进入后台 | 释放大部分缓存 |
| LMK (Low Memory Killer) | 系统内存紧张 | 根据 oom_score 杀进程 |
| `onOutOfMemoryError` | 堆内存耗尽 | 应用崩溃 |

### 常见内存泄漏模式

| 泄漏类型 | 特征 | 检测方法 |
|---------|------|---------|
| Activity 泄漏 | Activity 实例在 onDestroy 后仍被引用 | LeakCanary、heap dump |
| 静态集合 | static List/Map 持续添加不清理 | 代码审查 |
| Handler 泄漏 | 非静态内部类 Handler 持有 Activity 引用 | LeakCanary |
| 匿名内部类 | Runnable/Callback 隐式持有外部类引用 | 代码审查 |
| 单例持有 Context | 单例引用 Activity Context 而非 Application Context | 代码审查 |
| Native 泄漏 | Native 内存不随 GC 回收 | Native 内存分析工具 |
| Bitmap 未回收 | Bitmap 占用大量内存未释放 | heap dump 分析 |
| 监听器未注销 | 注册的监听器/回调未在 onDestroy 中注销 | 代码审查 |

## 数据解读

### Metric 字段

```
memory_trend.process_name          # 目标进程名
memory_trend.samples               # RSS 采样点数量
memory_trend.start_rss_mb          # 起始 RSS (MB)
memory_trend.end_rss_mb            # 结束 RSS (MB)
memory_trend.delta_mb              # 增量 (MB) = end - start
memory_trend.delta_pct             # 增量百分比 = delta / start * 100
memory_trend.trend_slope_mb_per_s  # 增长斜率 (MB/s)，线性拟合
memory_trend.jumps[]               # 阶段性跳跃事件
  ts_ns                            # 跳跃时间戳
  rss_mb                           # 跳跃后 RSS 值
  delta_mb                         # 跳跃增量
```

### 分析决策树

```
1. 检查 delta_pct
   ├─ > 50% → P0，内存可能泄漏，需要立即排查
   ├─ > 20% → P1，内存增长较大，建议关注
   ├─ > 10% → P2，轻微增长，正常范围边界
   └─ < 10% → 正常

2. 检查增长模式
   ├─ slope_mb_per_s > 1 → 线性增长，疑似泄漏
   ├─ slope 接近 0 但有 jumps → 阶梯增长，大对象分配
   └─ slope 接近 0 且无 jumps → 稳定，正常

3. 分析 jumps
   ├─ 单次 jump > 50MB → 可能是大型 Bitmap 或数据集加载
   ├─ 多次小 jump → 可能是逐步加载资源
   └─ jump 发生在启动阶段 → 初始化加载，属正常

4. 结合 trace 时长判断
   ├─ 10s trace 增长 100MB → slope = 10 MB/s，严重
   ├─ 10s trace 增长 10MB → slope = 1 MB/s，需关注
   └─ 10s trace 增长 1MB → slope = 0.1 MB/s，正常
```

### 关键模式识别

| 模式 | 数据特征 | 根因推测 |
|------|---------|---------|
| 持续线性增长 | slope > 0.5 MB/s, 无 jumps | 内存泄漏：集合未清理、回调未注销 |
| 启动阶段阶梯增长 | jumps 集中在前 2 秒 | 启动加载资源，属正常 |
| 后期突然跳增 | jump 在 trace 后期出现 | 可能是特定操作触发大对象分配 |
| RSS 只增不减 | delta_pct > 30%, 无回落 | GC 无法回收，长生命周期引用 |

### zRAM 与内存压缩

Android 使用 zRAM 将不活跃的内存页压缩存储，等效增加可用内存。当内存压力大时：

| 阶段 | 系统行为 | 对应用的影响 |
|------|---------|------------|
| 正常 | zRAM 压缩后台进程内存 | 前台应用无感知 |
| 轻度压力 | 压缩率增加，CPU 开销上升 | 可能触发调度延迟增加 |
| 中度压力 | kswapd 持续运行回收内存 | 后台进程被杀，IO 增加（zRAM 读写） |
| 重度压力 | LMK 触发杀进程 | 前台应用可能被杀 |

zRAM 压缩/解压会占用 CPU 和 IO 资源，在 trace 中可能表现为：CPU 占用高、IO 等待增加、调度延迟增加。

### Perfetto heap dump 分析

当 trace 包含 `android.java_hprof` 数据源时，可以使用 `heap_graph_*` 表分析对象引用链：

| 表名 | 内容 | 用途 |
|------|------|------|
| `heap_graph_class` | 类信息 | 找到占用内存最多的类 |
| `heap_graph_object` | 对象实例 | 统计某类对象的存活数量 |
| `heap_graph_reference` | 对象引用关系 | 追踪引用链定位泄漏 |
| `heap_graph_class_summary` | 类级别汇总 | 按 retained size 排序找最大贡献者 |

## Perfetto SQL 深入查询

### RSS 增长与 GC 事件关联

```sql
-- 分析 RSS 增长斜率是否与 GC 频率变化相关
SELECT
  rss.ts,
  rss.value / 1024 AS rss_mb,
  (SELECT COUNT(*) FROM slice WHERE name GLOB '*GC*'
   AND slice.ts BETWEEN rss.ts - 5e8 AND rss.ts) AS gc_count_500ms
FROM counter rss
JOIN process_counter_track pct ON rss.track_id = pct.id
JOIN process p ON pct.upid = p.upid
WHERE pct.name = 'mem.rss' AND p.name LIKE '%target%'
ORDER BY rss.ts;
```

### RSS 时序与 jank 帧关联

```sql
-- 检查高 RSS 时间段是否有更多 jank
SELECT
  rss.ts AS rss_time,
  rss.value / 1024 AS rss_mb,
  COUNT(frame.id) AS jank_frames_nearby
FROM counter rss
JOIN process_counter_track pct ON rss.track_id = pct.id
LEFT JOIN slice frame ON frame.track_id IN (
  SELECT id FROM thread_track WHERE utid = (SELECT utid FROM thread WHERE name = 'main'))
  AND frame.ts BETWEEN rss.ts - 1e9 AND rss.ts
  AND frame.dur > 16e6
WHERE pct.name = 'mem.rss'
GROUP BY rss.ts
ORDER BY rss_mb DESC LIMIT 20;
```

---

## 严重度标准

- **P0**: RSS 增长 > 50%
- **P1**: RSS 增长 > 20%
- **P2**: RSS 增长 > 10%

## 常见误判

| 数据现象 | 易误判为 | 实际可能是 |
|---------|---------|-----------|
| RSS 持续增长 | 内存泄漏 | 可能是正常的应用数据加载（缓存填充），GC 后 RSS 不回落是 ART 正常行为 |
| delta_pct > 20% | 严重问题 | 如果 trace 时间短（如 5s）且 start_rss_mb 很小，百分比增长大但绝对值小（如 30MB→40MB） |
| RSS 突然跳增 | 内存泄漏 | 可能是 Bitmap 加载或大文件 mmap，属于正常业务操作 |
| RSS 不增长 | 无内存问题 | 可能是 trace 时间太短（< 3s）无法观察到趋势 |

## 与其他维度的关联

| 关联维度 | 关联模式 | 分析方法 |
|---------|---------|---------|
| GC 事件 (gc-analysis) | 内存增长伴随 GC 频率增加 | RSS 增长斜率 + GC count/time 关联分析 |
| 帧时间线 (ui-jank) | GC pause 增加 jank，GC 因内存压力增加 | memory_trend + gc_events + jank 三方关联 |
| 文件 IO (io-analysis) | 内存不足触发 zRAM 压缩，增加 IO | 低内存期间 IO 等待增加 |
| 冷启动 (startup) | 启动阶段 RSS 跳增是初始化加载 | startup phases 内的 RSS jumps |
| CPU 降频 (cpu-throttling) | 内存压力导致 kswapd 消耗 CPU | 高 RSS 期间 CPU 频率可能降低 |

## 优化方向

### 内存泄漏检测

1. **LeakCanary**: 集成到 debug 构建变体，自动检测 Activity/Fragment 泄漏
2. **Android Studio Profiler**: Memory Profiler 查看 heap dump，按 retained size 排序
3. **Perfetto heap dump**: trace 中的 `heap_graph_*` 表可分析对象引用链

### 减少内存占用

1. **Bitmap 优化**: 降采样 (`inSampleSize`)、WebP 格式、`inBitmap` 复用
2. **缓存策略**: `LruCache` 限制缓存大小，`onTrimMemory` 时主动释放
3. **对象池**: 对频繁创建/销毁的对象使用对象池（如 Message.obtain()）
4. **避免自动装箱**: 使用 `SparseArray` 替代 `HashMap<Integer, V>`

### 典型内存优化模式

```kotlin
// 反模式: Activity 泄漏
class MyActivity : AppCompatActivity() {
    private val handler = Handler(Looper.getMainLooper())  // 隐式持有 Activity 引用
    private val listeners = mutableListOf<Listener>()       // 不断添加不清理

    override fun onCreate(savedInstanceState: Bundle?) {
        handler.postDelayed({ updateUI() }, 5000)  // Activity 销毁后仍执行
    }
}

// 推荐: 弱引用 + 清理
class MyActivity : AppCompatActivity() {
    private val handler = Handler(Looper.getMainLooper())

    override fun onDestroy() {
        handler.removeCallbacksAndMessages(null)  // 清理所有待执行消息
        listeners.clear()                         // 清理监听器
    }
}

// 反模式: 大 Bitmap 不缩放
val bitmap = BitmapFactory.decodeFile(path)  // 可能是 4000x3000 的原图

// 推荐: 降采样
val options = BitmapFactory.Options().apply {
    inJustDecodeBounds = true
}
BitmapFactory.decodeFile(path, options)
options.inSampleSize = calculateInSampleSize(options, reqWidth, reqHeight)
options.inJustDecodeBounds = false
val bitmap = BitmapFactory.decodeFile(path, options)
```

## zRAM 与交换分析

Android 使用 zRAM（RAM 中的压缩交换空间）来扩展有效内存。当 RSS 增长且物理内存不足时，内核将匿名页压缩到 zRAM 中。

### zRAM 核心参数

- zRAM 压缩比：通常 2:1 到 3:1（100MB RSS → 33-50MB zRAM）
- 压缩算法：通常使用 lz4（快速压缩）或 zstd（高压缩比）
- Android 典型 zRAM 大小：物理内存的 25-50%（如 8GB 设备配置 2-4GB zRAM）

### zRAM 压力信号

| 信号 | 检测方式 | 说明 |
|------|---------|------|
| `kswapd` 内核线程 CPU 占用高 | `sched` 表过滤 `kswapd` 线程 | 压缩/解压开销 |
| Swap 使用量增长 | `process_counter_track` 过滤 `mem.swap` | 页面被换出到 zRAM |
| GC 事件频率增加 | `gc_events` 维度数据 | zRAM 压力导致 page cache 驱逐，触发更多 GC |

### Perfetto 数据位置

- 在 `process_counter_track` 中查找 `mem.swap` 获取交换使用量
- 在 `sched` 表中查找 `kswapd` 线程的 CPU 时间

### 关键分析模式

```
RSS 增长 + Swap 增长 + kswapd CPU 时间高 = 内存压力导致 zRAM 交换

推导链：
  应用 RSS 持续增长
    → 系统可用内存减少
      → kswapd 被唤醒回收内存
        → 匿名页被压缩到 zRAM（Swap 增长）
          → kswapd CPU 开销增加
            → page cache 被驱逐
              → 后续 IO 需要重新从磁盘读取
```

| 现象 | 组合判断 | 根因 |
|------|---------|------|
| RSS 增长 + Swap 稳定 | 应用内存增长但未触发 zRAM | 内存使用在安全范围内 |
| RSS 增长 + Swap 增长 | 应用内存增长触发 zRAM 交换 | 内存压力开始显现 |
| RSS 稳定 + Swap 增长 | 其他进程导致系统内存压力 | 系统级内存问题 |
| Swap 增长 + kswapd CPU 高 | zRAM 压缩开销显著 | 内存压力影响 CPU 性能 |

## LMKD 与 OOM Score

Android 使用 lmkd（Low Memory Killer daemon）替代了传统的内核内驱动。lmkd 基于 `oom_score_adj` 杀死进程：

### OOM Score 等级

| 进程状态 | oom_score_adj | 说明 |
|---------|--------------|------|
| 前台应用 | 0 | 用户正在交互的应用 |
| 可见应用 | 100 | 部分可见但不在前台 |
| 服务 | 500 | 后台服务进程 |
| 前一个应用 | 700 | 最近使用的后台应用 |
| 缓存应用 | 900 | 可被随时回收的缓存进程 |

### lmkd 工作机制

- 现代 Android 使用 PSI（Pressure Stall Information）检测内存压力
- lmkd 杀进程策略：从高 oom_score_adj 开始，逐步向低 oom_score_adj 杀进程
- 在 Perfetto 中：`android.memory.lmk` 表记录 LMK 事件

### 关键分析模式

- 如果目标应用的 RSS 持续增长且 lmkd 正在杀死缓存进程，说明该应用正在造成系统级内存压力
- 模式：高 RSS + LMK 杀其他进程 = 本应用消耗了过多内存
- 如果目标应用自身被 LMK 杀死（trace 突然终止），说明 oom_score_adj 已被提升或系统内存极度紧张

```
RSS 增长趋势分析：
  应用 RSS 增长
    → 系统可用内存减少
      → lmkd 杀死缓存进程（oom_score_adj 900）
        → 系统内存仍不足
          → lmkd 杀死服务进程（oom_score_adj 500）
            → 最终可能杀死前台应用
```

## Java Heap vs Native Heap

`mem.rss` 包含 Java 堆和 Native 堆，难以区分泄漏来源：

### 增长模式差异

| 堆类型 | 增长模式 | GC 行为 | 常见消费者 |
|-------|---------|--------|-----------|
| Java Heap | 锯齿形（分配 → GC → 分配 → GC），持续上升趋势 = Java 泄漏 | GC 可回收无引用对象 | Java/Kotlin 对象、字符串、集合 |
| Native Heap | 单调递增，不会因 GC 自动回落 | 不受 GC 管理（需手动 free） | Bitmap 像素数据（API 26+）、Native 库、MediaCodec 缓冲区 |

### 区分方法

- **Bitmap 像素数据**：API 26 之前在 Java 堆，API 26+ 迁移到 Native 堆。因此 API 26+ 设备上大图片加载导致 RSS 增长但 Java 堆可能不增长
- **交叉验证**：如果 RSS 增长但 GC 事件未按比例增加，增长源很可能是 Native 堆
- `Debug.getMemoryInfo()` 返回 `nativePss` 和 `dalvikPss`，可以区分两者

### 分析流程

```
RSS 增长
  → 检查 GC 事件频率是否同步增长
    → GC 频率同步增长 → Java 堆泄漏（Java 对象未释放）
    → GC 频率不变或增长较少 → Native 堆泄漏
      → 检查是否有 Bitmap 大量加载
      → 检查是否有 Native 库调用（JNI、MediaCodec）
      → 检查是否有内存映射文件（mmap）
```

| RSS 增长 | GC 频率 | 判断 |
|---------|--------|------|
| 增长 | 同步增长 | Java 堆泄漏 |
| 增长 | 不变 | Native 堆增长（Bitmap/库/mmap） |
| 增长 | 减少 | GC 被抑制或大对象直接进入老年代 |

## 内存压力状态转换

Android 通过 `onTrimMemory` 回调通知应用内存压力状态变化：

### Trim Level 与系统行为

| Trim Level | 值 | 含义 | 应用的正确响应 |
|-----------|---|------|-------------|
| `TRIM_MEMORY_RUNNING_LOW` | 10 | 内存开始紧张 | 释放非必要缓存 |
| `TRIM_MEMORY_RUNNING_CRITICAL` | 15 | 内存非常紧张 | 释放所有非核心资源 |
| `TRIM_MEMORY_UI_HIDDEN` | 20 | UI 不再可见 | 释放 UI 相关缓存 |
| `TRIM_MEMORY_BACKGROUND` | 40 | 应用进入后台 | 释放大部分缓存 |
| `TRIM_MEMORY_MODERATE` | 60 | 应用在 LRU 列表中部 | 释放更多资源 |
| `TRIM_MEMORY_COMPLETE` | 80 | 应用即将被 LMK 杀死 | 释放所有可释放资源 |

### 与 lmkd 的对应关系

- Trim level 对应 lmkd 压力级别
- 如果应用忽略 trim 回调，RSS 会在系统压力下持续增长
- 典型恶化路径：`RUNNING_LOW` → `RUNNING_CRITICAL` → lmkd 杀缓存进程 → 最终前台应用受影响

### 关键分析模式

```
RSS 增长通过内存压力状态：
  RSS 正常增长（RUNNING_LOW 之前）
    → 系统发送 TRIM_MEMORY_RUNNING_LOW
      → 应用释放缓存（RSS 可能短暂下降）
        → 如果应用忽略 trim → RSS 继续增长
          → 系统发送 TRIM_MEMORY_RUNNING_CRITICAL
            → 如果应用仍忽略 → lmkd 开始杀进程
              → 最终前台应用受影响
```

## Perfetto SQL 查询模式

### 目标进程 RSS 时序趋势

```sql
-- RSS 时序趋势
SELECT
  ts,
  value / (1024 * 1024) as rss_mb
FROM counter c
JOIN process_counter_track t ON c.track_id = t.id
JOIN process USING (upid)
WHERE t.name = 'mem.rss'
  AND process.name = '<target_process>'
ORDER BY ts;
```

### 内存跳跃检测

```sql
-- 内存跳跃检测（相邻样本增量 > 10MB）
SELECT
  a.ts,
  a.value / (1024*1024) as rss_before_mb,
  b.value / (1024*1024) as rss_after_mb,
  (b.value - a.value) / (1024*1024) as delta_mb
FROM counter a
JOIN counter b ON a.track_id = b.track_id
  AND b.ts > a.ts
  AND b.ts = (SELECT MIN(ts) FROM counter WHERE ts > a.ts AND track_id = a.track_id)
WHERE a.track_id IN (
  SELECT id FROM process_counter_track WHERE name = 'mem.rss'
)
  AND (b.value - a.value) > 10 * 1024 * 1024  -- > 10MB jump
ORDER BY delta_mb DESC;
```

### LMK 事件查询

```sql
-- trace 期间的 LMK 事件
SELECT *
FROM android_memory_lmk
ORDER BY ts;
```

## 误报识别

### 启动 RSS 增长（误报：冷启动内存增长被误判为泄漏）

- 冷启动时 RSS 从 0 增长到基线（30-100MB）在 1-2 秒内完成
- 这是预期的类加载和初始化过程，不是泄漏
- 正确做法：如果 delta_pct 高但 start_rss_mb 很低（< 30MB），可能是启动增长

### 缓存预热 RSS 跳跃（误报：缓存填充被误判为泄漏）

- 当缓存被填充（图片缓存、数据库缓存等）时，RSS 会跳跃
- 如果 RSS 在跳跃后稳定，这是缓存预热，不是泄漏
- 正确做法：检查跳跃后 RSS 是否稳定（slope ≈ 0），而不仅仅是看跳跃幅度

### GC 引起的 RSS 波动（误报：GC 前后 RSS 变化被误判为趋势）

- Full GC 后 RSS 可能短暂下降（页面被释放），后续分配会带回
- 这种振荡是正常的
- 正确做法：用线性拟合（slope）判断整体趋势，而非单次 GC 前后的 RSS 变化

### 共享库 RSS（误报：共享库内存被误判为应用泄漏）

- 共享库贡献到 RSS 但在进程间共享
- 大部分 RSS 是共享库映射的应用，其内存问题不如全部为私有（匿名）的 RSS 严重
- 正确做法：通过 `dumpsys meminfo` 区分 PSS（按比例分摊）和 USS（独占集大小），USS 持续增长才是真正的泄漏
