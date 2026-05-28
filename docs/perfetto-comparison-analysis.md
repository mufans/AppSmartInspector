# SI改造方案 — 基于Perfetto+AI方案的对比分析

> 生成日期：2026-04-22
> 参考方案：基于Perfetto与AI的Android性能自动化诊断方案（Shell脚本方案）
> 对照项目：AppSmartInspector（SI，LangGraph Agent方案）

---

## 一、两方案全景对比

| 维度 | Shell脚本方案（参考文章） | SI Agent方案（当前项目） |
|------|--------------------------|------------------------|
| **架构** | 线性Pipeline：Shell入口 → Python SQL → Shell源码注入 → 远端LLM | LangGraph StateGraph：Orchestrator → Collector → Analyzer → Attributor → Reporter |
| **采集** | `1_ai_sampler.sh` 手动构造perfetto textproto config | `PerfettoCollector.pull_trace_from_device()` Python构造config，支持ADB直连 |
| **特征提取** | `2_trace_filter.py` 纯SQL查询 | `PerfettoCollector` 11个collect方法 + `deterministic.py` 预计算层 |
| **源码关联** | `3_ai_reporter.sh` Shell脚本grep/find定位源码后直接拼入Prompt | Attributor Agent（LangChain agent + manual tool-call loop）动态 Glob→Grep→Read |
| **AI诊断** | 单次远端LLM调用，全量源码注入Prompt | 多Agent协作：PerfAnalyzer（预计算+LLM）→ Attributor（LLM+工具）→ Reporter（LLM） |
| **用户交互** | `run_profiler.sh` 入口，无交互 | REPL交互式CLI + Perfetto UI插件 + WebSocket实时通信 |
| **可扩展性** | 添加新SQL查询需改脚本 | 添加新collect方法 + 新Agent即可，模块化 |
| **依赖** | Shell + Python + Perfetto CLI | Python + LangGraph + LangChain + Perfetto SDK + WebSocket |

---

## 二、逐维度深度对比

### 2.1 Trace采集

**Shell方案：**
- 通过Shell脚本 `1_ai_sampler.sh` 构造Perfetto textproto配置
- 使用 `perfetto -c - --txt` 命令行模式，通过cat管道绕过SELinux限制
- 采集类别：sched freq idle am wm gfx view binder_driver hal dalvik memory（11个atrace类别）
- 增加了 `linux.perf`（CPU函数采样）+ Frame Timeline + `linux.process_stats`
- `target_cmdline` 限定只对目标应用展开调用栈解析
- 自动降级：config模式失败时降级到命令行模式

**SI方案：**
- `PerfettoCollector.pull_trace_from_device()` 在Python中构造完整textproto配置
- 同样包含atrace类别 + Frame Timeline + `linux.perf` + `linux.process_stats`
- 额外支持 `android.java_hprof`（Java堆内存分析）和 `android.log`（logcat采集）
- 通过ADB直接执行perfetto命令
- 自动检测前台应用包名（`adb shell dumpsys`）

**对比评估：**

| 采集能力 | Shell方案 | SI方案 | 优势方 |
|---------|-----------|--------|--------|
| 基础atrace | 11类别 | 11类别 | 持平 |
| CPU函数采样 | linux.perf | linux.perf | 持平 |
| Frame Timeline | 支持 | 支持 | 持平 |
| Java堆分析 | 未提及 | java_hprof | **SI** |
| Logcat采集 | 未提及 | android.log | **SI** |
| SELinux绕过 | cat管道方案 | 未专门处理 | **Shell** |
| 自动降级 | 有 | 无 | **Shell** |
| 包名检测 | 手动传入 | 自动检测 | **SI** |
| 进程过滤 | target_cmdline | target_cmdline | 持平 |

