# UI / 帧率分析

## 数据源

- SQL 表: `actual_frame_timeline_slice` (实际帧耗时) + `expected_frame_timeline_slice` (预期帧耗时)
- SI$ 自定义切片: `SI$RV#`, `SI$view#`, `SI$inflate#`, `SI$compose#`
- 关联数据: `collect_view_slices()` 提供详细的 View 管线切片（doFrame → measure → layout → draw）

## 领域知识

### Android 渲染管线

```
App (UI Thread)           RenderThread          SurfaceFlinger
    │                         │                      │
    ├─ input event             │                      │
    ├─ animation               │                      │
    ├─ measure ───┐            │                      │
    ├─ layout  ───┤ doFrame    │                      │
    ├─ draw    ───┘            │                      │
    │                         ├─ DrawOp → OpenGL      │
    │                         ├─ flush commands       │
    │                         ├─ dequeueBuffer        │
    │                         └─ queueBuffer ─────────┤
    │                                                  ├─ compose layers
    │                                                  └─ present frame
```

### 帧预算与 Jank 定义

| 刷新率 | 帧预算 | Jank 阈值 | 严重 Jank |
|--------|--------|-----------|-----------|
| 60 Hz | 16.67ms | > 16.67ms | > 50ms (3x) |
| 90 Hz | 11.11ms | > 11.11ms | > 33ms (3x) |
| 120 Hz | 8.33ms | > 8.33ms | > 25ms (3x) |

**Jank**: 实际帧耗时 (`actual_frame_timeline_slice.dur`) 超过预期帧耗时 (`expected_frame_timeline_slice.dur`)。

**Big Jank**: 实际帧耗时超过 3 倍帧预算，用户明显感知卡顿。

### 帧渲染各阶段耗时分析

| 阶段 | 切片名称 | 典型耗时 | 瓶颈信号 |
|------|---------|---------|---------|
| Input | `Input Event Dispatch` | 1-5ms | > 8ms，事件处理逻辑过重 |
| Animation | `Animate` | 1-3ms | > 5ms，动画计算复杂 |
| Measure | `SI$view#*.onMeasure` | 1-5ms | > 10ms，布局层级深或自定义 measure 复杂 |
| Layout | `SI$view#*.onLayout` | 0.5-3ms | > 5ms，复杂布局规则 |
| Draw | `SI$view#*.onDraw` | 2-10ms | > 16ms，自定义绘制过重 |
| RV Bind | `SI$RV#*.onBindViewHolder` | 1-5ms | > 8ms，绑定逻辑过重 |
| RV Create | `SI$RV#*.onCreateViewHolder` | 5-20ms | > 16ms，ViewHolder 创建包含 inflate |
| Inflate | `SI$inflate#*` | 5-30ms | > 16ms，布局文件复杂 |
| Sync & Draw | `DrawFrame` | 1-5ms | > 8ms，DisplayList 过大 |
| GPU | `GPU Completion` | 1-10ms | > 16ms，渲染指令过多 |

### RecyclerView 性能模型

RecyclerView 的核心性能参数：

| 缓存层级 | 缓存名 | 大小 | 作用 |
|---------|--------|------|------|
| Level 1 | Attached Scrap | 无限制 | 布局期间临时分离的 ViewHolder |
| Level 2 | Cached Views | 2 (默认) | 离屏缓存，不需要重新 bind |
| Level 3 | ViewCacheExtension | 自定义 | 开发者自定义缓存 |
| Level 4 | RecycledViewPool | 5/type | 需要重新 bind，但不需要 inflate |

性能瓶颈诊断：
- `onCreateViewHolder` 频繁调用 → 缓存池太小或 item type 过多
- `onBindViewHolder` 耗时长 → 绑定逻辑过重（IO、计算、类型转换）
- 滚动卡顿但 onBind/create 不高 → 可能是 layout/draw 阶段慢

### Jetpack Compose 性能

Compose 的重组（Recomposition）是性能关键：

| 概念 | 说明 | 性能影响 |
|------|------|---------|
| Recomposition | 状态变化时重新执行 Composable 函数 | 范围越大越慢 |
| Skip | 读取的状态未变化，跳过重组 | 高效，是目标状态 |
| Composition | 构建 Compose 树节点 | 慢，应最小化 |
| Layout | 测量和放置节点 | 中等 |
| Drawing | 绘制到 Canvas | 取决于复杂度 |

Compose 性能工具标记:
- `mutableStateOf` → 触发重组的最小单元
- `remember` → 缓存计算结果，避免重组时重新计算
- `derivedStateOf` → 派生状态，只在源状态变化时重新计算
- `key` → 列表项标识，避免不必要的重组

