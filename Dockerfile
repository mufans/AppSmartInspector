# =============================================================================
# SmartInspector Dockerfile -- Multi-stage Build
# =============================================================================
# Build targets:
#   - si-analyzer: CI/Headless analysis image
#   - si-mcp: MCP Server image
#
# Build commands:
#   docker build -t smartinspector:latest .
#   docker build --target si-analyzer -t smartinspector:analyzer .
#   docker build --target si-mcp -t smartinspector:mcp .
# =============================================================================

# -- Stage 1: Builder --------------------------------------------------------
FROM python:3.12-slim AS builder

# Install uv (10-100x faster than pip)
COPY --from=ghcr.io/astral-sh/uv:latest /uv /uvx /bin/

WORKDIR /build

# Copy dependency declarations first for Docker layer caching
COPY pyproject.toml uv.lock ./

# Install dependencies into isolated venv using lock file
RUN uv venv /opt/venv && \
    uv pip install --python /opt/venv/bin/python \
    --frozen \
    --no-dev \
    .

# -- Stage 2a: Analyzer Runtime ----------------------------------------------
FROM python:3.12-slim AS si-analyzer

LABEL maintainer="SmartInspector Team"
LABEL description="SmartInspector CI/Headless Analysis Runtime"

# Install runtime system dependencies
RUN apt-get update && \
    apt-get install -y --no-install-recommends \
        android-sdk-platform-tools \
        curl \
        ca-certificates \
    && rm -rf /var/lib/apt/lists/*

# Copy Python venv from builder
COPY --from=builder /opt/venv /opt/venv
ENV PATH="/opt/venv/bin:$PATH"
ENV VIRTUAL_ENV="/opt/venv"

WORKDIR /app

# Copy application code
COPY src/smartinspector/ /app/src/smartinspector/
COPY prompts/ /app/prompts/
COPY pyproject.toml /app/

# Download Linux trace_processor_shell binary
ARG PERFETTO_VERSION=49.0
RUN mkdir -p /app/bin && \
    curl -fsSL \
    "https://github.com/google/perfetto/releases/download/v${PERFETTO_VERSION}/trace_processor_shell-linux-amd64" \
    -o /app/bin/trace_processor_shell && \
    chmod +x /app/bin/trace_processor_shell

# Install project in editable mode (no extra deps, venv already has them)
RUN pip install hatchling && \
    cd /app && pip install -e . --no-deps

# Create runtime directories
RUN mkdir -p /app/reports /traces

# Environment variable defaults
ENV SI_MODEL="deepseek-chat" \
    SI_BASE_URL="https://api.deepseek.com" \
    SI_API_KEY="" \
    SI_DEBUG="0" \
    SI_REPORT_MAX_TOKENS="4000" \
    SI_TOOL_TIMEOUT="30" \
    PYTHONUNBUFFERED="1" \
    PYTHONDONTWRITEBYTECODE="1"

# Health check
HEALTHCHECK --interval=30s --timeout=10s --start-period=5s --retries=3 \
    CMD test -x /app/bin/trace_processor_shell && \
        /app/bin/trace_processor_shell --version || exit 1

# Default entrypoint
ENTRYPOINT ["python", "-m", "smartinspector.graph"]
CMD ["--help"]

# -- Stage 2b: MCP Server Runtime --------------------------------------------
FROM si-analyzer AS si-mcp

LABEL description="SmartInspector MCP Server for AI Agent Integration"

# MCP Server uses stdio transport, no adb needed
RUN apt-get update && \
    apt-get remove -y android-sdk-platform-tools || true && \
    rm -rf /var/lib/apt/lists/*

# MCP Server entrypoint
ENTRYPOINT ["si-mcp"]

# Health check: verify Python import works
HEALTHCHECK --interval=60s --timeout=5s --start-period=5s --retries=3 \
    CMD python -c "from smartinspector.mcp_server import main; print('ok')" || exit 1
