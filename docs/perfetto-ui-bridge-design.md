# SI Agent x Perfetto UI 联动分析：可行性评估与设计方案

> 日期：2026-04-15
> 状态：可行性调研完成，待实施

## 一、需求概述

用户在 Perfetto UI 中打开 trace 文件，浏览并选中耗时帧后，SI Agent 自动获取选中帧的详细数据，调用 LLM 进行深度分析（包括调用链归因、源码定位），并将分析结果实时展示给用户。

**核心价值**：将"全量自动分析"升级为"用户驱动的交互式分析"，用户可聚焦关心的帧，获得更精准的分析结果。

## 二、现有架构概览

### 2.1 核心数据流

```
用户输入 -> orchestrator(路由) -> collector(采集trace) -> analyzer(LLM分析)
         -> attributor(源码归因) -> reporter(生成报告)
```

- **collector** 调用 `PerfettoCollector.pull_trace_from_device()` 通过 `adb shell perfetto` 采集 `.pb` trace 文件
- 使用 `trace_processor_shell` (Python API `perfetto.TraceProcessor`) 执行 SQL 查询
- 产出 `PerfSummary` JSON，包含 `frame_timeline`、`view_slices`、`block_events` 等

### 2.2 WebSocket 现状

`ws/server.py` 中 `SIServer` 是单例 WebSocket 服务器，与 Android App 通信：

| 方向       | 消息类型                             | 用途             |
| ---------- | ------------------------------------ | ---------------- |
| App->Server | `config_sync`, `block_events`, `ack` | 配置同步、事件上报 |
| Server->App | `config_update`, `start_trace`, `get_block_events` | 推送配置、触发采集 |

**协议特点**：JSON 消息、UUID msg_id、ACK 确认、threading.Event 阻塞等待。扩展新消息类型只需在 `_dispatch()` 中添加 `elif` 分支。

### 2.3 帧数据能力

- `collect_frame_timeline()`：查询 `actual/expected_frame_timeline_slice`，检测 jank、计算 FPS、返回 top10 最慢帧
- `collect_view_slices()`：查询 SI$ 自定义 slice，重建调用链（parent->grandparent），输出 slowest_slices / call_chains / rv_instances
- `compute_hints()`：确定性预计算（P0/P1/P2 严重度、jank 帧关联、CPU 热点）
- `extract_attributable_slices()`：从 view_slices 中过滤出可归因的 SI$ slice（排除系统类）

## 三、Perfetto UI 扩展能力调研

### 3.1 Plugin 系统（最强大）

Perfetto UI 拥有完整的 Plugin API：

| 扩展点           | API                                      | 描述                                 |
| ---------------- | ---------------------------------------- | ------------------------------------ |
| 自定义 Track     | `trace.tracks.registerTrack()`           | 注册 slice/counter/自定义 canvas track |
| 时间线 Overlay   | `trace.tracks.registerOverlay()`         | 在时间线上绘制箭头、标注、竖线       |
| Tab 面板         | `trace.registerTab()`                    | 在详情面板添加标签页                 |
| 命令             | `app.commands.registerCommand()`         | 注册命令面板操作+快捷键              |
| 侧边栏           | `trace.sidebar.addMenuItem()`            | 添加侧边栏菜单项                     |
| 区域选择 Tab     | `trace.selection.registerAreaSelectionTab()` | 选择时间范围时显示自定义面板      |
| 时间线标注       | `trace.notes.addSpanNote()`              | 添加高亮时间范围                     |
| 编程式选择       | `trace.selection.selectTrackEvent()`     | 程序化选中某个 slice                 |

**关键限制**：所有 Plugin 必须是 in-tree（提交到 google/perfetto 仓库），或自行 fork 托管。不支持动态加载外部插件。

### 3.2 URL Deep Linking

```
https://ui.perfetto.dev/#!/?url=<trace_url>&visStart=<ns>&visEnd=<ns>&ts=<ns>&dur=<ns>
```

- `url`：自动打开远程 trace 文件（需 HTTPS + CORS）
- `visStart/visEnd`：初始视口范围（纳秒）
- `ts/dur`：定位并选中特定 slice
- `startupCommands`：JSON 数组，自动执行命令（PinTracks、AddDebugTrack、RunQuery 等）
- `embed`：嵌入模式，隐藏侧边栏

### 3.3 iframe + postMessage