## 数据解读

### 关键数据字段

```
frame_timeline:
  jank_count             # jank 帧数量
  big_jank_count         # 严重 jank 帧数量 (>3x 帧预算)
  jank_detail[]          # jank 帧详情
  slowest_frames[]       # 最慢的帧

view_slices:
  slowest_slices[]       # 耗时最长的 View 操作切片
  slice_summary          # 按 SI$ 标签分类的统计

block_events[]           # 主线程卡顿事件（BlockMonitor 检测）
```

### 分析决策树

```
1. 检查 jank_count 和 big_jank_count
   ├─ big_jank_count > 0 → P0，用户明显感知卡顿
   ├─ jank_count > 总帧数 5% → P1，帧率不稳定
   └─ jank_count = 0 → 无 jank，帧率正常

2. 分析最慢帧的根因
   ├─ doFrame 内 measure/layout 耗时长 → 布局问题
   │   ├─ SI$inflate 存在 → 布局文件复杂，减少层级
   │   └─ SI$view#*.onMeasure 多 → 自定义 View measure 复杂
   ├─ doFrame 内 draw 耗时长 → 绘制问题
   │   ├─ SI$view#*.onDraw 存在 → 自定义绘制过重
   │   └─ 无自定义 onDraw → 可能是过度绘制
   ├─ doFrame 内 RV 相关切片耗时长 → 列表问题
   │   ├─ onCreateViewHolder → ViewHolder 缓存不足
   │   └─ onBindViewHolder → 绑定逻辑过重
   └─ doFrame 内无明确慢切片 → 可能是 GC 或调度延迟
       ├─ 同一时间段有 GC pause → GC 导致的 jank
       └─ 同一时间段有调度延迟 → CPU 资源不足

3. 分析 jank 帧的时间分布
   ├─ 集中在启动阶段 → 初始化导致的 jank
   ├─ 分散在滚动期间 → 滚动性能问题
   └─ 周期性出现 → 可能是后台任务周期性干扰
```

## 严重度标准

- **P0**: 帧耗时 > 3x 帧预算 (>50ms@60Hz, >25ms@120Hz)
- **P1**: 帧耗时 > 帧预算 (>16.67ms@60Hz, >8.33ms@120Hz)
- **P2**: 帧耗时 > 50% 帧预算

## 与其他维度的关联

| 关联维度 | 关联模式 | 分析方法 |
|---------|---------|---------|
| GC 事件 (gc-analysis) | GC pause 打断 doFrame | jank 帧时间窗口内是否有 GC 事件 |
| 锁竞争 (lock-contention) | 主线程锁等待导致 doFrame 延迟 | jank 帧期间 main 线程 futex 等待 |
| 文件 IO (io-analysis) | 主线程 IO 阻塞导致 doFrame 延迟 | jank 帧期间 main 线程 IO 等待 |
| CPU 降频 (cpu-throttling) | 降频导致帧渲染超预算 | 降频时间段与 jank 帧对齐 |
| CPU 调度 (cpu-scheduling) | 调度延迟导致 doFrame 延迟启动 | main 线程 runnable 延迟 + jank |
| Binder IPC (binder-ipc) | 主线程 IPC 等待导致 jank | jank 帧期间 Binder 等待 |
| 内存趋势 (memory-analysis) | GC 频繁因内存压力，GC 导致 jank | memory_trend + gc_events + jank 链式关联 |

## 优化方向

### RecyclerView 优化

1. **DiffUtil**: 使用 `ListAdapter` + `DiffUtil` 精确更新，避免全量 `notifyDataSetChanged()`
2. **ViewHolder 缓存**: 增大 `recycledViewPool` 大小、设置 `setItemViewCacheSize`
3. **预加载**: `LinearLayoutManager.setInitialPrefetchItemCount()` 预取即将显示的 item
4. **简化 item 布局**: 减少 item 布局层级，使用 ConstraintLayout 替代嵌套 LinearLayout
5. **避免 onCreateViewHolder 中的 inflate**: 使用 `layoutId` 构造方法让 RecyclerView 缓存处理

### 布局优化

1. **减少层级**: ConstraintLayout 实现扁平布局，避免多层嵌套
2. **ViewStub 延迟加载**: 不立即需要的布局使用 ViewStub
3. **Merge 标签**: 被_include_的布局根节点使用 `<merge>` 减少一层
4. **异步 Inflate**: `AsyncLayoutInflater` 在后台线程 inflate 布局

