# GC 事件分析

## 数据源

- SQL 表: `slice` (WHERE `name GLOB '*GC*'` OR `name GLOB '*GarbageCollector*'`)
- 参数提取: `EXTRACT_ARG(arg_set_id, 'reason')` → gc_reason
- 参数提取: `EXTRACT_ARG(arg_set_id, 'gc_type')` → gc_type
- 排序: `ORDER BY dur DESC` 取最长的 20 个 GC 事件

## 领域知识

### ART 垃圾回收器

Android Runtime (ART) 使用分代垃圾回收器，包含以下空间：

| 空间 | 对象类型 | GC 影响 |
|------|---------|---------|
| Alloc Space | 新分配对象 | Minor GC 回收，速度快 |
| Zygote Space | Zygote 预加载类 | 不参与回收 |
| Large Object Space | > 3 页的大对象（Bitmap 等） | 单独管理，回收代价高 |

### GC 类型

| gc_type | 含义 | 主线程影响 |
|---------|------|-----------|
| `Concurrent` | 并发标记-清除，大部分时间与应用线程并发运行 | 低，仅在标记开始和结束时短暂暂停 |
| `Non-concurrent` (STW) | Stop-the-World，暂停所有应用线程 | 高，全部时间都暂停应用线程 |
| `Sticky` | 粘性 GC，只回收上次 GC 以来分配的对象 | 通常为 Concurrent |
| `Partial` | 部分回收，只回收 Alloc Space | 通常为 Concurrent |
| `Full` | 全堆回收，包括 Large Object Space | 可能是 Non-concurrent |

### GC 触发原因

| gc_reason | 含义 | 是否可避免 |
|-----------|------|-----------|
| `Alloc` | 对象分配时堆空间不足触发 | 减少对象分配 |
| `AllocSticky` | 短生命周期对象快速分配触发 | 避免循环内分配 |
| `Explicit` | 调用 `System.gc()` | 移除 System.gc() 调用 |
| `NativeAlloc` | Native 内存分配触发 | 检查 Native 内存泄漏 |
| `Background` | 后台 GC，应用切到后台时触发 | 正常行为 |
| `Instrumentation` | 性能分析工具触发 | 正常行为 |
| `GCMutex` | 调试模式下的 GC 互斥锁 | 仅调试版本 |

### 影响 GC 性能的关键因素

1. **堆大小**: ART 堆越大，GC 标记-清除耗时越长（与存活对象数量成正比）
2. **对象分配速率**: 短时间内分配大量对象触发频繁 GC
3. **对象生命周期**: 短命对象（方法内临时变量）在 Minor GC 中快速回收，长命对象增加 Full GC 压力
4. **Fragmentation**: 堆碎片化导致分配失败，触发 compaction（压缩），耗时较长

## 数据解读

### Metric 字段

```
gc_events.total_count           # GC 总次数（trace 时间内的 GC 频率）
gc_events.total_pause_ms        # 总暂停时间 ms（所有 GC 暂停的累计）
gc_events.max_pause_ms          # 最长单次暂停 ms（最严重的单次 GC 卡顿）
gc_events.main_thread_pause_ms  # 影响主线程的 GC 总暂停 ms

gc_events.events[]:
  name       # GC 事件名称（含类型和原因信息）
  ts_ns      # 时间戳（ns）
  dur_ms     # 持续时间 ms
  gc_reason  # 触发原因
  gc_type    # GC 类型
```

### 主线程影响判断

trace 中影响主线程的 GC 事件通过名称判断：
- `"GC: Wait For Concurrent"` — 主线程分配时遇到并发 GC 正在进行，需等待其完成
- `"GC: Alloc"` — 主线程分配触发的 GC，直接暂停主线程

`main_thread_pause_ms` 是这两个类型事件的累计暂停时间。

### 分析决策树

```
1. 检查 GC 频率
   ├─ total_count / trace_duration_s > 2 次/秒 → 高频 GC，堆压力过大
   └─ 正常频率 → 继续分析单次影响

2. 检查单次最长暂停
   ├─ max_pause_ms > 帧预算 → P0，单次 GC 导致 jank
   ├─ max_pause_ms > 10ms → P1，GC 暂停明显
   └─ max_pause_ms < 5ms → 正常范围

3. 检查主线程影响
   ├─ main_thread_pause_ms > trace 时长的 5% → 主线程被 GC 严重拖慢
   └─ main_thread_pause_ms ≈ 0 → 主线程未受 GC 影响

4. 检查 GC 原因分布
   ├─ 大量 Alloc → 分配速率过高，优化对象分配
   ├─ 大量 AllocSticky → 短命对象过多，检查循环内分配
   ├─ 有 Explicit → 找到并移除 System.gc() 调用
   └─ NativeAlloc → 检查 Bitmap、ByteBuffer 等 Native 分配
```