```javascript
// 父窗口向 iframe 中的 Perfetto UI 发送 trace
iframe.contentWindow.postMessage({
  perfetto: { buffer: arrayBuffer, title: 'My Trace' }
}, 'https://ui.perfetto.dev');

// 控制视口
iframe.contentWindow.postMessage({
  perfetto: { timeStart: 123.456, timeEnd: 123.789 }
}, 'https://ui.perfetto.dev');
```

**限制**：postMessage 只接受来自 `window.opener`、`window.parent` 或 opener 关系窗口的消息，background script 发送的消息会被忽略。

### 3.4 trace_processor_shell HTTP 模式

```bash
trace_processor_shell server http trace_file.pb --port 9001
```

| 端点        | 用途                                              |
| ----------- | ------------------------------------------------- |
| `/websocket` | Protobuf-over-WebSocket，Perfetto UI 的主要通信通道 |
| `/rpc`      | HTTP POST + Protobuf，Python API 使用               |
| `/query`    | 执行 SQL 查询                                     |
| `/status`   | Trace processor 状态                              |

Perfetto UI 自动检测 localhost:9001 并启用 "Trace Processor native acceleration"。

### 3.5 Chrome Extension

**可行性低**。content script 运行在隔离世界，无法访问 Perfetto UI 内部 JS API。只能通过 iframe+postMessage 间接交互，或操作 DOM。

## 四、技术方案设计

### 4.1 方案对比

| 方案                              | 优点                                       | 缺点                              | 推荐度 |
| --------------------------------- | ------------------------------------------ | --------------------------------- | ------ |
| A: Fork Perfetto + 自定义 Plugin   | 最强控制力，可注册自定义 Tab/命令/Overlay   | 需维护 fork，构建复杂             | 3/5    |
| B: 本地 HTTP Server + iframe 嵌入 | 自托管 UI，绕过跨域限制，可注入 JS         | 需要构建和部署 UI                 | 3/5    |
| C: trace_processor HTTP + Chrome Extension | 不修改 Perfetto，独立扩展          | Extension 能力受限，交互不自然    | 2/5    |
| **D: 本地 Web Server 桥接（推荐）** | **最小侵入，利用现有 WS 架构，独立前端页面** | **需开发桥接前端**                | **5/5**|

### 4.2 推荐方案：本地 Web Server 桥接

**核心思路**：不修改 Perfetto UI，在 SI Agent 侧构建一个轻量 Web Server 作为桥接层。

```
+-----------------------------------------------------------+
|  浏览器                                                    |
|  +------------------------+  +--------------------------+  |
|  |  桥接页面 (localhost)   |  |  Perfetto UI (iframe)     |  |
|  |  - JS 拦截用户选中      |  |  - 打开 trace              |  |
|  |  - WS 发送到 Agent      |  |  - postMessage 控制        |  |
|  |  - 显示分析结果         |  |                            |  |
|  +----------+-------------+  +--------------------------+  |
|             | WS (localhost:9877)                            |
+-------------+----------------------------------------------+
              |
+-------------+----------------------------------------------+
|  SI Agent (Python)                                         |
|  +----------+-------------+  +--------------------------+   |
|  |  Web Bridge Server      |  |  trace_processor_shell    |   |
|  |  (aiohttp/websockets)   |  |  HTTP mode :9001          |   |
|  |  - 接收帧选中事件       |  |  - SQL 查询                |   |
|  |  - 调用 Agent 分析      |  |  - 与 Perfetto UI 直连     |   |
|  +----------+-------------+  +--------------------------+   |
|             |                                               |
|  +----------+-------------------------------------------+   |
|  |  LangGraph Pipeline (现有架构)                        |    |
|  |  collector -> analyzer -> attributor -> reporter       |    |
|  +------------------------------------------------------+   |
+-----------------------------------------------------------+
```

### 4.3 关键技术细节

#### 4.3.1 trace_processor_shell HTTP 模式启动

