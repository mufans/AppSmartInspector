"""Tests for deterministic analysis modules."""

import json

import pytest

from smartinspector.agents.deterministic import (
    compute_hints,
    _classify_severity,
    _compute_call_chain_distribution,
    _rank_rv_hotspots,
    _correlate_jank_frames,
    _identify_cpu_hotspots,
    _analyze_thread_state,
    _analyze_io_slices,
    _analyze_memory,
    _detect_empty_scenario,
    _detect_frame_budget_ms,
)


# ---------------------------------------------------------------------------
# Helper 0: Empty scenario detection
# ---------------------------------------------------------------------------


class TestDetectEmptyScenario:
    def test_empty_ui_activity(self):
        data = {
            "frame_timeline": {"fps": 0, "total_frames": 0},
            "cpu_usage": {"cpu_usage_pct": 10},
        }
        result = _detect_empty_scenario(data)
        assert "疑似无UI活动" in result

    def test_active_ui(self):
        data = {
            "frame_timeline": {"fps": 60, "total_frames": 100},
            "cpu_usage": {"cpu_usage_pct": 30},
        }
        result = _detect_empty_scenario(data)
        assert result == ""

    def test_partial_activity(self):
        """Even with high CPU, if FPS=0 and no frames, still empty."""
        data = {
            "frame_timeline": {"fps": 0, "total_frames": 0},
            "cpu_usage": {"cpu_usage_pct": 50},
        }
        result = _detect_empty_scenario(data)
        assert result == ""

    def test_missing_data(self):
        data = {}
        result = _detect_empty_scenario(data)
        # fps=0, total_frames=0, cpu_pct=0 → should detect empty
        assert "疑似无UI活动" in result


# ---------------------------------------------------------------------------
# Helper 1: Severity classification
# ---------------------------------------------------------------------------


class TestClassifySeverity:
    def test_no_custom_slices(self):
        data = {"view_slices": {"slowest_slices": []}}
        assert _classify_severity(data) == ""

    def test_p0_issue(self):
        data = {
            "view_slices": {
                "slowest_slices": [
                    {"name": "SI$MyClass.slow", "dur_ms": 50.0, "is_custom": True},
                ]
            }
        }
        result = _classify_severity(data)
        assert "P0" in result
        assert "MyClass.slow" in result
        assert "50.00ms" in result

    def test_p1_issue(self):
        data = {
            "view_slices": {
                "slowest_slices": [
                    {"name": "SI$MyClass.medium", "dur_ms": 8.0, "is_custom": True},
                ]
            }
        }
        result = _classify_severity(data, frame_budget_ms=16.67)
        assert "P1" in result

    def test_p2_issue(self):
        data = {
            "view_slices": {
                "slowest_slices": [
                    {"name": "SI$MyClass.fast", "dur_ms": 2.0, "is_custom": True},
                ]
            }
        }
        result = _classify_severity(data, frame_budget_ms=16.67)
        assert "P2" in result

    def test_below_threshold_excluded(self):
        data = {
            "view_slices": {
                "slowest_slices": [
                    {"name": "SI$MyClass.tiny", "dur_ms": 0.5, "is_custom": True},
                ]
            }
        }
        result = _classify_severity(data)
        assert result == ""

    def test_non_custom_excluded(self):
        data = {
            "view_slices": {
                "slowest_slices": [
                    {"name": "doFrame", "dur_ms": 50.0, "is_custom": False},
                ]
            }
        }
        result = _classify_severity(data)
        assert result == ""

    def test_120hz_device(self):
        """On 120Hz device, frame budget is 8.33ms."""
        data = {
            "view_slices": {
                "slowest_slices": [
                    {"name": "SI$MyClass.medium", "dur_ms": 10.0, "is_custom": True},
                ]
            }
        }
        result = _classify_severity(data, frame_budget_ms=8.33)
        assert "P0" in result

    def test_multiple_severity_levels(self):
        data = {
            "view_slices": {
                "slowest_slices": [
                    {"name": "SI$A.slow", "dur_ms": 50.0, "is_custom": True},
                    {"name": "SI$B.medium", "dur_ms": 8.0, "is_custom": True},
                    {"name": "SI$C.fast", "dur_ms": 2.0, "is_custom": True},
                ]
            }
        }
        result = _classify_severity(data, frame_budget_ms=16.67)
        assert "P0" in result
        assert "P1" in result
        assert "P2" in result


