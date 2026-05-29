# 冷启动分析

## 数据源

- SQL 表: `thread` (MIN(start_ts) 确定进程启动时间)
- SQL 表: `slice` + `thread_track` (查找 Application.onCreate、Activity.onCreate、doFrame 等关键切片)
- SI$ 标签: `SI$Activity.onCreate`、`SI$Application.onCreate`、`SI$Application.attachBaseContext`
- 关键路径提取: 主线程在启动期间耗时最长的 30 个切片（`dur > 0.5ms`）

## 领域知识

### 冷启动阶段划分

```
process fork                     doFrame (首帧渲染)
    │                                │
    ├─────── pre-main ───────┬── App.onCreate ──┬── Activity.onCreate → 首帧 ──┤
                             │                  │                              │
    │◄──── TTFD (Total) ─────────────────────────────────────────────────────►│
    │◄── TTID ───────────────────────────────────────────────────────────►│
    │                              │◄── Activity Phase ──►│
```

| 阶段 | 起止点 | 典型耗时 | 主要操作 |
|------|--------|---------|---------|
| pre-main | fork → Application.attachBaseContext | 50-500ms | Zygote 共享页加载、ClassLoader 初始化 |
| Application.onCreate | attachBaseContext → first Activity.onCreate | 100-2000ms | SDK 初始化、全局状态配置 |
| Activity.onCreate → 首帧 | Activity.onCreate → first doFrame | 100-1000ms | 布局 inflate、View 初始化、首次测量布局绘制 |
| 首帧渲染 | doFrame 执行 | 16.67ms | 一帧的完整渲染周期 |

### Google 冷启动性能基准

| 等级 | TTFD (Time To Full Display) | 用户体验 |
|------|---------------------------|---------|
| 优秀 | < 500ms | 几乎感知不到延迟 |
| 良好 | 500ms - 1s | 短暂等待，可接受 |
| 一般 | 1s - 2.5s | 明显等待 |
| 较慢 | 2.5s - 5s | 用户可能离开 |
| 极慢 | > 5s | 用户大概率离开 |

### 冷启动 vs 热启动 vs 温启动

| 类型 | 过程 | 典型耗时 |
|------|------|---------|
| 冷启动 | 进程创建 → Application → Activity → 首帧 | 500ms - 5s |
| 温启动 | Activity 重建（进程存活）| 100ms - 500ms |
| 热启动 | Activity 从后台恢复 | 10ms - 100ms |

### 启动阶段常见瓶颈

| 阶段 | 瓶颈类型 | 典型表现 | 优化方向 |
|------|---------|---------|---------|
| pre-main | Multidex | 500ms-2s (5.x 及以下) | 使用 AndroidX Multidex 或 minSdk 21+ |
| pre-main | ContentProvider 初始化 | 每个 Provider 100-500ms | App Startup 库延迟初始化 |
| App.onCreate | SDK 初始化 | 每个 SDK 10-200ms | 按需初始化、异步初始化 |
| App.onCreate | 全局状态加载 | 100-1000ms | 延迟到首帧后 |
| Activity.onCreate | 布局 inflate | 50-500ms | 减少布局层级、异步 inflate |
| Activity.onCreate | 数据加载 | 100-2000ms | 预加载、缓存、占位 UI |
| 首帧渲染 | 首次 draw | 16-100ms | 避免首次 draw 的复杂绘制 |

### Zygote 预加载

Android 系统通过 Zygote 进程预加载常用类和资源。应用 fork 自 Zygote，继承其预加载的堆空间（Copy-on-Write）。这意味着：
- 常用系统类（View、Activity 等）已在内存中，不需要从 dex 加载
- 但应用自身的类仍需首次加载和初始化
- ClassLoader 的首次类加载会触发 dex 验证（dex2oat），耗时较长

## 数据解读

### 关键数据字段

```
startup.total_ms           # 总冷启动耗时 ms
startup.phases[]           # 启动阶段列表
  name                     # 阶段名 (pre-main / App.onCreate / Activity.onCreate → 首帧)
  dur_ms                   # 阶段耗时 ms
  pct                      # 占总启动时间百分比

startup.critical_path[]    # 关键路径（启动期间耗时最长的切片）
  name                     # 切片名
  dur_ms                   # 耗时 ms
  thread_name              # 所在线程名

startup.bottlenecks[]      # 瓶颈分析
  phase                    # 所在阶段
  name                     # 瓶颈切片名
  dur_ms                   # 耗时 ms
  pct_of_phase             # 占该阶段百分比
  suggestion               # 优化建议
```

