"""Source code attribution: extract SI$ slices from perf_summary for explorer."""

import json

from smartinspector.si_tag import (
    SITag,
    parse_si_tag,
    _split_fqn_method,
    _extract_method_from_anonymous,
    SYSTEM_PREFIXES as _SYSTEM_PREFIXES,
    SYSTEM_CLASS_PATTERNS as _SYSTEM_CLASS_PATTERNS,
    RV_PIPELINE_METHODS as _RV_PIPELINE_METHODS,
)


# ---------------------------------------------------------------------------
# SI$ tag parsing — thin wrappers around parse_si_tag()
# ---------------------------------------------------------------------------


def _extract_method_from_stack(stack_trace: list[str]) -> str:
    """Extract the actual method name from the first stack frame.

    Stack frame format: "at com.example.Class$Inner.method(File.kt:42)"
    Returns the method name (e.g. "method") or empty string.
    """
    if not stack_trace:
        return ""
    frame = stack_trace[0]
    # Pattern: "at ...ClassName.method(File:line)"
    # Find the last "." before "(" that contains the method name
    paren = frame.rfind("(")
    if paren < 0:
        return ""
    before_paren = frame[:paren]
    dot = before_paren.rfind(".")
    if dot < 0:
        return ""
    method = before_paren[dot + 1:]
    # Filter out non-method segments (class names with $, file paths, etc.)
    if "." in method or "/" in method:
        return ""
    return method


def _extract_caller_from_stack(stack_trace: list[str], target_class: str) -> str:
    """Find the method in target_class that called the anonymous class.

    Walks the stack from bottom (outermost caller) to top (innermost),
    looking for frames from target_class that are NOT the anonymous
    class itself (i.e. no $ in the class part).

    Returns the method name, e.g. "loadAndDisplayItems".
    """
    if not stack_trace or not target_class:
        return ""
    for frame in reversed(stack_trace):
        # Format: "at com.example.ClassName.method(File.java:42)"
        # or "at com.example.ClassName$1.run(File.java:52)"
        if target_class + "." not in frame:
            continue
        # Skip anonymous inner class frames ($N)
        if f"{target_class}$" in frame:
            continue
        # Extract method from this frame
        paren = frame.rfind("(")
        if paren < 0:
            continue
        before_paren = frame[:paren]
        dot = before_paren.rfind(".")
        if dot < 0:
            continue
        method = before_paren[dot + 1:]
        if "." not in method and "/" not in method:
            return method
    return ""


def extract_class(name: str) -> str:
    """Extract simple class name from an SI$ tag.

    Delegates to :func:`parse_si_tag` for unified single-pass parsing.

    Formats (with fully-qualified class names from getName()):
        SI$com.example.ClassName.method           → ClassName
        SI$RV#viewId#com.example.Adapter.method    → Adapter
        SI$inflate#layout_name#com.example.Parent  → layout_name
        SI$view#com.example.ClassName.method       → ClassName
        SI$handler#com.example.Callback.run        → Callback
        SI$block#com.example.Callback.run#250ms    → Callback
        SI$db#com.example.DB.query#table_name      → DB

    Returns the simple class name (last segment of the FQN).
    """
    tag = parse_si_tag(name)
    if tag is None:
        # Not an SI$ tag — best-effort fallback
        fqn, _ = _split_fqn_method(name)
        return fqn.rsplit(".", 1)[-1] if fqn else name
    return tag.class_name


def extract_fqn(name: str) -> str:
    """Extract the fully-qualified class name from an SI$ tag.

    Delegates to :func:`parse_si_tag` for unified single-pass parsing.

    Returns empty string if no package info available.
    Used for system class detection before LLM search.
    """
    tag = parse_si_tag(name)
    if tag is None:
        fqn, _ = _split_fqn_method(name)
        return fqn
    return tag.fqn


def is_system_class(name: str) -> bool:
    """Check if an SI$ tag refers to a system/framework class.

    Delegates to :func:`parse_si_tag` and uses :attr:`SITag.is_system`.

    Two-level check:
    1. FQN starts with known system package prefixes (android., androidx., etc.)
    2. Short class name matches known system class patterns (Choreographer,
       FragmentManager, etc.) — catches cases where Perfetto atrace truncates
       the full package path in the tag.
    """
    tag = parse_si_tag(name)
    if tag is None:
        return False
    return tag.is_system


