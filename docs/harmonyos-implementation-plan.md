# AppSmartInspector 鸿蒙实施方案

> 基于 SI 项目代码分析 + 鸿蒙官方文档调研
> 日期：2026-05-17

---

## 一、SI 项目现状

### 架构概述
SmartInspector 是 AI 驱动的移动端性能分析 CLI 工具，通过自然语言交互自动采集 Android Perfetto trace、分析性能瓶颈、归因到源码。

### 核心流水线
```
用户输入 → orchestrator (LLM路由)
  → collector (adb perfetto → .pb → SQL查询 → PerfSummary JSON)
  → analyzer (perf_analyzer Agent: LLM解读 + 确定性预计算hints)
  → attributor (SI$切片 → Glob/Grep/Read源码搜索 → LLM归因)
  → reporter (LLM流式生成Markdown报告)
```

### 关键技术栈
| 组件 | 技术 |
|------|------|
| 编排 | LangGraph StateGraph |
| LLM | DeepSeek / Claude / OpenAI（可配置） |
| Trace采集 | adb + perfetto trace_processor_shell SQL |
| 数据模型 | PerfSummary JSON（14个维度） |
| 源码归因 | SI$ 自定义Hook（Pine AOP，12种Hook类型） |
| CLI | prompt_toolkit REPL |

### 核心功能清单
1. **全量性能分析**：采集trace → 14维度分析 → 源码归因 → 报告
2. **确定性预计算**（无LLM）：严重度分级、调用链分布、热点排名、帧关联
3. **SI$ Hook体系**：Activity/Fragment生命周期、RV管线、Layout、View Traverse等
4. **冷启动分析**：4阶段时序分析
5. **WebSocket双向通信**：CLI↔App桥接

---

## 二、鸿蒙性能分析生态调研

### 2.1 性能数据采集工具

#### SP_daemon（SmartPerf Device daemon）
鸿蒙最核心的命令行性能采集工具，**直接对标 SI 中的 adb perfetto**。

**采集能力**：
| 指标 | 参数 | 说明 |
|------|------|------|
| CPU频率/使用率 | `-c` | 整机+进程级，大中小核分别采集 |
| CPU指令数 | `-ci` | hw-instructions, hw-cpu-cycles |
| GPU频率/负载 | `-g` | gpuFrequency, gpuLoad |
| FPS | `-f` | fps, fpsJitters(每帧间隔ns), refreshrate |
| 内存 | `-r` | pss, heapSize, arktsHeapPss, nativeHeapPss, gpuPss等 |
| 温度 | `-t` | gpu-thermal, soc-thermal |
| 网络 | `-net` | networkDown, networkUp |
| 截图 | `-snapshot` | 每2秒截图 |
| DDR | `-d` | ddrFrequency |
| 线程 | `-threads` | 线程数和TID列表 |
| 文件描述符 | `-fds` | FD数量 |

**采集模式**：
- **定时采集**：`SP_daemon -N 10 -PKG com.xxx -c -g -f -r` （采集10秒）
- **启停采集**：`SP_daemon -start -c -g -f` → 操作应用 → `SP_daemon -stop`
- **场景化采集**：`SP_daemon -editor responseTime com.xxx appName`（响应时延）

**输出格式**：文本格式（key=value对），可导出为CSV
```
order:0 timestamp=1741415955626
order:1 ProcAppName=ohos.samples.ecg
order:2 ProcCpuLoad=2.641511
order:3 fps=30
order:4 fpsJitters=33501562;;50251042;;...
```

**关键路径**：`hdc shell` → `SP_daemon` 命令 → 输出到 stdout 或 CSV

#### hitrace（Trace采集）
**直接对标 SI 中的 perfetto trace 采集**。

```bash
# 采集5秒的ace（UI框架）+ graphic + sched trace
hdc shell hitrace -t 5 ace graphic sched -o /data/local/tmp/mytrace.ftrace

# 采集二进制格式（可用SmartPerf_Host可视化分析）
hdc shell hitrace --raw -t 5 ace graphic sched --file_size 102400

# 启停模式
hdc shell hitrace --trace_begin ace graphic sched
# ... 操作应用 ...
hdc shell hitrace --trace_finish -o /data/local/tmp/trace.ftrace
```

