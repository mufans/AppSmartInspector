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
        if issue.get("io_type"):
            result["io_type"] = issue["io_type"]

        cn = issue["class_name"]
        mn = issue["method_name"]
        context_method = issue.get("context_method", "")
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
            debug_log("attributor", f"  [fast-path] {cn}.{mn} -> system_class (glob: {glob_result[:60]})")
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
        # When context_method is set (anonymous inner class like Runnable.run inside
        # startMainThreadWork), search for the context_method to locate the enclosing
        # method — the actual performance-relevant code is inside it, not in a generic
        # method like "run".
        search_method = context_method if context_method else mn
        grep_args = {
            "pattern": search_method,
            "path": file_path,
            "output_mode": "content",
            "head_limit": 5,
        }
        grep_result = grep.invoke(grep_args)

        if grep_result.startswith("No matches") or grep_result.startswith("Error"):
            # If context_method search failed, try the original method name
            if context_method:
                grep_args["pattern"] = mn
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

        # Store context_method in result for downstream consumers
        if context_method:
            result["context_method"] = context_method

        result.update({
            "attributable": True,
            "reason": "found",
            "file_path": file_path,
            "line_start": line_start,
            "line_end": end_line,
            "source_snippet": snippet,
            "_fast_path": True,
        })
        search_desc = f"{cn}.{mn}"
        if context_method:
            search_desc += f" (via context_method={context_method})"
        debug_log("attributor", f"  [fast-path] {search_desc} -> {file_path}:{line_start}")
        results.append(result)

    return results


# ---------------------------------------------------------------------------
# P1-4: Dependency reference search — enrich context with related files
# ---------------------------------------------------------------------------

import re as _re

# Patterns for extracting dependency references from source files
_IMPORT_RE = _re.compile(r'^\s*import\s+([\w.]+)\s*;', _re.MULTILINE)
_R_LAYOUT_RE = _re.compile(r'R\.(?:layout)\.(\w+)')
_R_ID_RE = _re.compile(r'R\.id\.(\w+)')
_SET_CONTENT_VIEW_RE = _re.compile(r'setContentView\s*\(\s*R\.layout\.(\w+)')


def _extract_project_imports(source: str, source_dir: str) -> list[str]:
    """Extract import statements that refer to project-internal classes.

    Filters out android.*, java.*, kotlin.*, androidx.*, com.google.*
    and other standard library imports.
    """
    std_prefixes = (
        "android.", "androidx.", "java.", "javax.", "kotlin.",
        "kotlinx.", "com.google.", "com.android.", "dalvik.",
        "org.intellij.", "org.jetbrains.",
    )
    project_classes: list[str] = []
    for m in _IMPORT_RE.finditer(source):
        fqn = m.group(1)
        if fqn.startswith(std_prefixes):
            continue
        # Extract simple class name from FQN
        simple_name = fqn.rsplit(".", 1)[-1]
        # Skip inner class references ($)
        if "$" in simple_name:
            simple_name = simple_name.split("$")[0]
        project_classes.append(simple_name)
    return project_classes


def _extract_layout_refs(source: str) -> list[str]:
    """Extract XML layout file names referenced via R.layout.xxx."""
    layouts: list[str] = []
    seen: set[str] = set()
    for m in _R_LAYOUT_RE.finditer(source):
        name = m.group(1)
        if name not in seen:
            seen.add(name)
            layouts.append(name)
    return layouts