# ---------------------------------------------------------------------------
# Helper 2: Call-chain distribution
# ---------------------------------------------------------------------------


class TestComputeCallChainDistribution:
    def test_no_chains(self):
        data = {"view_slices": {"call_chains": []}}
        assert _compute_call_chain_distribution(data) == ""

    def test_basic_chain(self):
        data = {
            "view_slices": {
                "call_chains": [
                    {
                        "name": "SI$MyClass.doWork",
                        "dur_ms": 100.0,
                        "breakdown": [
                            {"name": "SI$A.step1", "dur_ms": 60.0},
                            {"name": "SI$B.step2", "dur_ms": 30.0},
                        ],
                    }
                ]
            }
        }
        result = _compute_call_chain_distribution(data)
        assert "调用链时间分布" in result
        assert "MyClass.doWork" in result

    def test_nested_breakdown(self):
        data = {
            "view_slices": {
                "call_chains": [
                    {
                        "name": "SI$Main.run",
                        "dur_ms": 100.0,
                        "breakdown": [
                            {
                                "name": "SI$A.step",
                                "dur_ms": 80.0,
                                "children": [
                                    {"name": "SI$B.substep", "dur_ms": 40.0},
                                ],
                            },
                        ],
                    }
                ]
            }
        }
        result = _compute_call_chain_distribution(data)
        assert "A.step" in result
        assert "B.substep" in result


# ---------------------------------------------------------------------------
# Helper 3: RV hotspots ranking
# ---------------------------------------------------------------------------


class TestRankRvHotspots:
    def test_no_instances(self):
        data = {"view_slices": {"rv_instances": []}}
        assert _rank_rv_hotspots(data) == ""

    def test_basic_ranking(self):
        data = {
            "view_slices": {
                "rv_instances": [
                    {
                        "view_id": "recycler",
                        "adapter_name": "DemoAdapter",
                        "methods": {
                            "onBindViewHolder": {
                                "count": 10,
                                "max_ms": 75.0,
                                "total_ms": 400.0,
                            },
                            "onCreateViewHolder": {
                                "count": 3,
                                "max_ms": 20.0,
                                "total_ms": 50.0,
                            },
                        },
                    }
                ]
            }
        }
        result = _rank_rv_hotspots(data)
        assert "RV热点排名" in result
        assert "onBindViewHolder" in result
        assert "75.00ms" in result
        # avg = 400/10 = 40ms
        assert "40.00ms" in result

    def test_empty_methods(self):
        data = {
            "view_slices": {
                "rv_instances": [
                    {
                        "view_id": "recycler",
                        "adapter_name": "DemoAdapter",
                        "methods": {},
                    }
                ]
            }
        }
        result = _rank_rv_hotspots(data)
        assert result == ""


# ---------------------------------------------------------------------------
# Helper 5: CPU hotspots
# ---------------------------------------------------------------------------


class TestIdentifyCpuHotspots:
    def test_no_cpu_data(self):
        assert _identify_cpu_hotspots({}) == ""

    def test_no_top_processes(self):
        assert _identify_cpu_hotspots({"cpu_usage": {"top_processes": []}}) == ""

    def test_hot_threads(self):
        data = {
            "cpu_usage": {
                "cpu_usage_pct": 45.0,
                "num_cpus": 8,
                "top_processes": [
                    {
                        "name": "myapp",
                        "cpu_pct": 30.0,
                        "threads": [
                            {"name": "main", "cpu_pct": 25.0},
                            {"name": "bg", "cpu_pct": 3.0},
                        ],
                    }
                ],
            }
        }
        result = _identify_cpu_hotspots(data)
        assert "CPU热点" in result
        assert "总CPU" in result
        assert "45.0%" in result
        assert "main" in result

    def test_low_cpu_skipped(self):
        data = {
            "cpu_usage": {
                "top_processes": [
                    {
                        "name": "system",
                        "cpu_pct": 2.0,
                        "threads": [
                            {"name": "t1", "cpu_pct": 1.0},
                        ],
                    }
                ],
            }
        }
        result = _identify_cpu_hotspots(data)
        assert result == ""