**可用 Tag（与性能分析最相关）**：
| Tag | 说明 |
|-----|------|
| `ace` | ACE UI框架（ArkUI渲染引擎） |
| `graphic` | 图形系统 |
| `sched` | CPU调度 |
| `freq` | CPU频率 |
| `idle` | CPU空闲 |
| `irq` | 中断事件 |
| `disk` | 磁盘I/O |
| `memory` | 内存 |
| `window` | 窗口管理 |
| `animation` | 动画 |
| `ark` | ARK虚拟机 |
| `app` | 应用模块 |
| `ffrt` | FFRT任务 |

**输出格式**：
- **文本格式**（`--text`，默认）：类似 Android systrace 的文本格式
- **二进制格式**（`--raw`）：需用 SmartPerf_Host 工具解析

#### PerfTest API（白盒性能测试）
ArkTS 应用内 API，用于自动化性能测试。

```typescript
import { PerfMetric, PerfTest, PerfTestStrategy } from '@kit.TestKit';

// 定义测试策略
let perfTestStrategy: PerfTestStrategy = {
  metrics: [PerfMetric.DURATION, PerfMetric.CPU_USAGE],  // 或 PerfMetric.LIST_SWIPE_FPS
  actionCode: async (finish) => {
    myFunction(); // 被测代码
    finish(true);
  },
  resetCode: async (finish) => {
    resetState();
    finish(true);
  },
  bundleName: "com.example.app",
  iterations: 10,
  timeout: 20000
};

let perfTest = PerfTest.create(perfTestStrategy);
await perfTest.run();
let result = perfTest.getMeasureResult(PerfMetric.DURATION);
// result.average, result.max, result.min, result.details[]
```

**支持指标**：`DURATION`、`CPU_USAGE`、`LIST_SWIPE_FPS`、启动时延、页面切换时延

#### HitraceMeter API（应用内打点）
应用内自定义 Trace 打点，**对标 SI 的 SI$ Hook 体系**。

```typescript
import { hiTraceMeter } from '@kit.PerformanceAnalysisKit';

// 开始trace
hiTraceMeter.startTrace("my_function", 1);
// ... 执行代码 ...
hiTraceMeter.finishTrace("my_function", 1);

// 带参数的trace
hiTraceMeter.startTrace("layout_measure", 1, "item_count", 50);
```

### 2.2 hdc 命令行工具
**对标 SI 中的 adb**。

```bash
# 基本连接
hdc list targets          # 列出设备
hdc shell                 # 进入shell

# 应用管理
hdc install app.hap       # 安装应用
hdc shell aa start -a EntryAbility -b com.example.app  # 启动应用
hdc shell aa force-stop com.example.app  # 停止应用

# 文件传输
hdc file recv /data/local/tmp/trace.ftrace ./local/  # 从设备拉取文件
hdc file send ./local/file.txt /data/local/tmp/       # 推送文件到设备

# 进程信息
hdc shell ps -ef | grep com.example.app  # 查看进程
hdc shell pidof com.example.app           # 获取PID
```

### 2.3 Trace 数据格式与可视化

| 格式 | 来源 | 可视化工具 |
|------|------|-----------|
| `.ftrace`（文本） | hitrace --text | 文本编辑器直接查看 |
| `.trace`（二进制） | hitrace --raw | **SmartPerf_Host** |
| CSV | SP_daemon | Excel/脚本分析 |
| key=value文本 | SP_daemon stdout | 脚本解析 |

**SmartPerf_Host**：鸿蒙官方 trace 可视化工具，类似 Android 的 Perfetto UI。
- GitHub: `openharmony/developtools_smartperf_host`
- 支持打开二进制 trace 文件
- 提供泳道图、帧分析、调用链分析

### 2.4 渲染流程 Trace（性能分析关键）