```python
# collector/perfetto.py 扩展
import subprocess

class TraceServer:
    """管理 trace_processor_shell HTTP 服务"""

    def __init__(self, trace_path: str, port: int = 9001):
        self.trace_path = trace_path
        self.port = port
        self.process: subprocess.Popen | None = None

    def start(self):
        self.process = subprocess.Popen(
            [SHELL_BIN, "server", "http", self.trace_path,
             "--port", str(self.port), "--ip-address", "127.0.0.1"],
            stdout=subprocess.PIPE, stderr=subprocess.PIPE
        )
        # 等待就绪
        import urllib.request
        for _ in range(50):
            try:
                urllib.request.urlopen(f"http://127.0.0.1:{self.port}/status")
                return True
            except Exception:
                time.sleep(0.1)
        return False

    def query(self, sql: str) -> list[dict]:
        """通过 HTTP RPC 执行 SQL"""
        from perfetto.trace_processor import TraceProcessor
        tp = TraceProcessor(addr=f'localhost:{self.port}')
        result = tp.query(sql)
        return result.as_dict()['rows']

    def stop(self):
        if self.process:
            self.process.terminate()
```

#### 4.3.2 桥接 Web Server

```python
# ws/bridge_server.py (新增)
import asyncio
import json
from aiohttp import web

class BridgeServer:
    """桥接浏览器与 SI Agent 的 Web Server"""

    def __init__(self, port: int = 9877, agent_callback=None):
        self.port = port
        self.agent_callback = agent_callback  # 回调：帧选中 -> Agent 分析
        self.ws_clients: set[web.WebSocketResponse] = set()

    async def handle_websocket(self, request):
        ws = web.WebSocketResponse()
        await ws.prepare(request)
        self.ws_clients.add(ws)
        try:
            async for msg in ws:
                data = json.loads(msg.data)
                if data['type'] == 'frame_selected':
                    # 用户在 Perfetto UI 中选中了一个帧/slice
                    result = await self.agent_callback(data['payload'])
                    await ws.send_json({
                        'type': 'analysis_result',
                        'payload': result
                    })
        finally:
            self.ws_clients.remove(ws)

    async def handle_index(self, request):
        """返回桥接页面 HTML"""
        return web.Response(text=BRIDGE_HTML, content_type='text/html')

    def start(self):
        app = web.Application()
        app.router.add_get('/', self.handle_index)
        app.router.add_get('/ws', self.handle_websocket)
        web.run_app(app, port=self.port)
```

#### 4.3.3 桥接前端页面核心逻辑

```javascript
// 桥接页面 (嵌入 Perfetto UI iframe + 拦截选中事件)
const PERFETTO_URL = 'https://ui.perfetto.dev';

// 1. 通过 URL 打开 trace（trace_processor 在 localhost:9001）
const iframe = document.getElementById('perfetto');
iframe.src = `${PERFETTO_URL}/#!/?url=http://127.0.0.1:9001`;

// 2. 监听 iframe 消息 + 定时查询选中状态
// Perfetto UI 没有直接暴露"选中变化"事件，
// 需要通过以下策略之一获取选中信息：

// 策略A：用户手动触发（推荐 MVP）
// 用户选中 slice 后点击"分析"按钮，页面读取 URL hash 中的 ts/dur
document.getElementById('analyzeBtn').addEventListener('click', () => {
    const hash = iframe.contentWindow.location.hash;
    const params = new URLSearchParams(hash.split('?')[1] || '');
    const ts = params.get('ts');
    const dur = params.get('dur');
    if (ts && dur) {
        ws.send(JSON.stringify({ type: 'frame_selected', payload: { ts, dur } }));
    }
});

// 策略B：MutationObserver 监听 DOM 变化（高级）
// Perfetto UI 选中 slice 后会更新详情面板 DOM
const observer = new MutationObserver(() => {
    const details = iframe.contentDocument.querySelector(
        'details-panel .slice-details'
    );
    if (details) {
        const ts = details.dataset.ts;
        const dur = details.dataset.dur;
        // 发送到 Agent...
    }
});

