# 内存分析

## 数据源
- SQL 表: `process_counter_track` + `counter` (mem.rss 时序)
- `heap_graph_*` 表: 堆对象图

## 领域知识
- RSS (Resident Set Size): 进程实际使用的物理内存
- 内存增长模式: 线性增长(可能泄漏)、阶梯增长(大对象)、锯齿形(GC 正常)

## 严重度标准
- P0: RSS 增长 > 50%
- P1: RSS 增长 > 20%
- P2: RSS 增长 > 10%

## Metric 字段
- `memory_trend.start_rss_mb`: 起始 RSS
- `memory_trend.end_rss_mb`: 结束 RSS
- `memory_trend.delta_mb`: 增量 (MB)
- `memory_trend.delta_pct`: 增量百分比
- `memory_trend.trend_slope_mb_per_s`: 增长斜率
- `memory_trend.jumps[]`: 阶段性跳跃事件

## 优化方向
- 检测泄漏: 使用 LeakCanary 或 Android Studio Profiler
- 减少大对象: Bitmap 缓存、对象池
- 优化缓存策略: LRU 缓存限制大小
