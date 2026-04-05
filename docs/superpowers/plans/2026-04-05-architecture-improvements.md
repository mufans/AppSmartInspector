# SmartInspector Architecture Improvements Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Fix P0 reliability issues, centralize configuration, refactor the oversized collector module, and extract common agent initialization patterns.

**Architecture:** Four independent workstreams that can be executed in any order. Each produces a self-contained, testable improvement without breaking existing functionality. No cross-task dependencies.

**Tech Stack:** Python 3.12, LangGraph, LangChain, Perfetto, pytest

---

## Task 1: Fix silent exception swallowing in collector/perfetto.py

**Files:**
- Modify: `src/smartinspector/collector/perfetto.py`

**Problem:** Multiple `except Exception: pass` blocks silently swallow errors during trace parsing. When a SQL query fails or data is malformed, there's zero diagnostic output — making it extremely hard to debug why certain analysis sections come back empty.

- [ ] **Step 1: Add `logging` import and module logger**

At the top of `src/smartinspector/collector/perfetto.py`, add:

```python
import logging

logger = logging.getLogger(__name__)
```

(Place after the existing `import` block, around line 6.)

- [ ] **Step 2: Replace all `except Exception: pass` with logged warnings**

There are 13 occurrences of `except Exception: pass` in this file. Replace each one:

1. **Line 134** (collect_sched — blocked_reasons query):
```python
        except Exception as e:
            logger.debug("sched_blocked_reason query failed: %s", e)
```

2. **Line 163** (collect_cpu_hotspots — main query):
```python
        except Exception as e:
            logger.debug("CPU hotspot query failed: %s", e)
            return []
```

3. **Line 179** (collect_cpu_hotspots — callsite_map query):
```python
        except Exception as e:
            logger.debug("callsite_map query failed: %s", e)
```

4. **Line 234** (collect_frame_timeline — expected_map query):
```python
        except Exception as e:
            logger.debug("Expected frame timeline query failed: %s", e)
```

5. **Line 440** (collect_sys_stats — cpu_idle query):
```python
        except Exception as e:
            logger.debug("CPU idle samples query failed: %s", e)
```

6. **Line 463** (collect_sys_stats — cpu_freq query):
```python
        except Exception as e:
            logger.debug("CPU frequency query failed: %s", e)
```

7. **Line 481** (collect_sys_stats — fork_rate query):
```python
        except Exception as e:
            logger.debug("Fork rate query failed: %s", e)
```

8. **Line 526** (collect_process_memory — main query):
```python
        except Exception as e:
            logger.debug("Process memory query failed: %s", e)
```

9. **Line 647** (collect_view_slices — grandparent query):
```python
        except Exception as e:
            logger.debug("Grandparent slice query failed: %s", e)
```

10. **Line 649** (collect_view_slices — parent query):
```python
        except Exception as e:
            logger.debug("Parent slice query failed: %s", e)
```

11. **Line 981** (collect_block_events — logcat query):
```python
        except Exception as e:
            logger.debug("SIBlock logcat query failed: %s", e)
```

12. **Line 1067** (summarize — table diagnosis):
```python
        except Exception as e:
            logger.debug("Table diagnosis failed: %s", e)
```

13. **Line 1134** (summarize — sys_stats):
```python
        except Exception as e:
            logger.debug("sys_stats collection failed: %s", e)
```

- [ ] **Step 3: Run existing tests to verify nothing breaks**

Run: `cd /Users/liujun/langchainProjects/smartinspector && python -m pytest tests/ -v`
Expected: All existing tests pass (same as before — we only added logging).

- [ ] **Step 4: Commit**

```bash
git add src/smartinspector/collector/perfetto.py
git commit -m "fix(collector): replace silent exception swallowing with debug logging"
```

---

## Task 2: Fix thread-unsafe global LLM initialization in agents

**Files:**
- Modify: `src/smartinspector/agents/perf_analyzer.py`
- Modify: `src/smartinspector/agents/attributor.py`
- Modify: `src/smartinspector/agents/explorer.py`
- Modify: `src/smartinspector/agents/android.py`

