# SmartInspector Docker 部署方案

> 生成日期: 2026-05-14
> 项目版本: 0.1.0
> 目标: 为 SmartInspector 提供 Docker 容器化部署方案，支持 CI 分析、MCP Server、交互式 CLI 三种运行模式

---

## 一、项目现状分析

### 1.1 技术栈概览

| 组件 | 技术 | 版本 |
|------|------|------|
| 语言 | Python | >= 3.12 |
| 包管理 | uv + hatchling | latest |
| LLM 框架 | LangChain + LangGraph | >= 1.1.3 |
| MCP 协议 | mcp (FastMCP) | >= 1.0.0 |
| Trace 分析 | perfetto (pip) + trace_processor_shell 二进制 | >= 0.16.0 |
| WebSocket | websockets | >= 16.0 |
| 交互 CLI | prompt_toolkit | >= 3.0 |
| 环境变量 | python-dotenv | >= 1.0.0 |
| 测试框架 | pytest | >= 9.0.2 |

### 1.2 入口点

| 入口 | 命令 | 运行模式 | 说明 |
|------|------|----------|------|
| CLI 交互 | smartinspector | 交互式 REPL | 需要 TTY、adb、WebSocket |
| CLI CI | smartinspector --ci | 非交互 | 无 TTY 依赖，适合容器 |
| MCP Server | si-mcp | stdio 长驻 | 供 Claude Desktop 等 Agent 调用 |
| Headless | HeadlessRunner | 单次执行 | CI/CD 管道调用 |

### 1.3 关键依赖与约束

**二进制依赖 -- trace_processor_shell**
- 当前 bin/trace_processor_shell 是 macOS ARM64 (Mach-O arm64) 二进制，无法在 Linux 容器中运行
- 需要替换为 Linux x86_64 或 ARM64 版本
- 下载源: https://github.com/google/perfetto/releases (Linux build)
- 大小: ~12MB

**外部系统依赖**
- adb (Android Debug Bridge): 设备连接和 trace 采集所需
- Docker 容器中需安装 android-sdk-platform-tools
- 如果只做离线分析（已有 trace 文件），不需要 adb