鸿蒙一帧的渲染 Trace 关键节点：

**UI 后端引擎侧**：
1. `OnVsyncEvent` → 收到 Vsync 信号
2. `FlushVsync` → 刷新视图同步
3. `UITaskScheduler::FlushTask` → 刷新 UI 任务
4. `FlushLayoutTask` → 布局
5. `FlushRenderTask` → 渲染
6. `Layout` → 节点布局
7. `FrameNode::RenderTask` → 单节点渲染

**Render Service 侧**：
1. `RSMainThread::DoComposition` → 合成图层
2. `RSMainThread::ProcessCommand` → 处理指令
3. `Animate` → 动画
4. `ProcessDisplayRenderNode` → 显示器绘制
5. `Repaint` / `Redraw` → 合成绘制/GPU重绘
6. `RenderFrame` → GPU绘制
7. `SwapBuffers` → 刷新缓冲区

---

## 三、功能映射与可行性分析

### 功能映射表

| SI 功能 | Android 实现 | 鸿蒙实现方案 | 可行性 | 说明 |
|---------|-------------|-------------|--------|------|
| **Trace 采集** | adb + perfetto | hdc + hitrace | ✅ 直接迁移 | 命令模式完全对应，hdc 替代 adb |
| **性能指标采集** | perfetto SQL查询 | SP_daemon 命令 | ✅ 直接迁移 | SP_daemon 覆盖 CPU/GPU/FPS/内存/温度 |
| **14维度 PerfSummary** | trace_processor SQL | SP_daemon + hitrace 解析 | ⚠️ 需适配 | 数据源不同，需重写数据采集层 |
| **确定性预计算** | 纯 Python | 纯 Python（不变） | ✅ 直接复用 | 无平台依赖，只改输入数据格式 |
| **LLM 分析** | LangGraph + LLM API | 不变 | ✅ 直接复用 | LLM 调用无平台依赖 |
| **SI$ Hook 归因** | Pine AOP + Android Hook | HitraceMeter API | ⚠️ 需重写 | 打点方式不同，需鸿蒙SDK重新Hook |
| **源码归因** | Glob/Grep/Read 工具 | 不变 | ✅ 直接复用 | 文件操作无平台依赖 |
| **CLI 交互** | prompt_toolkit | 不变 | ✅ 直接复用 | CLI 层无平台依赖 |
| **报告生成** | LLM → Markdown | 不变 | ✅ 直接复用 | 报告逻辑无平台依赖 |
| **WebSocket通信** | ws 库 | 不变 | ✅ 直接复用 | 通信层无平台依赖 |

### 可行性总结
- **可直接复用**（~60%）：LLM分析、确定性预计算、源码归因、CLI、报告生成、通信
- **需适配**（~25%）：数据采集层（hdc/SP_daemon/hitrace 替代 adb/perfetto）
- **需重写**（~15%）：SI$ Hook 体系（Android Pine → 鸿蒙 HitraceMeter）

---

## 四、具体实现方案

### 4.1 架构设计

保持 LangGraph 编排架构不变，只替换数据采集层：

```
                        ┌──────────────────────┐
                        │   LangGraph 编排层     │
                        │  (不变：orchestrator)  │
                        └──────────┬───────────┘
                                   │
                    ┌──────────────┴───────────────┐
                    │                              │
          ┌─────────▼─────────┐         ┌─────────▼─────────┐
          │  数据采集层（替换）  │         │  AI 分析层（不变）   │
          │                    │         │                     │
          │  HarmonyCollector  │         │  deterministic.py   │
          │  ├─ SPDaemonCollector │       │  perf_analyzer.py   │
          │  ├─ HiTraceCollector  │       │  attributor.py      │
          │  └─ HdcBridge         │       │  verifier.py        │
          │                    │         │  reporter.py        │
          └────────────────────┘         └────────────────────┘
```

### 4.2 数据采集层实现

#### 4.2.1 HdcBridge（设备通信）

