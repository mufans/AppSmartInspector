# SmartInspector MCP Server

SmartInspector provides a [Model Context Protocol (MCP)](https://modelcontextprotocol.io/) server that exposes all CLI commands as MCP tools. This allows external AI agents (Claude, OpenClaw, etc.) to call SmartInspector's performance analysis capabilities directly.

## Quick Start

```bash
# Install SmartInspector with MCP support
uv pip install -e ".[mcp]"

# Run the MCP server (stdio transport)
si-mcp
```

## Architecture

```
MCP Client (Claude Desktop / OpenClaw / etc.)
  ↕ stdio (JSON-RPC)
SmartInspector MCP Server (FastMCP)
  ↕ Python API calls
SmartInspector Command Handlers
  ↕ LangGraph Pipeline
PerfettoCollector / LLM Agents / Report Generator
```

The MCP server wraps existing slash command handlers (`/full`, `/trace`, `/analyze`, etc.) and exposes them as typed MCP tools. Session state is maintained across tool calls within a single MCP session.

## Tools Reference

### Analysis Pipeline

| Tool | Description | Key Parameters |
|------|-------------|----------------|
| `si_full` | Full pipeline: collect → analyze → attribute → report | `duration_ms`, `package_name`, `no_wait`, `debug` |
| `si_trace` | Collect + analyze trace (stops before attribution) | `duration_ms`, `package_name` |
| `si_analyze` | Analyze an existing trace file | `trace_path` |
| `si_quick` | Fast deterministic analysis (no LLM) | `trace_path` |
| `si_startup` | Cold start analysis | `package_name` |
| `si_record` | Record trace without analysis | `duration_ms`, `package_name` |
| `si_frame` | Analyze a specific frame/slice | `ts`, `dur` |
| `si_report` | Generate report from collected data | `output_path` |
| `si_ci_analyze` | Non-interactive CI/automation analysis | `trace_path`, `cmd`, `target`, `duration`, `output_format` |

### Comparison

| Tool | Description | Key Parameters |
|------|-------------|----------------|
| `si_compare` | Compare two analysis reports | `report1`, `report2`, `mode` (latest/list/files) |

### Device Management

| Tool | Description | Key Parameters |
|------|-------------|----------------|
| `si_devices` | List connected Android devices | — |
| `si_connect` | Connect to device via ADB TCP | `host_port` |
| `si_disconnect` | Disconnect from device | — |
| `si_status` | Show session status | — |

### Hook Configuration

| Tool | Description | Key Parameters |
|------|-------------|----------------|
| `si_config` | View/update hook configuration | `action`, `json_config`, `source_dir` |
| `si_hooks` | List available trace hooks | — |
| `si_hook` | Enable/disable/add/remove hooks | `action`, `hook_id`, `class_name`, `method_name` |

### Session & UI

| Tool | Description | Key Parameters |
|------|-------------|----------------|
| `si_summary` | Show performance summary | — |
| `si_clear` | Clear session data | — |
| `si_tokens` | Show token usage | — |
| `si_open` | Open Perfetto UI bridge | `trace_path` |
| `si_close` | Close Perfetto UI bridge | — |
| `si_help` | Show help | — |

## Configuration Examples

### Claude Desktop

Add to your Claude Desktop config file (`~/Library/Application Support/Claude/claude_desktop_config.json` on macOS):

```json
{
  "mcpServers": {
    "smartinspector": {
      "command": "si-mcp",
      "args": [],
      "env": {
        "SI_MODEL": "deepseek-chat",
        "SI_BASE_URL": "https://api.deepseek.com",
        "SI_API_KEY": "your-api-key"
      }
    }
  }
}
```

Or use `uv run` for development:

```json
{
  "mcpServers": {
    "smartinspector": {
      "command": "uv",
      "args": ["run", "smartinspector.mcp_server:main"],
      "env": {
        "SI_API_KEY": "your-api-key"
      }
    }
  }
}
```

### OpenClaw

Configure via OpenClaw's MCP server settings:

```yaml
mcp_servers:
  - name: smartinspector
    command: si-mcp
    env:
      SI_API_KEY: your-api-key
```

### Cursor / VS Code (Cline)

```json
{
  "mcp.servers": {
    "smartinspector": {
      "command": "si-mcp",
      "env": {
        "SI_API_KEY": "your-api-key"
      }
    }
  }
}
```

## Environment Variables

All `SI_*` environment variables from the CLI are supported:

| Variable | Default | Purpose |
|----------|---------|---------|
| `SI_MODEL` | `deepseek-chat` | LLM model name |
| `SI_BASE_URL` | `https://api.deepseek.com` | API base URL |
| `SI_API_KEY` | — | API key (fallback: `OPENAI_API_KEY`) |
| `SI_DEBUG` | — | Enable debug logging (`1`/`true`/`yes`) |
| `SI_REPORT_MAX_TOKENS` | `4000` | Max tokens for report generation |

## Typical Workflows

### Full Analysis from an Existing Trace

```
1. si_ci_analyze(trace_path="/path/to/trace.pb", target="com.example.app")
   → Returns full performance report
```

### Interactive Device Analysis

```
1. si_devices()           → List available devices
2. si_connect("192.168.1.100:5555")  → Connect to device
3. si_full(package_name="com.example.app", duration_ms=10000)
   → Collect trace, analyze, attribute, and generate report
4. si_summary()           → View formatted summary
```

### Cold Start Profiling

```
1. si_startup("com.example.app")
   → Force-stop app, record trace, launch, analyze cold start
```

### Quick Iteration (No LLM)

```
1. si_record(duration_ms=5000)  → Record trace
2. si_quick()                    → Fast deterministic analysis
```

### Compare Performance Across Builds

```
1. si_ci_analyze(trace_path="build_v1.pb", target="com.example.app")
2. si_ci_analyze(trace_path="build_v2.pb", target="com.example.app")
3. si_compare(mode="latest")  → Compare the two runs
```

## Development

The MCP server is implemented in `src/smartinspector/mcp_server.py` using the `FastMCP` class from the `mcp` Python package. Each tool wraps an existing command handler from `src/smartinspector/commands/`.

To add a new tool:

1. Create an `async def si_<name>(...)` function decorated with `@mcp.tool(title="...")`
2. Import the corresponding command handler inside the function body (lazy import to avoid circular deps)
3. Call `_run_command(handler, args)` to execute with stdout capture
4. The tool function returns a string (captured output from the command)

## Transport

The server uses **stdio transport** (standard MCP mode). Communication happens over stdin/stdout using JSON-RPC. Server startup messages are printed to stderr.