def is_system_method(name: str) -> bool:
    """Check if an SI$ tag's method belongs to a framework, not user code.

    Delegates to :func:`parse_si_tag` and uses :attr:`SITag.is_system_method`.

    This handles RV pipeline methods (dispatchLayoutStep2, onLayoutChildren, etc.)
    which are tagged with the adapter's class name but are actually RecyclerView
    internal methods that should not be searched in user source.
    """
    tag = parse_si_tag(name)
    if tag is None:
        return False
    return tag.is_system_method


def extract_method(name: str) -> str:
    """Extract method name from an SI$ tag.

    Delegates to :func:`parse_si_tag` for unified single-pass parsing.
    For block tags with anonymous inner classes, falls back to
    :func:`_extract_method_from_anonymous` to resolve the enclosing method.
    """
    tag = parse_si_tag(name)
    if tag is None:
        _, method = _split_fqn_method(name)
        return method if method else "unknown"
    return tag.method_name


# ---------------------------------------------------------------------------
# Slice classification
# ---------------------------------------------------------------------------

def classify_search_type(raw_name: str) -> str:
    """Classify how an SI$ slice should be searched.

    Delegates to :func:`parse_si_tag` for unified parsing, then checks
    system class patterns via :attr:`SITag.is_system`.

    Returns:
        "java"   — search for .java/.kt source files
        "xml"    — search for layout XML files
        "system" — system class, skip source search
    """
    tag = parse_si_tag(raw_name)
    if tag is None:
        return "java"

    # System class check (FQN prefix + class name pattern)
    if tag.is_system:
        return "system"

    # touch# tags are framework input events — skip attribution
    if tag.tag_type == "touch":
        return "system"

    return tag.search_type


# ---------------------------------------------------------------------------
# Slice extraction
# ---------------------------------------------------------------------------

def _is_block_system_class(raw_name: str) -> bool:
    """Check if a SI$block# event refers to a system/framework class.

    BlockMonitor sends shortened class names (e.g. 'app.FragmentManager$5',
    'view.Choreographer$FrameDisplayEventReceiver') without full package paths.
    The standard is_system_class() fails on these because extract_fqn()
    returns truncated prefixes like 'app' or 'view'.

    This function extracts the actual class short name from the block tag
    and checks it against _SYSTEM_CLASS_PATTERNS.
    """
    body = raw_name
    if body.startswith("SI$"):
        body = body[3:]
    if body.startswith("block#"):
        body = body[6:]
    # Strip duration suffix: #NNNms
    hash_idx = body.rfind("#")
    if hash_idx >= 0 and body[hash_idx:].endswith("ms"):
        body = body[:hash_idx]
    # body is now: "view.Choreographer$FrameDisplayEventReceiver.run"
    #   or: "app.FragmentManager$5"
    # Use _split_fqn_method to properly separate FQN from method name,
    # since block tags may include a trailing ".method" that simple
    # rsplit(".", 1) would mistake for the class name segment.
    fqn, _method = _split_fqn_method(body)
    # If _split_fqn_method didn't split (method segment looks like a class),
    # fall back to using the full body as the FQN.
    if not fqn:
        fqn = body
    # Take segment after last dot (the class+inner part)
    short_name = fqn.rsplit(".", 1)[-1] if "." in fqn else fqn
    # short_name is now: Choreographer$FrameDisplayEventReceiver or FragmentManager$5
    for pattern in _SYSTEM_CLASS_PATTERNS:
        if short_name == pattern or short_name.startswith(pattern + "$"):
            return True
    return False


