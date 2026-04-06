# SmartInspector 鸿蒙支持方案

> 日期：2026-04-07
> 状态：调研阶段

## 1. 目标

将 SmartInspector 从 Android-only 扩展为 **Android + HarmonyOS** 双平台性能分析工具，复用现有的 Agent 编排层、报告生成层，新增鸿蒙采集层和鸿蒙 SDK。

## 2. 鸿蒙性能分析技术栈对比

| 维度 | Android (现有) | HarmonyOS NEXT |
|------|---------------|----------------|
| Trace 采集 | `adb shell atrace` / Perfetto | ` hdc shell bytrace` / HiTrace |
| Trace 格式 | Perfetto format (.perfetto-trace) | Perfetto format (兼容) / Systrace format |
| Trace 分析 | Perfetto TraceProcessor (SQL) | Perfetto TraceProcessor (兼容) |
| 设备通信 | `adb` commands | `hdc` commands |
| 布局层级 | View Hierarchy (ViewCompat) | Component Tree (Inspector) |
| 布局 Hook | bytecode hook (layoutInflater) | ArkUI 声明式 (无传统 inflate) |
| 性能指标 | CPU/Memory/IO/Scheduling/GPU | CPU/Memory/IO/Scheduling/GPU (类似) |
| App Hook | Pine/Epic (bytecode instrument) | ArkCompiler AOT (暂无 hook 框架) |
| 日志系统 | logcat | hilog |
| 帧率监控 | `dumpsys gfxinfo` | `hidumper` / ArkUI DevTools |

## 3. 关键发现

### 3.1 ✅ Trace 格式兼容（最大利好）

鸿蒙 NEXT 的 `bytrace` 底层使用 Perfetto 作为 trace 引擎，输出的 trace 文件格式与 Android Perfetto 完全兼容。这意味着：

- **现有的 `PerfettoCollector` 和所有 SQL 查询可以直接复用**
- `TraceProcessor` 可以直接解析鸿蒙 trace 文件
- CPU hotspots、scheduling、IO 等 SQL 查询无需修改

### 3.2 ⚠️ 采集命令差异

```bash
# Android (现有)
adb shell atrace --async_start -b 32768 gfx view input sched freq
# ... 操作 ...
adb shell atrace --async_stop > trace.atrace
# 或
adb shell perfetto -c - txt <config> > trace.perfetto-trace

# HarmonyOS NEXT
hdc shell bytrace --async_start -b 32768 gfx view input sched freq
# ... 操作 ...
hdc shell bytrace --async_stop > trace.atrace
# 或通过 hdc file recv 获取
hdc file recv /data/local/tmp/trace.perfetto-trace ./trace.perfetto-trace
```

### 3.3 ⚠️ 设备通信差异

```bash
# Android: adb
adb devices
adb forward tcp:9876 tcp:9876
adb shell <cmd>

# HarmonyOS: hdc
hdc list targets
hdc fport tcp:9876 tcp:9876
hdc shell <cmd>
```

`hdc` 命令与 `adb` 接口高度相似，但参数名有细微差异。

### 3.4 ❌ 布局 Hook 不可行

这是最大的差异。鸿蒙 NEXT 使用 ArkUI 声明式开发，没有传统的 `LayoutInflater.inflate()` 调用点：

- **Android**: 可通过 bytecode hook 拦截 `LayoutInflater.inflate()` 获取布局文件名
- **HarmonyOS**: ArkUI 布局是声明式的，编译时生成 C++ 渲染代码，**无法 hook**
- **替代方案**: 通过 DevTools API 或 ArkUI Inspector 获取组件树

### 3.5 ❌ App 内 Hook 框架缺失

鸿蒙 NEXT 没有 Pine/Epic 这样的 bytecode instrument 框架：
- 没有 `BlockMonitor`（方法耗时 hook）
- 没有 `SI$ tag`（代码标记）
- **替代方案**: 依赖 trace 数据 + 源码静态分析