**静态资源**
- prompts/*.txt: 11 个 prompt 文件，打包在镜像内
- reports/: 运行时生成的日志和报告，需 volume 挂载
- perfetto-build/ui/out/dist/: Perfetto UI 静态文件（仅 bridge 模式需要，~200MB+）
- perfetto-build 目录 4.6GB，不应打入镜像

**网络服务**
- WebSocket Server: 端口 9876 (app 通信)
- Bridge Server: 端口 9877 (Perfetto UI 插件通信)

### 1.4 当前 Docker 状态

- 无任何 Docker 配置（无 Dockerfile、docker-compose.yml、.dockerignore）
- CI 使用 GitHub Actions，无容器化构建
- perfetto-build/ 子目录有构建相关的 Docker 配置，但属于上游项目

---

## 二、Docker 部署架构

### 2.1 架构图

```
外部访问:
  - CI Runner -> docker exec si-analyzer smartinspector --ci ...
  - Claude Desktop -> si-mcp-server (stdio via docker exec)
  - Volume 挂载 -> trace 文件输入 / 报告文件输出

+--------------------------------------------------------------+
|                     docker-compose 网络                        |
|                                                              |
|  +-----------------+   +------------------+                  |
|  |   si-analyzer    |   |   si-mcp-server  |                  |
|  |   (CI/Headless)  |   |   (MCP stdio)    |                  |
|  |                  |   |                  |                  |
|  |  - trace 分析    |   |  - 23 个 MCP 工具|                  |
|  |  - LLM 报告生成  |   |  - 会话管理      |                  |
|  |  - 确定性分析    |   |  - 转发分析请求  |                  |
|  +--------+---------+   +--------+---------+                 |
|           |                      |                            |
|           |   +------------------+                            |
|           |   |                                               |
|  +--------v---v----------------------+                       |
|  |        共享资源                    |                       |
|  |  - /app/bin/trace_processor_shell |                       |
|  |  - /app/prompts/                  |                       |
|  |  - /app/reports/ (volume)         |                       |
|  |  - /traces/        (volume)       |                       |
|  +-----------------------------------+                       |
|                                                              |
+--------------------------------------------------------------+
```

### 2.2 运行模式选择

| 模式 | 容器服务 | 使用场景 |
|------|---------|---------|
| CI 分析 | si-analyzer | GitHub Actions / Jenkins 调用 |
| MCP Server | si-mcp-server | Claude Desktop / OpenClaw 集成 |
| 交互式 CLI | 不推荐容器化 | 需要 TTY + adb 设备直连，建议本地运行 |

> 设计决策: 交互式 CLI 模式依赖 TTY、adb 设备热插拔、WebSocket 实时通信，不适合容器化。Docker 方案聚焦于 CI 分析和 MCP Server 两种无头模式。

---

## 三、Dockerfile 设计（多阶段构建）

```dockerfile
# =============================================================================
# SmartInspector Dockerfile -- Multi-stage Build
# =============================================================================
# 构建目标:
#   - si-analyzer: CI/Headless 分析镜像
#   - si-mcp: MCP Server 镜像
#
# 构建命令:
#   docker build -t smartinspector:latest .
#   docker build --target si-analyzer -t smartinspector:analyzer .
#   docker build --target si-mcp -t smartinspector:mcp .
# =============================================================================

# -- Stage 1: Builder --------------------------------------------------------
FROM python:3.12-slim AS builder

# 安装 uv（比 pip 快 10-100x）
COPY --from=ghcr.io/astral-sh/uv:latest /uv /uvx /bin/

WORKDIR /build

# 先复制依赖声明，利用 Docker 缓存层
COPY pyproject.toml uv.lock ./

# 使用 uv 安装依赖到独立虚拟环境
# --frozen 确保使用 lock 文件，不更新依赖
# --no-dev 排除开发依赖（pytest 等）
RUN uv venv /opt/venv && \
    uv pip install --python /opt/venv/bin/python \
    --frozen \
    --no-dev \
    .

# -- Stage 2a: Analyzer Runtime ----------------------------------------------
FROM python:3.12-slim AS si-analyzer

LABEL maintainer="SmartInspector Team"
LABEL description="SmartInspector CI/Headless Analysis Runtime"

# 安装运行时系统依赖
RUN apt-get update && \
    apt-get install -y --no-install-recommends \
        android-sdk-platform-tools \
        curl \
        ca-certificates \
    && rm -rf /var/lib/apt/lists/*

# 从 builder 复制 Python 虚拟环境
COPY --from=builder /opt/venv /opt/venv
ENV PATH="/opt/venv/bin:$PATH"
ENV VIRTUAL_ENV="/opt/venv"

# 设置工作目录
WORKDIR /app

# 复制应用代码
COPY src/smartinspector/ /app/src/smartinspector/
COPY prompts/ /app/prompts/
COPY pyproject.toml /app/

# 下载 Linux 版 trace_processor_shell
ARG PERFETTO_VERSION=49.0
RUN mkdir -p /app/bin && \
    curl -fsSL \
    "https://github.com/google/perfetto/releases/download/v${PERFETTO_VERSION}/trace_processor_shell-linux-amd64" \
    -o /app/bin/trace_processor_shell && \
    chmod +x /app/bin/trace_processor_shell

# 安装 hatchling 并安装项目（editable，无额外依赖）
RUN pip install hatchling && \
    cd /app && pip install -e . --no-deps

# 创建必要目录
RUN mkdir -p /app/reports /traces

# 环境变量默认值
ENV SI_MODEL="deepseek-chat" \
    SI_BASE_URL="https://api.deepseek.com" \
    SI_API_KEY="" \
    SI_DEBUG="0" \
    SI_REPORT_MAX_TOKENS="4000" \
    SI_TOOL_TIMEOUT="30" \
    PYTHONUNBUFFERED="1" \
    PYTHONDONTWRITEBYTECODE="1"

# 健康检查
HEALTHCHECK --interval=30s --timeout=10s --start-period=5s --retries=3 \
    CMD test -x /app/bin/trace_processor_shell && \
        /app/bin/trace_processor_shell --version || exit 1

# 默认入口
ENTRYPOINT ["python", "-m", "smartinspector.graph"]
CMD ["--help"]

# -- Stage 2b: MCP Server Runtime --------------------------------------------
FROM si-analyzer AS si-mcp

LABEL description="SmartInspector MCP Server for AI Agent Integration"

# MCP Server 使用 stdio 传输，无需 adb
RUN apt-get update && \
    apt-get remove -y android-sdk-platform-tools || true && \
    rm -rf /var/lib/apt/lists/*

# MCP Server 入口点
ENTRYPOINT ["si-mcp"]

# 健康检查: 验证 Python 入口可导入
HEALTHCHECK --interval=60s --timeout=5s --start-period=5s --retries=3 \
    CMD python -c "from smartinspector.mcp_server import main; print('ok')" || exit 1
```

### 3.1 镜像大小估算

| 层 | 大小 | 说明 |
|----|------|------|
| python:3.12-slim 基础 | ~45MB | Debian slim |
| 系统依赖 (adb 等) | ~30MB | android-sdk-platform-tools |
| Python 虚拟环境 | ~200MB | langchain, langgraph, mcp 等 |
| trace_processor_shell | ~12MB | Linux amd64 二进制 |
| 应用代码 + prompts | ~1MB | 源码和 prompt 文件 |
| **总计** | **~290MB** | 相比完整 venv 的 800MB+ 显著减小 |

---

## 四、.dockerignore 设计

```
# 版本控制
.git/
.gitignore

# CI/CD
.github/

# 开发环境
.venv/
.pytest_cache/
.mypy_cache/
.ruff_cache/
.claude/

# macOS
.DS_Store

# 大型目录 -- 不应打入镜像
perfetto-build/
perfetto-plugin/
platform/

# 构建产物
*.egg-info/
dist/
build/

# 环境配置（通过环境变量或 volume 注入）
.env

# 生成的报告（通过 volume 挂载）
reports/

# 文档（运行时不需要）
docs/
*.md
!README.md

# 测试（生产镜像不需要）
tests/

# IDE
.idea/
.vscode/
*.swp
*.swo
```

---

## 五、docker-compose.yml 设计

```yaml
# =============================================================================
# SmartInspector Docker Compose
# =============================================================================
# 使用方式:
#   开发: docker compose --profile dev up
#   生产: docker compose --profile prod up -d
#   CI:   docker compose run --rm si-analyzer smartinspector --ci ...
# =============================================================================

services:
  # -- CI/Headless 分析器 ----------------------------------------------------
  si-analyzer:
    build:
      context: .
      dockerfile: Dockerfile
      target: si-analyzer
    image: smartinspector:analyzer
    container_name: si-analyzer
    restart: "no"
    environment:
      - SI_MODEL=${SI_MODEL:-deepseek-chat}
      - SI_BASE_URL=${SI_BASE_URL:-https://api.deepseek.com}
      - SI_API_KEY=${SI_API_KEY:?SI_API_KEY is required}
      - SI_DEBUG=${SI_DEBUG:-0}
      - SI_REPORT_MAX_TOKENS=${SI_REPORT_MAX_TOKENS:-4000}
    volumes:
      - ${TRACE_DIR:-./traces}:/traces:ro
      - ./reports:/app/reports
      - ${SOURCE_DIR:-./src}:/source:ro
    working_dir: /app
    profiles:
      - ci
    healthcheck:
      test: ["CMD", "test", "-x", "/app/bin/trace_processor_shell"]
      interval: 30s
      timeout: 10s
      retries: 3

  # -- MCP Server ------------------------------------------------------------
  si-mcp-server:
    build:
      context: .
      dockerfile: Dockerfile
      target: si-mcp
    image: smartinspector:mcp
    container_name: si-mcp-server
    restart: unless-stopped
    environment:
      - SI_MODEL=${SI_MODEL:-deepseek-chat}
      - SI_BASE_URL=${SI_BASE_URL:-https://api.deepseek.com}
      - SI_API_KEY=${SI_API_KEY:?SI_API_KEY is required}
      - SI_DEBUG=${SI_DEBUG:-0}
    volumes:
      - ${TRACE_DIR:-./traces}:/traces:ro
      - ./reports:/app/reports
      - ${SOURCE_DIR:-./src}:/source:ro
    stdin_open: true
    profiles:
      - mcp
    healthcheck:
      test: ["CMD", "python", "-c", "from smartinspector.mcp_server import main; print('ok')"]
      interval: 60s
      timeout: 5s
      retries: 3

  # -- 开发环境 --------------------------------------------------------------
  si-dev:
    build:
      context: .
      dockerfile: Dockerfile
      target: si-analyzer
    image: smartinspector:dev
    container_name: si-dev
    restart: "no"
    environment:
      - SI_MODEL=${SI_MODEL:-deepseek-chat}
      - SI_BASE_URL=${SI_BASE_URL:-https://api.deepseek.com}
      - SI_API_KEY=${SI_API_KEY:-}
      - SI_DEBUG=1
      - SI_WS_PORT=${SI_WS_PORT:-9876}
    volumes:
      - .:/app
      - ./reports:/app/reports
    ports:
      - "${SI_WS_PORT:-9876}:9876"
    profiles:
      - dev
    stdin_open: true
    tty: true

  # -- 生产环境（常驻分析服务）----------------------------------------------
  si-prod:
    build:
      context: .
      dockerfile: Dockerfile
      target: si-analyzer
    image: smartinspector:prod
    container_name: si-prod
    restart: unless-stopped
    environment:
      - SI_MODEL=${SI_MODEL}
      - SI_BASE_URL=${SI_BASE_URL}
      - SI_API_KEY=${SI_API_KEY}
      - SI_DEBUG=0
      - SI_REPORT_MAX_TOKENS=${SI_REPORT_MAX_TOKENS:-4000}
    volumes:
      - ${TRACE_DIR:-/data/traces}:/traces:ro
      - ${REPORT_DIR:-/data/reports}:/app/reports
      - ${SOURCE_DIR:-/data/source}:/source:ro
    logging:
      driver: json-file
      options:
        max-size: "50m"
        max-file: "5"
    deploy:
      resources:
        limits:
          memory: 2G
          cpus: "2.0"
        reservations:
          memory: 512M
          cpus: "0.5"
    profiles:
      - prod
    healthcheck:
      test: ["CMD", "test", "-x", "/app/bin/trace_processor_shell"]
      interval: 30s
      timeout: 10s
      retries: 3
      start_period: 10s

networks:
  default:
    name: si-network
    driver: bridge
```

---

## 六、环境变量管理方案

### 6.1 .env 文件设计

```bash
# =============================================================================
# SmartInspector Docker 环境变量配置
# =============================================================================
# 使用方式:
#   1. 复制到 .env: cp .env.docker.example .env
#   2. 填写 [REQUIRED] 参数
#   3. 运行: docker compose --profile prod up -d
# =============================================================================

# -- LLM 配置 [REQUIRED] ----------------------------------------------------
SI_API_KEY=                           # [REQUIRED] LLM API 密钥
SI_MODEL=deepseek-chat                # 默认模型
SI_BASE_URL=https://api.deepseek.com  # API 端点

# -- 角色覆盖（可选）--------------------------------------------------------
# SI_ATTRIBUTOR_MODEL=claude-sonnet-4-20250514

# -- 功能配置 ----------------------------------------------------------------
SI_DEBUG=0
SI_REPORT_MAX_TOKENS=4000
SI_TOOL_TIMEOUT=30
SI_WS_PORT=9876
SI_WS_PING_TIMEOUT=30

# -- Docker 卷挂载路径 -------------------------------------------------------
TRACE_DIR=./traces
REPORT_DIR=./reports
SOURCE_DIR=./src
```

### 6.2 环境变量优先级

1. docker compose environment: 显式设置（最高优先级）
2. .env 文件（docker compose 自动加载）
3. Dockerfile ENV（默认值，最低优先级）

### 6.3 敏感信息管理

- SI_API_KEY: 通过 .env 文件注入，绝不写入镜像
- .env 文件已在 .gitignore 中排除
- CI/CD 环境通过 GitHub Secrets 注入
- 生产环境推荐使用 Docker Secrets 或外部密钥管理（Vault）

---

## 七、数据持久化方案

### 7.1 Volume 映射

| 容器路径 | 宿主机路径 | 读写 | 用途 |
|----------|-----------|------|------|
| /traces | ${TRACE_DIR} | ro | Trace 文件输入（.pb 文件） |
| /app/reports | ${REPORT_DIR} | rw | 日志和报告输出 |
| /source | ${SOURCE_DIR} | ro | 源码目录（source attribution） |
| /app/prompts | 镜像内 | ro | LLM prompt 文件 |

### 7.2 Named Volumes (生产推荐)

```yaml
volumes:
  si-reports:
    driver: local
    driver_opts:
      type: none
      o: bind
      device: /data/smartinspector/reports
  si-traces:
    driver: local
    driver_opts:
      type: none
      o: bind
      device: /data/smartinspector/traces
```

### 7.3 数据清理策略

```bash
# 报告文件清理（保留最近 30 天）
find /data/smartinspector/reports -name "perf_report_*.md" -mtime +30 -delete
find /data/smartinspector/reports -name "debug_*.log" -mtime +7 -delete
# Trace 文件清理（保留最近 7 天）
find /data/smartinspector/traces -name "*.pb" -mtime +7 -delete
```

---

## 八、网络配置

### 8.1 端口映射

| 服务 | 容器端口 | 宿主端口 | 协议 | 说明 |
|------|---------|---------|------|------|
| si-dev | 9876 | 9876 | TCP/WS | WebSocket app 通信 |
| si-dev | 9877 | 9877 | TCP/WS | Perfetto UI Bridge |
| si-mcp-server | - | - | stdio | 无端口，stdin/stdout |

### 8.2 ADB 网络连接

容器内通过 adb connect 连接远程设备:
```bash
adb connect 192.168.1.100:5555
```

或通过 --network=host 模式（仅开发环境）:
```bash
docker compose --profile dev run --network=host si-dev
```

---

## 九、健康检查配置

| 服务 | 检查方式 | 间隔 | 超时 | 重试 |
|------|---------|------|------|------|
| si-analyzer | test -x trace_processor_shell | 30s | 10s | 3 |
| si-mcp-server | Python import 检查 | 60s | 5s | 3 |
| si-prod | 二进制 + Python 双重检查 | 30s | 10s | 3 |

自定义健康检查脚本 docker/healthcheck.sh:
```bash
#!/bin/bash
set -e
test -x /app/bin/trace_processor_shell || exit 1
python -c "from smartinspector.graph import create_graph" || exit 1
test -d /app/prompts || exit 1
echo "healthy"
exit 0
```

---

## 十、日志收集方案

### 10.1 容器内日志
SmartInspector 日志已写入 reports/debug_*.log（通过 info_log / debug_log），通过 volume 映射可直接访问。

### 10.2 Docker stdout 日志

```yaml
logging:
  driver: json-file
  options:
    max-size: "50m"
    max-file: "5"
    tag: "si-{{.Name}}"
```

### 10.3 集中式日志收集（生产推荐）

方案 A: Filebeat + ELK
```yaml
filebeat:
  image: elastic/filebeat:8.12
  volumes:
    - ./reports:/logs:ro
    - ./filebeat.yml:/usr/share/filebeat/filebeat.yml:ro
```

方案 B: Fluentd driver
```yaml
si-prod:
  logging:
    driver: fluentd
    options:
      fluentd-address: "localhost:24224"
      tag: "si.analyzer"
```

方案 C: 直接挂载（最简单） -- 将 reports 目录挂载到宿主机统一日志收集点

---

## 十一、生产 vs 开发环境差异

| 维度 | 开发环境 | 生产环境 |
|------|---------|---------|
| Dockerfile target | si-analyzer | si-analyzer / si-mcp |
| Compose profile | dev | prod / mcp |
| 源码挂载 | .:/app | 镜像内 |
| 调试模式 | SI_DEBUG=1 | SI_DEBUG=0 |
| TTY | stdin_open + tty: true | 无 |
| ADB | USB 直连 --privileged | 网络ADB或离线分析 |
| 重启策略 | "no" | unless-stopped |
| 资源限制 | 无限制 | memory: 2G, cpus: 2.0 |
| 日志驱动 | 默认 json-file | json-file + max-size/max-file |
| 端口暴露 | 9876, 9877 | 无（CI 模式不需要） |
| API Key | .env 文件 | Docker Secrets / Vault |
| Trace 来源 | adb 实时采集 | volume 挂载已有文件 |
| 网络模式 | 可选 host 模式 | bridge 隔离 |

---

## 十二、使用示例

### 12.1 CI 分析（最常用）

```bash
# 构建
docker build --target si-analyzer -t smartinspector:analyzer .

# 运行分析
docker run --rm \
  -v /path/to/trace.pb:/traces/app.pb:ro \
  -v /path/to/source:/source:ro \
  -v ./reports:/app/reports \
  -e SI_API_KEY="sk-xxx" \
  smartinspector:analyzer \
  smartinspector --ci --trace /traces/app.pb --target com.example.app \
    --format json --output /app/reports/report.json

# 查看报告
cat ./reports/report.json
```

### 12.2 MCP Server（Claude Desktop 集成）

```bash
# 构建
docker build --target si-mcp -t smartinspector:mcp .

# claude_desktop_config.json:
{
  "mcpServers": {
    "smartinspector": {
      "command": "docker",
      "args": [
        "run", "--rm", "-i",
        "-v", "/path/to/traces:/traces:ro",
        "-v", "/path/to/source:/source:ro",
        "-v", "/path/to/reports:/app/reports",
        "-e", "SI_API_KEY=sk-xxx",
        "smartinspector:mcp"
      ]
    }
  }
}
```

### 12.3 docker compose 一键启动

```bash
# CI 分析
docker compose --profile ci run --rm si-analyzer \
  smartinspector --ci --trace /traces/app.pb --target com.example.app

# MCP Server
docker compose --profile mcp up -d si-mcp-server

# 开发环境
docker compose --profile dev run --rm si-dev
```

### 12.4 GitHub Actions 集成

```yaml
name: Performance Analysis
on:
  workflow_dispatch:
    inputs:
      trace_path:
        description: 'Path to trace file'
        required: true

jobs:
  analyze:
    runs-on: ubuntu-latest
    steps:
      - uses: actions/checkout@v4

      - name: Build Analyzer Image
        run: docker build --target si-analyzer -t si:latest .

      - name: Download Trace
        uses: actions/download-artifact@v4
        with:
          name: perfetto-trace
          path: ./traces

      - name: Run Analysis
        env:
          SI_API_KEY: ${{ secrets.SI_API_KEY }}
        run: |
          docker run --rm \
            -v ./traces:/traces:ro \
            -v ./reports:/app/reports \
            -e SI_API_KEY="$SI_API_KEY" \
            si:latest \
            smartinspector --ci \
              --trace /traces/trace.pb \
              --target com.example.app \
              --format json \
              --output /app/reports/report.json

      - name: Upload Report
        uses: actions/upload-artifact@v4
        with:
          name: performance-report
          path: ./reports/
```

---

## 十三、实施步骤

### Phase 1: 基础容器化

1. **准备 Linux 版 trace_processor_shell**
   ```bash
   curl -L -o bin/trace_processor_shell-linux \
     https://github.com/google/perfetto/releases/latest/download/trace_processor_shell-linux-amd64
   chmod +x bin/trace_processor_shell-linux
   ```

2. **创建 Dockerfile + .dockerignore**
   - 复制本文档中的配置
   - 验证构建: docker build -t smartinspector:test .

3. **创建 docker-compose.yml**

4. **创建 .env.docker.example**

### Phase 2: CI 集成

5. **更新 GitHub Actions 工作流** -- 使用 Docker 镜像替代直接 pip install
6. **添加 Docker 构建缓存** -- 使用 GHA cache

### Phase 3: MCP Server 部署

7. **配置 Claude Desktop 集成** -- 修改 claude_desktop_config.json
8. **配置 OpenClaw 集成**

### Phase 4: 生产加固

9. **安全扫描**: trivy image smartinspector:analyzer
10. **多架构构建**: docker buildx build --platform linux/amd64,linux/arm64
11. **资源调优**: 根据实际负载调整 memory/cpu limits

---

## 十四、注意事项与风险

### 14.1 关键风险

| 风险 | 影响 | 缓解措施 |
|------|------|---------|
| trace_processor_shell 平台不兼容 | 分析完全失败 | 下载对应平台二进制，CI 中验证 |
| LLM API 调用超时 | 报告生成失败 | 设置 SI_TOOL_TIMEOUT，重试机制 |
| 大 trace 文件 OOM | 进程崩溃 | 限制 trace 大小，增加 memory limit |
| .env 泄露 API Key | 安全风险 | .gitignore 排除，使用 Docker Secrets |
| 容器内无 adb 设备 | 无法实时采集 trace | 使用网络 adb 或离线分析模式 |

### 14.2 不适合容器化的场景

1. 交互式 REPL 模式 -- 需要 TTY、prompt_toolkit 高级终端特性
2. USB adb 设备直连 -- 需要 --privileged 或复杂的 USB 设备映射
3. Perfetto UI Bridge -- 需要浏览器交互，且依赖 4.6GB 的 perfetto-build

### 14.3 性能考量

- trace_processor_shell 大 trace 文件（>100MB）分析可能消耗大量内存
- LLM API 调用是主要延迟来源（网络 I/O，非 CPU 密集）
- 确定性分析（si_quick）无 LLM 调用，适合高频 CI 场景
- 建议生产环境 memory limit >= 2GB

---

## 附录 A: Docker 快速命令参考

```bash
# 构建
docker build -t smartinspector:latest .
docker build --target si-analyzer -t smartinspector:analyzer .
docker build --target si-mcp -t smartinspector:mcp .

# 运行 CI 分析
docker run --rm \
  -v ./traces/trace.pb:/traces/trace.pb:ro \
  -v ./reports:/app/reports \
  -e SI_API_KEY="sk-xxx" \
  smartinspector:analyzer \
  smartinspector --ci --trace /traces/trace.pb --target com.example.app

# 运行 MCP Server
docker run --rm -i -e SI_API_KEY="sk-xxx" smartinspector:mcp

# 进入容器调试
docker run --rm -it --entrypoint bash smartinspector:analyzer

# 清理
docker compose --profile ci down -v
docker image prune -f
```

## 附录 B: 完整文件清单

部署需要新增以下文件:

```
AppSmartInspector/
+-- Dockerfile                    # 多阶段构建（第三节）
+-- docker-compose.yml            # 编排配置（第五节）
+-- .dockerignore                 # 构建排除（第四节）
+-- .env.docker.example           # 环境变量模板（第六节）
+-- docker/
|   +-- healthcheck.sh            # 健康检查脚本（第九节）
+-- .github/workflows/
    +-- analyze.yml               # CI 分析工作流（第十二节）
```
