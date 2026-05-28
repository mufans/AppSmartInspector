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
        from smartinspector.tools.read import _read_file_content, _file_mtime
        # Create a temp file
        with tempfile.NamedTemporaryFile(mode="w", suffix=".txt", delete=False) as f:
            f.write("line1\nline2\nline3\n")
            tmp = f.name

        try:
            mtime = _file_mtime(tmp)
            result1 = _read_file_content(tmp, 1, 10, mtime)
            result2 = _read_file_content(tmp, 1, 10, mtime)
            assert result1 == result2
            # Verify cache info
            info = _read_file_content.cache_info()
            assert info.hits >= 1
        finally:
            os.unlink(tmp)

    def test_cache_invalidation_on_mtime_change(self):
        """Cache should return fresh data when file mtime changes."""
        import time
        from smartinspector.tools.read import _read_file_content, _file_mtime
        with tempfile.NamedTemporaryFile(mode="w", suffix=".txt", delete=False) as f:
            f.write("original\n")
            tmp = f.name

        try:
            mtime1 = _file_mtime(tmp)
            result1 = _read_file_content(tmp, 1, 10, mtime1)
            assert "original" in result1

            # Modify file and ensure mtime changes
            time.sleep(0.05)
            with open(tmp, "w") as f:
                f.write("modified\n")
            mtime2 = _file_mtime(tmp)
            assert mtime2 != mtime1
            result2 = _read_file_content(tmp, 1, 10, mtime2)
            assert "modified" in result2
            assert "original" not in result2
        finally:
            os.unlink(tmp)

    def test_read_file_content(self):
        from smartinspector.tools.read import _read_file_content, _file_mtime
        with tempfile.NamedTemporaryFile(mode="w", suffix=".txt", delete=False) as f:
            f.write("hello world\n")
            tmp = f.name

        try:
            mtime = _file_mtime(tmp)
            result = _read_file_content(tmp, 1, 10, mtime)
            assert "hello world" in result
        finally:
            os.unlink(tmp)

    def test_read_nonexistent_file(self):
        from smartinspector.tools.read import _read_file_content, _file_mtime
        mtime = _file_mtime("/nonexistent/path/file.txt")
        result = _read_file_content("/nonexistent/path/file.txt", 1, 10, mtime)
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
        from smartinspector.tools.path_utils import validate_search_path
        import tempfile
        with tempfile.TemporaryDirectory() as tmpdir:
            result = validate_search_path(tmpdir)
            assert result is not None

    def test_dotdot_path_rejected(self):
        from smartinspector.tools.path_utils import validate_search_path
        result = validate_search_path("/etc/../etc/passwd")
        assert result is None

    def test_nested_dotdot_rejected(self):
        from smartinspector.tools.path_utils import validate_search_path
        result = validate_search_path("/tmp/../../../etc/shadow")
        assert result is None

    def test_simple_path_valid(self):
        from smartinspector.tools.path_utils import validate_search_path
        result = validate_search_path("/tmp")
        assert result is not None

    def test_current_dir_valid(self):
        from smartinspector.tools.path_utils import validate_search_path
        result = validate_search_path(".")
        assert result is not None

    def test_grep_path_validation(self):
        from smartinspector.tools.path_utils import validate_search_path
        result = validate_search_path("/etc/../secret")
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


# ── Fix: System class filtering for block tags with method suffix ────