```python
# src/smartinspector/collector/hdc_bridge.py

import subprocess
from typing import Optional

class HdcBridge:
    """鸿蒙设备通信桥，对标 Android 的 ADB"""

    def __init__(self, device_serial: Optional[str] = None):
        self.device = device_serial
        self._base_cmd = ["hdc"]
        if device_serial:
            self._base_cmd += ["-t", device_serial]

    def shell(self, cmd: str, timeout: int = 30) -> str:
        """执行 hdc shell 命令"""
        full_cmd = self._base_cmd + ["shell", cmd]
        result = subprocess.run(
            full_cmd, capture_output=True, text=True, timeout=timeout
        )
        return result.stdout.strip()

    def pull(self, remote: str, local: str) -> bool:
        """从设备拉取文件"""
        cmd = self._base_cmd + ["file", "recv", remote, local]
        result = subprocess.run(cmd, capture_output=True, text=True)
        return result.returncode == 0

    def push(self, local: str, remote: str) -> bool:
        """推送文件到设备"""
        cmd = self._base_cmd + ["file", "send", local, remote]
        result = subprocess.run(cmd, capture_output=True, text=True)
        return result.returncode == 0

    def get_pid(self, package_name: str) -> Optional[int]:
        """获取应用PID"""
        output = self.shell(f"pidof {package_name}")
        try:
            return int(output.strip())
        except ValueError:
            return None

    def start_app(self, bundle_name: str, ability_name: str = "EntryAbility"):
        """启动应用"""
        self.shell(f"aa start -a {ability_name} -b {bundle_name}")

    def stop_app(self, bundle_name: str):
        """停止应用"""
        self.shell(f"aa force-stop {bundle_name}")
```

#### 4.2.2 SPDaemonCollector（性能指标采集）