def _attach_block_stacks(attributable: list[dict], block_events: list[dict]) -> None:
    """Attach BlockMonitor stack traces to matching attributable slices.

    For each block event, find existing attributable entries whose
    class_name + method_name match. If found, add stack_trace as
    supplementary info. If not found (blind spot — no hook coverage),
    add the block event as a new entry.
    """
    if not block_events:
        return

    # Build lookup: (class_name, method_name) → attributable entry
    attr_lookup: dict[str, dict] = {}
    for entry in attributable:
        key = f"{entry['class_name']}.{entry['method_name']}"
        attr_lookup[key] = entry

    for block in block_events:
        raw_name = block.get("raw_name", "")
        if not raw_name.startswith("SI$block#"):
            continue

        class_name = extract_class(raw_name)
        method_name = extract_method(raw_name)
        dur_ms = block.get("dur_ms", 0)
        stack = block.get("stack_trace", [])

        # For anonymous inner classes ($N suffix in FQN), the method name
        # derived from the FQN (via _extract_method_from_anonymous) is the
        # enclosing method that *defines* the anonymous class (e.g.
        # "startMainThreadWork" from CpuBurnWorker$startMainThreadWork$1),
        # NOT the method actually executed (e.g. "run").
        # Strategy:
        #   - Always treat the FQN-derived method as context_method.
        #   - Get the real executed method from stack trace.
        #   - If no stack trace, keep method_name as-is (enclosing method
        #     from extract_method) — fast path will use context_method to
        #     locate the correct code.
        context_method = ""
        if "$" in raw_name:
            # Extract FQN from block tag: SI$block#pkg.Class$Enclosing$N#NNms
            block_body = raw_name[9:]  # strip "SI$block#"
            hash_idx = block_body.rfind("#")
            if hash_idx >= 0 and block_body[hash_idx:].endswith("ms"):
                fqn = block_body[:hash_idx]
            else:
                fqn = block_body
            enclosing = _extract_method_from_anonymous(fqn)
            if enclosing:
                context_method = enclosing
                # Only override method_name if we have a stack trace
                if stack:
                    stack_method = _extract_method_from_stack(stack)
                    if stack_method and stack_method != enclosing:
                        method_name = stack_method
            elif method_name == "unknown" and stack:
                # Pure anonymous class ($1, $2) with no enclosing method in FQN.
                # Walk the stack trace to find the caller from the same class
                # (e.g. stack has "at MainActivity.loadAndDisplayItems" which
                # is the method that created this anonymous class).
                caller = _extract_caller_from_stack(stack, class_name)
                if caller:
                    context_method = caller
                    method_name = "run"  # anonymous inner class executes run()

        key = f"{class_name}.{method_name}"

        # When stack trace reveals the actual method differs from the
        # class-name-derived method (anonymous inner class), try to
        # update the original entry in-place so we don't create a duplicate.
        if context_method:
            orig_key = f"{class_name}.{context_method}"
            if orig_key in attr_lookup:
                orig = attr_lookup[orig_key]
                orig["method_name"] = method_name
                orig["context_method"] = context_method
                if stack and not orig.get("stack_trace"):
                    orig["stack_trace"] = stack
                if dur_ms > orig.get("dur_ms", 0):
                    orig["dur_ms"] = dur_ms
                # Re-index under the new key
                del attr_lookup[orig_key]
                attr_lookup[key] = orig
                continue

        if key in attr_lookup:
            # Existing hook slice — attach stack and update dur_ms if block has real duration
            # (Perfetto SQL dur is ~0 for block slices; real dur is in the name suffix)
            existing = attr_lookup[key]
            if stack and not existing.get("stack_trace"):
                existing["stack_trace"] = stack
            if dur_ms > existing.get("dur_ms", 0):
                existing["dur_ms"] = dur_ms
            if context_method and not existing.get("context_method"):
                existing["context_method"] = context_method
            # If the matched entry is itself a system class, mark it and skip
            if _is_block_system_class(raw_name):
                existing["_system"] = True
        else:
            # No existing hook — this is a blind spot, add as new entry
            # But skip system/framework classes (Choreographer, FragmentManager, etc.)
            if _is_block_system_class(raw_name):
                continue
            # Include even without stack_trace — the class+method info alone
            # is enough for the attributor agent to search source code
            entry = {
                "raw_name": raw_name,
                "class_name": class_name,
                "method_name": method_name,
                "dur_ms": dur_ms,
                "type": "block",
                "search_type": "java",
                "stack_trace": stack,
                "instance": None,
            }
            if context_method:
                entry["context_method"] = context_method
            attributable.append(entry)
            attr_lookup[key] = entry


# ---------------------------------------------------------------------------
# Call stack context extraction
# ---------------------------------------------------------------------------

_STAGE_KEYWORDS = {
    "doFrame": "帧渲染",
    "performMeasure": "measure阶段",
    "performLayout": "layout阶段",
    "performDraw": "draw阶段",
    "Choreographer": "vsync",
}


