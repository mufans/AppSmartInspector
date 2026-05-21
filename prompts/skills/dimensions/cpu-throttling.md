# CPU 降频检测

## 数据源
- SQL 表: `cpu_counter_track` + `counter` (CPU 频率计数器)

## 领域知识
- Thermal Throttling: CPU 过热自动降频保护
- 降频影响: 所有线程执行变慢，帧预算更难满足

## 严重度标准
- P0: 平均频率 < 最高频率的 30%
- P1: 平均频率 < 最高频率的 50%
- P2: 平均频率 < 最高频率的 70%

## Metric 字段
- `cpu_throttling.cpu_freq_by_core`: 各核心频率统计
- `cpu_throttling.throttled_cores[]`: 降频核心详情

## 优化方向
- 减少 CPU 密集操作: 降低持续 CPU 负载
- 优化算法: 降低时间复杂度
- 分散计算: 将密集计算分散到多帧