### Compose 优化

1. **避免不必要重组**: 使用 `remember`、`derivedStateOf`、`LaunchedEffect` 控制重组范围
2. **key 标识**: 列表使用 `key { item.id }` 帮助 Compose 跟踪项目
3. **稳定类型**: 确保数据类是 `@Stable` 或 `@Immutable`，避免 Compose 无法跳过重组
4. **LazyColumn 性能**: 使用 `contentType` 参数帮助 LazyColumn 复用

### 典型 UI 优化模式

```kotlin
// 反模式: onBindViewHolder 中创建对象
override fun onBindViewHolder(holder: VH, position: Int) {
    val item = items[position]
    holder.title.text = item.title
    Glide.with(context).load(item.url).into(holder.image)  // 每次绑定创建 RequestBuilder
}

// 推荐: 复用和预计算
override fun onBindViewHolder(holder: VH, position: Int) {
    val item = items[position]
    holder.bind(item)  // bind 方法内复用已有对象
}

// 反模式: 布局嵌套
<LinearLayout>
    <LinearLayout>
        <LinearLayout>
            <TextView />  <!-- 3 层嵌套 -->
        </LinearLayout>
    </LinearLayout>
</LinearLayout>

// 推荐: ConstraintLayout 扁平化
<ConstraintLayout>
    <TextView />  <!-- 1 层 -->
</ConstraintLayout>
```

### SurfaceFlinger 合成类型

SurfaceFlinger 使用 3 种合成类型：

| 合成类型 | 标记 | 执行者 | 性能 |
|---------|------|--------|------|
| Device composition | HWC DEVICE | 硬件合成器（Hardware Composer） | 最快，零 GPU 开销 |
| GPU composition | HWC GPU | GPU 合成图层到帧缓冲 | 中等开销 |
| Client composition | HWC CLIENT | SurfaceFlinger 软件渲染 | 最慢，作为降级方案 |

- 合成类型影响帧耗时：Client composition 每帧增加 5-15ms
- 图层过多（>4-8 个，取决于硬件）会强制使用 GPU 或 Client 合成
- 降级到 Client 合成的常见原因：
  - 过多重叠图层（Dialog、Popup、过多 Window）
  - 含非平凡混合效果的图层（alpha 混合、旧设备上的圆角）
  - 受保护内容（DRM）
- 在 Perfetto 中：`android.surfaceflinger` 表包含合成类型信息

### Buffer Queue 与三重缓冲

- Buffer Queue 工作模型：App 生产缓冲区 → SurfaceFlinger 消费缓冲区
- 双缓冲：2 个缓冲区（1 个正在显示，1 个正在绘制）。如果 App 绘制慢，会阻塞等待前缓冲区释放。
- 三重缓冲：3 个缓冲区（1 个显示中，1 个排队等待 SF，1 个正在绘制）。允许 App 在 SF 尚未消费上一帧时继续绘制。
- 当两个后备缓冲区都满，且 App 尝试 dequeue 另一个缓冲区 → `dequeueBuffer` 阻塞 → GPU 停滞 → 帧卡顿
- 检测模式：RenderThread 中 `dequeueBuffer` 阻塞表明 Buffer Queue 已满 → App 生产帧的速度快于 SF 消费速度（或 SF 本身慢）
- 三重缓冲增加 1 帧延迟但减少 jank。Android 对大多数 Surface 默认使用三重缓冲

| 缓冲区数量 | 延迟 | Jank 风险 | 适用场景 |
|-----------|------|----------|---------|
| 双缓冲 | 低 | 高（App 阻塞等待） | 低延迟要求的 VR/AR |
| 三缓冲 | +1 帧 | 低（App 可继续绘制） | 常规 UI 渲染 |

### HWUI 渲染管线细节

HWUI（Hardware UI）是 Android 的硬件加速 2D 渲染管线。管线阶段：

1. `DrawOp` 录制：UI 线程将绘制命令记录到 DisplayList
2. `DrawOp` 合并：RenderThread 合并兼容的操作（例如多个使用相同 Paint 的 `drawRect`）
3. `DrawOp` 批处理：批处理后的操作以更少的 draw call 发送给 GPU
4. `Texture` 上传：Bitmap 纹理上传到 GPU（大 Bitmap 可能很慢）

常见 HWUI 瓶颈：
- 大 DisplayList：View 数量多 → DrawOp 数量多 → 录制耗时长
- 纹理上传：大 Bitmap 上传阻塞 RenderThread
- `WebView` 渲染：WebView 绘制命令复杂且耗时