# ---------------------------------------------------------------------------
# Helper 6: Thread state analysis
# ---------------------------------------------------------------------------


class TestAnalyzeThreadStateDetailed:
    """More detailed tests beyond those in test_high_priority_fixes.py."""

    def test_touch_events_not_excluded_at_deterministic_level(self):
        """Thread state analysis includes all slices; touch filtering is done at collector level."""
        data = {
            "thread_state": [
                {
                    "slice_name": "SI$touch#MainActivity#DOWN",
                    "dur_ms": 50.0,
                    "state_distribution": {"Running": 90.0},
                    "dominant_state": "Running",
                },
            ]
        }
        result = _analyze_thread_state(data)
        # Thread state analysis processes all slices; touch is a valid Running slice
        assert "线程状态分析" in result

    def test_multiple_slices_sorted(self):
        data = {
            "thread_state": [
                {
                    "slice_name": "SI$A.fast",
                    "dur_ms": 10.0,
                    "state_distribution": {"Running": 90.0},
                    "dominant_state": "Running",
                },
                {
                    "slice_name": "SI$B.slow",
                    "dur_ms": 200.0,
                    "state_distribution": {"Sleeping": 80.0},
                    "dominant_state": "Sleeping",
                },
            ]
        }
        result = _analyze_thread_state(data)
        # Should be sorted by dur_ms descending
        assert result.index("B.slow") < result.index("A.fast")


# ---------------------------------------------------------------------------
# Helper 7: IO slices analysis
# ---------------------------------------------------------------------------


class TestAnalyzeIoSlices:
    def test_no_io_data(self):
        assert _analyze_io_slices({}) == ""

    def test_basic_io(self):
        data = {
            "io_slices": {
                "summary": [
                    {
                        "name": "SI$net#com.example.ApiClient.get",
                        "max_ms": 200.0,
                        "count": 5,
                        "total_ms": 800.0,
                    },
                ],
            }
        }
        result = _analyze_io_slices(data)
        assert "IO分析" in result
        assert "ApiClient" in result

    def test_io_types(self):
        data = {
            "io_slices": {
                "summary": [
                    {"name": "SI$net#a.b", "max_ms": 100.0, "count": 1, "total_ms": 100.0, "io_type": "network"},
                    {"name": "SI$db#c.d", "max_ms": 50.0, "count": 1, "total_ms": 50.0, "io_type": "database"},
                    {"name": "SI$img#e.f", "max_ms": 80.0, "count": 1, "total_ms": 80.0, "io_type": "image"},
                ],
            }
        }
        result = _analyze_io_slices(data)
        assert "网络IO" in result
        assert "数据库IO" in result
        assert "图片加载" in result


# ---------------------------------------------------------------------------
# Helper 8: Memory analysis
# ---------------------------------------------------------------------------


class TestAnalyzeMemory:
    def test_no_memory_data(self):
        assert _analyze_memory({}) == ""

    def test_basic_memory_with_heap_objects(self):
        data = {
            "memory": {
                "heap_objects": [
                    {"class_name": "java.lang.String", "obj_count": 5000, "total_size_kb": 1024.0},
                ],
            }
        }
        result = _analyze_memory(data)
        assert "内存分配分析" in result
        assert "String" in result

    def test_memory_with_leak_suspects(self):
        data = {
            "memory": {
                "leak_suspects": [
                    {"class_name": "com.example.LeakedActivity", "obj_count": 2, "total_size_kb": 512.0},
                ],
            }
        }
        result = _analyze_memory(data)
        assert "内存分配分析" in result
        assert "LeakedActivity" in result