def _extract_context_from_chain(chain: list[str]) -> list[str]:
    """从调用链中提取有意义的上下文节点。

    过滤掉系统标签（doFrame, Choreographer 等），保留 SI$ 自定义标签和
    关键系统标签（作为阶段标识）。
    """
    context_parts = []
    for item in chain:
        # chain item 格式: "slice_name [XX.XXms]" 或 "slice_name"
        name = item.split(" [")[0] if " [" in item else item

        if name.startswith("SI$"):
            # SI$ 标签：提取关键信息
            context_parts.append(_summarize_si_tag(name))
        else:
            # 系统标签：只保留阶段标识
            for keyword, label in _STAGE_KEYWORDS.items():
                if keyword in name:
                    context_parts.append(f"[{label}]")
                    break

    return context_parts


def _summarize_si_tag(tag: str) -> str:
    """将 SI$ 标签转换为可读的上下文摘要。"""
    body = tag[3:] if tag.startswith("SI$") else tag

    if body.startswith("RV#"):
        # SI$RV#viewId#Adapter.method → "RV(viewId, Adapter.method)"
        parts = body.split("#")
        if len(parts) >= 3:
            view_id = parts[1]
            fqn_method = parts[2]
            _, method = _split_fqn_method(fqn_method)
            adapter = fqn_method.rsplit(".", 1)[0].rsplit(".", 1)[-1]
            return f"RV#{view_id}#{adapter}.{method or '?'}"
        return body

    if body.startswith("inflate#"):
        parts = body[8:].split("#")
        layout = parts[0] if parts else "?"
        return f"inflate({layout})"

    if body.startswith("view#"):
        fqn, method = _split_fqn_method(body[5:])
        cls = fqn.rsplit(".", 1)[-1] if fqn else "?"
        return f"{cls}.{method or '?'}"

    if body.startswith("handler#"):
        fqn_part = body[8:].split("#")[0]
        fqn, method = _split_fqn_method(fqn_part)
        cls = fqn.rsplit(".", 1)[-1] if fqn else fqn_part
        return f"handler({cls}.{method or '?'})"

    # IO tags
    for prefix, label in (("net#", "网络IO"), ("db#", "数据库IO"), ("img#", "图片加载")):
        if body.startswith(prefix):
            rest = body[len(prefix):]
            parts = rest.split("#")
            fqn_method = parts[0]
            fqn, method = _split_fqn_method(fqn_method)
            cls = fqn.rsplit(".", 1)[-1] if fqn else fqn_method
            return f"{label}({cls}.{method or '?'})"

    if body.startswith("Activity.lifecycle"):
        return "Activity生命周期"

    if body.startswith("Fragment.lifecycle"):
        return "Fragment生命周期"

    # 默认
    fqn, method = _split_fqn_method(body)
    cls = fqn.rsplit(".", 1)[-1] if fqn else body
    return f"{cls}.{method or '?'}"


def _walk_parent_chain(slice_data: dict, slice_by_id: dict, max_depth: int = 5) -> list[str]:
    """从 slice 数据沿 parent_id 向上回溯，构建调用链。

    Returns:
        调用链 [root, ..., leaf]，每项格式 "name [dur_ms]"
    """
    chain = []
    visited = set()
    current = slice_data

    for _ in range(max_depth):
        sid = current.get("id")
        if sid is None or sid in visited:
            break
        visited.add(sid)

        name = current.get("name", "")
        dur_ms = current.get("dur_ms", 0)
        chain.append(f"{name} [{dur_ms:.2f}ms]")

        parent_id = current.get("parent_id")
        if not parent_id or parent_id not in slice_by_id:
            break
        current = slice_by_id[parent_id]

    chain.reverse()  # root → leaf
    return chain


def _build_parent_contexts(view_slices: dict) -> dict[str, str]:
    """为每个 slowest_slice 构建 parent chain 上下文摘要。

    利用 collect_view_slices() 已构建的 call_chains 数据和 slice 的 parent_id，
    生成精简的调用上下文字符串，用于辅助 attributor agent 精确定位。

    Returns:
        dict: slice_name → context_string 映射
    """
    slices_data = view_slices.get("slowest_slices", [])
    call_chains = view_slices.get("call_chains", [])

    # 从 call_chains 提取 name → chain 映射
    chain_map: dict[str, list[str]] = {}
    for cc in call_chains:
        name = cc.get("name", "")
        chain = cc.get("chain", [])
        if name and chain:
            chain_map[name] = chain

    # 从原始 slice 数据构建 parent_id → slice_name 映射
    slice_by_id: dict[int, dict] = {}
    for s in slices_data:
        sid = s.get("id")
        if sid is not None:
            slice_by_id[sid] = s

    contexts: dict[str, str] = {}
    for s in slices_data:
        name = s.get("name", "")
        if not name.startswith("SI$"):
            continue

        # 策略1: 使用 call_chains 中的预构建链
        if name in chain_map:
            chain = chain_map[name]
            # chain 是 [root, ..., leaf]，提取上下文节点
            context_parts = _extract_context_from_chain(chain)
            if context_parts:
                contexts[name] = " → ".join(context_parts)
                continue

        # 策略2: 从 parent_id 向上回溯（call_chains 未覆盖的 slice）
        parent_chain = _walk_parent_chain(s, slice_by_id, max_depth=5)
        if parent_chain:
            context_parts = _extract_context_from_chain(parent_chain)
            if context_parts:
                contexts[name] = " → ".join(context_parts)

    return contexts