// 策略C：轮询 URL hash 变化（最简单可靠）
let lastHash = '';
setInterval(() => {
    const hash = iframe.contentWindow.location.hash;
    if (hash !== lastHash) {
        lastHash = hash;
        const params = new URLSearchParams(hash.split('?')[1] || '');
        if (params.get('ts')) {
            // 用户导航到了新的位置，可以自动触发分析
        }
    }
}, 500);
```

#### 4.3.4 Agent 分析回调

```python
# agents/frame_analyzer.py (新增)
async def analyze_selected_frame(payload: dict, state: AgentState) -> dict:
    """分析用户在 Perfetto UI 中选中的帧"""
    ts_ns = int(payload['ts'])
    dur_ns = int(payload['dur'])

    # 1. 从 trace_processor 查询该帧的详细数据
    tp = TraceProcessor(addr='localhost:9001')

    # 查询该时间点的所有 slice
    slices = tp.query(f"""
        SELECT id, name, ts, dur, depth, track_id, cat
        FROM slice
        WHERE ts <= {ts_ns + dur_ns} AND ts + dur >= {ts_ns}
        ORDER BY dur DESC
        LIMIT 50
    """).as_dict()['rows']

    # 查询关联的 frame timeline
    frames = tp.query(f"""
        SELECT * FROM actual_frame_timeline_slice
        WHERE ts <= {ts_ns + dur_ns} AND ts + dur >= {ts_ns}
    """).as_dict()['rows']

    # 查询调用链
    call_chain = _build_call_chain(tp, slices[0]['id']) if slices else []

    # 2. 构建分析上下文
    frame_context = {
        'selected_slice': slices[0] if slices else None,
        'overlapping_slices': slices[1:20],
        'frame_timeline': frames,
        'call_chain': call_chain,
        'existing_perf_summary': state.get('perf_summary'),
    }

    # 3. 调用 LLM 分析（复用现有 analyzer prompt）
    analysis = await _llm_analyze_frame(frame_context)

    return {
        'type': 'frame_analysis',
        'slice': slices[0] if slices else None,
        'analysis': analysis,
        'suggestions': _generate_suggestions(frame_context),
    }
```

### 4.4 用户交互流程

```
1. SI Agent 采集 trace -> 启动 trace_processor_shell HTTP 模式
2. 自动打开浏览器 -> 桥接页面加载 -> iframe 嵌入 Perfetto UI
3. Perfetto UI 自动连接 localhost:9001（native acceleration）
4. 用户在 Perfetto UI 中浏览 trace，选中耗时帧
5. 用户点击"分析此帧"按钮（或自动检测选中变化）
6. 桥接页面通过 WebSocket 将 {ts, dur, track_id} 发送给 Agent
7. Agent 通过 trace_processor HTTP API 查询该帧详细数据
8. Agent 调用 LLM 分析（可复用现有 analyzer/attributor 能力）
9. 分析结果通过 WebSocket 回传给桥接页面展示
```

## 五、与现有 Graph Pipeline 的集成可行性

### 5.1 集成方式评估

| 集成点                      | 可行性 | 说明                                                      |
| --------------------------- | ------ | --------------------------------------------------------- |
| 复用 PerfSummary            | 5/5    | 帧分析可直接读取 `state['perf_summary']` 作为上下文       |
| 复用 analyzer prompt        | 4/5    | 现有 `prompts/perf_analysis.txt` 可适配帧级分析           |
| 复用 attributor             | 4/5    | `extract_attributable_slices()` + `run_attribution()` 可直接对选中帧归因 |
| 复用 deterministic.py       | 5/5    | `compute_hints()` 的子函数（severity、call chain）可直接复用 |
| 新增 graph 节点             | 5/5    | LangGraph 架构支持新增 `frame_analyzer` 节点              |
| 复用 trace_processor        | 5/5    | HTTP 模式与 Python API 查询结果格式一致                   |
| 复用 WS 架构                | 4/5    | `SIServer` 可扩展，或独立 `BridgeServer` 并行运行         |

### 5.2 推荐的 Graph 集成方案

**不修改现有 pipeline，新增独立交互路径：**

```python
# state.py 扩展
class AgentState(TypedDict):
    # ... 现有字段 ...
    selected_frame: dict | None        # 新增：用户选中的帧信息 {ts, dur, track_id}
    frame_analysis: str                # 新增：帧分析结果

# builder.py 扩展
def create_graph():
    # ... 现有节点 ...
    builder.add_node("frame_analyzer", frame_analyzer_node)
    # orchestrator 新增路由
    # 或者：frame_analyzer 作为独立入口，不经过 orchestrator