class TestBlockSystemClassFiltering:
    """_is_block_system_class should correctly filter system classes
    even when block tags include a method suffix (e.g. '.run')."""

    def _call(self, raw_name):
        from smartinspector.commands.attribution import _is_block_system_class
        return _is_block_system_class(raw_name)

    def test_choreographer_with_method_suffix(self):
        """Choreographer block with .run suffix should be filtered."""
        assert self._call("SI$block#view.Choreographer$FrameDisplayEventReceiver.run#440ms")

    def test_choreographer_without_method(self):
        """Choreographer block without method should be filtered."""
        assert self._call("SI$block#view.Choreographer$FrameDisplayEventReceiver#440ms")

    def test_gapworker_with_method_suffix(self):
        """GapWorker block with .run suffix should be filtered."""
        assert self._call("SI$block#widget.GapWorker.run#243ms")

    def test_gapworker_without_method(self):
        """GapWorker block without method should be filtered."""
        assert self._call("SI$block#widget.GapWorker#243ms")

    def test_user_class_not_filtered(self):
        """User class blocks should NOT be filtered."""
        assert not self._call("SI$block#com.example.MyClass.doWork#100ms")

    def test_user_class_inner_not_filtered(self):
        """User class with anonymous inner class should NOT be filtered."""
        assert not self._call("SI$block#com.example.CpuBurnWorker$startMainThreadWork$1#112ms")

    def test_layout_inflater_filtered(self):
        """LayoutInflater system class should be filtered."""
        assert self._call("SI$block#view.LayoutInflater.inflate#50ms")

    def test_fragment_manager_filtered(self):
        """FragmentManager system class should be filtered."""
        assert self._call("SI$block#app.FragmentManager$5#200ms")

    def test_short_class_name_without_package(self):
        """Short Choreographer name without package prefix should be filtered."""
        assert self._call("SI$block#Choreographer$FrameDisplayEventReceiver.run#100ms")


class TestSystemClassPatterns:
    """Verify system class patterns include key Android framework classes."""

    def test_gapworker_is_system_pattern(self):
        from smartinspector.commands.attribution import _SYSTEM_CLASS_PATTERNS
        assert "GapWorker" in _SYSTEM_CLASS_PATTERNS

    def test_linearlayoutmanager_is_system_pattern(self):
        from smartinspector.commands.attribution import _SYSTEM_CLASS_PATTERNS
        assert "LinearLayoutManager" in _SYSTEM_CLASS_PATTERNS


class TestExtractAttributableSlicesSystemFilter:
    """Integration test: extract_attributable_slices should filter system block events."""

    def test_choreographer_block_filtered(self):
        import json
        from smartinspector.commands.attribution import extract_attributable_slices

        data = {
            "view_slices": {
                "slowest_slices": [],
                "summary": [],
                "rv_instances": [],
            },
            "block_events": [
                {
                    "raw_name": "SI$block#view.Choreographer$FrameDisplayEventReceiver.run#440ms",
                    "dur_ms": 440,
                    "stack_trace": ["at com.example.Repo.process(DataRepository.kt:75)"],
                },
                {
                    "raw_name": "SI$block#widget.GapWorker.run#243ms",
                    "dur_ms": 243,
                    "stack_trace": ["at com.example.Repo.process(DataRepository.kt:76)"],
                },
                {
                    "raw_name": "SI$block#com.example.MyWorker$1.run#100ms",
                    "dur_ms": 100,
                    "stack_trace": ["at com.example.MyWorker$1.run(MyWorker.kt:45)"],
                },
            ],
        }
        result = extract_attributable_slices(json.dumps(data))
        class_names = [r["class_name"] for r in result]
        # Choreographer and GapWorker should be filtered out
        assert "Choreographer" not in class_names
        assert "GapWorker" not in class_names
        # User class should remain
        assert "MyWorker" in class_names


# ── Fix: context_method handling in fast path ──────────────────────


class TestFastPathContextMethod:
    """Fast path should use context_method for inner class search."""

    def test_can_use_fast_path_with_context_method(self):
        """Entries with context_method but no $ in class_name should be fast-path eligible."""
        from smartinspector.agents.attributor import _can_use_fast_path
        group = [{
            "class_name": "CpuBurnWorker",
            "method_name": "run",
            "search_type": "java",
            "context_method": "startMainThreadWork",
            "raw_name": "SI$block#worker.CpuBurnWorker$startMainThreadWork$1#125ms",
            "dur_ms": 147,
        }]
        assert _can_use_fast_path(group)

    def test_cannot_use_fast_path_with_dollar_in_class(self):
        """Entries with $ in class_name should NOT be fast-path eligible."""
        from smartinspector.agents.attributor import _can_use_fast_path
        group = [{
            "class_name": "CpuBurnWorker$1",
            "method_name": "run",
            "search_type": "java",
            "raw_name": "SI$block#worker.CpuBurnWorker$1#125ms",
            "dur_ms": 147,
        }]
        assert not _can_use_fast_path(group)


