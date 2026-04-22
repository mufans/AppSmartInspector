"""Attributor agent: read-only source code attribution via Glob->Grep->Read.

Receives a list of attributable SI$ slices, searches source code using
a precise strategy:
  1. Glob to locate file path by class name
  2. Grep to find method signature line number
  3. Read(offset, limit=40) to read only the method body

Slices whose files cannot be found are marked as system classes.
"""

import json
import os
import threading
from collections import OrderedDict

from pydantic import BaseModel

from langchain_core.messages import SystemMessage, HumanMessage, AIMessage, ToolMessage
from langchain_openai import ChatOpenAI

from smartinspector.config import get_llm_kwargs, get_source_dir
from smartinspector.debug_log import debug_log
from smartinspector.tools.grep import grep
from smartinspector.tools.glob import glob
from smartinspector.tools.read import read
from smartinspector.prompts import load_prompt
from smartinspector.token_tracker import get_tracker


class AttributionResult(BaseModel):
    """Single attribution result."""
    class_method: str
    status: str  # "found" | "system_class" | "not_found"
    file_path: str | None = None
    line_start: int | None = None
    line_end: int | None = None
    finding: str = ""


class AttributionResponse(BaseModel):
    """Structured response from attributor LLM."""
    results: list[AttributionResult]

# Tool name -> callable
_TOOLS = {
    "grep": grep,
    "glob": glob,
    "read": read,
}

_llm_with_tools = None
_system_prompt = None
_structured_llm = None
_structured_ok: bool | None = None  # None = not tested yet
_llm_lock = threading.Lock()


# ---------------------------------------------------------------------------
# LRU file cache — avoids re-reading files across groups and iterations
# ---------------------------------------------------------------------------

_FILE_CACHE_MAX = 32


class _FileCache:
    """Simple LRU cache keyed by (tool_name, frozenset of args items).

    Caches glob and read results so that the same file is not fetched
    multiple times across groups or iterations.
    """

    def __init__(self, maxsize: int = _FILE_CACHE_MAX):
        self._maxsize = maxsize
        self._cache: OrderedDict = OrderedDict()

    def get(self, tool_name: str, args: dict) -> str | None:
        key = self._make_key(tool_name, args)
        if key in self._cache:
            self._cache.move_to_end(key)
            return self._cache[key]
        return None

    def put(self, tool_name: str, args: dict, result: str) -> None:
        key = self._make_key(tool_name, args)
        if key in self._cache:
            self._cache.move_to_end(key)
        self._cache[key] = result
        while len(self._cache) > self._maxsize:
            self._cache.popitem(last=False)

    def clear(self) -> None:
        self._cache.clear()

    @staticmethod
    def _make_key(tool_name: str, args: dict) -> tuple:
        return (tool_name, tuple(sorted(args.items())))


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
        # Test structured output support — some providers (e.g. DeepSeek) don't
        # support response_format, so we probe once and cache the result.
        _structured_llm = llm.with_structured_output(AttributionResponse)
        _system_prompt = load_prompt("attributor")
        # Probe structured output support with a realistic multi-turn request.
        # A trivial "test" message can succeed on some providers that then fail
        # on real multi-turn tool-call conversations (e.g. DeepSeek returns
        # "This response_format type is unavailable now" intermittently).
        global _structured_ok
        try:
            probe_messages = [
                SystemMessage(content=_system_prompt),
                HumanMessage(content="1. TestClass.testMethod (10.00ms, java)\n\n按 Glob→Grep→Read 搜索，输出 RESULT 行。"),
                AIMessage(content="RESULT: TestClass.testMethod | found | /tmp/Test.java | 1-5 | test"),
            ]
            _structured_llm.invoke(probe_messages)
            _structured_ok = True
        except Exception as e:
            _structured_ok = False
            debug_log("attributor", f"structured output not supported ({e}), will use text parsing fallback")
    return _llm_with_tools, _system_prompt


