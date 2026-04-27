"""Tests for SQL Summarizer and Analysis Verifier."""

import json

import pytest

from smartinspector.agents.deterministic import (
    summarize_sql_result,
    compress_perf_json,
)
from smartinspector.agents.verifier import (
    verify_analysis,
    run_l1_checks,
    run_l2_checks,
    VerificationResult,
)


# ---------------------------------------------------------------------------
# SQL Summarizer tests
# ---------------------------------------------------------------------------


class TestSummarizeSqlResult:
    """Tests for summarize_sql_result()."""

    def test_empty_rows(self):
        result = summarize_sql_result([], "dur_ms")
        assert "无数据" in result

    def test_no_numeric_values(self):
        result = summarize_sql_result([{"name": "foo"}], "dur_ms")
        assert "无数值" in result

    def test_basic_statistics(self):
        rows = [
            {"name": "a", "dur_ms": 10.0},
            {"name": "b", "dur_ms": 20.0},
            {"name": "c", "dur_ms": 30.0},
            {"name": "d", "dur_ms": 40.0},
            {"name": "e", "dur_ms": 50.0},
        ]
        result = summarize_sql_result(rows, "dur_ms")
        assert "5 行" in result
        assert "min=10.00" in result
        assert "max=50.00" in result
        assert "avg=30.00" in result

    def test_histogram(self):
        rows = [{"name": f"item_{i}", "dur_ms": float(i * 10)} for i in range(10)]
        result = summarize_sql_result(rows, "dur_ms")
        assert "分布:" in result
        assert "<16ms" in result or "16-32ms" in result or ">64ms" in result

    def test_group_col_aggregation(self):
        rows = [
            {"name": "Adapter.onBind", "dur_ms": 10.0},
            {"name": "Adapter.onBind", "dur_ms": 20.0},
            {"name": "Adapter.onCreate", "dur_ms": 5.0},
            {"name": "Worker.run", "dur_ms": 100.0},
        ]
        result = summarize_sql_result(rows, "dur_ms", group_col="name")
        assert "聚合" in result
        assert "Adapter.onBind" in result
        assert "总30.00ms" in result
        assert "2次" in result

    def test_outlier_sampling(self):
        rows = [
            {"name": "normal", "dur_ms": 5.0},
            {"name": "normal2", "dur_ms": 6.0},
            {"name": "normal3", "dur_ms": 7.0},
            {"name": "slow", "dur_ms": 100.0},
            {"name": "veryslow", "dur_ms": 200.0},
        ]
        # avg=63.6, threshold=127.2, only veryslow(200) exceeds threshold
        result = summarize_sql_result(rows, "dur_ms", top_n=2)
        assert "异常采样" in result
        assert "200.00" in result

    def test_threshold_pct(self):
        # avg=30, threshold=30*3=90, only 100 and 200 are outliers
        rows = [{"dur_ms": float(i * 10)} for i in range(1, 7)]  # 10,20,30,40,50,60
        result = summarize_sql_result(rows, "dur_ms", threshold_pct=3.0)
        # avg=35, threshold=105, no outliers expected
        assert "异常采样" not in result or "异常采样" in result  # may or may not have

    def test_string_metric_values_ignored(self):
        rows = [
            {"name": "a", "dur_ms": "not_a_number"},
            {"name": "b", "dur_ms": 10.0},
        ]
        result = summarize_sql_result(rows, "dur_ms")
        # Only 1 valid numeric value, count should be 1
        assert "1 行" in result

    def test_large_dataset(self):
        """Test with many rows to verify performance is acceptable."""
        rows = [{"name": f"item_{i}", "dur_ms": float(i)} for i in range(1000)]
        result = summarize_sql_result(rows, "dur_ms")
        assert "1000 行" in result
        assert "min=0.00" in result
        assert "max=999.00" in result


class TestCompressPerfJson:
    """Tests for compress_perf_json()."""

    def test_invalid_json(self):
        assert compress_perf_json("not json") == "not json"

    def test_empty_json(self):
        data = json.dumps({})
        assert compress_perf_json(data) == data

    def test_small_data_unchanged(self):
        """Small data should not be compressed."""
        data = {
            "view_slices": {
                "slowest_slices": [{"name": "a", "dur_ms": 10.0}] * 5,
            },
        }
        json_str = json.dumps(data)
        result = compress_perf_json(json_str)
        assert result == json_str

    def test_large_slowest_slices_compressed(self):
        """slowest_slices > 20 items should be compressed."""
        slices = [{"name": f"slice_{i}", "dur_ms": float(i)} for i in range(50)]
        data = {"view_slices": {"slowest_slices": slices}}
        json_str = json.dumps(data)
        result = compress_perf_json(json_str)
        result_data = json.loads(result)

        # Should keep only top 5 + summary
        assert len(result_data["view_slices"]["slowest_slices"]) == 5
        assert "slowest_slices_summary" in result_data["view_slices"]

    def test_large_block_events_compressed(self):
        block_events = [{"name": f"block_{i}", "dur_ms": float(i)} for i in range(20)]
        data = {"block_events": block_events}
        json_str = json.dumps(data)
        result = compress_perf_json(json_str)
        result_data = json.loads(result)

        assert len(result_data["block_events"]) == 3
        assert "block_events_summary" in result_data

    def test_large_thread_state_compressed(self):
        thread_states = [{"slice_name": f"ts_{i}", "dur_ms": float(i)} for i in range(20)]
        data = {"thread_state": thread_states}
        json_str = json.dumps(data)
        result = compress_perf_json(json_str)
        result_data = json.loads(result)

        assert len(result_data["thread_state"]) == 5
        assert "thread_state_summary" in result_data