**SI可借鉴：**
1. **SELinux绕过策略**：参考Shell方案的cat管道方式（`cat config.pb | perfetto -c -`），增加在受限设备上的兼容性
2. **自动降级机制**：当config模式启动perfetto失败时，自动降级到命令行模式继续采集
3. **UID获取策略**：Shell方案通过 `linux.process_stats` 扫描 `/proc` 获取UID，解决冷启动阶段 `process` 表无数据时的包名匹配问题。SI目前依赖 `process` 表，可能在冷启动场景遗漏

---

### 2.2 特征提取（SQL查询）

**Shell方案：**
- `2_trace_filter.py` 核心是直接的SQL查询
- 查询维度覆盖：慢切片、线程状态、帧时间线、CPU采样、调用栈
- 关键特性：
  - 使用 `thread_state` 表区分"代码慢"还是"线程被挂起"（Running vs R/S/D）
  - `actual_frame_timeline_slice` 帧级归因，区分App/SF问题
  - `perf_sample` + `stack_profile_callsite` CPU调用栈链表（Android 15+）
  - `args` 表读取Slice附加参数（view_type, adapter_position）
  - `package_list` 冷启动场景的反查表

**SI方案：**
- `PerfettoCollector` 的11个collect方法
- `deterministic.py` 预计算层（纯Python，无LLM）：
  - 空场景检测（FPS=0, 无帧, CPU低）
  - 严重度分类（P0/P1/P2，按设备帧预算动态阈值）
  - 调用链时间分布（百分比 + 树形缩进）
  - RV热点排名（max_ms, avg_ms）
  - 卡顿帧关联（帧-Slice-输入事件三方关联）
  - CPU热点识别
- SI$自定义Tag系统：block/RV/inflate/view/handler/db/net/img/touch
- Block事件与Perfetto Slice通过timestamp bisect关联

**对比评估：**

| 特征提取能力 | Shell方案 | SI方案 | 优势方 |
|-------------|-----------|--------|--------|
| 慢切片SQL | 直接查询 | collect_view_slices | 持平 |
| 线程状态分析 | thread_state表区分R/S/D | collect_sched (end_state) | **Shell** |
| 帧时间线 | actual_frame_timeline_slice | collect_frame_timeline + expected | **SI** |
| CPU采样调用栈 | stack_profile_callsite链表 | collect_cpu_hotspots + 调用链重建 | 持平 |
| Slice附加参数 | args表 (view_type, adapter_position) | collect_view_slices (已解析) | 持平 |
| 冷启动场景 | package_list反查 | 未专门处理 | **Shell** |
| 确定性预计算 | 无 | deterministic.py 六大模块 | **SI** |
| 动态阈值 | 固定阈值 | 按设备帧预算自动调整 | **SI** |
| 帧级归因 | jank_type区分App/SF | USER_JANK_TYPES分类 | 持平 |
| 自定义Tag | 依赖atrace原生tag | SI$自定义Tag体系（覆盖8类场景） | **SI** |

**SI可借鉴：**
1. **thread_state深度分析**：Shell方案利用 `thread_state` 表区分线程在Running（代码慢）和 S/D（被挂起/IO等待）状态的时间分布。SI的 `collect_sched` 只有end_state，缺少per-slice级别的线程状态分析。建议增加 `thread_state` 查询，将每个慢Slice关联其执行期间的线程状态分布，帮助区分"代码需要优化"和"线程被系统挂起"两种不同根因
2. **package_list冷启动支持**：在冷启动场景中 `process` 表可能为空，Shell方案用 `package_list` 反查。SI应在 `pull_trace_from_device` 的config中确保 `linux.process_stats` 开启，并在包名匹配失败时 fallback 到 `package_list` 查询
3. **CPU采样调用栈深度重建**：Shell方案通过 `stack_profile_callsite.parent_id` 递归重建完整调用链。SI的 `collect_cpu_hotspots` 已实现类似逻辑（callsite_map + 递归回溯），但可增加对 `stack_profile_mapping` 的查询，关联so/apk的映射信息，使native方法调用栈更可读

---

### 2.3 源码关联