检测模式：如果 RenderThread 的 `DrawFrame` 慢但 GPU Completion 快，瓶颈在 DisplayList 录制或纹理上传。

### Choreographer VSYNC 模型

- `Choreographer` 将 UI 工作与 VSYNC 信号同步
- 每个 VSYNC 触发一次 `doFrame()` 回调，处理顺序：Input → Animation → Traversal (measure/layout/draw)
- 如果 doFrame 耗时超过一个 VSYNC 周期，帧会被丢弃
- VSYNC 偏移：大多数设备上，App 的 VSYNC 信号相对于显示 VSYNC 偏移约 2ms，这意味着 App 每帧少了 2ms 可用时间
- `Choreographer#doFrame` 时间戳在 slice 表中显示帧开始时间
- 检测模式：如果连续 doFrame 时间戳之间的间隔 > 16.67ms（60Hz），则发生了丢帧。间隔减去 16.67ms 即为空闲时间

```
VSYNC 时序模型 (60Hz):

Display VSYNC:  |-----16.67ms-----|-----16.67ms-----|
App VSYNC:        |  (offset ~2ms)   |
App doFrame:      |===doFrame===|      |===doFrame===|
                                ↑                  ↑
                        正常帧完成       如果超时，下帧被丢弃
```

### 帧耗时分解公式

- 总帧时间 = input_dispatch + animation + traversal + sync_and_draw + gpu_completion + present
- 其中：
  - `input_dispatch` = 输入事件处理时间（通常 <2ms）
  - `animation` = ObjectAnimator/ValueAnimator 计算（通常 <2ms）
  - `traversal` = measure + layout + draw（DisplayList 录制）
  - `sync_and_draw` = RenderThread sync + draw（上传纹理，刷新 GPU 命令）
  - `gpu_completion` = GPU 渲染时间（异步，通过 fence 测量）
  - `present` = SurfaceFlinger 合成 + 显示扫描输出
- 如果 `traversal` > 50% 帧预算 → 需要布局/绘制优化
- 如果 `gpu_completion` > 50% 帧预算 → 需要降低渲染复杂度
- 如果 `sync_and_draw` 较高 → 检查纹理上传或大 DisplayList

| 瓶颈阶段 | 占比阈值 | 优化方向 |
|---------|---------|---------|
| traversal > 50% | 布局/绘制过重 | 减少层级、简化 onDraw |
| gpu_completion > 50% | GPU 渲染过重 | 降低渲染复杂度、减少 overdraw |
| sync_and_draw 高 | 纹理上传/DisplayList 大 | 减少图片尺寸、优化 DisplayList |
| input_dispatch 高 | 事件处理过重 | 简化 touch 处理逻辑 |

### Perfetto SQL 查询模式

```sql
-- Jank frame analysis with expected vs actual
SELECT
  actual.name,
  actual.ts,
  actual.dur / 1e6 as actual_ms,
  expected.dur / 1e6 as expected_ms,
  (actual.dur - expected.dur) / 1e6 as overrun_ms
FROM actual_frame_timeline_slice actual
JOIN expected_frame_timeline_slice expected
  ON actual.track_id = expected.track_id
  AND actual.ts = expected.ts
WHERE actual.dur > expected.dur  -- only jank frames
ORDER BY overrun_ms DESC
LIMIT 20;

-- RV bind time distribution
SELECT
  slice.name,
  COUNT(*) as bind_count,
  AVG(slice.dur) / 1e6 as avg_bind_ms,
  MAX(slice.dur) / 1e6 as max_bind_ms,
  SUM(slice.dur) / 1e6 as total_bind_ms
FROM slice
WHERE slice.name LIKE 'SI$RV%'
  AND slice.name LIKE '%onBind%'
GROUP BY slice.name
ORDER BY avg_bind_ms DESC;
```

### 误报识别

- **resume 后首帧**：`onResume` 之后的第一个 `doFrame` 通常较慢，因为系统需要建立渲染管线。单个慢首帧是正常的。
- **屏幕旋转帧**：配置变更触发完整的 layout/draw。屏幕旋转期间的 jank 是预期的。
- **窗口调整/分屏**：切换到分屏模式或调整窗口大小触发 relayout。这些过渡期间的 jank 是预期的。
- **后台帧渲染**：如果 App 在后台渲染帧（例如持续动画），由于优先级降低，这些帧可能较慢。这类 jank 帧不影响用户体验。
- **GPU 预热**：硬件加速初始化后的前几帧可能因 shader 编译而较慢。这是一次性开销。
