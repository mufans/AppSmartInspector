# CPU 降频检测

## 数据源

- SQL 表: `cpu_counter_track` + `counter` (CPU 频率计数器)
- 查询: 按 `cpu` 分组，计算每核的 `MIN/MAX/AVG(value)/1e3` 得到频率统计
- 降频判定: `(1 - avg_mhz / max_mhz) * 100 > 50%` 视为降频核心

## 领域知识

### Thermal Throttling 机制

Android 设备的 SoC (System on Chip) 内置温度传感器和热管理策略。当芯片温度超过阈值时，内核 thermal driver 自动降低 CPU 频率以减少发热：

| 温度等级 | 系统响应 | 对应用的影响 |
|---------|---------|------------|
| 正常 (< 45°C) | 全频运行 | 无影响 |
| 轻度过热 (45-55°C) | 频率限制在最高频的 70-80% | 轻微性能下降 |
| 中度过热 (55-65°C) | 频率限制在最高频的 40-60% | 明显卡顿，jank 增加 |
| 重度过热 (> 65°C) | 频率限制在最低频，可能关闭部分核心 | 严重卡顿 |

### 降频的直接影响

1. **帧预算压缩**: 原本 16.67ms@60Hz 可完成的帧渲染，降频后可能需要 30-50ms
2. **GC 耗时增加**: GC 标记-清除是 CPU 密集型，降频直接延长 GC 暂停时间
3. **IO 调度变慢**: 虽然 IO 延迟主要受存储设备影响，但低频 CPU 处理 IO 中断更慢
4. **Binder 处理延迟**: 服务端低频导致 IPC 响应变慢

### 大小核频率特征

| 核心类型 | 典型最高频率 | 典型最低频率 | 说明 |
|---------|------------|------------|------|
| 超大核 (Prime) | 2.8-3.2 GHz | 300 MHz | Snapdragon 8 系列独有 |
| 大核 (Big) | 2.0-2.8 GHz | 300 MHz | 高性能核心 |
| 小核 (Little) | 1.5-2.0 GHz | 300 MHz | 低功耗核心 |

降频时通常大核先降频，小核维持。如果大核 avg 频率远低于 max，说明热管理已介入。

### EAS (Energy Aware Scheduling)

Android 10+ 使用 Energy Aware Scheduling，调度器会考虑能效：
- 小任务优先调度到小核
- 长时间运行的大任务调度到大核
- 温度升高时主动将任务迁移到小核

这可能导致关键线程（如 UI 线程）被调度到低频小核，即使未触发热管理也会感受到频率降低。

## 数据解读

### Metric 字段

```
cpu_throttling.cpu_freq_by_core:
  "<core_id>":
    min_mhz     # 该核心最低频率 MHz
    max_mhz     # 该核心最高频率 MHz
    avg_mhz     # 该核心平均频率 MHz（关键指标）
    samples     # 频率采样点数

cpu_throttling.throttled_cores[]:
  core          # 核心 ID
  max_mhz       # 最高频率 MHz
  avg_mhz       # 平均频率 MHz
  throttle_pct  # 降频百分比 ((1 - avg/max) * 100)
```

### 分析决策树

```
1. 检查 throttled_cores 是否为空
   ├─ 不为空 → 有核心降频，继续分析严重度
   └─ 为空 → 无显著降频，但检查 avg/max 比率

2. 分析降频严重度
   ├─ throttle_pct > 70% → P0，严重降频，CPU 性能仅为满载的 30%
   ├─ throttle_pct > 50% → P1，中度降频
   └─ throttle_pct > 30% → P2，轻度降频

3. 分析降频核心类型
   ├─ 大核降频 → 直接影响性能敏感任务（UI、渲染）
   ├─ 小核降频 → 影响后台任务，对 UI 间接影响
   └─ 所有核心降频 → 全局热管理，设备整体性能下降

4. 检查频率波动
   ├─ min_mhz 接近 300MHz → 核心曾进入极低频状态
   ├─ max_mhz 接近 3GHz → 大核，降频影响更大
   └─ samples 少 → trace 时间短，频率数据可能不充分
```

### 关键模式识别

| 模式 | 数据特征 | 根因推测 |
|------|---------|---------|
| 所有核心同时降频 | 每个 core 的 throttle_pct > 50% | 全局 thermal throttling |
| 仅大核降频 | 大核 throttle_pct > 50%，小核正常 | 大核过热，负载过于集中 |
| 频率剧烈波动 | min 很低，max 很高 | 热管理频繁介入和退出 |
| 频率持续低位 | avg 接近 min | 持续高负载导致温度居高不下 |

---

## 严重度标准

- **P0**: 平均频率 < 最高频率的 30% (throttle_pct > 70%)
- **P1**: 平均频率 < 最高频率的 50% (throttle_pct > 50%)
- **P2**: 平均频率 < 最高频率的 70% (throttle_pct > 30%)

## 与其他维度的关联