**Shell方案：**
- `3_ai_reporter.sh` 纯Shell脚本实现
- 流程：从trace_filter输出中提取类名 → 用find/grep在源码中定位文件 → 用sed/awk提取方法体 → 直接拼入AI Prompt
- 源码搜索范围：当前class文件 + import依赖 + 关联XML布局
- 特点：
  - **静态绑定**：在Prompt构造阶段一次性完成所有源码搜索
  - **确定性**：不依赖LLM做搜索决策，纯Shell脚本逻辑
  - **广度**：搜索依赖引用和关联XML布局，提供更完整的上下文

**SI方案：**
- `Attributor Agent` — LangChain agent with manual tool-call loop
- 流程：提取SI$切片 → 分类（java/xml/system）→ 分组 → 对每组执行 Glob→Grep→Read 三步搜索
- 特点：
  - **动态搜索**：LLM决定搜索策略，可以处理模糊/不规则的类名
  - **工具调用循环**：最多8轮迭代，LLM自主决定何时搜索完成
  - **Call Stack上下文**：利用parent chain和BlockMonitor堆栈辅助定位
  - **内部类处理**：匿名内部类($1/$2) → 搜索外部类 → 堆栈提取真实方法名
  - **LRU缓存**：避免重复读取文件
  - **Early termination**：连续3次搜索失败则终止
  - **结构化输出**：尝试with_structured_output，失败时fallback到RESULT行解析

**对比评估：**

| 源码关联能力 | Shell方案 | SI方案 | 优势方 |
|-------------|-----------|--------|--------|
| 搜索策略 | 静态find/grep/sed | 动态LLM Glob→Grep→Read | **SI** |
| 搜索准确性 | 依赖类名匹配规则 | LLM理解模糊/不规则名称 | **SI** |
| 搜索范围 | 当前类+import依赖+XML | 当前类+内部类+堆栈提示 | **Shell**（依赖链更广） |
| 搜索效率 | 一次性Shell脚本，快速 | 多轮LLM调用，较慢 | **Shell** |
| 匿名内部类 | 未专门处理 | 完整处理（外部类+堆栈方法名） | **SI** |
| 错误容忍 | Shell脚本出错即中断 | Early termination + 缓存 + 重试 | **SI** |
| Token消耗 | 无（不使用LLM搜索） | 高（每轮LLM调用消耗token） | **Shell** |
| 上下文完整性 | 包含import依赖和关联XML | 仅搜索目标类和方法体 | **Shell** |
| BlockMonitor堆栈 | 未提及 | 堆栈关联+bisect时间匹配 | **SI** |
| 调用链上下文 | 无 | parent chain回溯+上下文摘要 | **SI** |

**SI可借鉴：**
1. **依赖引用搜索**：当前SI只搜索目标类本身的方法体。Shell方案会额外搜索该类的import依赖和关联XML布局，为AI诊断提供更完整的上下文。建议在Attributor Agent中增加一步"关联搜索"：Read目标文件后，提取import列表中的项目内类和关联的XML布局ID，一并读取
2. **搜索效率优化**：Shell方案一次性完成所有源码搜索，而SI需要多轮LLM调用。对于类名和包名完整的简单场景，可以引入"快速路径"：跳过LLM搜索决策，直接用确定性代码执行Glob→Grep→Read。只在模糊/匿名内部类场景才走LLM搜索路径
3. **静态源码绑定模式**：提供一种"预处理模式"选项，在调用LLM之前先用确定性代码完成源码搜索，将结果直接注入Prompt，类似Shell方案的做法。好处是减少token消耗和LLM调用轮次

---

### 2.4 AI诊断

**Shell方案：**
- 单次LLM调用
- 将SQL查询结果+源码片段一起注入Prompt
- LLM直接输出Markdown诊断报告
- 优点：简单直接，一次调用完成
- 缺点：LLM需要同时处理数据分析和报告生成两个任务，可能导致质量下降

