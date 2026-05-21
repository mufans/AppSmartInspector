# 锁竞争分析

## 数据源
- SQL 表: `__intrinsic_thread_state` (blocked_function GLOB '*futex*')
- 需要 `sched_switch` ftrace 事件支持

## 领域知识
- futex (Fast Userspace Mutex): Linux 用户空间互斥锁
- blocked_function 常见值: futex_wait_queue_me, futex_wait, do_futex
- 主线程锁竞争直接导致 ANR 和 jank

## 严重度标准
- P0: 主线程 futex 等待 max > 帧预算
- P1: 主线程 futex 等待 max > 5ms
- P2: 其他线程 futex 等待 max > 10ms

## Metric 字段
- `lock_contention.threads[].thread_name`: 线程名
- `lock_contention.threads[].futex_wait_count`: futex 等待次数
- `lock_contention.threads[].total_wait_ms`: 总等待时间
- `lock_contention.threads[].max_wait_ms`: 最大单次等待
- `lock_contention.contention_hotspots[].blocked_function`: 阻塞函数名

## 优化方向
- 减小锁粒度: 使用细粒度锁替代全局锁
- 避免主线程持锁: 将耗时操作移到子线程
- 使用无锁数据结构: ConcurrentHashMap、原子操作