# ---------------------------------------------------------------------------
# Frame budget detection
# ---------------------------------------------------------------------------


class TestDetectFrameBudget:
    def test_default_60hz(self):
        data = {}
        assert _detect_frame_budget_ms(data) == 16.67

    def test_120hz_detected_from_expected_dur(self):
        """120Hz device has ~8.33ms frame budget in expected_dur_ms."""
        data = {
            "frame_timeline": {
                "jank_detail": [
                    {"expected_dur_ms": 8.33},
                    {"expected_dur_ms": 8.33},
                    {"expected_dur_ms": 8.33},
                ]
            }
        }
        assert _detect_frame_budget_ms(data) == pytest.approx(8.33, abs=0.01)

    def test_90hz_detected_from_expected_dur(self):
        data = {
            "frame_timeline": {
                "slowest_frames": [
                    {"expected_dur_ms": 11.11},
                    {"expected_dur_ms": 11.11},
                ]
            }
        }
        assert _detect_frame_budget_ms(data) == pytest.approx(11.11, abs=0.01)

    def test_zero_expected_dur_default(self):
        data = {"frame_timeline": {"jank_detail": [{"expected_dur_ms": 0}]}}
        assert _detect_frame_budget_ms(data) == 16.67


# ---------------------------------------------------------------------------
# compute_hints integration
# ---------------------------------------------------------------------------


class TestComputeHints:
    def test_invalid_json(self):
        assert compute_hints("not json") == ""

    def test_empty_data(self):
        result = compute_hints("{}")
        # Should detect empty scenario
        assert "疑似无UI活动" in result

    def test_severity_in_hints(self):
        data = {
            "frame_timeline": {"fps": 60, "total_frames": 100},
            "view_slices": {
                "slowest_slices": [
                    {"name": "SI$MyClass.slow", "dur_ms": 50.0, "is_custom": True},
                ],
            },
        }
        result = compute_hints(json.dumps(data))
        assert "严重度分类" in result
        assert "P0" in result

    def test_all_sections(self):
        """Test that all sections appear when relevant data is present."""
        data = {
            "frame_timeline": {"fps": 60, "total_frames": 100, "jank_detail": [
                {"frame_index": 1, "dur_ms": 30.0, "ts_ns": 1_000_000_000},
            ]},
            "view_slices": {
                "slowest_slices": [
                    {"name": "SI$A.slow", "dur_ms": 50.0, "is_custom": True, "ts_ns": 1_000_000_000},
                ],
                "call_chains": [
                    {"name": "SI$A.slow", "dur_ms": 100.0, "breakdown": [
                        {"name": "SI$B.step", "dur_ms": 60.0},
                    ]},
                ],
                "rv_instances": [
                    {
                        "view_id": "rv",
                        "adapter_name": "Adapter",
                        "methods": {"onBind": {"count": 5, "max_ms": 50.0, "total_ms": 200.0}},
                    },
                ],
            },
            "thread_state": [
                {
                    "slice_name": "SI$A.slow",
                    "dur_ms": 50.0,
                    "state_distribution": {"Running": 90.0},
                    "dominant_state": "Running",
                },
            ],
            "cpu_usage": {
                "cpu_usage_pct": 30.0,
                "num_cpus": 8,
                "top_processes": [
                    {"name": "myapp", "cpu_pct": 25.0, "threads": [
                        {"name": "main", "cpu_pct": 20.0},
                    ]},
                ],
            },
        }
        result = compute_hints(json.dumps(data))
        assert "严重度分类" in result
        assert "调用链时间分布" in result
        assert "RV热点排名" in result
        assert "CPU热点" in result
        assert "线程状态分析" in result


if __name__ == "__main__":
    pytest.main([__file__, "-v"])