### 关键模式识别

| 模式 | 数据特征 | 根因推测 |
|------|---------|---------|
| 频繁 Concurrent GC | count > 30, 每次 < 5ms | 对象分配速率高但存活时间短 |
| 偶发长 GC | count < 10, max > 30ms | 可能有内存泄漏或大对象分配 |
| Sticky GC 主导 | gc_reason 主要是 AllocSticky | 循环或高频回调内分配临时对象 |
| Full GC 出现 | gc_type 含 Full | 堆接近满或碎片化严重 |
| System.gc 触发 | gc_reason 含 Explicit | 第三方库或代码中调用了 System.gc() |

### ART GC 流程详解

ART 的并发 GC 分为四个阶段，每个阶段对应用线程有不同影响：

| 阶段 | 操作 | 应用线程影响 | 在 slice 中的标识 |
|------|------|------------|-----------------|
| 1. 标记初始化 | 暂停所有线程，扫描根集 | 短暂暂停（< 1ms） | `GC: Pause` |
| 2. 并发标记 | 标记可达对象，与应用线程并发运行 | 几乎无影响 | `GC: Concurrent` |
| 3. 标记结束 | 暂停所有线程，处理脏引用 | 短暂暂停（1-5ms） | `GC: Pause` |
| 4. 回收 | 清除不可达对象，回收内存 | 几乎无影响 | `GC: Sweep` |

当主线程在阶段 1 或 3 期间尝试分配对象时，会进入 `WaitForGcToComplete` 状态，在 trace 中表现为 `"GC: Wait For Concurrent"` 切片。

### GC 与内存压力 关系

| 指标组合 | 内存状态 | 风险等级 |
|---------|---------|---------|
| count 低, max 低, RSS 稳定 | 健康状态 | 无风险 |
| count 高, max 低, RSS 缓慢增长 | 分配频繁但可回收 | 低风险，可优化 |
| count 中, max 高, RSS 持续增长 | 可能存在内存泄漏 | 高风险 |
| count 高, max 高, RSS 接近上限 | 即将 OOM | 极高风险 |
| Full GC 频繁出现 | 堆碎片化严重 | 中风险 |

## Perfetto SQL 深入查询

### GC 事件与 jank 帧时间重叠分析

```sql
-- 查找 GC 暂停与 jank 帧（>16.67ms）的时间重叠
SELECT
  gc.name AS gc_event, gc.dur / 1e6 AS gc_ms,
  frame.name AS frame_slice, frame.dur / 1e6 AS frame_ms
FROM slice gc
JOIN slice frame ON
  gc.track_id IN (SELECT id FROM thread_track WHERE utid = (SELECT utid FROM thread WHERE name = 'main'))
  AND frame.track_id = gc.track_id
  AND gc.ts < frame.ts + frame.dur AND gc.ts + gc.dur > frame.ts
WHERE (gc.name GLOB '*GC*' OR gc.name GLOB '*GarbageCollector*')
  AND gc.dur > 5e6
  AND frame.dur > 16e6
ORDER BY gc.dur DESC LIMIT 10;
```

### GC 原因频率分布

```sql
-- 按 GC 原因分组统计频率和总耗时
SELECT
  EXTRACT_ARG(arg_set_id, 'reason') AS reason,
  COUNT(*) AS count,
  SUM(dur) / 1e6 AS total_ms,
  AVG(dur) / 1e6 AS avg_ms,
  MAX(dur) / 1e6 AS max_ms
FROM slice
WHERE name GLOB '*GC*' OR name GLOB '*GarbageCollector*'
GROUP BY reason
ORDER BY total_ms DESC;
```

## 严重度标准

- **P0**: GC pause > 帧预算 (16.67ms@60Hz, 8.33ms@120Hz) 且影响主线程
- **P1**: GC pause > 10ms
- **P2**: GC pause > 1ms

