# GC 事件分析

## 数据源
- SQL 表: `slice` (name GLOB '*GC*' OR name GLOB '*GarbageCollector*')
- 参数提取: `EXTRACT_ARG(arg_set_id, 'reason')` -> gc_reason
- 参数提取: `EXTRACT_ARG(arg_set_id, 'gc_type')` -> gc_type

## 领域知识
- GC 类型: Concurrent (后台并发), Non-concurrent (Stop-the-World)
- 常见触发原因: Alloc (分配触发), Explicit (System.gc), NativeAlloc
- 影响主线程的 GC: "GC: Wait For Concurrent" 和 "GC: Alloc"

## 严重度标准
- P0: GC pause > 帧预算 (16ms@60Hz, 8ms@120Hz) 且影响主线程
- P1: GC pause > 10ms
- P2: GC pause > 1ms

## Metric 字段
- `gc_events.total_count`: GC 总次数
- `gc_events.total_pause_ms`: 总暂停时间 (ms)
- `gc_events.max_pause_ms`: 最长单次暂停时间
- `gc_events.main_thread_pause_ms`: 影响主线程的 GC 总暂停时间
- `gc_events.events[].name`: GC 事件名称
- `gc_events.events[].dur_ms`: 单次持续时间
- `gc_events.events[].gc_reason`: 触发原因
- `gc_events.events[].gc_type`: GC 类型

## 与其他维度的关联
- GC <-> 帧时间线: GC pause 可能导致 jank 帧
- GC <-> 内存趋势: 频繁 GC 说明内存压力大

## 优化方向
- 减少 GC 频率: 避免短生命周期对象、使用对象池
- 避免 Concurrent GC pause: 减少堆大小波动
- 检查 NativeAlloc 泄漏
- 避免在主线程分配大对象