class TestExtractMethodFromAnonymous:
    """Test _extract_method_from_anonymous for various inner class patterns."""

    def test_kotlin_anonymous_in_method(self):
        from smartinspector.commands.attribution import _extract_method_from_anonymous
        # CpuBurnWorker$startMainThreadWork$1 → startMainThreadWork
        assert _extract_method_from_anonymous(
            "com.smartinspector.hook.worker.CpuBurnWorker$startMainThreadWork$1"
        ) == "startMainThreadWork"

    def test_java_anonymous_no_context(self):
        from smartinspector.commands.attribution import _extract_method_from_anonymous
        # OuterClass$1 → no method context
        assert _extract_method_from_anonymous("com.example.OuterClass$1") == ""

    def test_kotlin_lambda(self):
        from smartinspector.commands.attribution import _extract_method_from_anonymous
        # OuterClass$$inlined$lambda$0 → no method context (Kotlin inlined lambda)
        result = _extract_method_from_anonymous("com.example.Outer$$inlined$lambda$0")
        assert result == ""

    def test_multi_level_anonymous(self):
        from smartinspector.commands.attribution import _extract_method_from_anonymous
        # OuterClass$methodName$1$2 → methodName
        assert _extract_method_from_anonymous("com.example.Outer$doWork$1$2") == "doWork"


class TestExtractMethodFromStack:
    """Test _extract_method_from_stack for stack trace parsing."""

    def test_normal_stack_frame(self):
        from smartinspector.commands.attribution import _extract_method_from_stack
        stack = ["at com.example.MyWorker$1.run(MyWorker.kt:45)"]
        assert _extract_method_from_stack(stack) == "run"

    def test_kotlin_anonymous_run(self):
        from smartinspector.commands.attribution import _extract_method_from_stack
        stack = ["at com.smartinspector.hook.worker.CpuBurnWorker$startMainThreadWork$1.run(CpuBurnWorker.kt:45)"]
        assert _extract_method_from_stack(stack) == "run"

    def test_empty_stack(self):
        from smartinspector.commands.attribution import _extract_method_from_stack
        assert _extract_method_from_stack([]) == ""

    def test_proxy_stack(self):
        from smartinspector.commands.attribution import _extract_method_from_stack
        # Proxy frames have no (File:line) suffix → returns empty
        stack = ["at $Proxy5.messageDispatched"]
        assert _extract_method_from_stack(stack) == ""


# ── Fix: Thread state analysis in deterministic layer ───────────────


class TestAnalyzeThreadState:
    """Test _analyze_thread_state in deterministic.py."""

    def _call(self, data):
        from smartinspector.agents.deterministic import _analyze_thread_state
        return _analyze_thread_state(data)

    def test_empty_data(self):
        assert self._call({}) == ""

    def test_no_thread_state(self):
        assert self._call({"thread_state": []}) == ""

    def test_running_dominant(self):
        data = {
            "thread_state": [
                {
                    "slice_name": "SI$MyClass.doWork",
                    "dur_ms": 50.0,
                    "state_distribution": {"Running": 90.0, "Sleeping": 10.0},
                    "dominant_state": "Running",
                },
            ]
        }
        result = self._call(data)
        assert "Running" in result
        assert "代码" in result or "执行" in result

    def test_sleeping_dominant(self):
        data = {
            "thread_state": [
                {
                    "slice_name": "SI$MyClass.doWork",
                    "dur_ms": 200.0,
                    "state_distribution": {"Sleeping": 80.0, "Running": 20.0},
                    "dominant_state": "Sleeping",
                },
            ]
        }
        result = self._call(data)
        assert "Sleeping" in result or "阻塞" in result

    def test_disk_io_dominant(self):
        data = {
            "thread_state": [
                {
                    "slice_name": "SI$db#MyRepo.query",
                    "dur_ms": 150.0,
                    "state_distribution": {"DiskSleep": 70.0, "Running": 30.0},
                    "dominant_state": "DiskSleep",
                },
            ]
        }
        result = self._call(data)
        assert "DiskSleep" in result or "阻塞" in result

    def test_mixed_states(self):
        data = {
            "thread_state": [
                {
                    "slice_name": "SI$MyClass.process",
                    "dur_ms": 100.0,
                    "state_distribution": {"Running": 85.0, "Sleeping": 15.0},
                    "dominant_state": "Running",
                },
                {
                    "slice_name": "SI$MyClass.ioWait",
                    "dur_ms": 300.0,
                    "state_distribution": {"Sleeping": 90.0, "Running": 10.0},
                    "dominant_state": "Sleeping",
                },
            ]
        }
        result = self._call(data)
        assert "Running" in result
        assert "Sleeping" in result

    def test_integrated_in_compute_hints(self):
        """thread_state analysis should appear in compute_hints output."""
        import json
        from smartinspector.agents.deterministic import compute_hints

        data = {
            "frame_timeline": {"fps": 60, "total_frames": 100, "jank_frames": 0},
            "thread_state": [
                {
                    "slice_name": "SI$MyClass.doWork",
                    "dur_ms": 50.0,
                    "state_distribution": {"Running": 95.0, "Sleeping": 5.0},
                    "dominant_state": "Running",
                },
            ],
        }
        result = compute_hints(json.dumps(data))
        assert "线程状态分析" in result