**Problem:** All agent modules use a `_llm = None` global with lazy initialization via `_get_llm()` without any lock. If two threads call `_get_llm()` simultaneously, they could create duplicate LLM clients or read a partially initialized object.

- [ ] **Step 1: Add thread-safe singleton helper to `perf_analyzer.py`**

Replace the current pattern (lines 12-20):
```python
_prompt = load_prompt("perf-analyzer")
_llm = None


def _get_llm():
    global _llm
    if _llm is not None:
        return _llm
    _llm = ChatOpenAI(**get_llm_kwargs(temperature=0.1))
    return _llm
```

With:
```python
import threading

_prompt = load_prompt("perf-analyzer")
_llm = None
_llm_lock = threading.Lock()


def _get_llm():
    global _llm
    if _llm is not None:
        return _llm
    with _llm_lock:
        if _llm is not None:
            return _llm
        _llm = ChatOpenAI(**get_llm_kwargs(temperature=0.1))
    return _llm
```

- [ ] **Step 2: Add thread-safe initialization to `attributor.py`**

Add `import threading` at the top (after line 13). Replace the current global + `_get_llm` pattern (lines 49-105):

```python
_llm_with_tools = None
_system_prompt = None
_structured_llm = None
_llm_lock = threading.Lock()
```

Update `_get_llm`:
```python
def _get_llm():
    """Get LLM with bound tools (singleton, thread-safe)."""
    global _llm_with_tools, _system_prompt, _structured_llm
    if _llm_with_tools is not None:
        return _llm_with_tools, _system_prompt
    with _llm_lock:
        if _llm_with_tools is not None:
            return _llm_with_tools, _system_prompt
        llm = ChatOpenAI(**get_llm_kwargs(role="attributor", temperature=0))
        _llm_with_tools = llm.bind_tools([grep, glob, read])
        _structured_llm = llm.with_structured_output(AttributionResponse)
        _system_prompt = load_prompt("attributor")
    return _llm_with_tools, _system_prompt
```

- [ ] **Step 3: Add thread-safe initialization to `explorer.py`**

Add `import threading` at the top. Replace the current pattern (lines 13-28):

```python
_agent = None
_agent_lock = threading.Lock()


def _get_agent():
    global _agent
    if _agent is not None:
        return _agent
    with _agent_lock:
        if _agent is not None:
            return _agent
        llm = ChatOpenAI(**get_llm_kwargs(temperature=0.1, streaming=True))
        system_prompt = load_prompt("code-explorer")
        _agent = create_agent(
            model=llm,
            tools=[grep, glob, read],
            system_prompt=system_prompt,
        )
    return _agent
```

- [ ] **Step 4: Add thread-safe initialization to `android.py`**

Add `import threading` at the top. Replace the current pattern (lines 10-26):

```python
_agent = None
_agent_lock = threading.Lock()


def get_android_agent():
    """Return the compiled Android expert agent (singleton, thread-safe)."""
    global _agent
    if _agent is not None:
        return _agent
    with _agent_lock:
        if _agent is not None:
            return _agent
        llm = ChatOpenAI(**get_llm_kwargs(temperature=0.1, streaming=True))
        prompt = load_prompt("android-expert")
        _agent = create_agent(
            model=llm,
            tools=[analyze_perfetto, collect_android_trace],
            system_prompt=prompt,
        )
    return _agent
```

- [ ] **Step 5: Run existing tests**

Run: `cd /Users/liujun/langchainProjects/smartinspector && python -m pytest tests/ -v`
Expected: All existing tests pass.

- [ ] **Step 6: Commit**

```bash
git add src/smartinspector/agents/perf_analyzer.py src/smartinspector/agents/attributor.py src/smartinspector/agents/explorer.py src/smartinspector/agents/android.py
git commit -m "fix(agents): add thread-safe double-checked locking for LLM singletons"
```

---

## Task 3: Centralize hardcoded configuration values