```python
# src/smartinspector/collector/sp_daemon.py

import re
from dataclasses import dataclass, field
from typing import Dict, List, Optional

@dataclass
class HarmonyPerfSummary:
    """鸿蒙性能摘要，对标 Android 的 PerfSummary"""
    # 帧数据
    fps_list: List[int] = field(default_factory=list)
    fps_jitters: List[List[int]] = field(default_factory=list)
    refresh_rate: int = 0

    # CPU
    total_cpu_usage: List[float] = field(default_factory=list)
    proc_cpu_usage: List[float] = field(default_factory=list)
    proc_cpu_load: List[float] = field(default_factory=list)
    cpu_frequencies: Dict[str, List[int]] = field(default_factory=dict)

    # GPU
    gpu_frequency: List[int] = field(default_factory=list)
    gpu_load: List[float] = field(default_factory=list)

    # 内存
    pss: List[int] = field(default_factory=list)
    heap_alloc: List[int] = field(default_factory=list)
    arkts_heap_pss: List[int] = field(default_factory=list)
    native_heap_pss: List[int] = field(default_factory=list)
    gpu_pss: List[int] = field(default_factory=list)
    mem_available: List[int] = field(default_factory=list)

    # 温度
    gpu_thermal: List[float] = field(default_factory=list)
    soc_thermal: List[float] = field(default_factory=list)

    # 网络
    network_down: List[int] = field(default_factory=list)
    network_up: List[int] = field(default_factory=list)

    # 线程
    thread_count: List[int] = field(default_factory=list)

class SPDaemonCollector:
    """通过 SP_daemon 采集鸿蒙性能数据"""

    def __init__(self, hdc_bridge, package_name: str):
        self.hdc = hdc_bridge
        self.package = package_name

    def collect_all(self, duration: int = 10) -> HarmonyPerfSummary:
        """全量采集性能数据"""
        cmd = (
            f"SP_daemon -N {duration} -PKG {self.package} "
            f"-c -g -t -f -r -d -net -snapshot -threads"
        )
        output = self.hdc.shell(cmd, timeout=duration + 30)
        return self._parse_output(output)

    def collect_cpu_only(self, duration: int = 10) -> Dict:
        """只采集CPU数据"""
        cmd = f"SP_daemon -N {duration} -PKG {self.package} -c"
        output = self.hdc.shell(cmd, timeout=duration + 10)
        return self._parse_key_value(output)

    def collect_fps(self, duration: int = 10) -> Dict:
        """只采集帧率数据"""
        cmd = f"SP_daemon -N {duration} -PKG {self.package} -f"
        output = self.hdc.shell(cmd, timeout=duration + 10)
        return self._parse_key_value(output)

    def start_collection(self, metrics: str = "-c -g -t -f -r"):
        """启停模式：开始采集"""
        cmd = f"SP_daemon -start -PKG {self.package} {metrics}"
        self.hdc.shell(cmd)

    def stop_collection(self) -> str:
        """启停模式：停止采集，返回CSV路径"""
        output = self.hdc.shell("SP_daemon -stop")
        # 解析输出路径: "Output Path: data/local/tmp/smartperf/1/t_index_info.csv"
        match = re.search(r'Output Path:\s*(\S+)', output)
        if match:
            csv_path = match.group(1)
            self.hdc.pull(csv_path, f"/tmp/harmony_perf_{self.package}.csv")
            return f"/tmp/harmony_perf_{self.package}.csv"
        return ""

    def _parse_key_value(self, output: str) -> List[Dict]:
        """解析 SP_daemon 的 key=value 输出"""
        samples = []
        current = {}
        for line in output.split("\n"):
            line = line.strip()
            if line.startswith("order:0"):
                if current:
                    samples.append(current)
                current = {"timestamp": ""}
            match = re.match(r'order:\d+\s+(\w+)=(.*)', line)
            if match:
                key, value = match.group(1), match.group(2)
                try:
                    if '.' in value:
                        current[key] = float(value)
                    else:
                        current[key] = int(value)
                except ValueError:
                    current[key] = value
        if current:
            samples.append(current)
        return samples

    def _parse_output(self, output: str) -> HarmonyPerfSummary:
        """解析全量采集输出为 HarmonyPerfSummary"""
        samples = self._parse_key_value(output)
        summary = HarmonyPerfSummary()

        for sample in samples:
            if "fps" in sample:
                summary.fps_list.append(int(sample["fps"]))
            if "ProcCpuUsage" in sample:
                summary.proc_cpu_usage.append(sample["ProcCpuUsage"])
            if "TotalcpuUsage" in sample:
                summary.total_cpu_usage.append(sample["TotalcpuUsage"])
            if "pss" in sample:
                summary.pss.append(int(sample["pss"]))
            if "gpuLoad" in sample:
                summary.gpu_load.append(sample["gpuLoad"])
            if "gpu-thermal" in sample:
                summary.gpu_thermal.append(sample["gpu-thermal"])
            # ... 更多字段映射

        return summary
```

#### 4.2.3 HiTraceCollector（Trace 采集）

```python
# src/smartinspector/collector/hitrace.py

import os
import tempfile
from typing import Optional

class HiTraceCollector:
    """通过 hitrace 采集鸿蒙 trace 数据"""

    # 性能分析常用 tag 组合
    PERF_TAGS = "ace graphic sched freq idle irq disk memory window animation ark app ffrt"

    def __init__(self, hdc_bridge):
        self.hdc = hdc_bridge

    def collect_trace(self, duration: int = 5,
                      tags: Optional[str] = None,
                      binary: bool = True) -> str:
        """
        采集 trace 数据

        Args:
            duration: 采集时长（秒）
            tags: hitrace tag，默认用性能分析常用组合
            binary: 是否采集二进制格式

        Returns:
            本地 trace 文件路径
        """
        tags = tags or self.PERF_TAGS
        remote_path = f"/data/local/tmp/si_trace_{'raw' if binary else 'text'}"

        if binary:
            cmd = f"hitrace --raw -t {duration} {tags} -o {remote_path}"
        else:
            cmd = f"hitrace -t {duration} {tags} -o {remote_path}"

        self.hdc.shell(cmd, timeout=duration + 30)

        # 拉取到本地
        local_path = os.path.join(
            tempfile.gettempdir(),
            f"harmony_trace_{int(time.time())}.{'trace' if binary else 'ftrace'}"
        )
        self.hdc.pull(remote_path, local_path)
        return local_path

    def start_trace(self, tags: Optional[str] = None):
        """启停模式：开始采集"""
        tags = tags or self.PERF_TAGS
        self.hdc.shell(f"hitrace --trace_begin {tags}")

    def stop_trace(self, output_path: str = "/data/local/tmp/si_trace.ftrace") -> str:
        """启停模式：停止采集并拉取"""
        self.hdc.shell(f"hitrace --trace_finish -o {output_path}")
        local_path = os.path.join(temptempfile.gettempdir(), "harmony_trace.ftrace")
        self.hdc.pull(output_path, local_path)
        return local_path
```