## 4. 架构设计

### 4.1 抽象设备通信层

```
smartinspector/
├── device/
│   ├── base.py          # DeviceAdapter ABC
│   ├── adb.py           # Android AdbAdapter
│   └── hdc.py           # HarmonyOS HdcAdapter
```

```python
class DeviceAdapter(ABC):
    @abstractmethod
    def shell(self, cmd: str) -> str: ...
    
    @abstractmethod
    def forward(self, local_port: int, remote_port: int) -> None: ...
    
    @abstractmethod
    def pull(self, remote: str, local: str) -> None: ...
    
    @abstractmethod
    def push(self, local: str, remote: str) -> None: ...
    
    @abstractmethod
    def list_devices(self) -> list[str]: ...
    
    @abstractmethod
    def trace_command(self, categories: list[str]) -> str: ...  # atrace vs bytrace
```

### 4.2 采集层改造

```
smartinspector/collector/
├── perfetto.py          # 现有，保持不变（SQL 查询通用）
├── base.py              # TraceCollector ABC
├── android_collector.py # Android 特有采集逻辑
└── harmony_collector.py # 鸿蒙特有采集逻辑
```

关键改动点：
- `PerfettoCollector.__init__` 接受 `DeviceAdapter` 而非直接用 `subprocess.run("adb ...")`
- trace 采集命令通过 `adapter.trace_command()` 获取
- SQL 查询层完全复用（Perfetto 格式兼容）

### 4.3 鸿蒙特有功能

#### 4.3.1 布局分析（替代方案）

由于无法 hook ArkUI inflate，采用以下策略：

**方案A: 源码静态分析（推荐，第一版）**
- 解析 `.ets` 文件，提取 `@Builder`、`build()` 函数中的组件树
- 识别嵌套层级、重复组件、条件渲染分支
- 对应 Android 的 XML 布局分析，但基于源码而非运行时

```typescript
// 鸿蒙 ETS 文件示例
@Entry
@Component
struct ItemPage {
  build() {
    Column() {
      Row() {
        Image($r('app.icon')).width(48).height(48)
        Text('Title').fontSize(16)
      }
      List() {
        ForEach(this.items, (item: string) => {
          ListItem() {
            Text(item)
          }
        })
      }
    }
  }
}
```

**方案B: DevTools 协议（第二版）**
- 通过 WebSocket 连接 DevTools Inspector
- 获取运行时组件树（类似 Chrome DevTools Protocol）
- 需要设备开启开发者模式

#### 4.3.2 性能指标

鸿蒙特有的可采集指标：
- **ArkUI 渲染帧率**: 通过 trace 中的 `arkui` category
- **方舟编译器 JIT**: 通过 trace 中的 `arkcompiler` category  
- **分布式通信**: 通过 trace 中的 `distributed` category
- **GPU 渲染**: 通过 trace 中的 `gpu` category

这些都可以通过 Perfetto SQL 查询获取，与 Android 共用分析层。

### 4.4 Agent 层改造

```
smartinspector/agents/
├── android.py           # 现有
├── harmony.py           # 新增：鸿蒙专家 Agent
└── ...
```

`HarmonyExpert` Agent:
- 了解 ArkTS/ETS 语法
- 能分析 `.ets` 文件的组件结构
- 理解鸿蒙特有的性能问题（LazyForEach 滚动性能、@State/@Prop 重组开销等）
- 工具：源码搜索（复用 grep/glob/read）+ trace 分析（复用 perfetto tools）

### 4.5 SDK 层

```
platform/
├── android/tracelib/    # 现有
└── harmony/tracelib/    # 新增
    └── ohos/
        ├── entry/
        │   └── src/main/ets/
        │       ├── pages/
        │       │   └── HookConfigPage.ets     # Hook 配置 UI
        │       ├── services/
        │       │   ├── TraceService.ets        # Trace 控制
        │       │   └── WebSocketClient.ets     # WS 连接
        │       └── EntryAbility.ets
        └── oh-package.json5
```