**Files:**
- Modify: `src/smartinspector/config.py`
- Modify: `src/smartinspector/tools/read.py`
- Modify: `src/smartinspector/tools/grep.py`
- Modify: `src/smartinspector/tools/glob.py`
- Modify: `src/smartinspector/graph/nodes/reporter/__init__.py`
- Modify: `src/smartinspector/ws/server.py`

**Problem:** Timeout values (30s), size limits (50KB, 8000 chars), token limits (4000), and port (9876) are hardcoded in individual files. Changing them requires finding and editing multiple files.

- [ ] **Step 1: Add centralized config accessors to `config.py`**

Append these functions to the end of `src/smartinspector/config.py`:

```python
# ── Tool limits ───────────────────────────────────────────────


def get_tool_timeout() -> int:
    """Timeout in seconds for tool subprocess calls (grep, glob).

    Priority: SI_TOOL_TIMEOUT env var > default (30).
    """
    try:
        return int(os.environ.get("SI_TOOL_TIMEOUT", "30"))
    except (ValueError, TypeError):
        return 30


def get_read_max_lines() -> int:
    """Max lines returned by the read tool."""
    try:
        return int(os.environ.get("SI_READ_MAX_LINES", "2000"))
    except (ValueError, TypeError):
        return 2000


def get_read_max_bytes() -> int:
    """Max bytes returned by the read tool."""
    try:
        return int(os.environ.get("SI_READ_MAX_BYTES", str(50 * 1024)))
    except (ValueError, TypeError):
        return 50 * 1024


def get_read_max_line_length() -> int:
    """Max characters per line in read tool output."""
    try:
        return int(os.environ.get("SI_READ_MAX_LINE_LENGTH", "2000"))
    except (ValueError, TypeError):
        return 2000


def get_report_max_tokens() -> int:
    """Max input tokens for report generation."""
    try:
        return int(os.environ.get("SI_REPORT_MAX_TOKENS", "4000"))
    except (ValueError, TypeError):
        return 4000


def get_ws_ping_timeout() -> int:
    """WebSocket ping timeout in seconds."""
    try:
        return int(os.environ.get("SI_WS_PING_TIMEOUT", "30"))
    except (ValueError, TypeError):
        return 30
```

- [ ] **Step 2: Update `tools/read.py` to use config**

Replace lines 7-9:
```python
MAX_LINES = 2000
MAX_LINE_LENGTH = 2000
MAX_BYTES = 50 * 1024  # 50KB
```

With:
```python
from smartinspector.config import get_read_max_lines, get_read_max_bytes, get_read_max_line_length

# Module-level constants loaded from config (set once at import time)
MAX_LINES = get_read_max_lines()
MAX_LINE_LENGTH = get_read_max_line_length()
MAX_BYTES = get_read_max_bytes()
```

- [ ] **Step 3: Update `tools/grep.py` to use config**

Add import at the top:
```python
from smartinspector.config import get_tool_timeout
```

Replace line 51 (`timeout=30`):
```python
        result = subprocess.run(
            args, capture_output=True, text=True, timeout=get_tool_timeout(),
        )
```

Replace line 54:
```python
        return f"Error: search timed out after {get_tool_timeout()}s."
```

- [ ] **Step 4: Update `tools/glob.py` to use config**

Add import at the top:
```python
from smartinspector.config import get_tool_timeout
```

Replace line 43 (`timeout=30`):
```python
        result = subprocess.run(
            args, capture_output=True, text=True, timeout=get_tool_timeout(),
        )
```

Replace line 48:
```python
        return f"Error: search timed out after {get_tool_timeout()}s."
```

- [ ] **Step 5: Update `reporter/__init__.py` to use config**

Add import at the top:
```python
from smartinspector.config import get_report_max_tokens
```

Replace line 62 (`MAX_REPORT_INPUT_TOKENS = 4000`):
```python
    MAX_REPORT_INPUT_TOKENS = get_report_max_tokens()
```

(Move it inside the function body, right before line 63.)

- [ ] **Step 6: Update `ws/server.py` to use config**

Add import at the top:
```python
from smartinspector.config import get_ws_ping_timeout
```

Replace line 194 (`ping_timeout=30`):
```python
                ping_timeout=get_ws_ping_timeout(),
```