### 4.3 数据适配层

将 `HarmonyPerfSummary` 转换为现有 `PerfSummary` 格式，使下游分析逻辑无需修改：

```python
# src/smartinspector/collector/harmony_adapter.py

class HarmonyAdapter:
    """将鸿蒙性能数据适配为 SI 内部 PerfSummary 格式"""

    def adapt(self, harmony_summary: HarmonyPerfSummary,
              trace_path: Optional[str] = None) -> dict:
        """转换为 PerfSummary JSON"""
        return {
            "frame_timeline": self._adapt_frames(harmony_summary),
            "cpu_usage": self._adapt_cpu(harmony_summary),
            "gpu_usage": self._adapt_gpu(harmony_summary),
            "memory": self._adapt_memory(harmony_summary),
            "thermal": self._adapt_thermal(harmony_summary),
            "network": self._adapt_network(harmony_summary),
            # trace 数据需要额外解析
            "trace_slices": self._parse_trace(trace_path) if trace_path else [],
            "platform": "harmony",
        }

    def _adapt_frames(self, summary: HarmonyPerfSummary) -> dict:
        """帧数据适配"""
        jitters_ms = []
        for jitter_list in summary.fps_jitters:
            jitters_ms.append([j / 1_000_000 for j in jitter_list])

        return {
            "fps_samples": summary.fps_list,
            "avg_fps": sum(summary.fps_list) / len(summary.fps_list) if summary.fps_list else 0,
            "refresh_rate": summary.refresh_rate,
            "jitters_ms": jitters_ms,
            # 鸿蒙帧预算计算（和Android相同）
            "frame_budget_ms": 1000 / summary.refresh_rate if summary.refresh_rate else 16.67,
        }
```

### 4.4 SI$ Hook 鸿蒙替代方案

Android 的 SI$ Hook 通过 Pine AOP 框架注入框架方法。鸿蒙对应方案：

**方案：HitraceMeter 打点 + 框架层 Trace**

