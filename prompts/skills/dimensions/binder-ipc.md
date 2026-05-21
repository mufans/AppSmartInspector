# Binder IPC 分析

## 数据源
- SQL 表: `__intrinsic_thread_state` (blocked_function = 'binder_thread_read')

## 领域知识
- Binder: Android 跨进程通信机制
- 高延迟原因: 服务端处理慢、序列化数据大、系统服务繁忙

## 严重度标准
- P0: 主线程 binder 等待 max > 帧预算
- P1: 主线程 binder 等待 max > 10ms
- P2: 其他线程 binder 等待 max > 50ms

## Metric 字段
- `binder_ipc.threads[].thread_name`: 线程名
- `binder_ipc.threads[].binder_waits`: Binder 等待次数
- `binder_ipc.threads[].total_wait_ms`: 总等待时间
- `binder_ipc.threads[].max_wait_ms`: 最大单次等待

## 优化方向
- 减少 IPC 调用: 批量接口、缓存结果
- 避免主线程 IPC: 使用异步 Binder
- 优化序列化: 减少 Parcelable 数据量