# ---------------------------------------------------------------------------
# Deterministic fast path — skip LLM for straightforward searches
# ---------------------------------------------------------------------------

def _can_use_fast_path(group: list[dict]) -> bool:
    """Check if all issues in a group can be resolved deterministically.

    Fast path conditions:
      - All issues are java type (not xml)
      - No anonymous inner classes ($ in class_name)
      - Method name is known (not "unknown" or empty)
    """
    for issue in group:
        if issue.get("search_type") != "java":
            return False
        cn = issue.get("class_name", "")
        if "$" in cn:
            return False
        mn = issue.get("method_name", "")
        if not mn or mn == "unknown":
            return False
    return True


def _deterministic_search(group: list[dict], file_cache: _FileCache) -> list[dict]:
    """Execute Glob→Grep→Read without LLM for straightforward cases.

    Returns result dicts in the same format as _search_group().
    """
    results: list[dict] = []

    for issue in group:
        result = {
            "raw_name": issue["raw_name"],
            "class_name": issue["class_name"],
            "method_name": issue["method_name"],
            "dur_ms": issue["dur_ms"],
            "attributable": False,
            "reason": "not_found",
            "file_path": None,
            "line_start": None,
            "line_end": None,
            "source_snippet": None,
        }
        if issue.get("instance"):
            result["instance"] = issue["instance"]
        if issue.get("count"):
            result["count"] = issue["count"]
        if issue.get("total_ms"):
            result["total_ms"] = issue["total_ms"]

        cn = issue["class_name"]
        mn = issue["method_name"]
        source_dir = get_source_dir()

        # Step 1: Glob to find the file
        glob_args_java = {"pattern": f"**/{cn}.java", "path": source_dir}
        glob_args_kt = {"pattern": f"**/{cn}.kt", "path": source_dir}

        # Check cache first
        glob_result = file_cache.get("glob", glob_args_java)
        if glob_result is None:
            glob_result = file_cache.get("glob", glob_args_kt)
            if glob_result is None:
                # Try .java first
                glob_result = glob.invoke(glob_args_java)
                if glob_result.startswith("No files"):
                    # Try .kt
                    glob_result_kt = glob.invoke(glob_args_kt)
                    if not glob_result_kt.startswith("No files"):
                        glob_result = glob_result_kt
                        file_cache.put("glob", glob_args_kt, glob_result)
                    else:
                        file_cache.put("glob", glob_args_java, glob_result)
                else:
                    file_cache.put("glob", glob_args_java, glob_result)

        if glob_result.startswith("No files") or glob_result.startswith("Error"):
            result["reason"] = "system_class"
            results.append(result)
            continue

        # Parse first file path from glob result (skip header lines)
        file_path = None
        for line in glob_result.split("\n"):
            line = line.strip()
            if not line or line.startswith("Found") or line.startswith("(") or line.startswith("["):
                continue
            # Convert relative path to absolute
            if not line.startswith("/"):
                import os
                line = os.path.join(source_dir, line)
            file_path = line
            break

        if not file_path:
            result["reason"] = "system_class"
            results.append(result)
            continue

        result["file_path"] = file_path

        # Step 2: Grep for method signature
        grep_args = {
            "pattern": mn,
            "path": file_path,
            "output_mode": "content",
            "head_limit": 5,
        }
        grep_result = grep.invoke(grep_args)

        if grep_result.startswith("No matches") or grep_result.startswith("Error"):
            # Method not found in file — still mark file as found but method as not_found
            result["reason"] = "found_file_only"
            results.append(result)
            continue

        # Parse first matching line number
        line_start = None
        for line in grep_result.split("\n"):
            line = line.strip()
            if not line or line.startswith("Found") or line.startswith("("):
                continue
            parts = line.split(":", 2)
            if len(parts) >= 2:
                try:
                    line_start = int(parts[1])
                    break
                except ValueError:
                    continue

        if line_start is None:
            results.append(result)
            continue

        # Step 3: Read method body (offset=line_start, limit=40)
        read_args = {"file_path": file_path, "offset": line_start, "limit": 40}
        cached_read = file_cache.get("read", read_args)
        if cached_read is not None:
            read_result = cached_read
        else:
            read_result = read.invoke(read_args)
            if not str(read_result).startswith("Error"):
                file_cache.put("read", read_args, str(read_result))

        # Extract source snippet from read result (strip line numbers)
        snippet_lines = []
        end_line = line_start
        for line in str(read_result).split("\n"):
            line = line.strip()
            if line.startswith("(") or not line:
                continue
            # Format: "NN: content" — strip the line number prefix
            colon_idx = line.find(": ")
            if colon_idx >= 0:
                snippet_lines.append(line[colon_idx + 2:])
                try:
                    end_line = int(line[:colon_idx].strip())
                except ValueError:
                    pass

        snippet = "\n".join(snippet_lines[:40])

        result.update({
            "attributable": True,
            "reason": "found",
            "file_path": file_path,
            "line_start": line_start,
            "line_end": end_line,
            "source_snippet": snippet,
        })
        print(f"    [fast-path] {cn}.{mn} -> {file_path}:{line_start}", flush=True)
        results.append(result)

    return results


