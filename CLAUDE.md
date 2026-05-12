# SmartInspector - Project Instructions

## Collector 模块 (Perfetto SQL Stdlib 集成)

采集层位于 `src/smartinspector/collector/`，所有模块以 Mixin 形式挂载到 `PerfettoCollector`。每个 Mixin 依赖宿主提供 `self._open()` (返回 TraceProcessor) 和 `self._target_package`。

### 基础模块

| 文件 | 类 | 说明 |
|------|-----|------|
| `perfetto.py` | `PerfettoCollector` | 核心采集器 (adb → SQL → JSON)，提供 `_open()` / `_target_package`，所有 Mixin 的宿主 |

### Stdlib 分析模块 (P0 — 核心)

| 文件 | Mixin | 方法 | Perfetto Stdlib |
|------|-------|------|-----------------|
| `frame.py` | `FrameMixin` | `collect_frame_metrics()` — 帧指标 (overrun, cpu_time, ui_time, vsync_delay, jank) | `android.frames.per_frame_metrics`, `android.frames.timeline` |
| `startup.py` | `StartupMixin` | `collect_startup_metrics()` — TTID/TTFD 启动耗时 | `android.startup.startups`, `android.startup.time_to_display` |
| `startup.py` | `StartupMixin` | `collect_startup_breakdown()` — 启动瓶颈分解 | `android.startup.startup_breakdowns` |
| `memory.py` | `MemoryMixin` | `collect_heap_graph_stats()` — 堆图摘要统计 | `android.memory.heap_graph.heap_graph_stats` |
| `memory.py` | `MemoryMixin` | `collect_heap_class_aggregation()` — 按类聚合堆内存 (Top 20) | `android.memory.heap_graph.heap_graph_class_aggregation` |
| `memory.py` | `MemoryMixin` | `collect_heap_dominator_tree()` — 堆支配树 (最大 retained size) | `android.memory.heap_graph.dominator_tree` |

### Stdlib 分析模块 (P1 — 增强)

| 文件 | Mixin | 方法 | Perfetto Stdlib |
|------|-------|------|-----------------|
| `lock.py` | `LockMixin` | `collect_lock_contention()` — Java 锁竞争分析 | `android.monitor_contention` |
| `binder.py` | `BinderMixin` | `collect_binder_txns()` — Binder 事务 Top 30 | `android.binder` |
| `binder.py` | `BinderMixin` | `collect_binder_breakdown()` — Binder 延迟分解 | `android.binder`, `android.binder_breakdown` |
| `gc.py` | `GcMixin` | `collect_garbage_collection()` — GC 事件分析 | `android.garbage_collection` |
| `anr.py` | `AnrMixin` | `collect_anrs()` — ANR 检测与分析 | `android.anrs` |
| `slice_enhanced.py` | `SliceEnhancedMixin` | `collect_slice_cpu_time()` — SI$ slice 真实 CPU 耗时 | `slices.cpu_time` |
| `slice_enhanced.py` | `SliceEnhancedMixin` | `collect_slice_time_in_state()` — SI$ slice 线程状态分布 | `slices.time_in_state`, `sched.states` |
| `input.py` | `InputMixin` | `collect_input_latency()` — 输入事件延迟分解 | `android.input` |
| `sched_latency.py` | `SchedLatencyMixin` | `collect_sched_latency()` — 调度延迟分析 | `sched.latency` |
| `oom.py` | `OomMixin` | `collect_oom_rss_swap()` — OOM score 转换 + RSS/Swap + LMK 事件 | `android.memory.process`, `android.memory.lmk` |
| `cpu_utilization.py` | `CpuUtilizationMixin` | `collect_process_cpu_utilization()` — 进程级 CPU 利用率 | `linux.cpu.utilization.process` |
| `cpu_utilization.py` | `CpuUtilizationMixin` | `collect_thread_cpu_utilization()` — 线程级 CPU 利用率 | `linux.cpu.utilization.thread` |
| `surfaceflinger.py` | `SurfaceFlingerMixin` | `collect_surfaceflinger_timeline()` — App-SF 帧时间线匹配 | `android.surfaceflinger` |

### 添加新 Stdlib 模块的约定

1. 在 `src/smartinspector/collector/` 下创建新文件，以 Mixin 模式实现
2. 类名遵循 `{Feature}Mixin` 命名
3. 方法名遵循 `collect_{metric_name}()` 命名，返回 `list[dict]` 或 `dict`
4. SQL 查询中使用 `INCLUDE PERFETTO MODULE {module_path};` 引入 stdlib
5. 在 `PerfettoCollector` 中通过多重继承挂载 Mixin
6. 查询结果按耗时/大小降序排列，使用 `LIMIT` 控制返回数量
