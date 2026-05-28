# UI / 帧率分析

## 数据源
- SQL 表: `actual_frame_timeline_slice` + `expected_frame_timeline_slice`
- SI$ 标签: SI$RV#, SI$view#, SI$inflate#, SI$compose#

## 领域知识
- 帧预算: 60Hz = 16.67ms, 120Hz = 8.33ms
- Jank: 实际帧耗时超过预期帧耗时
- RecyclerView 瓶颈: onBindViewHolder / onCreateViewHolder 耗时

## 严重度标准
- P0: 帧耗时 > 3x 帧预算 (>50ms@60Hz)
- P1: 帧耗时 > 帧预算 (>16.67ms@60Hz)
- P2: 帧耗时 > 50% 帧预算

## 优化方向
- RecyclerView: 使用 DiffUtil、ViewHolder 缓存、预加载
- 布局优化: 减少层级、使用 ViewStub、Merge
- Compose: 避免不必要重组、remember、derivedStateOf
