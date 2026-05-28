"""Unified SI$ tag parser — single-pass extraction of all fields."""

from __future__ import annotations

import re
from dataclasses import dataclass, field

# Matches trailing $number (anonymous inner class index)
_ANON_SUFFIX = re.compile(r"\$(\d+)$")


def _split_fqn_method(body: str) -> tuple[str, str]:
    """Split 'com.example.ClassName.method' into (fqn, method).

    The last dot-separated segment is the method name, everything before it
    is the fully-qualified class name.

    Handles edge cases where there is no separate method segment and the
    entire string is a class FQN (e.g. block tags whose msgClass is the
    full FQN like ``com.smartinspector.hook.worker.CpuBurnWorker$startMainThreadWork$1``).
    Java method names always start with a lowercase letter by convention,
    so if the last segment starts with an uppercase letter or contains '$'
    it is part of the class name, not a method.
    """
    if "." in body:
        fqn, method = body.rsplit(".", 1)
        if method[:1].isupper() or "$" in method:
            return body, ""
        return fqn, method
    return "", body


def _extract_method_from_anonymous(fqn: str) -> str:
    """Extract context method name from an anonymous inner class FQN.

    JVM anonymous inner class naming (compiled Java/Kotlin):
    - OuterClass$1 → anonymous inner class, no method context
    - OuterClass$MethodName$1 → Kotlin method-scoped anonymous class
    - OuterClass$Inner$1 → named inner class Inner's anonymous, no method context
    - OuterClass$MethodName$1$2 → multi-level anonymous, MethodName is the method
    - OuterClass$$inlined$lambda$0 → Kotlin inlined lambda, no method context

    Heuristic: walk $-segments from the end, skipping numeric (anonymous index)
    segments, until we find a segment that looks like a method name (starts with
    a lowercase letter and is not a Kotlin compiler artifact).
    """
    m = _ANON_SUFFIX.search(fqn)
    if not m:
        return ""
    prefix = fqn[: m.start()]
    if "$" not in prefix:
        return ""

    remaining = prefix
    while "$" in remaining:
        last_seg = remaining.rsplit("$", 1)[-1]
        remaining = remaining.rsplit("$", 1)[0]
        if last_seg.isdigit():
            continue
        if not last_seg or not last_seg[0].islower():
            continue
        if last_seg in ("lambda", "inlined"):
            continue
        if "$" in last_seg:
            continue
        if "$lambda$" in prefix:
            lambda_idx = prefix.rfind("$lambda$")
            if lambda_idx >= 0 and prefix[lambda_idx + 8 :].startswith(last_seg):
                continue
        return last_seg
    return ""


# Known Android/system package prefixes
SYSTEM_PREFIXES: tuple[str, ...] = (
    "android.",
    "androidx.",
    "java.",
    "javax.",
    "kotlin.",
    "kotlinx.",
    "dalvik.",
    "libcore.",
    "com.android.",
    "com.google.",
)

# Known system class name patterns (short names, no package prefix)
SYSTEM_CLASS_PATTERNS: tuple[str, ...] = (
    "Choreographer",
    "FragmentManager",
    "LayoutInflater",
    "Handler",
    "ActivityThread",
    "ViewRootImpl",
    "InputEventReceiver",
    "ViewImpl",
    "Window",
    "Binder",
    "Looper",
    "MessageQueue",
    "HandlerThread",
    "FragmentActivity",
    "AppCompatActivity",
    "AppCompatDelegateImpl",
    "ComponentActivity",
    "AppCompatViewInflater",
    "ActionBarActivity",
    "ActionBarImpl",
    "KeyEvent",
    "MotionEvent",
    "View",
    "ViewGroup",
    "RecyclerView",
    "GapWorker",
    "LinearLayoutManager",
    "GestureDetector",
    "InputMethodManager",
    "PhoneWindow",
)

# RV pipeline method names — framework methods, not user code
RV_PIPELINE_METHODS: frozenset[str] = frozenset(
    {
        "dispatchLayoutStep1",
        "dispatchLayoutStep2",
        "dispatchLayoutStep3",
        "onLayoutChildren",
        "onDraw",
        "onScrollStateChanged",
        "prefetch",
        "gapWorker",
    }
)

# IO type mapping from tag prefix
_IO_TYPE_MAP: dict[str, str] = {
    "net#": "network",
    "db#": "database",
    "img#": "image",
}


@dataclass
class SITag:
    """Structured representation of a parsed SI$ tag.

    Attributes:
        tag_type: Tag category — "block", "RV", "inflate", "view", "handler",
                  "net", "db", "img", "touch", "default"
        class_name: Simple class name (e.g. "DemoAdapter")
        method_name: Method name (e.g. "onBindViewHolder")
        fqn: Fully-qualified class name (e.g. "com.example.DemoAdapter"), may be empty
        search_type: How to search — "java", "xml", or "system"
        io_type: IO category for IO tags — "network", "database", "image", or None
        raw_name: Original tag string as-is
        extras: Additional parsed fields (view_id, layout, duration_ms, table, etc.)
    """

    tag_type: str
    class_name: str
    method_name: str
    fqn: str
    search_type: str
    io_type: str | None
    raw_name: str
    extras: dict = field(default_factory=dict)

    @property
    def is_system(self) -> bool:
        """Check if this tag refers to a system/framework class."""
        if self.fqn and "." in self.fqn:
            if any(self.fqn.startswith(p) for p in SYSTEM_PREFIXES):
                return True
        cn = self.class_name
        if cn:
            for pattern in SYSTEM_CLASS_PATTERNS:
                if cn == pattern or cn.startswith(pattern + "$"):
                    return True
        return False

    @property
    def is_system_method(self) -> bool:
        """Check if the method is a framework pipeline method."""
        return self.method_name in RV_PIPELINE_METHODS