### 分析决策树

```
1. 检查 total_ms
   ├─ > 5s → P0，极慢启动
   ├─ > 2s → P1，较慢启动
   ├─ > 1s → P2，一般启动
   └─ < 1s → 良好

2. 分析阶段分布
   ├─ pre-main 占比 > 40% → 进程启动开销大
   │   └─ 可能原因: Multidex、ContentProvider 过多
   ├─ App.onCreate 占比 > 40% → 初始化过重
   │   └─ 可能原因: SDK 全量初始化、同步 IO
   └─ Activity.onCreate → 首帧 占比 > 40% → 首屏渲染慢
       └─ 可能原因: 布局复杂、首次数据加载

3. 分析 critical_path 中的慢切片
   ├─ SI$inflate#* → 布局 inflate 耗时长
   │   └─ 建议: ViewStub 延迟加载、AsyncLayoutInflater
   ├─ SI$RV#*.onBindViewHolder → 列表初始化绑定慢
   │   └─ 建议: 简化绑定逻辑、预加载数据
   ├─ 含 "init" / "initialize" → SDK 初始化
   │   └─ 建议: 按需初始化、异步初始化
   ├─ 含 "database" / "db" → 数据库操作
   │   └─ 建议: 预创建数据库、异步查询
   └─ 含 "net" / "http" → 网络请求
       └─ 建议: 缓存策略、预加载

4. 检查瓶颈切片的 thread_name
   ├─ thread_name == "main" → 主线程瓶颈，必须优化
   └─ thread_name != "main" → 子线程瓶颈，可能通过调度优化改善
```

### 关键模式识别

| 模式 | 数据特征 | 根因推测 |
|------|---------|---------|
| App.onCreate 占 80% | phases 中 App.onCreate pct > 80 | SDK 全量同步初始化 |
| 大量 inflate 切片 | critical_path 中多个 SI$inflate | 首屏布局过于复杂 |
| 启动阶段有 IO 阻塞 | critical_path 含 IO 相关切片 | 启动时读取配置文件或数据库 |
| 启动阶段有 GC | critical_path 含 GC 切片 | 启动阶段大量对象分配 |
| pre-main 过长 | pre-main > 500ms | Multidex 或 ClassLoader 问题 |

## 严重度标准

- **P0**: 冷启动 > 5s
- **P1**: 冷启动 > 2s
- **P2**: 冷启动 > 1s

## 与其他维度的关联

| 关联维度 | 关联模式 | 分析方法 |
|---------|---------|---------|
| 文件 IO (io-analysis) | 启动阶段主线程 IO 阻塞 | startup 时间窗口内的 IO 等待事件 |
| GC 事件 (gc-analysis) | 启动阶段 GC 暂停延长启动时间 | startup 时间窗口内的 GC 事件 |
| 锁竞争 (lock-contention) | 启动阶段主线程锁等待 | startup 时间窗口内的 futex 等待 |
| Binder IPC (binder-ipc) | 启动阶段大量系统服务调用 | startup 时间窗口内的 Binder 等待 |
| 内存趋势 (memory-analysis) | 启动阶段 RSS 跳增是初始化加载 | memory_trend.jumps 中的启动阶段跳跃 |
| 帧时间线 (ui-jank) | 首帧渲染 jank | first_frame 时间点的帧耗时 |

## 优化方向

### 延迟初始化

1. **App Startup 库**: 使用 Jetpack App Startup 管理初始化顺序和依赖
2. **按需初始化**: 非首屏必需的 SDK 延迟到首帧渲染后初始化
3. **异步初始化**: 使用协程或线程池在后台初始化非关键组件
4. **ContentProvider 精简**: 合并和移除不必要的 ContentProvider 自动初始化

### 布局优化

1. **减少首屏布局复杂度**: 只加载首屏可见的布局，其余用 ViewStub
2. **AsyncLayoutInflater**: 在后台线程 inflate 首屏布局
3. **占位 UI**: 先显示简单的占位 UI，再异步加载真实内容

### 数据预加载

1. **预加载核心数据**: 在 Application.onCreate 的后半段异步加载首屏数据
2. **缓存策略**: 启动时优先使用缓存数据，异步更新
3. **数据库预创建**: 使用 Room 的 `RoomDatabase.Builder` 预创建数据库文件

### 典型启动优化模式