- [ ] **Step 7: Run existing tests**

Run: `cd /Users/liujun/langchainProjects/smartinspector && python -m pytest tests/ -v`
Expected: All existing tests pass.

- [ ] **Step 8: Commit**

```bash
git add src/smartinspector/config.py src/smartinspector/tools/read.py src/smartinspector/tools/grep.py src/smartinspector/tools/glob.py src/smartinspector/graph/nodes/reporter/__init__.py src/smartinspector/ws/server.py
git commit -m "refactor: centralize hardcoded config values with env var overrides"
```

---

## Task 4: Add Resource context manager to PerfettoCollector

**Files:**
- Modify: `src/smartinspector/collector/perfetto.py`

**Problem:** `PerfettoCollector` has a `close()` method but doesn't implement the context manager protocol (`__enter__`/`__exit__`). Callers must manually call `close()`, leading to resource leaks if they forget.

- [ ] **Step 1: Add `__enter__` and `__exit__` methods to `PerfettoCollector`**

Add these methods right after the existing `close()` method (after line 83):

```python
    def __enter__(self):
        return self

    def __exit__(self, exc_type, exc_val, exc_tb):
        self.close()
        return False
```

- [ ] **Step 2: Verify by running existing tests**

Run: `cd /Users/liujun/langchainProjects/smartinspector && python -m pytest tests/test_collector.py -v`
Expected: All tests pass.

- [ ] **Step 3: Commit**

```bash
git add src/smartinspector/collector/perfetto.py
git commit -m "feat(collector): add context manager protocol to PerfettoCollector"
```

---

## Task 5: Fix WebSocket server potential race condition

**Files:**
- Modify: `src/smartinspector/ws/server.py`

**Problem:** In `SIServer.start()`, the thread starts at line 68 and then `self._thread.join(timeout=0.5)` is called to check for startup errors. However, the `_loop` and `_server` are set inside the thread's `_run_loop()`. If `is_running()` is called between thread start and `_run_loop` completing initialization, `_loop` may be `None`.

The real fix is minor: ensure `_started_event` is used to signal that the server is ready, so callers don't access uninitialized state.

- [ ] **Step 1: Add a ready event to `SIServer.__init__`**

In `__init__` (line 38), add a ready event:
```python
        self._ready_event = threading.Event()
```

- [ ] **Step 2: Set the ready event in `_run_loop` after server starts**

In `_run_loop` (line 188), inside `_serve()`, set the event after `websockets.serve` succeeds:

```python
        async def _serve():
            self._server = await websockets.serve(
                self._handler,
                "0.0.0.0",
                self.port,
                ping_interval=20,
                ping_timeout=get_ws_ping_timeout(),
            )
            self._ready_event.set()
            await asyncio.Future()  # run forever
```

- [ ] **Step 3: Update `start()` to use the ready event**

Replace the `start()` method:
```python
    def start(self) -> None:
        """Start the WS server in a background daemon thread."""
        if self.is_running():
            return
        if websockets is None:
            print("  websockets not installed. Run: uv add websockets")
            return

        self._ready_event.clear()
        self._thread = threading.Thread(target=self._run_loop, daemon=True)
        self._thread.start()
        # Wait for server to be ready (or thread to die)
        if self._ready_event.wait(timeout=2.0):
            print(f"  WS server started on port {self.port}")
        elif self._thread.is_alive():
            print(f"  WS server starting on port {self.port} (still initializing)")
        else:
            print(f"  WS server FAILED to start on port {self.port} (check port conflict)")
```

- [ ] **Step 4: Run tests**

Run: `cd /Users/liujun/langchainProjects/smartinspector && python -m pytest tests/ -v`
Expected: All tests pass.

- [ ] **Step 5: Commit**

```bash
git add src/smartinspector/ws/server.py
git commit -m "fix(ws): add ready event to prevent race condition on server startup"
```

---

## Task 6: Extract duplicate `_validate_search_path` to shared module

**Files:**
- Create: `src/smartinspector/tools/path_utils.py`
- Modify: `src/smartinspector/tools/grep.py`
- Modify: `src/smartinspector/tools/glob.py`

