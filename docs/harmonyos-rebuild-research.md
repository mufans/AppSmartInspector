# 鸿蒙 ArkUI 组件重绘定位方案调研

> 日期：2026-04-07
> 状态：调研完成

## 方案一：ArkUI Inspector（IDE 集成）+ hidumper CLI

### 技术方案

**ArkUI Inspector** 是 DevEco Studio 内置的可视化调试工具，可通过 `View → Tool Windows → ArkUI Inspector` 打开。它能在真机/模拟器上实时查看组件树、组件属性和布局信息。

**hidumper CLI** 是命令行版本的组件树/状态变量获取工具，核心命令：

```bash
# 1. 开启 debug 模式（一次性）
hdc shell param set persist.ace.debug.enabled 1

# 2. 获取窗口 ID
hdc shell "hidumper -s WindowManagerService -a '-a'"

# 3. 获取自定义组件树（递归）
hdc shell "hidumper -s WindowManagerService -a '-w <WinId> -jsdump -viewHierarchy -r'"

# 4. 获取指定组件的状态变量详情（含 @State/@Link/@Prop、同步对象、关联组件）
hdc shell "hidumper -s WindowManagerService -a '-w <WinId> -jsdump -stateVariables -viewId=<NodeId>'"

# 5. 获取完整控件树（含 if/else 节点、FrameRect、BackgroundColor 等）
hdc shell "hidumper -s WindowManagerService -a '-w <WinId> -element -c'"
```

**关键能力：**
- `stateVariables` 命令能展示每个 `@State`/`@Link`/`@Prop` 变量的**同步对象（Sync peers）**和**关联组件（Dependent elements）**，明确知道哪个状态变量变化会影响哪些 UI 组件
- `-element -c` 输出 dump 文件到设备，可 `hdc file recv` 到本地分析
- `-viewHierarchy -r` 递归打印自定义组件树，含节点 ID

### 可行性评估
- **成熟度：高** — 华为官方最佳实践文档推荐的方案
- **局限：** 这是**静态快照**工具，无法实时捕捉每次 build()/重绘事件。需要手动操作：先触发状态变化 → 再 dump 快照对比前后差异
- **能否获取 @State 值：** 能获取状态变量的**类型、名称、关联关系**，但**不能直接读取运行时值**

### 推荐优先级：★★★★☆（基础工具，辅助验证用）

---

## 方案二：HiTrace / bytrace 追踪组件 Build 事件

### 技术方案

ArkUI 框架内部通过 `ACE_FUNCTION_TRACE()` / `ACE_SCOPED_TRACE` 宏进行打点。系统已有的 trace 事件包括：

| Trace 名称 | 含义 |
|---|---|
| `FlushVsync` | 收到 Vsync 信号，开始整帧渲染流程 |
| `FlushBuild` | 执行组件创建/重建 |
| `Build[tag][self:id][parent:id]` | 单个组件节点构建，tag 为组件类型名 |
| `CustomNode:BuildItem ComponentName` | 自定义组件 build() 执行 |
| `CustomNode:BuildRecycle ComponentName` | 复用组件重新 build |
| `Layout[tag][self:id][parent:id]` | 节点布局 |
| `FlushLayoutTask` | 执行布局任务 |
| `FlushRenderTask` | 执行渲染任务 |

抓取 trace 的方式：
```bash
# 使用 hitrace 抓取（ACE/ArkUI 相关 tag）
hdc shell "hitrace -t 10 ace aceui arkui" > trace.ftrace

# 或使用 SmartPerf-Host / DevEco Profiler 可视化分析
```

**关键发现：** `Build[tag][self:id][parent:id]` 和 `CustomNode:BuildItem ComponentName` 这两个 trace 能精确标识每个自定义组件何时执行了 build()，这就是组件"重建"事件。通过对比连续帧的 trace，可以定位哪些组件被冗余重建。

### 可行性评估
- **成熟度：高** — 系统内置打点，无需修改应用代码
- **局限：** 
  - 没有 `aboutToRebuild` 这样的**前置回调**，只能事后分析 trace
  - 不包含状态变量的具体变更值，只知"哪个组件 build 了"
  - 需要用 SmartPerf-Host 或 DevEco Profiler 可视化分析
  - trace 抓取有性能开销，不宜长期开启
- **不存在 `arkuitree` 专门的 category**，ACE 相关 tag 包括 `ace`、`aceui`

