#!/usr/bin/env bash
# build.sh — Build self-hosted Perfetto UI with SI Bridge plugin
#
# Usage:
#   ./perfetto-plugin/build.sh          # Full build
#   ./perfetto-plugin/build.sh --skip-clone  # Skip git clone (already cloned)
#
# Prerequisites:
#   - Node.js >= 18
#   - npm
#   - git
#
# Note: Automatically removes Android NDK from PATH to prevent strip(1)
# conflicts on macOS. If you need proxy, set http_proxy/https_proxy first.
#
# Output:
#   perfetto-build/ui/out/dist/ — static files to serve via bridge_server.py

set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "$0")" && pwd)"
PROJECT_ROOT="$(cd "$SCRIPT_DIR/.." && pwd)"
PERFETTO_DIR="$PROJECT_ROOT/perfetto-build"
PLUGIN_SRC="$SCRIPT_DIR/com.smartinspector.Bridge"
OUTPUT_DIR="$PERFETTO_DIR/ui/out/dist"

SKIP_CLONE=false
if [[ "${1:-}" == "--skip-clone" ]]; then
  SKIP_CLONE=true
fi

echo "=== SI Bridge Perfetto UI Builder ==="
echo ""

# ── Step 1: Clone Perfetto ───────────────────────────────────────
if [[ "$SKIP_CLONE" == false ]]; then
  if [[ -d "$PERFETTO_DIR/.git" ]]; then
    echo "[1/5] Updating Perfetto repo..."
    cd "$PERFETTO_DIR"
    git pull --ff-only origin master || {
      echo "WARNING: git pull failed. Using existing checkout."
    }
  else
    echo "[1/5] Cloning Perfetto repo (shallow)..."
    git clone --depth 1 https://github.com/google/perfetto.git "$PERFETTO_DIR"
  fi
else
  echo "[1/5] Skipping clone (--skip-clone)"
  if [[ ! -d "$PERFETTO_DIR/.git" ]]; then
    echo "ERROR: perfetto-build/ not found. Run without --skip-clone first."
    exit 1
  fi
fi

cd "$PERFETTO_DIR"

# ── Step 2: Copy plugin ─────────────────────────────────────────
echo "[2/5] Copying SI Bridge plugin..."
PLUGIN_DIR="ui/src/plugins/com.smartinspector.Bridge"
mkdir -p "$PLUGIN_DIR"
cp "$PLUGIN_SRC/index.ts" "$PLUGIN_DIR/index.ts"
echo "  Plugin copied to $PLUGIN_DIR"

# ── Step 3: Register plugin in default_plugins.ts ───────────────
echo "[3/5] Registering plugin in default_plugins..."
DEFAULT_PLUGINS="ui/src/core/embedder/default_plugins.ts"

if ! grep -q "com.smartinspector.Bridge" "$DEFAULT_PLUGINS"; then
  # The file is a simple array of plugin ID strings:
  #   export const defaultPlugins = [
  #     'com.android.AndroidAnr',
  #     ...
  #     'org.kernel.Wattson',
  #   ];
  # We add our plugin ID before the closing '];'
  python3 -c "
import re
with open('$DEFAULT_PLUGINS', 'r') as f:
    content = f.read()
# Insert before the closing '];'
content = content.replace(
    \"'org.kernel.Wattson',\n];\",
    \"'org.kernel.Wattson',\n  'com.smartinspector.Bridge',\n];\",
)
with open('$DEFAULT_PLUGINS', 'w') as f:
    f.write(content)
"
  echo "  Plugin ID added to defaultPlugins array."
else
  echo "  Plugin already registered."
fi

# ── Step 4: Build (includes npm install + TypeScript compile + WASM) ──
echo "[4/5] Building Perfetto UI (includes dependency install, this takes a few minutes)..."

# Remove Android NDK strip from PATH — it overrides macOS /usr/bin/strip
# and cannot handle Mach-O arm64 files, breaking npm postinstall scripts.
export PATH=$(echo "$PATH" | tr ':' '\n' | grep -v "android.*strip\|ndk.*bin" | tr '\n' ':' | sed 's/:$//')

ui/build

echo "[5/5] Verifying output..."
if [[ -f "$OUTPUT_DIR/index.html" ]]; then
  echo "  OK: $OUTPUT_DIR/index.html found"
else
  echo "  WARNING: $OUTPUT_DIR/index.html not found. Build may have failed."
fi

echo ""
echo "=== Build complete! ==="
echo "Static files: $OUTPUT_DIR"
echo ""
echo "To serve:"
echo "  python3 -m http.server 8080 --directory $OUTPUT_DIR"
echo ""
echo "Or use the SI Agent bridge server:"
echo "  /open   (in SmartInspector CLI)"