def run_attribution(attributable: list[dict], on_progress=None) -> list[dict]:
    """Run source code attribution on a list of SI$ slices.

    Args:
        attributable: List of dicts from extract_attributable_slices().
                      Each must have: class_name, method_name, dur_ms,
                      raw_name, search_type.

    Returns:
        List of attribution result dicts with fields:
          - raw_name, class_name, method_name, dur_ms (from input)
          - attributable: bool — whether source was found
          - reason: str — "found" / "system_class" / "error"
          - file_path: str or None
          - line_start: int or None
          - line_end: int or None
          - source_snippet: str or None — method body text
    """
    from smartinspector.commands.attribution import group_issues_by_file

    if not attributable:
        return []

    # Shared file cache across all groups in this run
    file_cache = _FileCache()

    # Decide: all-at-once (<=2 issues) or grouped (>2 issues)
    if len(attributable) <= 2:
        groups = [attributable]
    else:
        groups = group_issues_by_file(attributable)

    results: list[dict] = []

    for group in groups:
        # Fast path: deterministic search for straightforward cases
        if _can_use_fast_path(group):
            fast_results = _deterministic_search(group, file_cache)
            if all(r.get("reason") == "found" for r in fast_results):
                results.extend(fast_results)
                continue
            # Partial success: merge found results, fall back to LLM for rest
            failed_issues = []
            for r, issue in zip(fast_results, group):
                if r.get("reason") == "found":
                    results.append(r)
                else:
                    failed_issues.append(issue)
            if failed_issues:
                llm_results = _search_group(failed_issues, file_cache, on_progress)
                results.extend(llm_results)
            continue

        group_results = _search_group(group, file_cache, on_progress)
        results.extend(group_results)

    # Sort by dur_ms descending
    results.sort(key=lambda x: -x.get("dur_ms", 0))
    return results


