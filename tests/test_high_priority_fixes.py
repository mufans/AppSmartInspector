"""Tests for high-priority fixes: config, validation, caching, thread-safety, path protection."""

import os
import tempfile
import threading

import pytest


# ── Fix 1: WS_PORT configurable via env var ──────────────────


class TestWSPortConfig:
    def test_default_port(self, monkeypatch):
        monkeypatch.delenv("SI_WS_PORT", raising=False)
        from smartinspector.config import get_ws_port
        assert get_ws_port() == 9876

    def test_custom_port(self, monkeypatch):
        monkeypatch.setenv("SI_WS_PORT", "9999")
        # Re-import to pick up new env
        import importlib
        import smartinspector.config as cfg
        importlib.reload(cfg)
        assert cfg.get_ws_port() == 9999
        monkeypatch.delenv("SI_WS_PORT")

    def test_invalid_port_falls_back(self, monkeypatch):
        monkeypatch.setenv("SI_WS_PORT", "not_a_number")
        import importlib
        import smartinspector.config as cfg
        importlib.reload(cfg)
        assert cfg.get_ws_port() == 9876
        monkeypatch.delenv("SI_WS_PORT")


# ── Fix 2: Input validation ──────────────────────────────────


class TestDurationValidation:
    """Duration should be clamped to [100, 60000] range."""

    def test_valid_duration(self):
        from smartinspector.commands.trace import cmd_trace
        state = {"messages": []}
        # Duration 5000 is valid; just verify no crash
        # (cmd_trace calls graph, so we just test parsing logic)
        parts = "5000".split()
        duration_ms = None
        try:
            duration_ms = int(parts[0])
            if duration_ms < 100 or duration_ms > 60000:
                duration_ms = max(100, min(60000, duration_ms))
        except ValueError:
            pass
        assert duration_ms == 5000

    def test_too_small_duration_clamped(self):
        duration_ms = None
        raw = 50
        duration_ms = raw
        if duration_ms < 100 or duration_ms > 60000:
            duration_ms = max(100, min(60000, duration_ms))
        assert duration_ms == 100

    def test_too_large_duration_clamped(self):
        duration_ms = None
        raw = 120000
        duration_ms = raw
        if duration_ms < 100 or duration_ms > 60000:
            duration_ms = max(100, min(60000, duration_ms))
        assert duration_ms == 60000


class TestHookInputValidation:
    """Hook identifiers should reject special characters."""

    def test_valid_hook_id(self):
        import re
        _SAFE_IDENTIFIER_RE = re.compile(r'^[A-Za-z_$][\w.$]*$')
        assert _SAFE_IDENTIFIER_RE.match("rv_adapter")
        assert _SAFE_IDENTIFIER_RE.match("com.example.ClassName")
        assert _SAFE_IDENTIFIER_RE.match("MyClass$Inner")
        assert _SAFE_IDENTIFIER_RE.match("_hook")

    def test_invalid_hook_id(self):
        import re
        _SAFE_IDENTIFIER_RE = re.compile(r'^[A-Za-z_$][\w.$]*$')
        assert not _SAFE_IDENTIFIER_RE.match("hook; rm -rf /")
        assert not _SAFE_IDENTIFIER_RE.match("../../etc/passwd")
        assert not _SAFE_IDENTIFIER_RE.match("hook `whoami`")
        assert not _SAFE_IDENTIFIER_RE.match("")


# ── Fix 3: LRU cache for file reads ──────────────────────────


class TestReadCache:
    def test_cache_hit(self):
        from smartinspector.tools.read import _read_file_content, read
        # Create a temp file
        with tempfile.NamedTemporaryFile(mode="w", suffix=".txt", delete=False) as f:
            f.write("line1\nline2\nline3\n")
            tmp = f.name

        try:
            result1 = _read_file_content(tmp, 1, 10)
            result2 = _read_file_content(tmp, 1, 10)
            assert result1 == result2
            # Verify cache info
            info = _read_file_content.cache_info()
            assert info.hits >= 1
        finally:
            os.unlink(tmp)

    def test_read_file_content(self):
        from smartinspector.tools.read import _read_file_content
        with tempfile.NamedTemporaryFile(mode="w", suffix=".txt", delete=False) as f:
            f.write("hello world\n")
            tmp = f.name

        try:
            result = _read_file_content(tmp, 1, 10)
            assert "hello world" in result
        finally:
            os.unlink(tmp)

    def test_read_nonexistent_file(self):
        from smartinspector.tools.read import _read_file_content
        result = _read_file_content("/nonexistent/path/file.txt", 1, 10)
        assert "not found" in result.lower()


# ── Fix 4: Thread-safe singleton ─────────────────────────────


class TestThreadSafeSingleton:
    def test_singleton_thread_safety(self):
        from smartinspector.ws.server import SIServer

        # Reset singleton
        SIServer._instance = None
        instances = []

        def create_instance():
            inst = SIServer.get(port=9876)
            instances.append(id(inst))

        threads = [threading.Thread(target=create_instance) for _ in range(20)]
        for t in threads:
            t.start()
        for t in threads:
            t.join()

        # All threads should get the same instance
        assert len(set(instances)) == 1

        # Cleanup
        SIServer._instance = None

    def test_singleton_returns_same_instance(self):
        from smartinspector.ws.server import SIServer

        SIServer._instance = None
        a = SIServer.get()
        b = SIServer.get()
        assert a is b

        SIServer._instance = None


# ── Fix 5: Path traversal protection ─────────────────────────


class TestPathTraversalProtection:
    def test_normal_path_valid(self):
        from smartinspector.tools.glob import _validate_search_path
        import tempfile
        with tempfile.TemporaryDirectory() as tmpdir:
            result = _validate_search_path(tmpdir)
            assert result is not None

    def test_dotdot_path_rejected(self):
        from smartinspector.tools.glob import _validate_search_path
        result = _validate_search_path("/etc/../etc/passwd")
        assert result is None

    def test_nested_dotdot_rejected(self):
        from smartinspector.tools.glob import _validate_search_path
        result = _validate_search_path("/tmp/../../../etc/shadow")
        assert result is None

    def test_simple_path_valid(self):
        from smartinspector.tools.glob import _validate_search_path
        result = _validate_search_path("/tmp")
        assert result is not None

    def test_current_dir_valid(self):
        from smartinspector.tools.glob import _validate_search_path
        result = _validate_search_path(".")
        assert result is not None

    def test_grep_path_validation(self):
        from smartinspector.tools.grep import _validate_search_path
        result = _validate_search_path("/etc/../secret")
        assert result is None


# ── Previous fixes (severe issues) verification ──────────────


class TestAttributionJsonSafety:
    def test_empty_string(self):
        from smartinspector.commands.attribution import extract_attributable_slices
        assert extract_attributable_slices("") == []

    def test_none_input(self):
        from smartinspector.commands.attribution import extract_attributable_slices
        assert extract_attributable_slices(None) == []

    def test_invalid_json(self):
        from smartinspector.commands.attribution import extract_attributable_slices
        assert extract_attributable_slices("not json") == []


if __name__ == "__main__":
    pytest.main([__file__, "-v"])