### 推荐优先级：★★★★★（最佳方案，系统原生支持，无需侵入代码）

---

## 方案三：hidumper --dump ArkUI 完整组件树

已在方案一中覆盖。补充要点：

- `hidumper -s WindowManagerService -a '-w <WinId> -element -c'` 输出的 `arkui.dump` 文件包含：
  - 完整组件树层级（含 if/else 节点，比 ArkUI Inspector 更详细）
  - 每个节点的：ID、Depth、IsDisappearing、FrameRect（位置尺寸）、BackgroundColor、Visible、Active 状态等
- 通过对比操作前后的 dump 文件，可以发现组件的创建/销毁/属性变化

### 可行性评估
- **成熟度：高** — 官方支持
- **局限：** 
  - 纯快照对比，无法实时监听
  - 不含 `@State` 变量运行时值
  - 需要手动触发 dump + 文件传输
  - 不区分"因状态变化导致的重绘"和"初次创建"

### 推荐优先级：★★★☆☆（辅助验证，配合 trace 使用）

---

## 方案四：开源方案搜索

在 GitHub 和 GitCode 上搜索 `arkui rebuild`、`arkui performance monitor`、`harmonyos 组件刷新` 后：

- **未找到**专门针对 ArkUI 组件重绘检测的开源工具或框架
- 华为官方推荐的性能分析链路是：**HiTrace → SmartPerf-Host → DevEco Profiler**，没有第三方替代方案
- 开源社区在此方向几乎是空白，尚无可直接复用的方案

### 可行性评估：★☆☆☆☆（无现成方案可用）

---

## 方案五：aboutToRebuild + Hvigor 编译插件自动注入

### aboutToRebuild：不存在此生命周期

ArkUI 自定义组件的生命周期回调：

| 生命周期 | 时机 |
|---|---|
| `aboutToAppear` | 组件创建后、build() 前（仅首次） |
| `onDidBuild` (API 12+) | build() 执行后 |
| `aboutToDisappear` | 组件销毁时 |
| `aboutToReuse` / `aboutToRecycle` | @Reusable 组件复用时 |

**没有 `aboutToRebuild` 回调**。组件的 build() 可以被多次执行（状态变化触发），但没有"即将重建"的前置通知。

### Hvigor 自动注入：困难

| 维度 | 评估 |
|---|---|
| Hvigor 插件机制 | 官方支持，基于 TypeScript 开发 npm 插件 |
| 能否操作 ArkTS 源码 | **困难** — Hvigor 是构建编排工具（类似 Gradle），操作的是 Task 级别，**不提供 ArkTS AST 转换能力** |
| ArkTS 编译链 | ArkTS → 方舟字节码(ABC)，编译器是封闭的 `arkc`，无公开的 AST 插件机制 |
| 替代方案 | 在 Hvigor Task 中用正则/简单 parser 对 `.ets` 源码做字符串级注入，但脆弱且不可靠 |

### 可行性评估：★★☆☆☆（思路可行但实现困难，ROI 低）

---

## 综合推荐

| 优先级 | 方案 | 适用场景 | 侵入性 |
|---|---|---|---|
| **1** | **HiTrace Build[tag] trace** | 精确定位哪些组件在哪些帧被执行了 build()，配合 SmartPerf-Host 可视化 | 零侵入 |
| **2** | **hidumper stateVariables** | 分析状态变量 → 组件的依赖关系，定位冗余刷新根因 | 零侵入 |
| **3** | **hidumper 组件树快照对比** | 对比前后帧的组件树结构变化 | 零侵入 |
| **4** | **onDidBuild 手动埋点** | 需要精确统计特定组件的重绘次数时 | 需改业务代码 |
| **5** | **Hvigor 自动注入** | 理论方案，当前 ArkTS 编译链不支持 AST 级扩展 | — |

### 推荐的组合策略

**日常定位流程：**
1. 用 `hitrace` 抓取操作场景的 trace → 在 SmartPerf-Host 中查看 `Build[xxx]`/`CustomNode:BuildItem xxx` 泳道，识别被冗余重建的组件
2. 用 `hidumper -stateVariables` 查看该组件的状态变量依赖关系，找到触发冗余重建的 @State 变量
3. 按照官方最佳实践（拆分状态变量、使用 @Observed/@ObjectLink 等）优化

---

*文档版本: v1.0 | 2026-04-07*