```typescript
// 鸿蒙应用侧打点模块
// entry/src/main/ets/utils/SITrace.ets

import { hiTraceMeter } from '@kit.PerformanceAnalysisKit';

export class SITrace {
  // 页面生命周期
  static onPageShow(tag: string) {
    hiTraceMeter.startTrace(`SI$PageShow#${tag}`, 1);
  }
  static onPageHide(tag: string) {
    hiTraceMeter.finishTrace(`SI$PageShow#${tag}`, 1);
  }

  // 布局测量
  static onMeasure(component: string) {
    hiTraceMeter.startTrace(`SI$Measure#${component}`, 2);
  }
  static onMeasureEnd(component: string) {
    hiTraceMeter.finishTrace(`SI$Measure#${component}`, 2);
  }

  // 列表渲染
  static onListItemRender(index: number) {
    hiTraceMeter.startTrace(`SI$ListItem#${index}`, 3);
  }
  static onListItemRenderEnd(index: number) {
    hiTraceMeter.finishTrace(`SI$ListItem#${index}`, 3);
  }

  // 自定义方法
  static traceMethod(method: string, fn: () => void) {
    hiTraceMeter.startTrace(`SI$Custom#${method}`, 4);
    try {
      fn();
    } finally {
      hiTraceMeter.finishTrace(`SI$Custom#${method}`, 4);
    }
  }

  static async traceAsyncMethod(method: string, fn: () => Promise<void>) {
    hiTraceMeter.startTrace(`SI$Custom#${method}`, 5);
    try {
      await fn();
    } finally {
      hiTraceMeter.finishTrace(`SI$Custom#${method}`, 5);
    }
  }
}
```

**使用方式**：
```typescript
// 在页面/组件中使用
@Entry
@Component
struct ListPage {
  build() {
    List() {
      LazyForEach(this.dataSource, (item) => {
        ListItem() {
          SITrace.traceMethod(`Item_${item.id}`, () => {
            this.buildItem(item)
          })
        }
      })
    }
    .onAppear(() => SITrace.onPageShow('ListPage'))
    .onDisappear(() => SITrace.onPageHide('ListPage'))
  }
}
```

### 4.5 LLM 适配

现有的 `deterministic.py` 和 `perf_analyzer.py` 只需要适配输入数据格式：

```python
# 修改点：deterministic.py 中的帧预算计算
def calculate_severity(self, frame_time_ms: float, refresh_rate: int = 60) -> str:
    """帧严重度分级 - 通用逻辑，Android/Harmony 共用"""
    frame_budget = 1000.0 / refresh_rate
    if frame_time_ms <= frame_budget:
        return "OK"
    elif frame_time_ms <= frame_budget * 2:
        return "P1"
    else:
        return "P0"