```kotlin
// 反模式: Application.onCreate 全量同步初始化
class MyApp : Application() {
    override fun onCreate() {
        super.onCreate()
        Analytics.init(this)        // 100ms
        CrashReport.init(this)      // 50ms
        ImageLoader.init(this)      // 200ms
        Database.init(this)         // 500ms
        PushService.init(this)      // 300ms
        // 总计: 1150ms 在主线程
    }
}

// 推荐: 核心同步 + 非核心异步
class MyApp : Application() {
    override fun onCreate() {
        super.onCreate()
        // 核心: 同步初始化（crash report 必须）
        CrashReport.init(this)      // 50ms

        // 非核心: 异步初始化
        CoroutineScope(Dispatchers.IO).launch {
            Analytics.init(this@MyApp)
            ImageLoader.init(this@MyApp)
            Database.init(this@MyApp)
            PushService.init(this@MyApp)
        }
    }
}

// 反模式: 首屏加载全部数据
override fun onCreate(savedInstanceState: Bundle?) {
    super.onCreate(savedInstanceState)
    setContentView(R.layout.main)           // inflate: 200ms
    val data = repository.loadAllData()     // 网络: 1000ms
    adapter.submitList(data)                // bind: 100ms
}

// 推荐: 占位 + 异步加载
override fun onCreate(savedInstanceState: Bundle?) {
    super.onCreate(savedInstanceState)
    setContentView(R.layout.main)           // inflate: 200ms

    viewModel.data.observe(this) { data ->  // 异步加载
        adapter.submitList(data)
    }
}
```

### Baseline Profile 优化

- Baseline Profiles（Android 7+，前身为 DSLV）指定应预编译（AOT）的代码路径
- 没有 Baseline Profiles：App 代码以解释执行（慢）直到 JIT 编译热点路径
- 有 Baseline Profiles：关键启动路径在安装时预编译
- 影响：启动时间提升 20-40%
- Jetpack Macrobenchmark 可自动生成 Baseline Profiles
- 在 trace 分析中：如果启动慢且没有编译切片（`jit compiling`、`dex2oat`），说明 App 可能已有 Baseline Profiles。如果启动期间出现编译切片，说明缺少 Profiles。
- 检查方法：`slice` 表中的 `BaselineProfile` 或 `CompilationFilter` 切片

| 状态 | trace 表现 | 性能影响 |
|------|-----------|---------|
| 无 Baseline Profile | 启动期间出现 `jit compiling` / `dex2oat` 切片 | 解释执行，慢 |
| 有 Baseline Profile | 启动期间无编译切片 | AOT 编译，快 |
| Profile 过期 | 首次启动正常，更新后变慢 | 需要重新生成 |

### dex2oat 与编译优化

ART 使用 dex2oat 将 DEX 字节码编译为本地代码：

| 编译过滤器 | 行为 | 安装速度 | 运行速度 |
|-----------|------|---------|---------|
| `verify` | 仅验证，不编译 | 最快 | 最慢 |
| `quicken` | 快速验证（大多数 App 默认） | 快 | 慢 |
| `speed-profile` | 基于使用 profile 编译 | 中等 | 中等 |
| `speed` | 编译全部 | 最慢 | 最快 |
| `everything` | 编译全部含 boot classpath | 极慢 | 最快 |

- 冷启动通常使用 `quicken` 过滤器 — 意味着大部分代码是解释执行
- dex2oat 运行时机：App 安装时、空闲维护时、按需触发
- 在 trace 中：启动期间出现 `dex2oat` 切片表示按需编译，会增加启动时间
- 检测模式：如果 `dex2oat` 出现在关键路径上，说明 App 在启动期间正在被编译（不好）。应使用 Baseline Profiles 代替。

### ContentProvider 初始化序列

Android 在 `Application.onCreate()` 之前初始化 ContentProviders：

```
process fork
  → Application.attachBaseContext()
  → ContentProvider.onCreate()  (所有 Providers，按声明顺序)
  → Application.onCreate()
```

- 每个 ContentProvider 的 `onCreate()` 在主线程上顺序执行
- 第三方库经常使用 ContentProvider 进行自动初始化：
  - Firebase: `FirebaseInitProvider`
  - LeakCanary: `LeakCanaryFileProvider`
  - WorkManager: `WorkManagerInitializer`
- 每个 Provider 增加 10-200ms 启动时间
- 检测方法：在 `critical_path` 中，查找 Application.onCreate 之前包含 "Provider" 或 "init" 的切片
- 修复方案：使用 Jetpack App Startup 合并和延迟 ContentProvider 初始化