**SI方案：**
- 三阶段Agent协作：
  1. **PerfAnalyzer**：接收deterministic预计算结论 + 原始数据 → 组织性能分析报告
  2. **Attributor**：根据SI$切片搜索源码 → 返回归因结果（文件路径+方法体+行号）
  3. **Reporter**：综合性能分析 + 源码归因 + 待归因热点 → 生成最终报告
- **混合架构**：确定性计算（deterministic.py）+ LLM语言生成
- **Token控制**：消息窗口裁剪、LRU文件缓存、结构化输出探测、Early termination
- **多Provider支持**：结构化输出失败时自动fallback到文本解析

**对比评估：**

| AI诊断能力 | Shell方案 | SI方案 | 优势方 |
|-----------|-----------|--------|--------|
| LLM调用次数 | 1次 | 3-5次（per_analyzer + attributor迭代 + reporter） | **Shell**（简单） |
| 分析精度 | 依赖LLM做算术 | 确定性预计算+LLM语言组织 | **SI** |
| 源码上下文 | 预注入完整源码 | 动态搜索+精准方法体 | **SI**（灵活） |
| 报告质量 | 单次输出 | 分层生成+格式化 | **SI** |
| Token消耗 | 低 | 高（多轮对话） | **Shell** |
| 错误恢复 | 无 | 重试+fallback+graceful degrade | **SI** |
| 可扩展性 | 硬编码Prompt | 模块化Agent+独立Prompt文件 | **SI** |
| 结果一致性 | 完全依赖LLM | 确定性部分保证一致 | **SI** |

**SI可借鉴：**
1. **轻量模式**：参考Shell方案的单次调用模式，为SI增加一个"快速诊断"模式。跳过Attributor阶段，直接将perf_summary中的类名/方法名/耗时信息交给Reporter LLM，由LLM基于经验推测优化建议。适用场景：不需要精确源码定位的快速初筛
2. **Token消耗优化**：Shell方案的Prompt是静态构造的，token消耗可控。SI的Attributor Agent每轮迭代都会增加消息历史。当前的消息窗口裁剪（keep last 12 messages）和max 8 iterations是好的防护措施，但可进一步优化：对于确定性高的场景（FQN完整、方法名明确），直接执行工具调用不经过LLM决策

---

### 2.5 系统架构

**Shell方案：**
```
run_profiler.sh
  ├── 1_ai_sampler.sh      # Trace采集
  ├── 2_trace_filter.py    # SQL特征提取
  └── 3_ai_reporter.sh     # 源码注入 + AI诊断
```
- 线性Pipeline，阶段间通过文件传递数据
- 每个阶段独立可执行
- 依赖少：只需Shell + Python + curl/LLM API

**SI方案：**
```
LangGraph StateGraph
  orchestrator → collector → analyzer → attributor → reporter
                 android_expert    perf_analyzer
                                  explorer
```
- 状态机架构，通过AgentState传递数据
- 条件路由：根据用户意图分发到不同处理路径
- REPL交互式CLI + Perfetto UI WebSocket集成
- 依赖多：LangGraph + LangChain + WebSocket + Perfetto SDK

**对比评估：**

| 架构特性 | Shell方案 | SI方案 | 优势方 |
|---------|-----------|--------|--------|
| 复杂度 | 低（3个脚本） | 高（多Agent系统） | **Shell**（简单） |
| 可维护性 | Shell脚本维护困难 | Python模块化，可维护 | **SI** |
| 可扩展性 | 修改脚本，耦合高 | 添加Agent/Node即可 | **SI** |
| 部署难度 | 低（仅需Shell环境） | 高（Python环境+依赖） | **Shell** |
| 交互性 | 无交互 | REPL + Perfetto UI | **SI** |
| 状态管理 | 文件传递 | AgentState TypedDict | **SI** |
| 错误处理 | 脚本失败即中断 | node_error_handler + 全局try/except | **SI** |
| 并发安全 | 无 | thread-safe LLM singleton + Lock | **SI** |
| 实时反馈 | 无 | WebSocket进度推送 | **SI** |