# 修改点：perf_analyzer.py 的 prompt 模板
HARMONY_SYSTEM_PROMPT = """
你是鸿蒙应用性能分析专家。分析以下性能数据，数据来自 HarmonyOS 设备：
- SP_daemon 采集的 CPU/GPU/FPS/内存/温度数据
- hitrace 采集的系统 trace 数据
- 应用内 HitraceMeter 打点（SI$ 前缀）

鸿蒙性能关键指标：
- ACE UI框架（ark）：UI渲染管线
- Render Service（graphic）：合成渲染
- ARK虚拟机（ark）：JS/TS执行
- FFRT：异步任务调度

请参照 Android 分析的输出格式...
"""
```

---

## 五、技术栈选型

| 组件 | 技术 | 版本 |
|------|------|------|
| **开发语言** | Python 3.10+（后端）/ ArkTS（应用侧Hook） | - |
| **编排框架** | LangGraph | 与现有SI一致 |
| **设备通信** | hdc CLI | HarmonyOS SDK 5.0+ |
| **性能采集** | SP_daemon | 系统预置（API 9+） |
| **Trace采集** | hitrace CLI | 系统预置 |
| **应用打点** | @kit.PerformanceAnalysisKit HitraceMeter | API 9+ |
| **白盒测试** | @kit.TestKit PerfTest | API 10+ |
| **Trace可视化** | SmartPerf_Host | 开源工具 |
| **LLM** | DeepSeek / Claude（复用现有配置） | - |

---

## 六、开发路线图

### P0 - 核心可用（2-3周）
**目标**：能用 hdc 采集性能数据 + LLM 分析 + 生成报告

1. **HdcBridge**：hdc 命令封装（设备发现、shell、文件传输）
2. **SPDaemonCollector**：SP_daemon 数据采集和解析
3. **HarmonyAdapter**：数据格式适配（HarmonyPerfSummary → PerfSummary）
4. **graph/nodes/collector.py 适配**：增加 `harmony` 平台路由
5. **测试**：真机 SP_daemon 采集 → 数据解析 → LLM 分析 → 报告

**验证标准**：在鸿蒙真机上采集某应用的 CPU/FPS/内存数据，生成性能分析报告

### P1 - Trace 分析（2-3周）
**目标**：支持 hitrace 采集 + trace 数据分析 + 帧级归因

6. **HiTraceCollector**：hitrace 命令封装和文件拉取
7. **Trace 解析**：文本格式 ftrace 解析（提取渲染管线关键节点）
8. **帧分析适配**：将鸿蒙渲染 Trace（FlushVsync、RenderFrame等）映射到帧分析逻辑
9. **deterministic.py 适配**：增加鸿蒙特有的热点检测规则
10. **测试**：采集 UI 渲染 trace → 识别丢帧 → 归因分析

**验证标准**：采集某列表页面的渲染 trace，识别卡顿帧并归因到具体渲染阶段

### P2 - 高级特性（3-4周）
**目标**：完整 SI$ Hook + 冷启动 + 可视化

11. **SITrace 鸿蒙 SDK**：HitraceMeter 封装的打点库
12. **源码归因适配**：鸿蒙 ArkTS 项目结构的源码搜索
13. **冷启动分析**：鸿蒙应用启动阶段的 trace 采集和分析
14. **SmartPerf_Host 集成**：自动打开 trace 可视化
15. **PerfTest 集成**：自动化性能测试流水线

**验证标准**：完整的采集 → 分析 → 归因 → 报告流程，支持冷启动和滑动场景

---

## 七、风险点和应对策略

### 高风险

| 风险 | 影响 | 概率 | 应对策略 |
|------|------|------|---------|
| **SP_daemon 输出格式不稳定** | 解析失败 | 中 | 多设备测试 + 容错解析 + 版本检测 |
| **hitrace 二进制格式文档缺失** | 无法深度解析 trace | 高 | 优先用文本格式；SmartPerf_Host 可视化辅助分析 |
| **真机权限限制** | 部分采集需要 root | 中 | SP_daemon 基本采集不需 root；需要 root 的标注清楚 |

### 中风险

| 风险 | 影响 | 概率 | 应对策略 |
|------|------|------|---------|
| **hdc 生态不如 adb 成熟** | 连接稳定性 | 中 | 增加重连逻辑 + 错误处理 |
| **鸿蒙渲染 Trace 节点与 Android 差异大** | 帧分析不准确 | 中 | 基于官方 Trace 文档逐个映射，建立对照表 |
| **HitraceMeter 打点有限** | 无法 Hook 框架内部方法 | 高 | 明确标注应用层打点 vs 框架层 Trace 的边界 |

### 低风险

| 风险 | 影响 | 概率 | 应对策略 |
|------|------|------|---------|
| **LLM 分析准确率** | 分析质量 | 低 | 已有 Android 验证，鸿蒙数据格式类似 |
| **Python 依赖兼容** | 环境问题 | 低 | 现有依赖均为纯 Python，跨平台无问题 |

### 需要验证项

1. **SP_daemon 在不同鸿蒙版本（3.x/4.x/5.x）的输出差异** → 在多台设备上测试
2. **hitrace 文本格式的解析可行性** → 采集实际 trace 文件验证解析规则
3. **hdc 的连接稳定性** → 长时间采集场景测试
4. **HitraceMeter 打点数据在 hitrace 输出中的格式** → 实际打点后采集验证
5. **二进制 trace 格式的解析** → 调研 SmartPerf_Host 是否提供解析库或 API

---

## 八、总结

### 核心判断
**SI 鸿蒙化是完全可行的**，原因：
1. **60%的代码可以直接复用**（LLM分析、确定性计算、CLI、报告）
2. **数据采集有成熟替代**（SP_daemon 对标 perfetto，hitrace 对标 systrace）
3. **架构解耦好**：只需替换数据采集层，不影响分析层

### 差异化价值
SI 鸿蒙版的独特价值在于：
- 鸿蒙生态目前**缺乏 AI 驱动的性能分析工具**
- SP_daemon 和 hitrace 数据靠人工分析效率低
- SI 的 LLM 分析 + 源码归因能力在鸿蒙领域是空白

### 建议行动
1. **先搞一台鸿蒙真机**，验证 SP_daemon 和 hitrace 的实际输出
2. **从 P0 开始**，2周内出 MVP（能采集 + 能分析 + 能出报告）
3. P0 验证通过后再投入 P1/P2
