#!/usr/bin/env bash
# SmartInspector CLI launcher
# Usage:
#   ./scripts/si.sh                          # Interactive REPL
#   ./scripts/si.sh --source-dir ./src       # With source directory
#   ./scripts/si.sh --ci --target com.example.app --duration 10000
#   ./scripts/si.sh --ci --trace trace.pb --output report.md
#   ./scripts/si.sh --ci --startup --target com.example.app
#
# All arguments are forwarded to the smartinspector CLI.

set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
PROJECT_ROOT="$(cd "$SCRIPT_DIR/.." && pwd)"

# Resolve the uv executable
if command -v uv &>/dev/null; then
    UV="uv"
elif [ -f "$HOME/.local/bin/uv" ]; then
    UV="$HOME/.local/bin/uv"
else
    echo "ERROR: uv not found. Install from https://docs.astral.sh/uv/" >&2
    exit 1
fi

# Change to project root so .env is loaded correctly
cd "$PROJECT_ROOT"

exec $UV run smartinspector "$@"