**Problem:** `grep.py` and `glob.py` contain identical `_validate_search_path()` functions (lines 11-17 in both files).

- [ ] **Step 1: Create `src/smartinspector/tools/path_utils.py`**

```python
"""Shared path validation utilities for tools."""

import os


def validate_search_path(path: str) -> str | None:
    """Validate and resolve search path. Returns resolved path or None if invalid."""
    parts = path.replace("\\", "/").split("/")
    if ".." in parts:
        return None
    return os.path.realpath(path)
```

- [ ] **Step 2: Update `tools/grep.py`**

Remove the `_validate_search_path` function (lines 11-17) and replace the import section:

Add:
```python
from smartinspector.tools.path_utils import validate_search_path
```

Replace `_validate_search_path(path)` call on line 33 with `validate_search_path(path)`.

- [ ] **Step 3: Update `tools/glob.py`**

Remove the `_validate_search_path` function (lines 11-17) and replace the import section:

Add:
```python
from smartinspector.tools.path_utils import validate_search_path
```

Replace `_validate_search_path(path)` call on line 33 with `validate_search_path(path)`.

- [ ] **Step 4: Run tests**

Run: `cd /Users/liujun/langchainProjects/smartinspector && python -m pytest tests/ -v`
Expected: All tests pass.

- [ ] **Step 5: Commit**

```bash
git add src/smartinspector/tools/path_utils.py src/smartinspector/tools/grep.py src/smartinspector/tools/glob.py
git commit -m "refactor(tools): extract shared path validation to path_utils module"
```

---

## Task 7: Fix silent exception swallowing in ws/server.py

**Files:**
- Modify: `src/smartinspector/ws/server.py`

**Problem:** Two places in `ws/server.py` silently swallow exceptions: `_persist_config` (line 169) and `_load_cached_config` (line 179).

- [ ] **Step 1: Add logging import and module logger**

Add at the top of `ws/server.py`:
```python
import logging

logger = logging.getLogger(__name__)
```

- [ ] **Step 2: Replace silent exception in `_persist_config`**

Replace:
```python
    def _persist_config(self, config_json: str) -> None:
        """Save config to local cache file."""
        try:
            _CONFIG_PATH.write_text(config_json)
        except Exception:
            pass
```

With:
```python
    def _persist_config(self, config_json: str) -> None:
        """Save config to local cache file."""
        try:
            _CONFIG_PATH.write_text(config_json)
        except OSError as e:
            logger.debug("Failed to persist config: %s", e)
```

- [ ] **Step 3: Replace silent exception in `_load_cached_config`**

Replace:
```python
    @staticmethod
    def _load_cached_config() -> str:
        """Load config from local cache file."""
        try:
            if _CONFIG_PATH.exists():
                return _CONFIG_PATH.read_text()
        except Exception:
            pass
        return ""
```

With:
```python
    @staticmethod
    def _load_cached_config() -> str:
        """Load config from local cache file."""
        try:
            if _CONFIG_PATH.exists():
                return _CONFIG_PATH.read_text()
        except OSError as e:
            logger.debug("Failed to load cached config: %s", e)
        return ""
```

- [ ] **Step 4: Run tests**

Run: `cd /Users/liujun/langchainProjects/smartinspector && python -m pytest tests/ -v`
Expected: All tests pass.

- [ ] **Step 5: Commit**

```bash
git add src/smartinspector/ws/server.py
git commit -m "fix(ws): replace silent exception swallowing with debug logging"
```

---

## Dependency Graph

```
Task 1 (collector logging)     — independent
Task 2 (agent thread safety)   — independent
Task 3 (centralize config)     — Task 5 depends on this (ws/server.py imports from config)
Task 4 (context manager)       — independent
Task 5 (ws race condition)     — depends on Task 3
Task 6 (path utils)            — independent
Task 7 (ws logging)            — independent, but apply after Task 5 to avoid conflicts on same file
```

Recommended execution order: Task 1 → Task 6 → Task 4 → Task 2 → Task 3 → Task 5 → Task 7