def extract_attributable_slices(perf_summary_json: str, min_dur_ms: float = 1.0) -> list[dict]:
    """Extract SI$ slices from perf_summary for source code attribution.

    Args:
        perf_summary_json: JSON string from PerfettoCollector.
        min_dur_ms: Minimum duration threshold in ms. Slices below this
                    are not considered attributable performance issues.
                    Default 1.0ms.

    Returns a sorted list of dicts with class_name, method_name, dur_ms, etc.
    Only includes slices with the SI$ prefix AND dur_ms >= min_dur_ms.
    """
    if not perf_summary_json:
        return []
    try:
        data = json.loads(perf_summary_json)
    except (json.JSONDecodeError, TypeError):
        return []
    view_slices = data.get("view_slices", {})
    if not view_slices:
        return []

    attributable: list[dict] = []

    # From slowest_slices
    for s in view_slices.get("slowest_slices", []):
        name = s.get("name", "")
        if not name.startswith("SI$"):
            continue

        # Skip system/framework classes and RV pipeline methods
        if classify_search_type(name) == "system":
            continue
        if is_system_method(name):
            continue
        # Block tags have shortened class names — use pattern-based check
        if name.startswith("SI$block#") and _is_block_system_class(name):
            continue

        class_name = extract_class(name)
        method_name = extract_method(name)

        # Skip inflate slices with hex resource IDs (unresolvable layout names)
        if method_name == "inflate" and class_name.startswith("0x"):
            continue

        entry = {
            "raw_name": name,
            "class_name": class_name,
            "method_name": method_name,
            "dur_ms": s.get("dur_ms", 0),
            "type": "slice",
            "search_type": classify_search_type(name),
            "instance": None,
        }
        attributable.append(entry)

    # From summary (aggregated stats — no top-N truncation) to catch slices
    # missed by slowest_slices cap
    seen_names: set[str] = {e["raw_name"] for e in attributable}
    for s in view_slices.get("summary", []):
        name = s.get("name", "")
        if not name.startswith("SI$") or name in seen_names:
            continue
        if classify_search_type(name) == "system" or is_system_method(name):
            continue
        # Block tags have shortened class names — use pattern-based check
        if name.startswith("SI$block#") and _is_block_system_class(name):
            continue

        class_name = extract_class(name)
        method_name = extract_method(name)

        # Skip inflate slices with hex resource IDs
        if method_name == "inflate" and class_name.startswith("0x"):
            continue

        entry = {
            "raw_name": name,
            "class_name": class_name,
            "method_name": method_name,
            "dur_ms": s.get("max_ms", 0),
            "type": "summary",
            "search_type": classify_search_type(name),
            "instance": None,
            "count": s.get("count", 0),
            "total_ms": s.get("total_ms", 0),
        }
        attributable.append(entry)
        seen_names.add(name)

    # From rv_instances — only include user-code adapter methods
    for inst in view_slices.get("rv_instances", []):
        instance_key = inst.get("instance", "")
        # instance format: RV#viewId#AdapterName
        parts = instance_key.split("#")
        adapter_name = parts[2] if len(parts) >= 3 else instance_key

        # Skip if adapter is a system class
        if any(adapter_name.startswith(p.replace(".", "")) for p in _SYSTEM_PREFIXES):
            continue

        for method_name, stats in inst.get("methods", {}).items():
            # Skip RV pipeline methods in rv_instances too
            if method_name in _RV_PIPELINE_METHODS:
                continue

            raw = f"SI${instance_key}.{method_name}"
            entry = {
                "raw_name": raw,
                "class_name": adapter_name,
                "method_name": method_name,
                "dur_ms": stats.get("max_ms", 0),
                "type": "rv_method",
                "search_type": "java",
                "instance": instance_key,
                "count": stats.get("count", 0),
                "total_ms": stats.get("total_ms", 0),
            }
            attributable.append(entry)

    # ── Block events: extract and merge stack_trace ──
    # Process BEFORE min_dur filter — block events may be the only data source
    # when all hook slices are below threshold (e.g. <1ms UI slices but 129ms block events)
    block_events = data.get("block_events", [])
    if block_events:
        _attach_block_stacks(attributable, block_events)

    # ── IO slices: extract from io_slices (SI$net#/SI$db#/SI$img#) ──
    io_slices_data = data.get("io_slices", {})
    io_summary = io_slices_data.get("summary", []) if io_slices_data else []
    for s in io_summary:
        name = s.get("name", "")
        if not name.startswith("SI$"):
            continue
        if classify_search_type(name) == "system":
            continue

        class_name = extract_class(name)
        method_name = extract_method(name)
        dur_ms = s.get("max_ms", 0)
        count = s.get("count", 0)
        total_ms = s.get("total_ms", 0)

        # Determine IO type for tagging
        body = name[3:]
        io_type = "unknown"
        if body.startswith("net#"):
            io_type = "network"
        elif body.startswith("db#"):
            io_type = "database"
        elif body.startswith("img#"):
            io_type = "image"

        entry = {
            "raw_name": name,
            "class_name": class_name,
            "method_name": method_name,
            "dur_ms": dur_ms,
            "type": "io_slice",
            "search_type": "java",
            "instance": None,
            "io_type": io_type,
            "count": count,
            "total_ms": total_ms,
        }
        attributable.append(entry)

    # Remove entries marked as system classes by block event matching
    attributable = [e for e in attributable if not e.get("_system")]

    # ── CPU hotspots: extract from cpu_hotspots (perf_sample stack profiles) ──
    # These are function-level CPU usage from stack sampling, not SI$ slices.
    cpu_hotspots = data.get("cpu_hotspots", [])
    existing_keys = {f"{e['class_name']}.{e['method_name']}" for e in attributable}
    for hs in cpu_hotspots:
        if hs.get("error"):
            continue
        func = hs.get("function", "")
        if not func or func.startswith("/") or func.startswith("[") or "::" in func:
            continue  # Skip native/library/C++ functions

        # Parse function name: "com.example.ClassName.method" -> (ClassName, method)
        parts = func.rsplit(".", 1)
        if len(parts) != 2:
            continue
        class_path, method = parts

        # Get simple class name from FQN
        simple_class = class_path.rsplit(".", 1)[-1] if "." in class_path else class_path

        # Skip system classes by prefix
        if any(class_path.startswith(p) for p in _SYSTEM_PREFIXES):
            continue
        # Skip known system class patterns
        if simple_class in _SYSTEM_CLASS_PATTERNS:
            continue

        pct = hs.get("pct", 0)
        if pct < 3:
            continue  # Only include significant hotspots (>3% CPU)

        # Skip if already attributed via SI$ slices (more precise timing)
        key = f"{simple_class}.{method}"
        if key in existing_keys:
            continue

        # Estimate dur_ms from CPU percentage (assuming ~10s trace)
        estimated_ms = pct * 100

        entry = {
            "raw_name": f"CPU$hotspot#{class_path}.{method}",
            "class_name": simple_class,
            "method_name": method,
            "dur_ms": estimated_ms,
            "type": "cpu_hotspot",
            "search_type": "java",
            "instance": None,
            "count": hs.get("samples", 0),
            "total_ms": estimated_ms,
        }

        # Add callchain context for the LLM
        callchain = hs.get("callchain", [])
        if callchain:
            entry["call_context"] = " → ".join(
                n.rsplit(".", 1)[-1] if "." in n else n
                for n in callchain[:5]
            )

        attributable.append(entry)
        existing_keys.add(key)

    # Filter by minimum duration threshold
    attributable = [e for e in attributable if e["dur_ms"] >= min_dur_ms]

    if not attributable:
        return []

    # Deduplicate by class+method, keep highest dur_ms, merge stack_trace
    seen: dict[str, dict] = {}
    for entry in attributable:
        key = f"{entry['class_name']}.{entry['method_name']}"
        if key not in seen:
            seen[key] = entry
        else:
            existing = seen[key]
            # Merge stack_trace: prefer entries that have it
            stack = entry.get("stack_trace") or existing.get("stack_trace")
            # Keep the entry with higher dur_ms
            if entry["dur_ms"] > existing["dur_ms"]:
                if stack:
                    entry["stack_trace"] = stack
                seen[key] = entry
            elif stack and not existing.get("stack_trace"):
                existing["stack_trace"] = stack

    # ── 注入调用栈上下文 ──
    parent_contexts = _build_parent_contexts(view_slices)

    for entry in seen.values():
        raw_name = entry.get("raw_name", "")
        if raw_name in parent_contexts:
            entry["call_context"] = parent_contexts[raw_name]

        # 对 RV 实例方法，补充 RV 上下文
        if entry.get("instance"):
            entry["call_context"] = f"RV实例: {entry['instance']}"

    return sorted(seen.values(), key=lambda x: -x["dur_ms"])