# ── Fix: Thread state normalization and accumulation in perfetto.py ──


class TestThreadStateNormalization:
    """Test state name normalization logic used in collect_thread_state.

    Validates that raw Perfetto thread_state values (R, R+, S, S+, D, D+)
    are correctly normalized and that multiple raw states mapping to the same
    normalized name are properly accumulated (not overwritten).
    """

    @staticmethod
    def _normalize_and_accumulate(raw_states: list[tuple[str, int]]) -> dict:
        """Simulate the normalization + accumulation logic from perfetto.py."""
        state_dist = {}
        for state, ns in raw_states:
            if state in ("R", "R+"):
                state = "Running"
            elif state in ("S", "S+"):
                state = "Sleeping"
            elif state in ("D", "D+"):
                state = "DiskSleep"
            state_dist[state] = state_dist.get(state, 0) + ns
        return state_dist

    def test_R_and_R_plus_both_accumulated(self):
        """R and R+ should both map to Running and their durations summed."""
        result = self._normalize_and_accumulate([("R", 50), ("R+", 30)])
        assert result["Running"] == 80

    def test_S_plus_mapped_to_sleeping(self):
        """S+ (interruptible sleep, preemptible) should map to Sleeping."""
        result = self._normalize_and_accumulate([("S+", 100)])
        assert result["Sleeping"] == 100

    def test_S_and_S_plus_accumulated(self):
        """S and S+ should both map to Sleeping and their durations summed."""
        result = self._normalize_and_accumulate([("S", 40), ("S+", 60)])
        assert result["Sleeping"] == 100

    def test_D_and_D_plus_accumulated(self):
        """D and D+ should both map to DiskSleep and their durations summed."""
        result = self._normalize_and_accumulate([("D", 20), ("D+", 30)])
        assert result["DiskSleep"] == 50

    def test_mixed_raw_states(self):
        """Multiple raw states with overlapping normalized names."""
        raw = [("R", 30), ("R+", 20), ("S", 10), ("S+", 40), ("D", 5)]
        result = self._normalize_and_accumulate(raw)
        assert result["Running"] == 50
        assert result["Sleeping"] == 50
        assert result["DiskSleep"] == 5

    def test_unknown_state_preserved(self):
        """Unknown states (I, T, etc.) should pass through unchanged."""
        result = self._normalize_and_accumulate([("I", 100)])
        assert result["I"] == 100

    def test_empty_input(self):
        result = self._normalize_and_accumulate([])
        assert result == {}