def parse_si_tag(name: str) -> SITag | None:
    """Single-pass SI$ tag parser.

    Replaces the former ``extract_class()`` + ``extract_method()`` +
    ``extract_fqn()`` triple-parse pattern with one unified parse.

    Args:
        name: Raw SI$ tag string (e.g. ``SI$RV#recycler#com.example.A.onBind``).

    Returns:
        ``SITag`` with all fields populated, or ``None`` if *name* is not
        an SI$ tag.
    """
    if not name or not name.startswith("SI$"):
        return None

    raw_name = name
    body = name[3:]

    tag_type = "default"
    class_name = ""
    method_name = ""
    fqn = ""
    search_type = "java"
    io_type: str | None = None
    extras: dict = {}

    # ── block# ──
    if body.startswith("block#"):
        tag_type = "block"
        rest = body[6:]
        # Strip duration suffix (#NNNms)
        hash_idx = rest.rfind("#")
        if hash_idx >= 0 and rest[hash_idx:].endswith("ms"):
            try:
                extras["duration_ms"] = float(rest[hash_idx + 1 : -2])
            except ValueError:
                pass
            rest = rest[:hash_idx]
        fqn, method = _split_fqn_method(rest)
        simple = fqn.rsplit(".", 1)[-1] if fqn else rest
        if "$" in simple:
            simple = simple.split("$")[0]
        class_name = simple
        # For anonymous inner classes, try to extract enclosing method
        if not method and "$" in fqn:
            method = _extract_method_from_anonymous(fqn)
        method_name = method if method else "unknown"

    # ── RV# ──
    elif body.startswith("RV#"):
        tag_type = "RV"
        parts = body.split("#")
        if len(parts) >= 3:
            extras["view_id"] = parts[1]
            fqn, method = _split_fqn_method(parts[2])
            class_name = fqn.rsplit(".", 1)[-1] if fqn else parts[2]
            method_name = method if method else "unknown"
        else:
            class_name = body.rsplit(".", 1)[-1] if "." in body else body
            method_name = "unknown"

    # ── inflate# ──
    elif body.startswith("inflate#"):
        tag_type = "inflate"
        search_type = "xml"
        parts = body[8:].split("#")
        class_name = parts[0] if parts else "LayoutInflater"
        method_name = "inflate"
        if len(parts) >= 2:
            extras["parent"] = parts[1]

    # ── view# ──
    elif body.startswith("view#"):
        tag_type = "view"
        rest = body[5:]
        fqn, method = _split_fqn_method(rest)
        class_name = fqn.rsplit(".", 1)[-1] if fqn else rest
        method_name = method if method else "unknown"

    # ── handler# ──
    elif body.startswith("handler#"):
        tag_type = "handler"
        rest = body[8:]
        fqn_part = rest.split("#")[0] if "#" in rest else rest
        fqn, method = _split_fqn_method(fqn_part)
        class_name = fqn.rsplit(".", 1)[-1] if fqn else fqn_part
        method_name = method if method else "unknown"

    # ── db# ──
    elif body.startswith("db#"):
        tag_type = "db"
        io_type = "database"
        rest = body[3:]
        hash_idx = rest.rfind("#")
        if hash_idx >= 0:
            extras["table"] = rest[hash_idx + 1 :]
            rest = rest[:hash_idx]
        fqn, method = _split_fqn_method(rest)
        class_name = fqn.rsplit(".", 1)[-1] if fqn else rest
        method_name = method if method else "unknown"

    # ── net# ──
    elif body.startswith("net#"):
        tag_type = "net"
        io_type = "network"
        rest = body[4:]
        fqn, method = _split_fqn_method(rest)
        class_name = fqn.rsplit(".", 1)[-1] if fqn else rest
        method_name = method if method else "unknown"

    # ── img# ──
    elif body.startswith("img#"):
        tag_type = "img"
        io_type = "image"
        rest = body[4:]
        fqn, method = _split_fqn_method(rest)
        class_name = fqn.rsplit(".", 1)[-1] if fqn else rest
        method_name = method if method else "unknown"

    # ── touch# ──
    elif body.startswith("touch#"):
        tag_type = "touch"
        search_type = "system"

    # ── Default: bare FQN.method ──
    else:
        fqn, method = _split_fqn_method(body)
        class_name = fqn.rsplit(".", 1)[-1] if fqn else body
        method_name = method if method else "unknown"

    return SITag(
        tag_type=tag_type,
        class_name=class_name,
        method_name=method_name,
        fqn=fqn,
        search_type=search_type,
        io_type=io_type,
        raw_name=raw_name,
        extras=extras,
    )