| 关联维度 | 关联模式 | 分析方法 |
|---------|---------|---------|
| CPU 调度 (cpu-scheduling) | 降频延长线程执行时间，间接增加调度延迟 | 低 avg_mhz 期间 runnable 等待时间增加 |
| 帧时间线 (ui-jank) | 降频使帧渲染超预算 | 降频时间段与 jank 帧时间段对齐 |
| GC 事件 (gc-analysis) | 降频延长 GC 暂停时间 | 低频期间的 GC pause_ms 更长 |
| 内存趋势 (memory-analysis) | 降频可能伴随内存压缩（zRAM）| 低频期间 RSS 变化 |

## 优化方向

### 减少 CPU 负载

1. **降低持续 CPU 占用**: 避免后台线程持续高负载运算
2. **分散计算**: 将密集计算拆分到多帧执行（如分帧计算布局）
3. **降低算法复杂度**: O(n²) → O(n log n)，减少单次计算的 CPU 时间

### 热管理友好

1. **Sustained Performance Mode**: Android 7+ API，告知系统保持稳定性能而非突发性能
   ```kotlin
   window.setSustainedPerformanceMode(true)
   ```
2. **性能提示**: Android 11+ `WindowManager.setPerformancePoint()` 告知系统期望性能级别
3. **避免长时间高负载**: 游戏和视频编码等场景应主动降帧率避免过热

### 典型 CPU 优化模式

```kotlin
// 反模式: 主线程密集计算
fun heavyComputation() {
    val result = dataList.map { transform(it) }  // 主线程 + 可能触发 GC
    adapter.submitList(result)
}

// 推荐: 分帧计算 + 后台线程
fun heavyComputation() {
    viewModelScope.launch(Dispatchers.Default) {
        val result = dataList.map { transform(it) }  // 后台线程计算
        withContext(Dispatchers.Main) {
            adapter.submitList(result)
        }
    }
}
```

## Thermal Zone 与 Trip Point 分析

SoC 具有多个热区（thermal zone）：CPU、GPU、电池、调制解调器（modem）、外壳温度（skin temperature）。每个热区有一组 trip point（阈值），触发不同的系统响应。

### Trip Point 等级

| Trip Point | 系统响应 | 对性能的影响 |
|-----------|---------|------------|
| trip_point_0 | 被动冷却（passive cooling），频率逐渐降低 | 轻微，用户不易察觉 |
| trip_point_1 | 主动冷却（active cooling），更激进的频率降低 | 明显，帧率下降 |
| trip_point_2 | 临界温度（critical），系统关闭保护 | 设备关机 |

### Perfetto 数据位置

- 热区数据在 `counter` 表中，track 名称包含 `thermalthrottling` 或 `thermal`
- 温度数据在 `counter` 表中，track 名称包含 `temperature` 或 `temp`

### 关键分析模式

- 如果热区温度在 trace 期间持续上升且 CPU 频率同步下降，则确认是 thermal throttling
- 不同设备的热特性差异很大：轻薄手机降频更快，游戏手机有更好的散热设计
- 某些 SoC（如 MediaTek Dimensity）的热管理更激进，在较低温度就开始降频

| 模式 | 数据特征 | 根因推测 |
|------|---------|---------|
| 温度线性上升 + 频率线性下降 | 稳态热管理 | 持续负载导致温度逐步升高 |
| 温度突升 + 频率骤降 | 热点触发 | 突发高负载（如启动动画）触发 trip point |
| 温度高位振荡 + 频率波动 | 热管理振荡 | 负载在 trip point 阈值附近波动 |

## 频率调控器分析

CPU 频率调控器（governor）决定频率变化的策略和速度：

### Governor 类型

| Governor | 响应速度 | 频率策略 | Android 使用场景 |
|----------|---------|---------|---------------|
| `schedutil` | 快（1-2ms） | 基于 PELT 信号动态调节 | Android 默认（8.x+） |
| `performance` | 无（固定） | 始终保持最高频率 | 基准测试、性能模式 |
| `powersave` | 无（固定） | 始终保持最低频率 | 省电模式 |
| `interactive` | 中等（10-20ms） | 基于定时器采样 | 旧版 Android |

### schedutil 行为特征

- schedutil 使用 PELT 信号决定频率——PELT 负载高则频率快速上升
- 频率转换延迟：现代 SoC 通常 1-5ms。在转换期间核心以中间频率运行
- 特征模式：如果 avg_mhz 低但 max_mhz 高且存在接近 max 的频率采样点，说明 governor 工作正常但负载是突发性的
- 持续低频：所有核心的 avg_mhz 接近 min_mhz 表示 thermal throttling 或功耗约束，而非 governor 行为

### 分析要点

```
avg_mhz 低 + max_mhz 高 + 高频样本存在 → governor 正常，负载突发
avg_mhz 低 + max_mhz 低 → governor 设为 powersave 或硬件限制
avg_mhz ≈ max_mhz → governor 设为 performance 或持续高负载
所有核心 avg_mhz ≈ min_mhz → thermal throttling 或极低负载
```

## GPU 降频关联

GPU throttling 往往与 CPU throttling 同时发生（共享热区）：

### GPU 频率数据

- GPU 频率数据可能在 `counter` 表中（track 名称包含 `gpu` 或 `gfx`）
- 并非所有 Perfetto trace 都包含 GPU 频率数据，取决于 trace config