def _search_group(group: list[dict], file_cache: _FileCache, on_progress=None) -> list[dict]:
    """Search source code for a group of issues using manual tool-call loop.

    Uses llm.bind_tools() + manual tool dispatch to avoid message history
    accumulation that causes O(n^2) token growth in agent frameworks.

    Args:
        group: List of issues sharing the same target file/class.
        file_cache: Shared LRU cache for glob/read results across groups.
    """
    global _structured_ok
    results: list[dict] = []

    # Base result template for each issue
    for issue in group:
        result = {
            "raw_name": issue["raw_name"],
            "class_name": issue["class_name"],
            "method_name": issue["method_name"],
            "dur_ms": issue["dur_ms"],
            "attributable": False,
            "reason": "pending",
            "file_path": None,
            "line_start": None,
            "line_end": None,
            "source_snippet": None,
        }
        if issue.get("instance"):
            result["instance"] = issue["instance"]
        if issue.get("count"):
            result["count"] = issue["count"]
        if issue.get("total_ms"):
            result["total_ms"] = issue["total_ms"]
        if issue.get("context_method"):
            result["context_method"] = issue["context_method"]
        results.append(result)

    # Validate source_dir exists before entering expensive LLM loop
    from smartinspector.config import get_source_dir
    source_dir = get_source_dir()
    resolved = os.path.realpath(source_dir)
    if not os.path.isdir(resolved):
        debug_log("attributor", f"source_dir '{source_dir}' resolves to '{resolved}' which does not exist")
        for r in results:
            r["reason"] = "source_dir_not_found"
        return results

    # Build prompt for the agent
    prompt = _build_group_prompt(group)
    llm, system_prompt = _get_llm()

    try:
        # Manual tool-call loop: each iteration sends only the current messages
        messages = [
            SystemMessage(content=system_prompt),
            HumanMessage(content=prompt),
        ]

        max_iterations = 8  # Safety limit
        consecutive_failures = 0
        for iteration in range(max_iterations):
            # Message window trimming: keep system(0) + human(1) + recent 6 rounds
            # Each round = 1 AIMessage + 1 ToolMessage = 2 messages
            if len(messages) > 16:
                messages = [messages[0], messages[1]] + messages[-12:]

            debug_log("attributor", f"iteration {iteration}: invoking LLM ({len(messages)} messages)...")
            response = llm.invoke(messages)
            debug_log("attributor", f"iteration {iteration}: LLM responded")

            # Record token usage
            get_tracker().record_from_message("attributor", response)

            # Check if LLM wants to call tools
            tool_calls = response.tool_calls if hasattr(response, "tool_calls") else []
            if not tool_calls:
                # No more tool calls — LLM is done
                debug_log("attributor", f"iteration {iteration}: no tool calls, done")
                messages.append(response)
                break

            debug_log("attributor", f"iteration {iteration}: {len(tool_calls)} tool calls: {[tc['name'] for tc in tool_calls]}")
            print(f"  [attributor] iteration {iteration}: {[tc['name'] for tc in tool_calls]}", flush=True)
            if on_progress:
                on_progress(f"  [attributor] iteration {iteration}: {[tc['name'] for tc in tool_calls]}")

            # Add AI message with tool calls
            messages.append(response)

            # Execute each tool call
            for tc in tool_calls:
                tool_name = tc["name"]
                tool_args = tc["args"]
                tool_fn = _TOOLS.get(tool_name)

                # Check cache for glob and read operations
                if tool_name in ("glob", "read"):
                    cached = file_cache.get(tool_name, tool_args)
                    if cached is not None:
                        tool_result = cached
                        args_preview = ", ".join(f"{k}={v!r}" for k, v in tool_args.items() if isinstance(v, (str, int)) and len(str(v)) < 80)
                        debug_log("attributor", f"  [{tool_name}] (cached) {args_preview or '(no args)'}")
                        print(f"    [{tool_name}] (cached) {args_preview or '(no args)'}", flush=True)
                        if on_progress:
                            on_progress(f"  [attributor]   [{tool_name}] (cached) {args_preview or '(no args)'}")
                        messages.append(ToolMessage(
                            content=str(tool_result),
                            tool_call_id=tc["id"],
                            name=tool_name,
                        ))
                        continue

                if not tool_fn:
                    tool_result = f"Error: unknown tool {tool_name}"
                elif tool_name == "read" and not tool_args.get("file_path", "").strip():
                    tool_result = "Error: file_path is required and cannot be empty. You must first use glob or grep to find the file path, then pass it to read()."
                else:
                    try:
                        tool_result = tool_fn.invoke(tool_args)
                    except Exception as e:
                        tool_result = f"Error: {e}"

                # Cache glob and read results
                if tool_name in ("glob", "read") and not str(tool_result).startswith("Error:"):
                    file_cache.put(tool_name, tool_args, str(tool_result))

                # Log tool call
                args_preview = ", ".join(f"{k}={v!r}" for k, v in tool_args.items() if isinstance(v, (str, int)) and len(str(v)) < 80)
                if not args_preview:
                    args_preview = "(no args)"
                debug_log("attributor", f"  [{tool_name}] {args_preview}")
                print(f"    [{tool_name}] {args_preview}", flush=True)
                if on_progress:
                    on_progress(f"  [attributor]   [{tool_name}] {args_preview}")

                # Add tool result to messages
                messages.append(ToolMessage(
                    content=str(tool_result),
                    tool_call_id=tc["id"],
                    name=tool_name,
                ))

                # Track consecutive search failures for early termination
                result_str = str(tool_result)
                if tool_name in ("glob", "grep") and ("No files found" in result_str or not result_str.strip()):
                    consecutive_failures += 1
                else:
                    consecutive_failures = 0

            # Early termination on repeated search failures
            if consecutive_failures >= 3:
                debug_log("attributor", f"early termination: {consecutive_failures} consecutive search failures")
                break

        # Scan ALL messages for RESULT lines
        all_text = ""
        for msg in messages:
            content = getattr(msg, "content", "")
            if content:
                all_text += "\n" + content

        debug_log("attributor", f"group prompt: {prompt[:300]}")
        debug_log("attributor", f"all_text (last 1500 chars): {all_text[-1500:]}")

        # Try structured output first, fall back to text parsing
        structured_ok = False
        if _structured_ok and _structured_llm is not None and all_text:
            try:
                structured = _structured_llm.invoke(messages)
                debug_log("attributor", f"structured results: {[sr.model_dump() for sr in structured.results]}")
                for sr in structured.results:
                    for r in results:
                        if _match_result(r, sr.class_method):
                            if sr.status == "found":
                                r.update({
                                    "attributable": True,
                                    "reason": "found",
                                    "file_path": sr.file_path,
                                    "line_start": sr.line_start,
                                    "line_end": sr.line_end,
                                    "source_snippet": sr.finding,
                                })
                            elif sr.status == "system_class":
                                r["reason"] = "system_class"
                            break
                structured_ok = True
            except Exception as e:
                debug_log("attributor", f"structured output failed: {e}")
                # Permanently disable structured output after first failure
                # to avoid repeated errors across groups
                _structured_ok = False

        if not structured_ok and all_text:
            _parse_agent_response(all_text, results)

        # Mark any still-pending results as parse_failed
        for r in results:
            if r.get("reason") == "pending":
                r["reason"] = "parse_failed"
    except Exception as e:
        for r in results:
            r["reason"] = f"error: {e}"

    # Normalize file paths to relative from source dir
    from smartinspector.config import get_source_dir
    source_dir = get_source_dir()
    for r in results:
        fp = r.get("file_path")
        if fp and fp.startswith(source_dir):
            r["file_path"] = fp[len(source_dir):].lstrip("/")

    return results


