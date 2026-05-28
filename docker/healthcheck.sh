#!/bin/bash
# =============================================================================
# SmartInspector Docker Health Check
# =============================================================================
# Verifies:
#   1. trace_processor_shell binary is executable
#   2. Python smartinspector.graph module can be imported
#   3. prompts/ directory exists
# =============================================================================
set -e

test -x /app/bin/trace_processor_shell || exit 1
python -c "from smartinspector.graph import create_graph" || exit 1
test -d /app/prompts || exit 1

echo "healthy"
exit 0