| Provider | 典型耗时 | 是否可延迟 |
|----------|---------|-----------|
| FirebaseInitProvider | 50-200ms | 可延迟到首次使用 |
| LeakCanaryFileProvider | 10-50ms | Debug only，可移除 |
| WorkManagerInitializer | 20-100ms | 可使用手动初始化 |
| 自定义 Provider | 不定 | 逐一分析 |

### ClassLoader 与类加载优化

- Android 使用 `PathClassLoader` 加载 App 代码，使用 `DexClassLoader` 进行动态加载
- 首次类加载涉及：DEX 文件解析 → 类验证 →（可选）编译
- 类验证是最慢的部分：它遍历整个类层次结构并检查所有引用
- `pre-main` 阶段包含 Application 类及其依赖的类加载
- Multidex（API < 21）：次要 DEX 文件必须在启动时提取和优化（非常慢，500ms-2s）
- 检测模式：`pre-main` 阶段很长但没有明确的切片名 → 可能是类加载开销
- 在 trace 中：类加载期间出现 `ClassLoader.loadClass` 或 `DexFile.openDexFile` 切片

```
类加载开销在 trace 中的表现：
1. pre-main 阶段 > 300ms 但没有明确切片
2. 没有显著的 IO 等待或 Binder 等待
3. CPU 使用率高（类验证是 CPU 密集型）
→ 推测为类加载开销
```

### 启动阶段线程调度分析

启动期间，主线程唤醒许多其他线程：

- 线程创建开销：每个 `new Thread()` → `clone` 系统调用 → 调度器开销
- Binder 线程随 IPC 发生而创建
- SDK 初始化创建的工作线程

如果启动期间多个线程同时变为 runnable，调度延迟会急剧增加：
- 检测模式：检查启动窗口内的 `sched_runnable` 数据。如果主线程的 `avg_runnable_ms` 在启动期间较高，说明主线程在与初始化线程竞争 CPU
- 优化方向：延迟非必要的线程创建到首帧之后

| 现象 | 数据特征 | 推测原因 |
|------|---------|---------|
| 主线程 runnable 高 | 启动期间 main avg_runnable > 5ms | 线程过多竞争 CPU |
| Binder 线程创建密集 | 启动期间大量 Binder 线程出现 | 系统服务调用频繁 |
| 工作线程 runnable 高 | 多个工作线程同时 runnable | SDK 并发初始化 |

### Perfetto SQL 查询模式

```sql
-- Startup critical path: longest slices on main thread during startup
SELECT
  slice.name,
  slice.dur / 1e6 as dur_ms,
  thread.name as thread_name,
  ts
FROM slice
JOIN thread_track ON slice.track_id = thread_track.id
JOIN thread ON thread_track.utid = thread.utid
WHERE thread.name = 'main'
  AND slice.ts BETWEEN <start_ts> AND <first_frame_ts>
  AND slice.dur > 500000  -- > 0.5ms
ORDER BY slice.dur DESC
LIMIT 30;

-- ContentProvider initialization timing
SELECT
  slice.name,
  slice.dur / 1e6 as dur_ms,
  ts
FROM slice
JOIN thread_track ON slice.track_id = thread_track.id
JOIN thread ON thread_track.utid = thread.utid
WHERE thread.name = 'main'
  AND (slice.name LIKE '%Provider%' OR slice.name LIKE '%ContentProvider%')
  AND slice.dur > 100000
ORDER BY ts;

-- Phase breakdown with timeline
SELECT
  'pre-main' as phase,
  (<app_create_ts> - <process_start_ts>) / 1e6 as dur_ms
UNION ALL
SELECT
  'App.onCreate' as phase,
  (<activity_create_ts> - <app_create_ts>) / 1e6 as dur_ms
UNION ALL
SELECT
  'Activity→FirstFrame' as phase,
  (<first_frame_ts> - <activity_create_ts>) / 1e6 as dur_ms;
```

### 误报识别

- **温启动分析**：如果 trace 以已有进程开始（无 process fork），这不是冷启动。检查 `ZygoteInit` 或 `ActivityThread.main` 切片来确认冷启动。
- **现代设备上的 Multidex**：API 21+ 使用 ART 原生支持 multidex。Multidex 开销仅适用于 API < 21。不要在现代设备上标记 multidex 问题。
- **系统触发的重启**：如果 `System.exit()` 或崩溃导致进程重启，启动可能因清理工作而比正常慢。这不代表用户发起的冷启动。
- **Debug 构建开销**：Debug 构建有额外的插桩（StrictMode、调试器、profiling）。Debug 构建的冷启动时间不能代表 release 版性能。