## 常见误判

| 数据现象 | 易误判为 | 实际可能是 |
|---------|---------|-----------|
| Concurrent GC 耗时 50ms | 严重 GC 问题 | Concurrent GC 大部分时间与 app 并发，实际暂停仅 1-5ms |
| Background GC 频繁 | 应用有内存泄漏 | 应用切到后台后的正常行为，系统主动回收 |
| Instrumentation GC | 需要优化 | 调试/分析工具触发，Release 版本不会出现 |
| NativeAlloc GC | Java 层有泄漏 | Native 层（Bitmap/ByteBuffer）分配触发，需检查 Native 代码 |

## 与其他维度的关联

| 关联维度 | 关联模式 | 分析方法 |
|---------|---------|---------|
| 帧时间线 (ui-jank) | GC pause 导致 jank 帧 | 对比 GC 事件时间戳与 jank 帧时间戳 |
| 内存趋势 (memory-analysis) | 频繁 GC 反映内存压力大 | RSS 增长斜率 + GC 频率联合判断 |
| 锁竞争 (lock-contention) | GC pause 期间持有 heap 锁 | GC 事件期间其他线程 futex 等待增加 |
| UI 分析 (ui-jank) | GC 暂停打断 UI 线程的 doFrame | doFrame 切片内嵌套 GC 切片 |

## 优化方向

### 减少对象分配

1. **避免循环内分配**: 将对象创建移到循环外，或使用对象池
2. **使用原始类型**: `IntArray` 替代 `List<Int>`，避免自动装箱
3. **避免字符串拼接**: 循环内使用 `StringBuilder` 而非 `+` 拼接
4. **Kotlin 避免高频 lambda 分配**: 使用 `inline` 函数

### ART 特定优化

1. **Large Object Space**: Bitmap 和大数组分配在此空间，单独管理。注意 Bitmap 复用 (`BitmapFactory.Options.inBitmap`)
2. **避免显式 GC**: 搜索并移除 `System.gc()` 调用，某些第三方库（如 Gson）可能触发
3. **Foreground GC 抑制**: Android 12+ 可通过 `AndroidManifest` 的 `<profileable>` 减少调试 GC

### 典型 GC 优化模式

```kotlin
// 反模式: 循环内分配
fun processItems(items: List<Data>) {
    items.forEach { item ->
        val result = process(item)  // 每次 forEach 创建 lambda 对象
        val temp = ByteArray(1024)  // 循环内分配
    }
}

// 推荐: 避免循环内分配
fun processItems(items: List<Data>) {
    val temp = ByteArray(1024)      // 复用缓冲区
    for (item in items) {           // 普通 for 循环，无 lambda
        process(item, temp)
    }
}

// 反模式: onDraw 内创建对象
fun onDraw(canvas: Canvas) {
    val paint = Paint()             // 每帧创建 Paint
    paint.color = Color.RED
    canvas.drawRect(rect, paint)
}

// 推荐: 复用对象
private val paint = Paint().apply { color = Color.RED }  // 初始化一次
fun onDraw(canvas: Canvas) {
    canvas.drawRect(rect, paint)
}
```

### ART GC 算法演进

ART 垃圾回收器经历了多个版本的演进，每个版本在暂停时间和并发性上有显著改进：

| Android 版本 | GC 算法 | 特点 | 典型暂停时间 |
|-------------|---------|------|------------|
| Android 5-6 (ART) | Non-concurrent Compacting GC | Pause-all，全堆扫描 + 压缩 | 20-100ms |
| Android 7-10 | Concurrent Copying (CC) | 大部分并发，仅在 thread flip 时短暂暂停 | 1-5ms |
| Android 11+ | Generational CC (GenCC) | 年轻代 copying + 老年代 concurrent marking | < 2ms (young), 1-5ms (old) |
| Android 12+ | CC with concurrent reference processing | 引用处理（WeakReference/PhantomReference）也并发执行 | < 2ms |

关键洞察：
- Android 7+ 的 Concurrent GC 暂停时间应 < 5ms
- Android 11+ 的年轻代 GC 暂停时间应 < 2ms
- 任何 GC 暂停 > 10ms 在现代 Android 设备上都是异常的
- 如果 trace 数据显示 GC 暂停频繁超过上述阈值，应检查是否存在特殊条件（调试模式、堆极度紧张、系统内存压力）