```

**两种集成模式：**

1. **对话式**：用户在 REPL 中说"分析我选中的帧"，orchestrator 路由到 `frame_analyzer` 节点
2. **实时式**：用户在 Perfetto UI 中点击，通过 WS 直接触发分析，结果推送到前端页面（不经过 LangGraph）

**推荐 MVP 用模式 2（实时式）**，因为交互更自然。后续可扩展为模式 1 实现对话式帧分析。

### 5.3 数据复用度分析

```
现有 Pipeline 数据 -> 帧分析可复用：
+-- perf_summary (全量)     -> 作为帧分析的上下文背景
+-- view_slices.slowest     -> 快速匹配选中帧
+-- frame_timeline.jank     -> 判断选中帧是否为 jank 帧
+-- compute_hints()         -> 复用 severity/classify 逻辑
+-- extract_attributable_slices() -> 复用归因逻辑
+-- attributor agent        -> 复用源码搜索能力
+-- prompts/*.txt           -> 复用/扩展分析 prompt
```

## 六、关键技术风险与应对

| 风险                              | 级别 | 应对方案                                                              |
| --------------------------------- | ---- | --------------------------------------------------------------------- |
| Perfetto UI iframe 跨域限制       | 中   | 使用 `trace_processor_shell HTTP` 模式，Perfetto UI 自动连接本地服务    |
| 无法直接获取 Perfetto UI 选中事件 | 高   | URL hash 轮询（MVP）-> MutationObserver（进阶）-> Fork + Plugin API（终极方案） |
| trace_processor_shell HTTP 稳定性 | 低   | 官方支持的功能，Python API 底层就是用 HTTP 协议                      |
| 大 trace 文件加载性能             | 中   | HTTP 模式下 Perfetto UI 使用 native acceleration（WASM -> native）    |
| 桥接页面 UI 复杂度                | 中   | MVP 只做"选中+分析按钮+结果展示"，后续迭代优化                       |

## 七、实现步骤（MVP）

### Phase 1：基础设施（3 个文件改动 + 2 个新文件）

1. **扩展 `collector/perfetto.py`**：新增 `TraceServer` 类，管理 `trace_processor_shell server http` 的生命周期
2. **新增 `ws/bridge_server.py`**：aiohttp Web Server，提供桥接页面 + WebSocket 端点
3. **新增 `ws/bridge.html`**（或内嵌在 bridge_server.py 中）：桥接前端页面，嵌入 Perfetto UI iframe

### Phase 2：帧分析核心（2 个新文件）

4. **新增 `agents/frame_analyzer.py`**：接收帧选中信息，查询 trace_processor，调用 LLM 分析
5. **新增 `graph/nodes/frame_analyzer.py`**（可选）：如果需要集成到 LangGraph pipeline

### Phase 3：端到端串联

6. **修改 `graph/cli.py`**：采集 trace 后自动启动 TraceServer + 打开浏览器
7. **修改 `commands/trace.py`**：新增 `/open` 命令，手动打开 Perfetto UI 桥接页面

### Phase 4：体验优化

8. 自动检测选中变化（MutationObserver）
9. 分析结果面板美化（Markdown 渲染、源码高亮）
10. 支持多次选中分析、分析历史

## 八、结论

**可行性评估：可行，推荐实施。**

- **技术成熟度**：Perfetto UI 的 URL API、postMessage、trace_processor HTTP 模式均为官方支持的功能，非 hack
- **架构兼容性**：与现有 LangGraph pipeline 完全兼容，可渐进式集成（独立运行 -> pipeline 节点 -> 对话式）
- **数据复用度**：现有 80%+ 的分析能力（collector、analyzer、attributor、deterministic）可直接复用
- **主要挑战**：获取 Perfetto UI 的选中事件需要变通方案（URL hash 轮询为最可靠的 MVP 方案）
- **最大价值**：将"全量自动分析"升级为"用户驱动的交互式分析"，用户可以聚焦自己关心的帧，获得更精准的分析结果

**MVP 改动量**：约 5 个新文件 + 3 个现有文件小改动，核心逻辑约 800-1200 行代码。

## 参考资料

- [Perfetto UI Plugin 文档](https://perfetto.dev/docs/contributing/ui-plugins)
- [Perfetto UI Deep Linking](https://perfetto.dev/docs/visualization/deep-linking-to-perfetto-ui)
- [Perfetto Commands Automation Reference](https://perfetto.dev/docs/visualization/commands-automation-reference)
- [trace_processor_shell HTTP 源码](https://github.com/google/perfetto/blob/master/src/trace_processor/rpc/httpd.cc)
- [post_message_handler.ts 源码](https://github.com/google/perfetto/blob/master/ui/src/frontend/post_message_handler.ts)
