# SmartInspector Knowledge Base

## 维度 Skills (prompts/skills/dimensions/)

| Skill | 维度 | 触发词 |
|-------|------|--------|
| cpu-scheduling | CPU 调度延迟 | 调度/runnable/sched_latency |
| lock-contention | 锁竞争 | 锁/futex/lock_contention |
| gc-analysis | GC 事件 | gc/垃圾回收/gc_events |
| io-analysis | 文件 IO | io/磁盘/文件/file_io |
| memory-analysis | 内存趋势 | 内存/泄漏/memory_trend |
| binder-ipc | Binder IPC | binder/ipc/跨进程 |
| cpu-throttling | CPU 降频 | 降频/throttling/cpu频率 |
| ui-jank | UI/帧率 | 帧率/jank/卡顿/rv/compose |
| startup | 冷启动 | 启动/cold start |

## 共享 Skills (prompts/skills/shared/)

| Skill | 描述 |
|-------|------|
| si-tag-system | SI$ 标签格式定义和解析规则 |
| search-strategy | Java/Kotlin/XML 源码搜索策略 |