### GC 暂停预算计算

GC 暂停是否可接受取决于设备的刷新率和帧预算：

#### 帧预算计算

```
帧预算 (frame_budget) = 1000 / refresh_rate (ms)

常见设备:
  60Hz: 16.67ms
  90Hz: 11.11ms
  120Hz: 8.33ms
  144Hz: 6.94ms
```

#### GC 暂停预算

```
GC 暂停预算 = frame_budget * 0.3  (30% 经验法则)

  60Hz:  5.0ms
  90Hz:  3.3ms
  120Hz: 2.5ms
  144Hz: 2.1ms
```

判断逻辑：
- 如果 `gc_events.max_pause_ms > GC 暂停预算`，GC 正在消耗过多帧时间
- 如果 `gc_events.max_pause_ms > frame_budget`，GC 暂停直接导致 jank（P0 级别）

#### GC 开销比

```
GC 开销比 = total_pause_ms / trace_duration_ms * 100

  > 5%: 过度 GC，内存管理需要优化（P0 级别）
  > 2%: 中等 GC 压力，值得关注（P1 级别）
  < 1%: 正常范围
```

注意：`total_pause_ms` 应只计算 pause 阶段（不是 Concurrent GC 的整个 duration），因为只有 pause 阶段会阻塞应用线程。

### GC 触发链分析

ART GC 有一个从轻到重的升级链，理解这个链条有助于定位根因：

#### Alloc -> Sticky -> Partial -> Full 升级链

```
对象分配失败
  → AllocSticky GC (只回收上次 GC 以来分配的对象，速度最快)
    → 如果回收后空间仍不足
      → Partial GC (回收 Alloc Space，跳过 Zygote Space)
        → 如果回收后空间仍不足
          → Full GC (回收所有空间，包括 Large Object Space)
            → 如果回收后空间仍不足
              → OOM (OutOfMemoryError)
```

#### 各级别 GC 的诊断意义

| GC 级别 | 频率阈值 | 诊断结论 |
|---------|---------|---------|
| AllocSticky | > 20次/10s trace | 热路径上存在过多短命对象分配（如 onDraw、onBindViewHolder 内分配） |
| Alloc (Partial) | > 10次/10s trace | 对象分配速率超过正常水平，堆空间回收效率不足 |
| Full | 任何出现 | 红旗：堆接近满、碎片化严重或存在内存泄漏 |
| Explicit | 任何出现 | 代码或第三方库调用了 `System.gc()`，应定位并移除 |

#### GC-for-cause 连锁反应

```
线程 A 分配失败 → 触发 Concurrent GC
  → 线程 B 也分配失败 → "GC: Wait For Concurrent" (等待线程 A 触发的 GC 完成)
    → 线程 C 也分配失败 → "GC: Wait For Concurrent"
      → 多个线程同时被阻塞 → 级联暂停
```

识别方法：同一时间窗口内多个线程出现 "GC: Wait For Concurrent" 切片，且有一个 "GC: Concurrent" 切片正在执行。

#### NativeAlloc 触发模式

如果 `gc_reason` 为 `NativeAlloc`，应检查以下场景：
- Bitmap 分配未复用（每次创建新 Bitmap 而非复用现有 Bitmap）
- `ByteBuffer.allocateDirect()` 累积（未显式释放 DirectByteBuffer）
- Native 库内存泄漏（JNI 代码中 malloc 未对应 free）
- MediaPlayer / MediaCodec 资源未释放

### 对象分配热定位

GC 事件本身不直接显示分配了什么对象，但通过分析 GC 模式可以推断分配来源：

#### GC 模式与分配来源对照

| GC 模式 | 分配来源推测 | 定位方法 |
|---------|------------|---------|
| 高频 Sticky GC (每 50-100ms 一次) | 热路径分配：onDraw、onBindViewHolder、scroll handler、动画回调 | 交叉比对 cpu_hotspots 中的高频方法 |
| 某操作后突发 Alloc GC | 该操作触发大量分配：加载列表、解析 JSON、批量数据处理 | 查看 GC 事件前的 SI$ slice，定位触发操作 |
| Full GC + 堆大小持续增长 | 大对象分配：Bitmap、byte 数组、集合扩容 | 交叉比对 memory_trend 中 RSS 增长节点 |
| NativeAlloc GC | Native 层分配：Bitmap 解码、DirectByteBuffer、JNI | 搜索代码中 Bitmap.createBitmap、ByteBuffer.allocateDirect |