def _enrich_with_dependencies(results: list[dict], file_cache: _FileCache) -> None:
    """Enrich found results with dependency context: project imports and XML layouts.

    For each result with a found file, reads the full file, extracts:
      - Project-internal imports → search for those class files → read relevant snippets
      - R.layout.xxx references → search for XML layout files → read relevant content

    Appends the dependency context to each result's ``dependency_context`` field.
    """
    from smartinspector.config import get_source_dir as _get_source_dir

    source_dir = _get_source_dir()
    if not source_dir or not os.path.isdir(source_dir):
        return

    # Process each found result (limit to top 10 to avoid excessive reads)
    found_results = [r for r in results if r.get("attributable") and r.get("file_path")]
    for r in found_results[:10]:
        file_path = r["file_path"]
        if not os.path.isabs(file_path):
            file_path = os.path.join(source_dir, file_path)

        # Read the full source file to extract imports and layout refs
        full_read_args = {"file_path": file_path, "offset": 1, "limit": 200}
        cached = file_cache.get("read", full_read_args)
        if cached is not None:
            full_source = cached
        else:
            full_source = read.invoke(full_read_args)
            if str(full_source).startswith("Error"):
                continue
            file_cache.put("read", full_read_args, str(full_source))

        full_source_str = str(full_source)

        # Extract dependency references
        project_imports = _extract_project_imports(full_source_str, source_dir)
        layout_refs = _extract_layout_refs(full_source_str)

        dep_parts: list[str] = []

        # Resolve project imports — glob for each class and read first few lines
        for class_name in project_imports[:5]:  # limit to 5 imports
            for ext in (".java", ".kt"):
                glob_args = {"pattern": f"**/{class_name}{ext}", "path": source_dir}
                glob_result = file_cache.get("glob", glob_args)
                if glob_result is None:
                    glob_result = glob.invoke(glob_args)
                    if not glob_result.startswith("No files") and not glob_result.startswith("Error"):
                        file_cache.put("glob", glob_args, glob_result)
                if glob_result.startswith("No files") or glob_result.startswith("Error"):
                    continue

                # Parse first file path
                dep_file = None
                for line in glob_result.split("\n"):
                    line = line.strip()
                    if not line or line.startswith("Found") or line.startswith("(") or line.startswith("["):
                        continue
                    if not line.startswith("/"):
                        line = os.path.join(source_dir, line)
                    dep_file = line
                    break

                if dep_file:
                    # Read first 30 lines (class declaration + key fields)
                    dep_read_args = {"file_path": dep_file, "offset": 1, "limit": 30}
                    dep_content = file_cache.get("read", dep_read_args)
                    if dep_content is None:
                        dep_content = read.invoke(dep_read_args)
                        if not str(dep_content).startswith("Error"):
                            file_cache.put("read", dep_read_args, str(dep_content))
                    if not str(dep_content).startswith("Error"):
                        # Extract clean lines
                        clean_lines = []
                        for ln in str(dep_content).split("\n"):
                            ln = ln.strip()
                            if ln.startswith("(") or not ln:
                                continue
                            colon_idx = ln.find(": ")
                            if colon_idx >= 0:
                                clean_lines.append(ln[colon_idx + 2:])
                        if clean_lines:
                            short_path = dep_file
                            if short_path.startswith(source_dir):
                                short_path = short_path[len(source_dir):].lstrip("/")
                            dep_parts.append(f"[关联类] {class_name} -> {short_path}\n" + "\n".join(clean_lines[:20]))
                    break  # found .java or .kt, no need to try other extension

        # Resolve XML layout references
        for layout_name in layout_refs[:3]:  # limit to 3 layouts
            glob_args = {"pattern": f"**/{layout_name}.xml", "path": source_dir}
            glob_result = file_cache.get("glob", glob_args)
            if glob_result is None:
                glob_result = glob.invoke(glob_args)
                if not glob_result.startswith("No files") and not glob_result.startswith("Error"):
                    file_cache.put("glob", glob_args, glob_result)
            if glob_result.startswith("No files") or glob_result.startswith("Error"):
                continue

            xml_file = None
            for line in glob_result.split("\n"):
                line = line.strip()
                if not line or line.startswith("Found") or line.startswith("(") or line.startswith("["):
                    continue
                if not line.startswith("/"):
                    line = os.path.join(source_dir, line)
                xml_file = line
                break

            if xml_file:
                xml_read_args = {"file_path": xml_file, "offset": 1, "limit": 60}
                xml_content = file_cache.get("read", xml_read_args)
                if xml_content is None:
                    xml_content = read.invoke(xml_read_args)
                    if not str(xml_content).startswith("Error"):
                        file_cache.put("read", xml_read_args, str(xml_content))
                if not str(xml_content).startswith("Error"):
                    clean_lines = []
                    for ln in str(xml_content).split("\n"):
                        ln = ln.strip()
                        if ln.startswith("(") or not ln:
                            continue
                        colon_idx = ln.find(": ")
                        if colon_idx >= 0:
                            clean_lines.append(ln[colon_idx + 2:])
                    if clean_lines:
                        short_path = xml_file
                        if short_path.startswith(source_dir):
                            short_path = short_path[len(source_dir):].lstrip("/")
                        dep_parts.append(f"[关联布局] {layout_name} -> {short_path}\n" + "\n".join(clean_lines[:40]))

        if dep_parts:
            r["dependency_context"] = "\n\n".join(dep_parts)
            debug_log("attributor", f"  [dep-search] {r['class_name']}.{r['method_name']}: "
                      f"{len(project_imports)} imports, {len(layout_refs)} layouts, "
                      f"resolved {len(dep_parts)} deps")