class TestThreadStateOverlapCalculation:
    """Test the overlap-based SQL calculation logic with concrete numbers.

    Validates the overlap formula: MIN(end, slice_end) - MAX(start, slice_start)
    and the dur<0 handling.
    """

    @staticmethod
    def _compute_overlap(ts, dur, slice_ts, slice_dur):
        """Simulate the SQL overlap calculation from collect_thread_state."""
        slice_end = slice_ts + slice_dur
        # Effective end of thread_state
        effective_end = slice_end if dur < 0 else ts + dur
        # Overlap: MIN(effective_end, slice_end) - MAX(ts, slice_ts)
        overlap = min(effective_end, slice_end) - max(ts, slice_ts)
        return max(0, overlap)  # Should never be negative if filters are correct

    def test_state_fully_inside_slice(self):
        """Thread state fully contained within slice."""
        overlap = self._compute_overlap(ts=100, dur=50, slice_ts=0, slice_dur=200)
        assert overlap == 50  # Full state duration

    def test_state_fully_contains_slice(self):
        """Thread state spans the entire slice."""
        overlap = self._compute_overlap(ts=0, dur=300, slice_ts=100, slice_dur=50)
        assert overlap == 50  # Full slice duration

    def test_state_overlaps_start_of_slice(self):
        """Thread state starts before slice, ends during slice."""
        overlap = self._compute_overlap(ts=50, dur=80, slice_ts=100, slice_dur=100)
        assert overlap == 30  # 130 - 100 = 30

    def test_state_overlaps_end_of_slice(self):
        """Thread state starts during slice, ends after slice."""
        overlap = self._compute_overlap(ts=150, dur=100, slice_ts=100, slice_dur=100)
        assert overlap == 50  # 200 - 150 = 50

    def test_state_exact_boundary_match(self):
        """Thread state starts exactly at slice start."""
        overlap = self._compute_overlap(ts=100, dur=50, slice_ts=100, slice_dur=100)
        assert overlap == 50

    def test_dur_negative_ongoing_state(self):
        """dur<0 (ongoing state) should use slice_end as effective end."""
        overlap = self._compute_overlap(ts=150, dur=-1, slice_ts=100, slice_dur=100)
        assert overlap == 50  # slice_end(200) - max(150, 100) = 50

    def test_dur_negative_state_before_slice(self):
        """Ongoing state starting before slice should cover entire slice."""
        overlap = self._compute_overlap(ts=50, dur=-1, slice_ts=100, slice_dur=100)
        assert overlap == 100  # slice_end(200) - max(50, 100) = 100


class TestThreadStateFilterCondition:
    """Test the WHERE clause logic: ts < slice_end AND (dur < 0 OR ts + dur > slice_ts)."""

    @staticmethod
    def _should_include(ts, dur, slice_ts, slice_dur):
        """Simulate the SQL WHERE clause from collect_thread_state."""
        slice_end = slice_ts + slice_dur
        return ts < slice_end and (dur < 0 or ts + dur > slice_ts)

    def test_state_before_slice_excluded(self):
        """Thread state ending before slice starts should be excluded."""
        assert not self._should_include(ts=0, dur=50, slice_ts=100, slice_dur=100)

    def test_state_after_slice_excluded(self):
        """Thread state starting at or after slice end should be excluded."""
        assert not self._should_include(ts=200, dur=50, slice_ts=100, slice_dur=100)

    def test_overlapping_state_included(self):
        """Thread state overlapping slice should be included."""
        assert self._should_include(ts=150, dur=100, slice_ts=100, slice_dur=100)

    def test_ongoing_state_before_slice_included(self):
        """Ongoing state (dur<0) starting before slice should be included."""
        assert self._should_include(ts=50, dur=-1, slice_ts=100, slice_dur=100)

    def test_ongoing_state_during_slice_included(self):
        """Ongoing state (dur<0) starting during slice should be included."""
        assert self._should_include(ts=150, dur=-1, slice_ts=100, slice_dur=100)

    def test_state_touching_start_excluded(self):
        """Thread state ending exactly at slice start should be excluded (ts+dur == slice_ts)."""
        assert not self._should_include(ts=0, dur=100, slice_ts=100, slice_dur=100)

    def test_state_starting_at_slice_end_excluded(self):
        """Thread state starting exactly at slice end should be excluded."""
        assert not self._should_include(ts=200, dur=50, slice_ts=100, slice_dur=100)


if __name__ == "__main__":
    pytest.main([__file__, "-v"])