def _build_group_prompt(group: list[dict]) -> str:
    """Build a search prompt for one group of issues."""
    from smartinspector.config import get_source_dir

    source_dir = get_source_dir()

    lines = [
        f"源码目录: {source_dir}\n",
    ]

    for i, issue in enumerate(group, 1):
        search_type = issue.get("search_type", "java")
        cn = issue["class_name"]
        line = f"{i}. {cn}.{issue['method_name']} ({issue['dur_ms']:.2f}ms, {search_type}"
        if issue.get("count"):
            line += f", count={issue['count']}"

        # ── 调用栈上下文 ──
        call_ctx = issue.get("call_context", "")
        if call_ctx:
            line += f", 调用链: {call_ctx}"

        # Hint for inner classes ($ in name) — extract outer class for Glob
        if "$" in cn:
            outer = cn.split("$")[0]
            line += f", 内部类:用Glob搜索外部类 {outer}"
            line += f", RESULT行请用完整类名: {cn}.{issue['method_name']}"
        # Append BlockMonitor stack trace if available
        if issue.get("stack_trace"):
            line += f", 堆栈:{issue['stack_trace'][0]}"
        # Hint for XML layout files — search .xml directly, not .java/.kt
        if search_type == "xml":
            line += f", xml布局:Glob **/{cn}.xml → Read完整文件, RESULT行请用: {cn}.{issue['method_name']}"
        line += ")"
        lines.append(line)

    # Hint: multiple methods share the same file, read once
    if len(group) > 1:
        class_names = set(issue["class_name"] for issue in group)
        if len(class_names) == 1:
            lines.append(f"\n注意: 上述 {len(group)} 个方法属于同一个类，只需 Glob 一次定位文件，Grep 各方法的行号后分别 Read。")

    lines.append("\n按 Glob→Grep→Read 搜索，输出 RESULT 行。")

    return "\n".join(lines)