### GPU 降频影响链

```
GPU 降频
  → 渲染命令执行时间延长
    → RenderThread / GPU Completion slice 耗时增加
      → 帧 jank
```

### 关键分析模式

- CPU throttling + GPU throttling + jank = thermal throttling 是根因
- 在 Snapdragon 平台上：Adreno GPU 与 Kryo CPU 通过 HLOS（Host Linux Operating System）热管理策略共享热管理
- GPU 降频通常比 CPU 降频更难检测（Perfetto 中 GPU 频率数据不一定可用），需通过渲染时间间接推断

| GPU 指标 | 正常值 | 降频时 | 说明 |
|---------|-------|-------|------|
| GPU Completion 单帧耗时 | < 8ms | > 16ms | GPU 处理变慢 |
| dequeueBuffer 等待 | < 2ms | > 5ms | GPU 来不及消费缓冲区 |
| GPU 频率（如有） | 接近 max | 接近 min | 直接证据 |

## EAS 能效调度与线程迁移

Energy Aware Scheduling（Android 10+）会主动在线程大小核之间迁移：

### EAS 迁移行为

- UI 线程在低活动期间（动画空闲、无触摸事件）可能被放置到小核
- 当 UI 活动恢复（触摸事件、doFrame）时，线程应迁移到大核
- 迁移延迟：迁移后有 1-3 帧的延迟升高期
- 如果主线程的调度延迟峰值与核心迁移事件相关，可能是 EAS 迁移导致

### 关键分析模式

- 交叉验证：cpu_throttling 展示每核频率。如果主线程在 jank 帧期间运行在低频核心上，EAS 可能错误地分类了工作负载
- EAS 的"错误分类"常见场景：刚从 idle 恢复的 UI 线程仍在小核上执行第一帧

```
Touch Event → UI 线程唤醒（仍在小核）→ doFrame 执行慢（小核低频）
  → 1-3 帧后 EAS 迁移到大核 → doFrame 执行恢复正常
```

| 场景 | EAS 行为 | 影响 |
|------|---------|------|
| UI 空闲后触摸 | 线程在小核唤醒，延迟 1-3 帧迁移大核 | 首次触摸响应慢 |
| 持续滚动 | 线程稳定在大核 | 无影响 |
| 后台任务突然活跃 | EAS 可能将 UI 线程挤到小核 | 滚动时卡顿 |
| 温度升高 | EAS 主动将任务迁移到小核 | 性能下降 |

## Perfetto SQL 查询模式

### 每核 CPU 频率统计

```sql
-- 每核 CPU 频率统计
SELECT
  cpu,
  MIN(value) / 1e3 as min_mhz,
  MAX(value) / 1e3 as max_mhz,
  AVG(value) / 1e3 as avg_mhz,
  (1 - AVG(value) / MAX(value)) * 100 as throttle_pct,
  COUNT(*) as samples
FROM counter c
JOIN cpu_counter_track t ON c.track_id = t.id
WHERE t.name = 'cpu_frequency'
GROUP BY cpu
ORDER BY cpu;
```

### 每核频率分布（检测降频模式）

```sql
-- 每核频率分布（检测降频模式）
SELECT
  cpu,
  CASE
    WHEN value / MAX(value) OVER (PARTITION BY cpu) > 0.8 THEN 'high_freq'
    WHEN value / MAX(value) OVER (PARTITION BY cpu) > 0.5 THEN 'mid_freq'
    ELSE 'low_freq'
  END as freq_band,
  COUNT(*) as samples,
  SUM(dur) / 1e9 as total_time_s
FROM counter c
JOIN cpu_counter_track t ON c.track_id = t.id
WHERE t.name = 'cpu_frequency'
GROUP BY cpu, freq_band
ORDER BY cpu, freq_band;
```

## 误报识别

### Idle 低频（误报：空闲核心频率低）

- 核心空闲时频率降至最低。如果核心有长时间空闲期，avg_mhz 会很低，但这不是降频
- 检查 samples 数量——采样点少的核心可能大部分时间处于空闲
- 正确做法：只分析非空闲期间的频率样本，或将 idle 期间排除

### LITTLE 核心基线（误报：小核正常频率被误判为降频）

- LITTLE 核心的最高频率本身就很低。一个小核 avg_mhz/max_mhz = 60% 是正常的，不是降频
- 只能对同一类型的核心比较 avg/max 比率
- 正确做法：先区分核心类型（大核/小核/超大核），再分别评估

### 频率转换样本（误报：过渡期频率被计入）

- 在频率转换过程中，计数器可能短暂记录中间值
- 这些过渡样本不应被计入降频证据
- 如果 min_mhz 和 avg_mhz 之间有大量散布的低频样本点，需要检查是否为过渡值

### EAS 驱动降频（误报：能效优化被误判为降频）

- EAS 可能为了能效降低 CPU 频率，即使没有热压力
- 这是系统主动优化行为，不是问题
- 交叉验证：与热区数据对比，区分 thermal throttling 和 EAS 优化
- 如果热区温度正常但频率低，大概率是 EAS 行为