#### 交叉分析方法

1. **与 cpu_hotspots 交叉**：CPU 火焰图中的高频方法如果是分配密集型方法（如 `toString()`、`StringBuilder.append()`），这些方法就是分配热点
2. **与 SI$ slices 交叉**：检查哪些 SI$ slice 与 GC 事件在时间上重叠——重叠的 slice 对应的操作很可能就是分配来源
3. **与 frame_timeline 交叉**：如果 GC 事件集中在特定 jank 帧期间，该帧执行的 UI 操作（measure/layout/draw）可能包含分配

```sql
-- GC 事件与 SI$ slice 时间重叠分析
SELECT
  gc.name AS gc_event,
  gc.dur / 1e6 AS gc_ms,
  si.name AS si_slice,
  si.dur / 1e6 AS si_ms
FROM slice gc
JOIN slice si ON
  gc.track_id IN (SELECT id FROM thread_track WHERE utid = (SELECT utid FROM thread WHERE name = 'main'))
  AND si.track_id = gc.track_id
  AND gc.ts < si.ts + si.dur AND gc.ts + gc.dur > si.ts
WHERE gc.name GLOB '*GC*'
  AND si.name GLOB 'SI$*'
ORDER BY gc.dur DESC
LIMIT 20;
```

### Perfetto SQL 查询模式

```sql
-- GC 事件按 reason 和 type 分组统计
SELECT
  slice.name,
  EXTRACT_ARG(arg_set_id, 'reason') as gc_reason,
  EXTRACT_ARG(arg_set_id, 'gc_type') as gc_type,
  dur / 1e6 as dur_ms,
  ts
FROM slice
JOIN thread_track ON slice.track_id = thread_track.id
JOIN thread USING (utid)
WHERE (slice.name GLOB '*GC*' OR slice.name GLOB '*GarbageCollector*')
  AND thread.name = 'main'
ORDER BY dur DESC
LIMIT 20;

-- GC 频率分析 (每秒直方图)
SELECT
  CAST(ts / 1e9 AS INTEGER) as second,
  COUNT(*) as gc_count,
  SUM(dur) / 1e6 as total_pause_ms,
  MAX(dur) / 1e6 as max_pause_ms
FROM slice
WHERE name GLOB '*GC*'
GROUP BY second
ORDER BY gc_count DESC;

-- GC 暂停与 doFrame 时间重叠 (定位 GC 导致的 jank)
SELECT
  gc.name AS gc_event,
  gc.dur / 1e6 AS gc_ms,
  frame.name AS frame_op,
  frame.dur / 1e6 AS frame_ms
FROM slice gc
JOIN slice frame ON
  gc.track_id = frame.track_id
  AND gc.ts < frame.ts + frame.dur AND gc.ts + gc.dur > frame.ts
WHERE gc.name GLOB '*GC*' AND gc.dur > 1e6
  AND frame.name GLOB '*doFrame*'
ORDER BY gc.dur DESC
LIMIT 15;
```

### 误报识别

| 误报类型 | 数据特征 | 判断依据 |
|---------|---------|---------|
| **启动 GC 突发** | 冷启动前 1-2 秒 GC 频率显著高于平均值 | 类加载和初始化分配导致的高 GC 频率是预期行为。仅当单次暂停 > 30ms 时才需关注 |
| **Concurrent GC "duration" 误读** | Concurrent GC slice duration 显示 50-100ms | Concurrent GC 的 duration 包含并发阶段和暂停阶段，仅暂停阶段影响主线程。查看 "GC: Wait For Concurrent" 获取实际暂停影响 |
| **后台 GC** | `gc_reason = 'Background'` 且应用处于后台 | 应用切到后台后系统主动触发内存回收，不影响 UI 性能，应从分析中排除 |
| **Instrumentation GC** | `gc_reason = 'Instrumentation'` | 性能分析工具（包括 Perfetto 自身）可能触发额外 GC。Release 版本不会出现，应从分析中排除 |
| **GC 与 Heap Dump 同时发生** | GC 暂停异常高且与 heap dump 时间重叠 | `android.java_hprof` 数据源会触发 Full GC 来获取一致性堆快照，这是工具开销而非应用问题 |
