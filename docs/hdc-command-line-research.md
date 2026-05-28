# HarmonyOS NEXT hdc 命令行工具完整调研

> 调研日期: 2026-04-07
> 来源: 华为官方文档 + 社区资料

---

## 目录

1. [hdc 核心命令对照表 (与 adb 对比)](#1-hdc-核心命令对照表)
2. [hitrace 完整用法](#2-hitrace-完整用法)
3. [hidumper 性能采集能力](#3-hidumper-性能采集能力)
4. [hilog 日志系统](#4-hilog-日志系统)
5. [SmartPerf / SP_daemon 命令行工具](#5-smartperf--sp_daemon)
6. [hiperf 性能剖析工具](#6-hiperf)
7. [aa / bm 等辅助工具](#7-辅助工具)

---

## 1. hdc 核心命令对照表

### 1.1 hdc 架构概述

hdc (HarmonyOS Device Connector) 由三部分组成:
- **客户端 (client)**: 运行在电脑端，执行 hdc 命令时启动，命令结束后自动退出
- **服务器 (server)**: 运行在电脑端的后台服务，管理客户端和设备端 daemon 之间的通信
- **守护程序 (daemon)**: 运行在设备端，响应服务器请求

服务器默认监听电脑端 **8710 端口**，可通过环境变量 `OHOS_HDC_SERVER_PORT` 自定义 (1~65535)。

hdc 工具位于 `DevEco Studio/sdk/default/openharmony/toolchains` 路径下。

### 1.2 设备管理

| 功能 | hdc 命令 | adb 命令 | 说明 |
|------|---------|---------|------|
| 查看连接设备 | `hdc list targets` | `adb devices` | 列出所有已连接设备 |
| 指定设备执行 | `hdc -t <serial>` | `adb -s <serial>` | 多设备时指定目标 |
| 启动服务 | `hdc start` 或 `hdc kill -r` | `adb start-server` | 启动/重启 hdc 服务 |
| 停止服务 | `hdc kill` | `adb kill-server` | 终止 hdc 服务 |
| 查看版本 | `hdc -v` | `adb version` | 查看工具版本 |
| 查看帮助 | `hdc -h` | `adb help` | 帮助信息 |

### 1.3 文件传输

| 功能 | hdc 命令 | adb 命令 | 说明 |
|------|---------|---------|------|
| 推送文件到设备 | `hdc file send <local> <remote>` | `adb push <local> <remote>` | 电脑 -> 设备 |
| 从设备拉取文件 | `hdc file recv <remote> <local>` | `adb pull <remote> <local>` | 设备 -> 电脑 |

**注意**: hdc 使用 `file send/recv`，而非 adb 的 `push/pull`。

```bash
# 推送文件示例
hdc file send ./test.hap /data/local/tmp/test.hap

# 拉取文件示例
hdc file recv /data/local/tmp/arkui.dump ./arkui.dump
```

### 1.4 端口转发

| 功能 | hdc 命令 | adb 命令 | 说明 |
|------|---------|---------|------|
| 正向端口转发 | `hdc fport tcp:<host_port> tcp:<device_port>` | `adb forward tcp:<local> tcp:<remote>` | 主机端口 -> 设备端口 |
| 反向端口转发 | `hdc rport tcp:<device_port> tcp:<host_port>` | `adb reverse tcp:<remote> tcp:<local>` | 设备端口 -> 主机端口 |
| 删除转发 | `hdc fport rm tcp:<host_port> tcp:<device_port>` | `adb forward --remove` | 删除指定端口转发 |

```bash
# 正向转发：主机 8080 -> 设备 8080
hdc fport tcp:8080 tcp:8080

# 反向转发：设备 8080 -> 主机 8080
hdc rport tcp:8080 tcp:8080

# 删除转发
hdc fport rm tcp:8080 tcp:8080
```

### 1.5 无线连接

```bash
# 开启设备网络端口 (TCP 连接)
hdc tmode port 5555

# 通过网络连接设备
hdc tconn <device_ip>:5555

# 关闭网络连接通道，恢复 USB
hdc tmode usb
```

### 1.6 Shell 执行

```bash
# 交互式 shell
hdc shell

# 执行单条命令
hdc shell <command>

# 执行带引号的复杂命令
hdc shell "hidumper -s WindowManagerService -a '-a'"
```

### 1.7 应用安装/卸载

| 功能 | hdc 命令 | adb 命令 | 说明 |
|------|---------|---------|------|
| 安装应用 | `hdc install <hap_path>` | `adb install <apk_path>` | 安装 HAP 包 |
| 卸载应用 | `hdc uninstall <bundle_name>` | `adb uninstall <package>` | 卸载应用 |
| 指定设备安装 | `hdc -t <serial> install <hap>` | `adb -s <serial> install <apk>` | 多设备指定 |

```bash
# 安装应用
hdc install /path/to/app.hap

# 卸载应用 (注意: 使用 bundleName 而非包路径)
hdc uninstall com.example.myapp

# 拉起指定 UIAbility
hdc shell aa start -a <UIAbilityName> -b <BundleName>

# 强制停止应用
hdc shell aa force-stop <BundleName>

# 清除应用数据
hdc shell bm clean -n <BundleName> -d
```

### 1.8 日志获取

| 功能 | hdc 命令 | adb 命令 | 说明 |
|------|---------|---------|------|
| 查看日志 | `hdc hilog` | `adb logcat` | 实时查看日志 |
| 清除日志 | `hdc shell hilog -r` | `adb logcat -c` | 清除日志缓冲区 |

### 1.9 其他常用命令

```bash
# 获取设备 UDID
hdc shell bm get -u
hdc shell bm get --udid

# 获取设备信息
hdc shell param get const.product.model    # 设备型号
hdc shell param get const.product.devtype   # 设备类型
hdc shell param get const.ohos.apiversion   # API 版本

# 设置系统参数
hdc shell param set persist.ace.debug.enabled 1        # 开启 ArkUI debug
hdc shell param set persist.ace.testmode.enabled 1    # 开启 ArkUI test mode

# 设备操作
hdc shell reboot            # 重启设备
hdc shell reboot shutdown   # 关机
hdc shell power-shell wakeup  # 唤醒设备

# 截屏
hdc shell snapshot_display -f /data/local/tmp/snapshot.png
hdc file recv /data/local/tmp/snapshot.png ./
```

---

## 2. hitrace 完整用法

hitrace 是 HarmonyOS 的系统级 trace 采集工具，类似 Android 的 atrace/systrace，但能力更强。

### 2.1 所有支持的 category (tag) 列表

执行 `hdc shell hitrace -l` 查看。完整列表如下:

| Tag 名称 | 描述 | 性能分析相关性 |
|---------|------|-------------|
| **ace** | ACE development framework | **高** - ArkUI 框架核心 |
| **animation** | Animation | **高** - 动画性能 |
| **app** | APP Module | **高** - 应用层 |
| **ark** | ARK Module | **高** - ArkCompiler 运行时 |
| **graphic** | Graphic Module | **高** - 图形渲染 |
| **window** | Window Manager | **高** - 窗口管理 |
| **ability** | Ability Manager | 中 - 能力管理 |
| **ffrt** | ffrt tasks | **高** - 并发任务调度 |
| **sched** | CPU Scheduling | **高** - CPU 调度 |
| **freq** | CPU Frequency | **高** - CPU 频率 |
| **idle** | CPU Idle | 中 - CPU 空闲 |
| **load** | CPU Load | **高** - CPU 负载 |
| **binder** | Binder kernel Info | **高** - 进程间通信 |
| **zbinder** | HarmonyOS binder | **高** - 鸿蒙 binder |
| **disk** | Disk I/O | 中 - 磁盘 I/O |
| **memory** | Memory | 中 - 内存 |
| **memreclaim** | Kernel Memory Reclaim | 中 - 内存回收 |
| **membus** | Memory Bus Utilization | 中 - 内存总线 |
| **irq** | IRQ Events | 低 - 中断事件 |
| **irqoff** | IRQ-disabled code section | 低 |
| **preemptoff** | Preempt-disabled code section | 低 |
| **sync** | Synchronization | 低 - 同步 |
| **workq** | Kernel Workqueues | 低 - 内核工作队列 |
| **mmc** | eMMC commands | 低 |
| **ufs** | UFS commands | 低 |
| **pagecache** | Page cache | 低 |
| **regulators** | Voltage and Current Regulators | 低 |
| **ipa** | Thermal power allocator | 低 |
| accesscontrol | Access Control Module | 低 |
| accessibility | Accessibility Manager | 低 |
| account | Account Manager | 低 |
| bluetooth | Communication bluetooth | 低 |
| cloud | Cloud subsystem tag | 低 |
| commonlibrary | Commonlibrary subsystem | 低 |
| daudio | Distributed Audio | 低 |
| dcamera | Distributed Camera | 低 |
| deviceauth | Device Auth | 低 |
| devicemanager | Device Manager | 低 |
| dhfwk | Distributed Hardware FWK | 低 |
| dinput | Distributed Input | 低 |
| distributeddatamgr | Distributed Data Manager | 低 |
| dlpcre | Dlp Credential Service | 低 |
| drm | Digital Rights Management | 低 |
| dsched | Distributed Schedule | 低 |
| dscreen | Distributed Screen | 低 |
| dslm | Device security level | 低 |
| dsoftbus | Distributed Softbus | 低 |
| filemanagement | File management | 低 |
| gresource | Global Resource Manager | 低 |
| hdcd | hdcd | 低 |
| hdf | HDF subsystem | 低 |
| hmfs | HMFS commands | 低 |
| huks | Universal KeyStore | 低 |
| i2c | I2C Events | 低 |
| interconn | Interconnection subsystem | 低 |
| mdfs | Mobile Distributed File System | 低 |
| misc | Misc Module | 低 |
| msdp | Multimodal Sensor Data Platform | 低 |
| multimodalinput | Multimodal Input | 中 |
| musl | musl module | 低 |
| net | Net | 低 |
| notification | Notification Module | 低 |
| nweb | NWEB Module | 中 - WebView |
| ohos | HarmonyOS | 低 |
| power | Power Manager | 低 |
| push | Push subsystem | 低 |
| rpc | RPC and IPC | 低 |
| samgr | SAMGR | 低 |
| security | Security subsystem | 低 |
| sensors | Sensors Module | 低 |
| usb | USB subsystem | 低 |
| useriam | User IAM | 低 |
| virse | Virtualization Service | 低 |
| zaudio | HarmonyOS Audio Module | 低 |
| zcamera | HarmonyOS Camera Module | 低 |
| zimage | HarmonyOS Image Module | 低 |
| zmedia | HarmonyOS Media Module | 低 |

**性能分析常用 tag 组合**:
```bash
# UI 渲染分析 (帧率/绘制)
hitrace -t 10 ace graphic window app

# CPU 性能分析
hitrace -t 10 sched freq load idle

# 全面性能分析
hitrace -t 10 ace graphic window app ark sched freq binder ffrt

# 内存分析
hitrace -t 10 memory memreclaim membus

# I/O 分析
hitrace -t 10 disk mmc ufs pagecache
```

### 2.2 采集命令格式

#### 基础采集 (指定时长 + 文本格式)

```bash
# 格式
hitrace -t <秒> -b <缓冲区KB> [tags...] [-o <输出文件>]

# 采集 10 秒，缓冲区 200MB，tag 为 ace + graphic + app
hdc shell "hitrace -t 10 -b 204800 ace graphic app"

# 保存到设备文件
hdc shell "hitrace -t 10 -b 204800 ace graphic app -o /data/local/tmp/trace.ftrace"

# 拉取到本地
hdc file recv /data/local/tmp/trace.ftrace ./trace.ftrace
```

#### 二进制格式采集 (用 SmartPerf_Host 可视化)

```bash
# --raw 参数采集二进制格式，固定保存到 /data/log/hitrace/
hdc shell "hitrace -t 10 -b 204800 ace graphic app --raw"

# 输出示例:
# /data/log/hitrace/record_trace_20250604102116@590322-695861087.sys
```

#### 快照模式 (手动控制起停)

```bash
# 开始采集
hdc shell "hitrace --trace_begin -b 204800 ace graphic app"

# 导出当前数据
hdc shell "hitrace --trace_dump -o /data/local/tmp/snapshot.ftrace"

# 停止并导出
hdc shell "hitrace --trace_finish -o /data/local/tmp/final.ftrace"

# 停止不导出
hdc shell "hitrace --trace_finish_nodump"
```

#### 录制模式 (长时间采集 + 自动落盘)

```bash
# 开始录制模式
hdc shell "hitrace --trace_begin --record -b 204800 --file_size 102400 ace graphic"

# 停止录制 (自动输出文件列表)
hdc shell "hitrace --trace_finish --record"
```

#### 快照模式 (二进制 - bgsrv)

```bash
# 开启
hdc shell "hitrace --start_bgsrv"

# 导出
hdc shell "hitrace --dump_bgsrv"

# 关闭
hdc shell "hitrace --stop_bgsrv"
```

#### 压缩输出

```bash
hdc shell "hitrace -z -b 102400 -t 10 sched freq idle disk -o /data/local/tmp/test.ftrace"
```

### 2.3 命令参数汇总

| 参数 | 说明 |
|------|------|
| `-h` / `--help` | 查看帮助 |
| `-l` / `--list_categories` | 查看支持的 tag 列表 |
| `-t N` / `--time N` | 采集时长(秒)，默认 5s |
| `-b N` / `--buffer_size N` | 缓冲区大小(KB)，最小 512，默认 18432 |
| `-o file` / `--output file` | 输出文件路径(文本格式)，建议 /data/local/tmp |
| `--raw` | 二进制格式输出，固定保存到 /data/log/hitrace/ |
| `--text` | 文本格式输出(默认) |
| `-z` | 压缩捕获的 trace |
| `--trace_begin` | 开始捕获 |
| `--trace_finish` | 停止捕获并输出 |
| `--trace_finish_nodump` | 停止捕获不输出 |
| `--trace_dump` | 导出当前缓冲区数据 |
| `--record` | 录制模式(长时间采集+落盘) |
| `--overwrite` | 缓冲区满时丢弃最新数据(默认丢弃最老) |
| `--file_size N` | 录制模式下单个文件大小(KB)，默认 102400 |
| `--trace_clock <type>` | 时钟类型: boot(默认)/global/mono/uptime/perf |
| `--start_bgsrv` | 开启快照模式 |
| `--dump_bgsrv` | 导出快照数据 |
| `--stop_bgsrv` | 关闭快照模式 |
| `--trace_level <level>` | 设置级别阈值: D/I/C/M |
| `--get_level` | 查询级别阈值 |

### 2.4 输出格式 (文本)

```
# tracer: nop
#                                          _-----=> irqs-off
#                                         / _----=> need-resched
#                                        | / _---=> hardirq/softirq
#                                        || / _--=> preempt-depth
#                                        ||| /     delay
#           TASK-PID       TGID    CPU#  ||||   TIMESTAMP  FUNCTION
#              | |           |       |   ||||      |         |
KstateRecvThrea-1132    (    952) [003] .... 589942.951387: tracing_mark_write: B|952|H:CheckMsgFromNetlink|I62
KstateRecvThrea-1132    (    952) [003] .... 589942.951554: tracing_mark_write: E|952|I62
```

- `B|pid|H:name|label` = Begin (开始打点)
- `E|pid|label` = End (结束打点)
- 时间戳为 boot time (从开机算起的秒数)

### 2.5 hitrace 与 atrace 的差异

| 对比项 | hitrace (HarmonyOS) | atrace (Android) |
|--------|--------------------|--------------------|
| tag 命名 | ace, ark, graphic, window 等 | gfx, view, dalvik, hwui 等 |
| 输出格式 | 文本 (.ftrace) 和二进制 (.sys) | 文本 (.atrace) 和二进制 (.perfetto) |
| 长时间录制 | 支持 `--record` 模式 | 通过 perfetto 录制 |
| 快照模式 | `--start_bgsrv/dump_bgsrv/stop_bgsrv` | 无内置 |
| 可视化 | SmartPerf_Host | Perfetto UI / Systrace |
| 缓冲区控制 | `-b` 参数，最小 512KB | `-b` 参数 |
| 压缩 | `-z` 内置 | 需外部 gzip |

---

## 3. hidumper 性能采集能力

HiDumper 是 HarmonyOS 统一系统信息导出的命令行工具，支持 CPU、内存、存储等资源分析。

### 3.1 命令行参数总览

| 选项 | 说明 |
|------|------|
| `-h` | 帮助 |
| `-lc` | 列出系统信息簇 |
| `-ls` | 列出正在运行的系统能力 (SA) |
| `-c` | 获取全量系统信息 (设备/内核/环境变量) |
| `-c [base\|system]` | 获取指定信息簇 |
| `-s` | 获取所有系统能力详细信息 |
| `-s [SA0 SA1]` | 获取指定 SA 的信息 |
| `-s [SA] -a ["option"]` | 执行 SA 的特定选项 |
| `-e` | 获取故障日志 (CppCrash/JSCrash/AppFreeze) |
| `--net [pid]` | 获取网络信息 |
| `--storage [pid]` | 获取存储信息 |
| `-p [pid]` | 获取进程信息 |
| `--cpuusage [pid]` | 获取 CPU 使用率 |
| `--cpufreq` | 获取 CPU 各核真实频率 (kHz) |
| `--mem` | 获取整机内存 |
| `--mem [pid]` | 获取进程内存 |
| `--mem --prune` | 获取精简整机内存 |
| `--mem [pid] --show-ashmem` | 显示 ashmem 详情 |
| `--mem [pid] --show-dmabuf` | 显示 DMA 内存详情 |
| `--mem-smaps [pid] [-v]` | 获取 smaps 内存统计 (仅 debug 应用) |
| `--mem-jsheap [pid] [--gc] [--leakobj] [--raw]` | 导出 JS 堆快照 |
| `--zip` | 输出压缩到 /data/log/hidumper/ |
| `--ipc [pid]` / `--start-stat` / `--stat` / `--stop-stat` | IPC 统计 |

### 3.2 系统信息采集

```bash
# 列出所有系统信息簇
hdc shell hidumper -lc

# 列出所有系统能力 (SA)
hdc shell hidumper -ls

# 获取全量系统信息
hdc shell hidumper -c

# 获取基础系统信息
hdc shell hidumper -c base

# 获取系统信息
hdc shell hidumper -c system

# 获取进程信息
hdc shell hidumper -p <pid>

# 获取网络信息
hdc shell hidumper --net
hdc shell hidumper --net <pid>

# 获取存储信息
hdc shell hidumper --storage
hdc shell hidumper --storage <pid>

# 获取 IPC 信息
hdc shell "hidumper --ipc <pid> --start-stat"
hdc shell "hidumper --ipc <pid> --stat"
hdc shell "hidumper --ipc <pid> --stop-stat"

# 压缩导出所有信息
hdc shell "hidumper --zip --cpuusage --mem"
```

### 3.3 内存信息

```bash
# 整机内存
hdc shell hidumper --mem

# 整机内存 (精简)
hdc shell hidumper --mem --prune

# 进程内存
hdc shell hidumper --mem <pid>

# 进程内存 + ashmem 详情
hdc shell hidumper --mem <pid> --show-ashmem

# 进程内存 + DMA 详情
hdc shell hidumper --mem <pid> --show-dmabuf

# 进程 smaps 详情
hdc shell hidumper --mem-smaps <pid>
hdc shell hidumper --mem-smaps <pid> -v

# JS 堆内存快照
hdc shell hidumper --mem-jsheap <pid>

# 仅触发 GC (不导出快照)
hdc shell hidumper --mem-jsheap <pid> --gc

# 获取泄露对象列表
hdc shell hidumper --mem-jsheap <pid> --leakobj

# rawheap 格式导出
hdc shell hidumper --mem-jsheap <pid> --raw
```

**内存输出关键字段解读**:

| 字段 | 含义 |
|------|------|
| PSS Total | 实际使用物理内存 (Proportional Set Size) |
| VSS | 虚拟内存 (Virtual Set Size) |
| RSS | 驻留物理内存 (Resident Set Size) |
| USS | 独占物理内存 (Unique Set Size) |
| GL | GPU 内存 |
| Graph | 图形内存 (DMA) |
| ark ts heap | ArkUI 堆内存 |
| native heap | Native 堆内存 |
| AdjLabel | 内存回收优先级 [-1000, 1000] |

### 3.4 CPU 信息

```bash
# 整机 CPU 使用率
hdc shell hidumper --cpuusage

# 指定进程 CPU 使用率
hdc shell hidumper --cpuusage <pid>

# CPU 各核频率
hdc shell hidumper --cpufreq
```

**CPU 输出关键字段**:
- Total Usage: 总 CPU 使用率
- User Space: 用户空间使用率
- Kernel Space: 内核空间使用率

### 3.5 ArkUI 组件树 (重点)

#### 步骤 1: 开启 debug 模式

```bash
hdc shell param set persist.ace.debug.enabled 1
# 然后需要重启目标应用
```

#### 步骤 2: 获取窗口列表和 WinId

```bash
hdc shell hidumper -s WindowManagerService -a '-a'
```

输出示例:
```
WindowName             DisplayId Pid     WinId Type Mode Flag ZOrd Orientation [ x    y    w    h    ]
ScreenLockWindow       0         1274    2     2110 1    0    4    0           [ 0    0    720  1280 ]
SystemUi_StatusBar     0         1274    4     2108 102  1    2    0           [ 0    0    720  72   ]
settings0              0         10733   11    1    1    1    1    0           [ 0    72   720  1136 ]
```

**常见 WindowName 映射**:

| WindowName | 说明 |
|-----------|------|
| EntryView | 桌面 |
| RecentView | 最近任务 |
| SystemUi_NavigationBar | 三键导航 |
| SystemUi_StatusBar | 状态栏 |
| ScreenLockWindow | 锁屏 |

#### 步骤 3: 获取组件树

```bash
# 获取指定窗口的组件树
hdc shell "hidumper -s WindowManagerService -a '-w <WinId> -element'"

# 示例: WinId 为 11
hdc shell "hidumper -s WindowManagerService -a '-w 11 -element'"
```

输出示例:
```
|-> RootElement childSize:1
  | ID: 0
  | elmtId: -1
  | Active: Y
  |-> StackElement childSize:2
    |-> StageElement childSize:1
      |-> PageElement childSize:1
        |-> Column childSize:3
          |-> Text childSize:0
            ID: 5
            FrameRect: RectT (0.00, 0.00) - [720.00 x 50.00]
            BackgroundColor: #FF0000FF
```

#### 步骤 4: 获取指定 Node 的组件信息

```bash
hdc shell "hidumper -s WindowManagerService -a '-w <WinId> -element -lastpage <NodeId>'"
```

#### 步骤 5: 获取 Inspector 树 (与 DevEco Studio ArkUI Inspector 匹配)

```bash
# 先开启 testmode
hdc shell param set persist.ace.testmode.enabled 1

# 获取 Inspector 树
hdc shell "hidumper -s WindowManagerService -a '-w <WinId> -inspector'"
```

输出示例:
```
|-> Column childSize:1
| ID: 128
| compid:
| text:
| top: 72.000000
| left: 0.000000
| width: 720.000000
| height: 1136.000000
| visible: 1
| clickable: 0
| checkable: 0
```

#### 步骤 6: 获取应用路由栈信息

```bash
hdc shell "hidumper -s WindowManagerService -a '-w <WinId> -router'"
```

#### 步骤 7: 获取完整组件树 dump 文件

```bash
# 生成 dump 文件
hdc shell "hidumper -s WindowManagerService -a '-w <WinId> -element -c'"

# 查找文件路径
hdc shell find /data/ -name arkui.dump

# 拉取到本地
hdc file recv /data/app/el2/100/base/<bundleName>/haps/entry/files/arkui.dump ./
```

### 3.6 帧率信息

HiDumper 本身不直接提供帧率数据。帧率采集需要通过以下方式:

1. **hitrace**: 采集 `ace` + `graphic` tag 分析帧时间
2. **SP_daemon**: 使用 `-f` 参数采集 FPS
3. **Graphics Profiler**: DevEco Studio 内置工具

---

## 4. hilog 日志系统

### 4.1 日志格式

hilog 日志格式:
```
<时间> <级别><domainId><pid/tid>: <消息>
```

示例:
```
01-01 12:00:00.000 I A03200[1234/5678]: This is an info log
```

**格式详解**:
- `I` = Info 级别
- `A03200` = A 表示应用日志, 3200 是 domainId (十六进制)
- `[1234/5678]` = 进程号/线程号

### 4.2 日志级别

| 级别 | 字母 | 说明 |
|------|------|------|
| Debug | D | 调试信息 |
| Info | I | 一般信息 |
| Warn | W | 警告 |
| Error | E | 错误 |
| Fatal | F | 致命错误 |

### 4.3 命令行用法

```bash
# 实时查看日志
hdc shell hilog

# 清除日志缓冲区
hdc shell hilog -r

# 按级别过滤 (D=Debug, I=Info, W=Warn, E=Error, F=Fatal)
hdc shell hilog -l D          # Debug 及以上
hdc shell hilog -l E          # Error 及以上

# 按进程 PID 过滤
hdc shell hilog -p <pid>

# 组合过滤
hdc shell "hilog -p <pid> -l D"

# 按标签过滤
hdc shell "hilog | grep <tag>"

# 设置日志级别 (只输出指定级别及以上)
hdc shell hilog -b D    # Debug 及以上
hdc shell hilog -b I    # Info 及以上
hdc shell hilog -b W    # Warn 及以上
hdc shell hilog -b E    # Error 及以上
hdc shell hilog -b F    # Fatal
```

### 4.4 hilog 与 logcat 的差异

| 对比项 | hilog (HarmonyOS) | logcat (Android) |
|--------|-------------------|------------------|
| 日志域 | domainId (十六进制) | tag (字符串) |
| 级别格式 | 单字母在行首 `I A03200` | 级别字符后跟 tag `I/tag:` |
| 进程标识 | `[pid/tid]` | `pid tid` |
| 单条最大长度 | 4096 字节 | 4068 字节 |
| 隐私标识 | 支持隐私参数格式化 `{private}` | 无内置 |
| 过滤方式 | `-l` 级别, `-p` PID | `-s` tag, `*:level` |
| 缓冲区 | ring buffer | log buffer (main/system/radio/events) |
| 环境变量 | `HDC_SERVER_PORT` | `ANDROID_LOG_TAGS` |

---

## 5. SmartPerf / SP_daemon

SmartPerf Device 是 OpenHarmony 预置的性能功耗测试工具 (bin 名称: SP_daemon)，从 3.2.5.1 版本开始预制。

### 5.1 支持采集的指标

| 参数 | 说明 | 输出字段 |
|------|------|---------|
| `-c` | CPU 频率和负载 | cpuNfreq, cpuNload |
| `-g` | GPU 频率和负载 | gpufreq, gpuload |
| `-f` | FPS 和帧抖动 | fps, fps_jitters |
| `-t` | 温度 | soc-thermal, gpu-thermal |
| `-p` | 电流和电压 | current_now, voltage_now |
| `-r` | 内存 (需 -PID) | ram(pss) |
| `-snapshot` | 截图 | - |

### 5.2 命令格式

```bash
# 基本格式
hdc shell "SP_daemon -N <次数> -PKG <包名> [选项]"

# 必选参数
#   -N: 采集次数 (必选)
# 可选参数:
#   -PKG: 包名
#   -PID: 进程 PID (对 RAM 采集适用)
#   -OUT: CSV 输出路径
```

### 5.3 使用示例

```bash
# 采集 100 次全部性能指标
hdc shell "SP_daemon -N 100 -PKG com.example.app -c -g -t -p -f"

# 仅采集 CPU 和 FPS
hdc shell "SP_daemon -N 50 -c -f"

# 采集内存 (需指定 PID)
hdc shell "SP_daemon -N 20 -PID <pid> -r"

# 指定 CSV 输出路径
hdc shell "SP_daemon -N 50 -PKG com.example.app -c -f -OUT /data/local/tmp/perf.csv"

# 查看帮助
hdc shell "SP_daemon --help"
```

### 5.4 输出格式

实时打印示例:
```
----------------------------------Print START------------------------------------
order:0 cpu0freq=1992000
order:1 cpu0load=23.469387
order:2 cpu1freq=1992000
order:3 cpu1load=26.262627
order:8 current_now=-1000.000000
order:9 gpu-thermal=48333.000000
order:10 gpufreq=200000000
order:11 gpuload=0.000000
order:12 soc-thermal=48888.000000
order:13 timestamp=1501925596847
order:14 voltage_now=4123456.000000
----------------------------------Print END--------------------------------------
```

CSV 输出 (默认保存到 `/data/local/tmp/data.csv`):
```
cpu0freq,cpu0load,...,gpuload,soc-thermal,timestamp,voltage_now
1992000,23.469387,...,0.000000,48888.000000,1501925596847,4123456.000000
```

---

## 6. hiperf

hiperf 是 HarmonyOS 的性能剖析工具 (类似 Android 的 simpleperf)。

```bash
# 采集指定进程的 CPU 剖析
hdc shell "hiperf record -p <pid>"

# 采集系统级剖析
hdc shell "hiperf record -a"

# 采集指定时长
hdc shell "hiperf record -p <pid> -d 10"

# 查看统计
hdc shell "hiperf stat -p <pid>"

# 查看帮助
hdc shell "hiperf --help"
```

---

## 7. 辅助工具

### 7.1 aa 工具 (Ability Assistant)

```bash
# 启动 Ability
hdc shell aa start -a <UIAbilityName> -b <BundleName>

# 强制停止应用
hdc shell aa force-stop <BundleName>

# dump Ability 信息
hdc shell aa dump -a    # 全部
hdc shell aa dump -l    # 列表
```

### 7.2 bm 工具 (Bundle Manager)

```bash
# 查看已安装应用列表
hdc shell bm dump -a

# 查看指定应用信息
hdc shell bm dump -n <BundleName>

# 获取设备 UDID
hdc shell bm get -u
hdc shell bm get --udid

# 清除应用数据
hdc shell bm clean -n <BundleName> -d

# 卸载应用
hdc shell bm uninstall -n <BundleName>
```

### 7.3 param 工具 (系统参数)

```bash
# 获取参数
hdc shell param get <参数名>

# 设置参数
hdc shell param set <参数名> <值>

# 常用参数
hdc shell param get const.product.model         # 设备型号
hdc shell param get const.product.devtype        # 设备类型
hdc shell param get const.ohos.apiversion        # API 版本
hdc shell param get persist.ace.debug.enabled    # ArkUI debug 开关
hdc shell param set persist.ace.debug.enabled 1  # 开启 ArkUI debug
hdc shell param set persist.ace.testmode.enabled 1  # 开启 ArkUI test mode
```

### 7.4 power-shell 工具

```bash
# 唤醒设备
hdc shell power-shell wakeup

# 休眠设备
hdc shell power-shell suspend

# 查看屏幕状态
hdc shell hidumper -s 3301 -a "查询手机屏幕状态"
```

---

## 附录: 性能采集命令速查表

### 场景: 应用卡顿/掉帧分析

```bash
# 1. 采集 UI 渲染 trace (10秒)
hdc shell "hitrace -t 10 -b 204800 ace graphic window app -o /data/local/tmp/trace.ftrace"
hdc file recv /data/local/tmp/trace.ftrace ./

# 2. 采集 FPS
hdc shell "SP_daemon -N 100 -PKG <包名> -f -c"

# 3. 获取组件树
hdc shell "hidumper -s WindowManagerService -a '-a'"        # 获取 WinId
hdc shell "hidumper -s WindowManagerService -a '-w <WinId> -element -c'"  # 获取组件树
```

### 场景: 内存泄漏分析

```bash
# 1. 获取进程内存
hdc shell hidumper --mem <pid>

# 2. 获取 JS 堆快照
hdc shell hidumper --mem-jsheap <pid>

# 3. 获取泄露对象
hdc shell hidumper --mem-jsheap <pid> --leakobj

# 4. 获取内存详细映射
hdc shell hidumper --mem-smaps <pid> -v
```

### 场景: CPU 热点分析

```bash
# 1. CPU 使用率
hdc shell hidumper --cpuusage <pid>

# 2. CPU 频率
hdc shell hidumper --cpufreq

# 3. CPU trace
hdc shell "hitrace -t 10 sched freq load"

# 4. hiperf 剖析
hdc shell "hiperf record -p <pid> -d 10"
```

### 场景: 完整性能数据采集

```bash
# 一键采集 SP_daemon 全量数据
hdc shell "SP_daemon -N 200 -PKG <包名> -c -g -t -p -f -r -PID <pid>"

# 一键采集 hitrace
hdc shell "hitrace -t 10 -b 204800 ace graphic window app ark sched freq binder ffrt --raw"

# 一键导出 hidumper
hdc shell "hidumper --zip --cpuusage --mem"
hdc file recv /data/log/hidumper/ ./
```