鸿蒙 SDK 能力（第一版）：
- WebSocket 连接到 Agent 端
- 上报设备信息（OS 版本、设备型号）
- 接收 trace 控制命令（start/stop）
- **不支持 App 内 hook**（无 bytecode instrument）

## 5. 实施路径

### Phase 1: 基础设施（1-2 周）

**目标**: 能在鸿蒙设备上采集 trace 并分析

1. 实现 `DeviceAdapter` 抽象 + `HdcAdapter`
2. 改造 `PerfettoCollector` 支持 `DeviceAdapter` 注入
3. 实现 `HarmonyCollector`（bytrace 采集 + Perfetto 分析）
4. CLI 支持 `hdc` 设备发现和连接
5. 基础测试

**产出**: 能通过 CLI 连接鸿蒙设备，采集 trace，生成性能报告

### Phase 2: 鸿蒙专家 Agent（1-2 周）

**目标**: 鸿蒙特有性能问题识别

1. 实现 `HarmonyExpert` Agent
2. 新增 `.ets` 文件解析工具（组件树提取）
3. ArkUI 性能问题知识库（LazyForEach、@State 重组、组件复用等）
4. 鸿蒙特有 trace category 的 SQL 查询
5. 集成到现有的 graph 编排流程

**产出**: 能识别鸿蒙特有性能问题并给出优化建议

### Phase 3: 鸿蒙 SDK（1 周）

**目标**: App 端集成

1. 实现鸿蒙 SDK（WebSocket 连接 + trace 控制）
2. Hook 配置 UI（ArkUI 页面）
3. 设备信息上报
4. SDK 文档

**产出**: 鸿蒙 App 可集成 SDK，通过 WS 与 Agent 通信

### Phase 4: 高级功能（后续）

1. DevTools 协议集成（运行时组件树）
2. 方舟编译器性能分析
3. 分布式性能分析
4. 与 SmartInspector Android 版统一报告格式

## 6. 风险和挑战

| 风险 | 影响 | 缓解措施 |
|------|------|---------|
| ArkUI 无法 hook 布局 | 无法自动标记布局性能问题 | 源码静态分析替代 |
| hdc 文档不全 | 适配可能有坑 | hdc 与 adb 高度相似，参考 adb 实现 |
| 鸿蒙 trace category 差异 | 部分 SQL 查询可能不适用 | 先验证现有 SQL 在鸿蒙 trace 上的兼容性 |
| ArkTS 语法解析复杂 | .ets 解析可能不完整 | 第一版用正则/简单解析，后续用 Tree-sitter |
| 鸿蒙 SDK API 不稳定 | 未来版本可能需要适配 | 锁定 API Level，跟进官方更新 |

## 7. 与现有代码的兼容性

- **完全兼容**: Perfetto SQL 查询、报告生成、Agent 编排、WS 通信、Token 追踪
- **需要适配**: 设备通信（adb→hdc）、trace 采集命令、Agent 知识库
- **需要新增**: 鸿蒙特有分析、ETS 解析、鸿蒙 SDK
- **不适用**: Android bytecode hook、BlockMonitor、SI$ tag

## 8. 技术验证清单

在正式开发前，建议先验证以下技术点：

- [ ] `hdc shell bytrace` 采集的 trace 文件能否被 `TraceProcessor` 正常解析
- [ ] 现有 CPU hotspots SQL 查询在鸿蒙 trace 上是否有效
- [ ] `hdc fport` 端口转发是否稳定（WS 连接依赖）
- [ ] 鸿蒙 trace 中 view/render 相关的 slice 名称
- [ ] 鸿蒙日志系统 `hilog` 的输出格式

---

*文档版本: v1.0 | 作者: Claw | 2026-04-07*