def _analyze_snippets(results: list[dict]) -> None:
    """Run lightweight LLM analysis on fast-path results that have raw source_snippet.

    Replaces source_snippet (raw code) with LLM-generated analysis text,
    matching the behavior of the full LLM path where source_snippet stores
    the finding/analysis, not raw code.

    Fails gracefully: on any error, keeps the original raw snippet.
    """
    to_analyze = [(i, r) for i, r in enumerate(results)
                  if r.get("attributable") and r.get("source_snippet") and r.get("_fast_path")]
    if not to_analyze:
        return

    prompt_parts = []
    for _, r in to_analyze:
        snippet = r["source_snippet"]
        # Truncate very long snippets to keep token usage reasonable
        if len(snippet) > 2000:
            snippet = snippet[:2000] + "\n... (truncated)"
        cm = f"{r['class_name']}.{r['method_name']}"
        ctx = ""
        if r.get("context_method"):
            ctx = f" (匿名类定义在 {r['context_method']} 内)"
        io_type = r.get("io_type")
        if io_type:
            _IO_LABELS = {"network": "网络IO", "database": "数据库IO", "image": "图片加载"}
            ctx += f" [{_IO_LABELS.get(io_type, 'IO')}]"
        dep_ctx = ""
        if r.get("dependency_context"):
            dep_ctx = f"\n\n### 关联依赖上下文\n{r['dependency_context']}"
        prompt_parts.append(
            f"## {cm} ({r['dur_ms']:.2f}ms){ctx}\n"
            f"文件: {r['file_path']}:{r['line_start']}-{r['line_end']}\n"
            f"```\n{snippet}\n```"
            f"{dep_ctx}"
        )

    user_msg = (
        "分析以下 Android 源码片段，找出性能问题和潜在瓶颈。"
        "同时参考关联依赖上下文（import的类、XML布局）辅助分析。"
        "对每个方法输出一行，格式严格如下:\n"
        "FINDING: ClassName.methodName | 关键发现描述\n\n"
        + "\n\n".join(prompt_parts)
    )

    try:
        llm = ChatOpenAI(**get_llm_kwargs(role="attributor", temperature=0))
        response = llm.invoke([HumanMessage(content=user_msg)])
        get_tracker().record_from_message("attributor", response)

        # Parse FINDING lines from response
        findings: dict[str, str] = {}
        for line in response.content.split("\n"):
            line = line.strip()
            if not line.startswith("FINDING:"):
                continue
            parts = line[8:].split("|", 1)
            if len(parts) == 2:
                cm_key = parts[0].strip()
                finding_text = parts[1].strip()
                findings[cm_key] = finding_text

        # Match findings to results and replace source_snippet
        for _, r in to_analyze:
            cm = f"{r['class_name']}.{r['method_name']}"
            if cm in findings:
                r["source_snippet"] = findings[cm]
                debug_log("attributor", f"  [fast-path] LLM analysis for {cm}: {findings[cm][:80]}")
            else:
                debug_log("attributor", f"  [fast-path] no FINDING match for {cm}, keeping raw snippet")

    except Exception as e:
        debug_log("attributor", f"  [fast-path] LLM analysis failed: {e}, keeping raw snippets")


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
        group_label = ", ".join(f"{g['class_name']}.{g['method_name']}" for g in group)
        # Fast path: deterministic search for straightforward cases
        if _can_use_fast_path(group):
            debug_log("attributor", f"fast path: searching {group_label}")
            fast_results = _deterministic_search(group, file_cache)
            found_count = sum(1 for r in fast_results if r.get("reason") == "found")
            if all(r.get("reason") == "found" for r in fast_results):
                results.extend(fast_results)
                debug_log("attributor", f"fast path: all {found_count} found for {group_label}")
                continue
            # Partial success: merge found results, fall back to LLM for rest
            debug_log("attributor", f"fast path: {found_count}/{len(fast_results)} found, rest falls back to LLM for {group_label}")
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

    # Analyze fast-path results with lightweight LLM call
    fast_path_results = [r for r in results if r.get("_fast_path")]
    if fast_path_results:
        debug_log("attributor", f"analyzing {len(fast_path_results)} fast-path snippets with LLM")
        _analyze_snippets(results)

    # P1-4: Enrich found results with dependency context (imports + XML layouts)
    found_results = [r for r in results if r.get("attributable") and r.get("file_path")]
    if found_results:
        debug_log("attributor", f"enriching {len(found_results)} results with dependency context")
        _enrich_with_dependencies(results, file_cache)

    # Clean up internal markers before returning
    for r in results:
        r.pop("_fast_path", None)

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
        if issue.get("io_type"):
            result["io_type"] = issue["io_type"]
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
        # When context_method is set (anonymous inner class), hint to search
        # for the enclosing method first — the actual code is inside it
        if issue.get("context_method"):
            line += f", 匿名类在方法 {issue['context_method']}() 中定义,请先Grep {issue['context_method']} 定位外层方法,然后在其中找 {issue['method_name']} 的实现"
        # Append BlockMonitor stack trace if available
        if issue.get("stack_trace"):
            line += f", 堆栈:{issue['stack_trace'][0]}"
        # Hint for XML layout files — search .xml directly, not .java/.kt
        if search_type == "xml":
            line += f", xml布局:Glob **/{cn}.xml → Read完整文件, RESULT行请用: {cn}.{issue['method_name']}"
        # IO slice type hint — helps LLM focus on IO-specific patterns
        io_type = issue.get("io_type")
        if io_type:
            _IO_HINTS = {
                "network": "网络IO操作",
                "database": "数据库IO操作",
                "image": "图片加载操作",
            }
            io_label = _IO_HINTS.get(io_type, "IO操作")
            line += f", {io_label}:重点关注同步调用、缺少缓存、大对象分配"
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