def _match_result(result: dict, class_method: str) -> bool:
    """Check if a LLM-returned class_method string matches a result entry.

    Handles edge cases:
      - method_name is "unknown"/empty -> match by class_name only
      - inner classes ($ in class_name) -> match outer class name
      - LLM uses partial class name -> match by method name suffix
    """
    r_cls = result["class_name"]
    r_mtd = result["method_name"]

    # Exact match: "ClassName.method"
    if f"{r_cls}.{r_mtd}" == class_method:
        return True

    # method_name is unknown/empty: LLM may return "ClassName" or "ClassName.run"
    if r_mtd in ("unknown", ""):
        if class_method == r_cls or class_method.startswith(r_cls + "."):
            return True
        # Inner class: LLM may use outer class name
        if "$" in r_cls:
            outer = r_cls.split("$")[0]
            if class_method == outer or class_method.startswith(outer + "."):
                return True

    # Inner class fallback: LLM may output "OuterClass.method"
    if "$" in r_cls and class_method == f"{r_cls.split('$')[0]}.{r_mtd}":
        return True

    # LLM returned just the class_name without method (e.g. "item_complex")
    if class_method == r_cls:
        return True

    # Method name match: LLM may use partial class name
    if r_mtd and r_mtd not in ("unknown", "") and r_mtd in class_method and class_method.endswith(r_mtd):
        return True

    return False


def _parse_agent_response(response_text: str, results: list[dict]) -> None:
    """Parse structured RESULT lines from agent response into result dicts."""
    for line in response_text.split("\n"):
        line = line.strip()
        # Strip markdown bold markers
        clean = line.lstrip("*").rstrip("*").strip()
        if not clean.startswith("RESULT:"):
            continue

        parts = [p.strip() for p in clean[7:].split("|")]
        if len(parts) < 4:
            continue

        class_method = parts[0]
        status = parts[1]
        file_path = parts[2] if parts[2] != "None" else None
        line_range = parts[3] if parts[3] != "None" else None
        finding = parts[4].strip() if len(parts) > 4 else ""

        # Match to the corresponding result
        for r in results:
            if not _match_result(r, class_method):
                continue

            if status == "found":
                r["attributable"] = True
                r["reason"] = "found"
                r["file_path"] = file_path
                if line_range and "-" in line_range:
                    try:
                        start_s, end_s = line_range.split("-", 1)
                        r["line_start"] = int(start_s.strip())
                        r["line_end"] = int(end_s.strip())
                    except ValueError:
                        pass
                r["source_snippet"] = finding
            elif status == "system_class":
                r["attributable"] = False
                r["reason"] = "system_class"
            break
