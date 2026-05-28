# CPU 调度延迟分析

## 数据源
- Perfetto 标准库模块: `INCLUDE PERFETTO MODULE sched.runnable`
- 表: `sched_runnable`

## 领域知识
- 调度延迟: 线程变为 runnable 到实际获得 CPU 的时间差
- 高调度延迟原因: CPU 被其他线程抢占、核数不足、调度器负载高

## 严重度标准
- P0: 平均调度延迟 > 帧预算的 50%
- P1: 平均调度延迟 > 4ms
- P2: 最大调度延迟 > 8ms

## Metric 字段
- `sched_latency.threads[].thread_name`: 线程名
- `sched_latency.threads[].runnable_count`: runnable 次数
- `sched_latency.threads[].avg_runnable_ms`: 平均调度延迟
- `sched_latency.threads[].max_runnable_ms`: 最大调度延迟

## 优化方向
- 减少线程数: 降低调度器负载
- 绑核: 将关键线程绑定到大核
- 降低后台 CPU 占用: 使用 WorkManager 替代常驻线程