# ---------------------------------------------------------------------------
# Grouping
# ---------------------------------------------------------------------------

def group_issues_by_file(issues: list[dict]) -> list[list[dict]]:
    """Group attributable issues by their target file (class/layout).

    Issues targeting the same class or layout XML are grouped together
    so that explore agent reads the file only once.

    Returns:
        List of issue groups. Each group shares the same file target.
    """
    groups: dict[str, list[dict]] = {}
    for issue in issues:
        search_type = issue.get("search_type", "java")
        class_name = issue.get("class_name", "")

        if search_type == "xml":
            key = f"xml:{class_name}"
        else:
            key = f"java:{class_name}"

        if key not in groups:
            groups[key] = []
        groups[key].append(issue)

    return list(groups.values())


# ---------------------------------------------------------------------------
# Prompt building
# ---------------------------------------------------------------------------

def build_attribution_prompt(attributable: list[dict]) -> str:
    """Build a structured prompt for the explorer agent from attributable slices.

    Args:
        attributable: List of attributable slice dicts.

    Returns:
        Structured prompt string for the code explorer agent.
    """
    if not attributable:
        return ""

    lines = [
        "请搜索以下类和方法的源码，分析性能问题的根因：\n",
        "## 需要归因的性能热点\n",
    ]

    for i, s in enumerate(attributable[:15], 1):
        lines.append(f"### {i}. {s['class_name']}.{s['method_name']}")
        lines.append(f"   - 耗时: {s['dur_ms']:.2f}ms")
        if s.get("instance"):
            lines.append(f"   - 实例: {s['instance']}")
        if s.get("count"):
            lines.append(f"   - 调用次数: {s['count']}")
        if s.get("total_ms"):
            lines.append(f"   - 总耗时: {s['total_ms']:.1f}ms")

        # ── 调用链上下文 ──
        if s.get("call_context"):
            lines.append(f"   - 调用链上下文: {s['call_context']}")

        if s.get("stack_trace"):
            lines.append(f"   - 堆栈采样 (BlockMonitor):")
            for frame in s["stack_trace"][:12]:
                lines.append(f"     {frame}")
        if "$" in s['class_name']:
            outer_class = s['class_name'].split("$")[0]
            lines.append(f"   - 匿名/内部类，请搜索外层类 {outer_class} 的源码")
        if s.get("context_method"):
            lines.append(f"   - 匿名类定义在方法 {s['context_method']} 中，耗时操作在 {s['method_name']} 方法体内")
        lines.append(f"   - 搜索类型: {s.get('search_type', 'java')}")
        lines.append(f"   - 原始tag: {s['raw_name']}")
        lines.append("")

    lines.append("\n请搜索这些类和方法的源码实现，找出：")
    lines.append("1. 具体的耗时操作（IO、数据库、复杂计算、嵌套循环）")
    lines.append("2. 是否有优化空间（缓存、懒加载、异步处理）")
    lines.append("3. 具体的修改建议")
    lines.append("\n搜索策略：")
    lines.append("- java类型: Glob **/{class_name}.java 或 **/{class_name}.kt → Grep方法签名获取行号 → Read(offset, limit=40)精准读取方法体")
    lines.append("- xml类型: Glob **/{class_name}.xml → Read读取完整layout")
    lines.append("- 如果Glob找不到文件，标记为系统类，不需要搜索源码")

    return "\n".join(lines)