---

### 2.6 用户体验

**Shell方案：**
- 命令行执行 `run_profiler.sh <包名>`
- 无交互，全自动化
- 输出：Markdown诊断报告文件
- 适用场景：CI/CD集成、批量测试

**SI方案：**
- 交互式REPL（`smartinspector`命令）
- 21个Slash命令（/full、/trace、/frame、/open等）
- Perfetto UI集成：浏览器中选择时间范围 → Agent实时分析
- WebSocket实时进度推送
- 多种交互模式：自然语言、命令、UI选择
- 适用场景：开发阶段调试、性能问题深入分析

**对比评估：**

| 用户体验 | Shell方案 | SI方案 | 优势方 |
|---------|-----------|--------|--------|
| 上手难度 | 低（一条命令） | 中（需学习命令和交互） | **Shell** |
| 灵活性 | 低（固定Pipeline） | 高（多种入口和交互方式） | **SI** |
| CI/CD集成 | 天然适合 | 需封装 | **Shell** |
| 深度分析 | 浅（一次性报告） | 深（交互式追问、时间范围选择） | **SI** |
| 可视化 | 无 | Perfetto UI集成 | **SI** |
| 实时反馈 | 无 | 进度条 + 工具调用展示 | **SI** |

**SI可借鉴：**
1. **Headless/CI模式**：参考Shell方案的"一条命令全流程"设计，为SI增加非交互模式。例如 `smartinspector --headless --package com.example.app --duration 10` 直接完成采集→分析→报告，适合CI/CD集成
2. **输出标准化**：Shell方案的输出是一个完整的Markdown文件，可直接作为测试报告。SI的报告已很完善（含header tables + 问题列表），可增加机器可读的JSON输出选项，方便CI系统解析

---

## 三、SI可借鉴的改进点汇总

按优先级排序（P0=高优先级，P1=中优先级，P2=低优先级）：

### P0：核心能力提升

#### 1. thread_state深度分析 — 区分"代码慢"vs"被挂起"
- **现状**：SI的 `collect_sched` 只查询调度统计和blocked_reason，没有per-slice级别的线程状态分析
- **改进**：新增 `collect_thread_state` 方法，查询 `thread_state` 表，对每个SI$慢Slice关联其执行期间的状态分布（Running/S/D）
- **价值**：帮助开发者区分"代码需要优化"和"线程被IO/锁阻塞"两种根本不同的根因
- **参考SQL**：
  ```sql
  SELECT ts, dur, state
  FROM thread_state
  WHERE utid = (SELECT utid FROM thread WHERE name = 'main')
    AND ts >= {slice_ts} AND ts + dur <= {slice_ts} + {slice_dur}
  ```

#### 2. 源码搜索"快速路径" — 确定性搜索减少LLM调用
- **现状**：所有源码搜索都走Attributor Agent的LLM工具调用循环
- **改进**：对于FQN完整、方法名明确的场景（非匿名内部类、非模糊匹配），直接用Python代码执行Glob→Grep→Read，不经过LLM决策
- **价值**：减少50%+的token消耗和响应时间
- **实现**：在 `_search_group()` 前增加确定性搜索判断
  ```python
  if all(issue.get("search_type") == "java" and "$" not in issue["class_name"] for issue in group):
      results = _deterministic_search(group, file_cache)
      if all(r["reason"] == "found" for r in results):
          return results  # 跳过LLM
  ```

#### 3. Headless/CI模式 — 一条命令完成全流程
- **现状**：SI只支持交互式REPL
- **改进**：增加CLI参数 `--headless`，跳过交互直接执行 full pipeline
- **价值**：适合CI/CD集成和批量测试

### P1：体验和完整性提升