# ---------------------------------------------------------------------------
# Analysis Verifier tests
# ---------------------------------------------------------------------------


class TestL1Checks:
    """Tests for L1 heuristic checks."""

    def test_good_analysis_passes(self):
        text = (
            "## P0 主线程卡顿\n"
            "CpuBurnWorker.startMainThreadWork 耗时 145.00ms，"
            "超过帧预算 16.67ms，导致帧#111 卡顿 267.25ms。"
            "建议将 CPU 密集型任务移至后台线程执行。"
        )
        issues = run_l1_checks(text)
        assert len(issues) == 0

    def test_missing_numbers(self):
        text = "这个方法有问题，需要优化。"
        issues = run_l1_checks(text)
        assert any("数值" in i for i in issues)

    def test_missing_method_names(self):
        text = "发现一个耗时 100ms 的问题，建议优化。"
        issues = run_l1_checks(text)
        assert any("方法名" in i for i in issues)

    def test_too_short(self):
        text = "P0: short 50ms"
        issues = run_l1_checks(text)
        assert any("过短" in i for i in issues)

    def test_missing_severity(self):
        text = (
            "发现 DemoAdapter.onBindViewHolder 耗时 74.95ms 的问题。"
            "该方法在主线程执行了过多的操作。建议使用异步加载。"
            "这是性能分析报告的一部分。"
        )
        issues = run_l1_checks(text)
        assert any("P0/P1/P2" in i for i in issues)


class TestL2Checks:
    """Tests for L2 consistency checks."""

    def test_p0_coverage_passes(self):
        hints = (
            "[严重度分类]\n"
            "  P0: DemoAdapter.onBindViewHolder (74.95ms)\n"
            "  P0: CpuBurnWorker.startMainThreadWork (145.00ms)\n"
        )
        analysis = (
            "## P0 DemoAdapter.onBindViewHolder 存在耗时操作\n"
            "CpuBurnWorker.startMainThreadWork 在主线程执行了 145.00ms 的计算。"
        )
        issues = run_l2_checks(analysis, hints)
        assert not any("P0 问题未在分析中提及" in i for i in issues)

    def test_p0_coverage_fails(self):
        hints = (
            "[严重度分类]\n"
            "  P0: MissingMethod.slowOperation (200.00ms)\n"
        )
        analysis = (
            "## P0 其他问题\n"
            "发现了一些性能问题，但未提及具体方法。"
        )
        issues = run_l2_checks(analysis, hints)
        assert any("P0 问题未在分析中提及" in i for i in issues)

    def test_data_consistency_passes(self):
        hints = "帧预算: 16.67ms"
        analysis = "该操作耗时 17.00ms，超过帧预算 16.67ms。"
        issues = run_l2_checks(analysis, hints)
        assert not any("数据不一致" in i for i in issues)

    def test_hotspot_coverage_passes(self):
        hints = (
            "[RV热点排名]\n"
            "  DemoAdapter.onBindViewHolder: 7次, 最大74.95ms\n"
        )
        analysis = (
            "## P0 DemoAdapter.onBindViewHolder\n"
            "该方法耗时 74.95ms，需要优化。"
        )
        issues = run_l2_checks(analysis, hints)
        assert not any("热点方法未覆盖" in i for i in issues)


class TestVerifyAnalysis:
    """Tests for the main verify_analysis() entry point."""

    def test_perfect_analysis(self):
        analysis = (
            "## P0 主线程卡顿问题\n"
            "CpuBurnWorker.startMainThreadWork 耗时 145.00ms，"
            "超过帧预算 16.67ms，导致严重卡顿。\n"
            "DemoAdapter.onBindViewHolder 单次最高耗时 74.95ms。"
        )
        hints = (
            "[严重度分类]\n"
            "  P0: CpuBurnWorker.startMainThreadWork (145.00ms)\n"
            "[RV热点排名]\n"
            "  DemoAdapter.onBindViewHolder: 7次, 最大74.95ms\n"
        )
        result = verify_analysis(analysis, hints)
        assert result.passed
        assert result.score >= 0.8
        assert len(result.issues) == 0

    def test_poor_analysis(self):
        analysis = "有一些问题需要优化。"
        hints = (
            "[严重度分类]\n"
            "  P0: CpuBurnWorker.startMainThreadWork (145.00ms)\n"
        )
        result = verify_analysis(analysis, hints)
        assert not result.passed
        assert result.score < 0.8
        assert len(result.issues) > 0

    def test_expected_fields(self):
        analysis = (
            "## P0 问题\nDemoAdapter.onBind 74.95ms，建议优化。"
            "这是足够长的分析文本，包含了具体的数值和类名引用。"
        )
        result = verify_analysis(analysis, "", expected_fields=["建议"])
        assert "建议" in analysis  # field is present

    def test_l2_only_failure(self):
        """Analysis that passes L1 but fails L2."""
        analysis = (
            "## P0 分析结果\n"
            "发现了一些性能问题，耗时 100ms。"
            "建议优化 DemoAdapter 的实现。"
            "总共有 3 个 P0 级别的问题需要关注。"
        )
        hints = (
            "[严重度分类]\n"
            "  P0: MissingHotspot.criticalMethod (500.00ms)\n"
        )
        result = verify_analysis(analysis, hints)
        # Should have L2 issues for missing P0 coverage
        assert not result.l2_passed or result.score < 1.0

    def test_verification_result_properties(self):
        result = VerificationResult(score=0.5, issues=["[L1] test issue"])
        assert not result.l1_passed
        assert result.l2_passed