#### 4. 依赖引用搜索 — 扩展源码上下文
- **现状**：Attributor只搜索目标类的方法体
- **改进**：Read目标文件后，提取import列表中的项目内类和XML布局ID，一并搜索
- **价值**：为AI诊断提供更完整的上下文，提升诊断准确度
- **实现**：在Attributor Agent的Read后增加一步"关联分析"

#### 5. package_list冷启动支持
- **现状**：SI依赖 `process` 表获取包名，冷启动场景可能匹配失败
- **改进**：在包名匹配失败时，fallback到 `package_list` 表反查
- **价值**：支持冷启动性能分析场景
- **实现**：修改 `pull_trace_from_device` 确保config包含 `linux.process_stats`，在 `collect_view_slices` 中增加 `package_list` fallback

#### 6. SELinux兼容性 — cat管道绕过
- **现状**：SI直接执行 `perfetto -c -` 命令
- **改进**：增加cat管道方案作为fallback（`cat config.pb | perfetto -c -`）
- **价值**：在受限设备上正常采集

#### 7. Perfetto采集自动降级
- **现状**：config模式失败时报错
- **改进**：自动降级到命令行模式（类似Shell方案）
- **价值**：提高采集成功率

### P2：长期演进

#### 8. stack_profile_mapping关联 — Native调用栈可读性
- **现状**：`collect_cpu_hotspots` 只重建函数调用链，不关联so/apk映射
- **改进**：增加 `stack_profile_mapping` 查询，关联native库信息
- **价值**：提升native方法调用栈的可读性

#### 9. 轻量快速诊断模式
- **现状**：每次诊断都走完整Attributor流程
- **改进**：增加"快速模式"跳过Attributor，由LLM基于经验推测
- **价值**：快速初筛，减少等待时间

#### 10. JSON格式机器可读输出
- **现状**：只输出Markdown报告
- **改进**：增加JSON输出选项，包含结构化的问题列表和源码定位结果
- **价值**：CI系统可解析，支持自动化处理

---

## 四、SI架构优势总结

SI相比Shell脚本方案的核心优势：

1. **混合确定性+LLM架构**：算术和阈值分类由deterministic.py保证准确性，LLM只负责语言组织和因果分析。这是最关键的架构优势
2. **SI$自定义Tag体系**：覆盖block/RV/inflate/view/handler/db/net/img/touch八大场景，远比依赖atrace原生tag精准
3. **动态源码搜索**：Attributor Agent的LLM工具调用循环可以处理匿名内部类、模糊类名等复杂场景，比Shell的find/grep灵活得多
4. **交互式分析**：REPL + Perfetto UI集成，支持深度追问和时间范围选择
5. **模块化可扩展**：LangGraph状态机 + 独立Agent + 独立Prompt文件，添加新功能只需添加新Node
6. **BlockMonitor集成**：主线程卡顿检测 + 堆栈关联 + bisect时间匹配，填补Hook覆盖的盲区

SI当前的不足：
1. **Token消耗高**：多轮LLM调用，Attributor尤其消耗token
2. **响应时间长**：LLM搜索+多Agent串行执行
3. **CI集成困难**：缺少非交互模式
4. **线程状态分析浅**：缺少per-slice级别的Running/S/D区分

---

## 五、实施建议

### 阶段一：核心能力补强
1. 新增 `collect_thread_state` 方法（P0-1）
2. 实现源码搜索快速路径（P0-2）
3. 新增Headless CLI模式（P0-3）

### 阶段二：鲁棒性提升
4. package_list冷启动fallback（P1-5）
5. SELinux兼容性改进（P1-6）
6. Perfetto采集自动降级（P1-7）
7. 依赖引用搜索扩展（P1-4）

### 阶段三：长期演进
8. Native调用栈可读性（P2-8）
9. 快速诊断模式（P2-9）
10. JSON机器可读输出（P2-10）

每个改进点都可以独立实现和测试，不影响现有功能。建议按阶段推进，每个阶段完成后进行回归测试。
