"""Tests for unified SI$ tag parser (si_tag.py) and attribution wrappers."""

import json

import pytest

from smartinspector.si_tag import (
    SITag,
    parse_si_tag,
    _split_fqn_method,
    _extract_method_from_anonymous,
    SYSTEM_PREFIXES,
    SYSTEM_CLASS_PATTERNS,
    RV_PIPELINE_METHODS,
)
from smartinspector.commands.attribution import (
    extract_class,
    extract_method,
    extract_fqn,
    classify_search_type,
    is_system_class,
    is_system_method,
)


# ---------------------------------------------------------------------------
# parse_si_tag — basic behavior
# ---------------------------------------------------------------------------


class TestParseSiTagBasic:
    """Basic parse_si_tag() behavior tests."""

    def test_non_si_tag_returns_none(self):
        assert parse_si_tag("") is None
        assert parse_si_tag("regular.slice") is None
        assert parse_si_tag("Choreographer#doFrame") is None

    def test_default_tag(self):
        tag = parse_si_tag("SI$com.example.ClassName.method")
        assert tag is not None
        assert tag.tag_type == "default"
        assert tag.class_name == "ClassName"
        assert tag.method_name == "method"
        assert tag.fqn == "com.example.ClassName"
        assert tag.search_type == "java"
        assert tag.io_type is None
        assert tag.raw_name == "SI$com.example.ClassName.method"

    def test_tag_without_method(self):
        """Bare class FQN without method segment."""
        tag = parse_si_tag("SI$com.example.ClassName")
        assert tag is not None
        assert tag.class_name == "ClassName"
        assert tag.method_name == "unknown"
        assert tag.fqn == "com.example.ClassName"


# ---------------------------------------------------------------------------
# parse_si_tag — block# tags
# ---------------------------------------------------------------------------


class TestParseSiTagBlock:
    """Tests for SI$block# tag parsing."""

    def test_block_with_method(self):
        tag = parse_si_tag("SI$block#com.example.Worker.run#250ms")
        assert tag.tag_type == "block"
        assert tag.class_name == "Worker"
        assert tag.method_name == "run"
        assert tag.fqn == "com.example.Worker"
        assert tag.extras.get("duration_ms") == 250.0

    def test_block_with_anonymous_inner_class(self):
        tag = parse_si_tag(
            "SI$block#worker.CpuBurnWorker$startMainThreadWork$1#129ms"
        )
        assert tag.tag_type == "block"
        assert tag.class_name == "CpuBurnWorker"
        assert tag.method_name == "startMainThreadWork"
        assert tag.extras.get("duration_ms") == 129.0

    def test_block_without_duration(self):
        tag = parse_si_tag("SI$block#com.example.Worker.run")
        assert tag.tag_type == "block"
        assert tag.class_name == "Worker"
        assert tag.method_name == "run"
        assert "duration_ms" not in tag.extras

    def test_block_class_name_strips_dollar(self):
        """Anonymous inner class $N suffix should be stripped for class name."""
        tag = parse_si_tag("SI$block#com.example.MyClass$1.run#50ms")
        assert tag.class_name == "MyClass"

    def test_block_pure_anonymous_no_method(self):
        """Pure $N anonymous class with no enclosing method in FQN."""
        tag = parse_si_tag("SI$block#com.example.MyClass$1#50ms")
        assert tag.class_name == "MyClass"
        # method_name is unknown since there's no method in $1
        assert tag.method_name == "unknown"


# ---------------------------------------------------------------------------
# parse_si_tag — RV# tags
# ---------------------------------------------------------------------------


class TestParseSiTagRV:
    """Tests for SI$RV# tag parsing."""

    def test_rv_full_format(self):
        tag = parse_si_tag("SI$RV#recycler_view#com.example.DemoAdapter.onBindViewHolder")
        assert tag.tag_type == "RV"
        assert tag.class_name == "DemoAdapter"
        assert tag.method_name == "onBindViewHolder"
        assert tag.fqn == "com.example.DemoAdapter"
        assert tag.extras.get("view_id") == "recycler_view"

    def test_rv_without_view_id(self):
        tag = parse_si_tag("SI$RV#adapter")
        assert tag.tag_type == "RV"
        assert tag.class_name == "RV#adapter"  # No "#", so falls to else branch

    def test_rv_without_method(self):
        tag = parse_si_tag("SI$RV#view#com.example.Adapter")
        assert tag.tag_type == "RV"
        assert tag.class_name == "Adapter"
        assert tag.method_name == "unknown"


# ---------------------------------------------------------------------------
# parse_si_tag — inflate# tags
# ---------------------------------------------------------------------------


class TestParseSiTagInflate:
    """Tests for SI$inflate# tag parsing."""

    def test_inflate_with_parent(self):
        tag = parse_si_tag("SI$inflate#item_complex#recycler_view")
        assert tag.tag_type == "inflate"
        assert tag.class_name == "item_complex"
        assert tag.method_name == "inflate"
        assert tag.search_type == "xml"
        assert tag.extras.get("parent") == "recycler_view"

    def test_inflate_without_parent(self):
        tag = parse_si_tag("SI$inflate#simple_layout")
        assert tag.tag_type == "inflate"
        assert tag.class_name == "simple_layout"
        assert tag.search_type == "xml"


# ---------------------------------------------------------------------------
# parse_si_tag — view# tags
# ---------------------------------------------------------------------------


class TestParseSiTagView:
    """Tests for SI$view# tag parsing."""

    def test_view_with_method(self):
        tag = parse_si_tag("SI$view#com.example.HeavyDrawView.onDraw")
        assert tag.tag_type == "view"
        assert tag.class_name == "HeavyDrawView"
        assert tag.method_name == "onDraw"
        assert tag.fqn == "com.example.HeavyDrawView"

    def test_view_without_method(self):
        tag = parse_si_tag("SI$view#com.example.CustomView")
        assert tag.tag_type == "view"
        assert tag.class_name == "CustomView"
        assert tag.method_name == "unknown"


# ---------------------------------------------------------------------------
# parse_si_tag — handler# tags
# ---------------------------------------------------------------------------


class TestParseSiTagHandler:
    """Tests for SI$handler# tag parsing."""

    def test_handler_with_method(self):
        tag = parse_si_tag("SI$handler#com.example.Callback.onClick")
        assert tag.tag_type == "handler"
        assert tag.class_name == "Callback"
        assert tag.method_name == "onClick"
        assert tag.fqn == "com.example.Callback"

    def test_handler_with_hash_suffix(self):
        tag = parse_si_tag("SI$handler#com.example.Runnable.run#extra")
        assert tag.tag_type == "handler"
        assert tag.class_name == "Runnable"
        assert tag.method_name == "run"


# ---------------------------------------------------------------------------
# parse_si_tag — IO tags (db#, net#, img#)
# ---------------------------------------------------------------------------


class TestParseSiTagIO:
    """Tests for SI$ IO tag parsing."""

    def test_db_tag(self):
        tag = parse_si_tag("SI$db#com.example.DBHelper.query#users_table")
        assert tag.tag_type == "db"
        assert tag.class_name == "DBHelper"
        assert tag.method_name == "query"
        assert tag.io_type == "database"
        assert tag.extras.get("table") == "users_table"

    def test_db_tag_without_table(self):
        tag = parse_si_tag("SI$db#com.example.Repo.insert")
        assert tag.tag_type == "db"
        assert tag.io_type == "database"
        assert "table" not in tag.extras

    def test_net_tag(self):
        tag = parse_si_tag("SI$net#com.example.ApiClient.execute")
        assert tag.tag_type == "net"
        assert tag.class_name == "ApiClient"
        assert tag.method_name == "execute"
        assert tag.io_type == "network"

    def test_img_tag(self):
        tag = parse_si_tag("SI$img#com.example.GlideLoader.into")
        assert tag.tag_type == "img"
        assert tag.class_name == "GlideLoader"
        assert tag.method_name == "into"
        assert tag.io_type == "image"


# ---------------------------------------------------------------------------
# parse_si_tag — touch# tags
# ---------------------------------------------------------------------------


class TestParseSiTagTouch:
    """Tests for SI$touch# tag parsing."""

    def test_touch_tag(self):
        tag = parse_si_tag("SI$touch#MainActivity#ACTION_DOWN")
        assert tag.tag_type == "touch"
        assert tag.search_type == "system"


# ---------------------------------------------------------------------------
# SITag properties
# ---------------------------------------------------------------------------


class TestSITagProperties:
    """Tests for SITag.is_system and is_system_method properties."""

    def test_system_by_fqn_prefix(self):
        tag = parse_si_tag("SI$view#android.view.Choreographer.doFrame")
        assert tag.is_system is True

    def test_system_by_class_pattern(self):
        tag = parse_si_tag("SI$view#Choreographer.doFrame")
        assert tag.is_system is True

    def test_system_by_class_pattern_with_dollar(self):
        tag = parse_si_tag("SI$view#FragmentManager$5")
        assert tag.is_system is True

    def test_not_system_user_class(self):
        tag = parse_si_tag("SI$view#com.example.MyClass.doWork")
        assert tag.is_system is False

    def test_is_system_method_rv_pipeline(self):
        tag = parse_si_tag("SI$RV#recycler#com.example.Adapter.dispatchLayoutStep2")
        assert tag.is_system_method is True

    def test_is_not_system_method(self):
        tag = parse_si_tag("SI$RV#recycler#com.example.Adapter.onBindViewHolder")
        assert tag.is_system_method is False


# ---------------------------------------------------------------------------
# Backward-compatible wrapper functions
# ---------------------------------------------------------------------------


class TestExtractClass:
    """Tests for extract_class() wrapper."""

    def test_all_tag_types(self):
        assert extract_class("SI$com.example.ClassName.method") == "ClassName"
        assert extract_class("SI$RV#vid#com.example.Adapter.onBind") == "Adapter"
        assert extract_class("SI$inflate#layout#parent") == "layout"
        assert extract_class("SI$view#com.example.View.onDraw") == "View"
        assert extract_class("SI$handler#com.example.Callback.run") == "Callback"
        assert extract_class("SI$block#com.example.Worker.run#250ms") == "Worker"
        assert extract_class("SI$db#com.example.DB.query#tbl") == "DB"
        assert extract_class("SI$net#com.example.Client.exec") == "Client"
        assert extract_class("SI$img#com.example.Loader.into") == "Loader"

    def test_non_si_tag_fallback(self):
        """Non-SI$ input should use _split_fqn_method fallback."""
        assert extract_class("com.example.Foo.bar") == "Foo"


class TestExtractMethod:
    """Tests for extract_method() wrapper."""

    def test_all_tag_types(self):
        assert extract_method("SI$com.example.Class.method") == "method"
        assert extract_method("SI$RV#vid#com.example.Adapter.onBind") == "onBind"
        assert extract_method("SI$inflate#layout#parent") == "inflate"
        assert extract_method("SI$view#com.example.View.onDraw") == "onDraw"
        assert extract_method("SI$handler#com.example.Callback.run") == "run"
        assert extract_method("SI$block#com.example.Worker.run#250ms") == "run"
        assert extract_method("SI$db#com.example.DB.query#tbl") == "query"

    def test_anonymous_inner_class_method(self):
        assert (
            extract_method(
                "SI$block#worker.CpuBurnWorker$startMainThreadWork$1#129ms"
            )
            == "startMainThreadWork"
        )


class TestExtractFqn:
    """Tests for extract_fqn() wrapper."""

    def test_all_tag_types(self):
        assert extract_fqn("SI$com.example.Class.method") == "com.example.Class"
        assert extract_fqn("SI$RV#vid#com.example.Adapter.onBind") == "com.example.Adapter"
        assert extract_fqn("SI$inflate#layout#parent") == ""
        assert extract_fqn("SI$view#com.example.View.onDraw") == "com.example.View"
        assert extract_fqn("SI$handler#com.example.Callback.run") == "com.example.Callback"
        assert extract_fqn("SI$block#com.example.Worker.run#250ms") == "com.example.Worker"
        assert extract_fqn("SI$db#com.example.DB.query#tbl") == "com.example.DB"


class TestClassifySearchType:
    """Tests for classify_search_type() wrapper."""

    def test_xml_search(self):
        assert classify_search_type("SI$inflate#layout#parent") == "xml"

    def test_system_search(self):
        assert classify_search_type("SI$touch#Activity#DOWN") == "system"
        assert (
            classify_search_type("SI$android.view.Choreographer.doFrame") == "system"
        )

    def test_java_search(self):
        assert classify_search_type("SI$com.example.MyClass.doWork") == "java"
        assert classify_search_type("SI$net#com.example.Api.call") == "java"
        assert classify_search_type("SI$db#com.example.DB.query") == "java"
        assert classify_search_type("SI$img#com.example.Loader.into") == "java"


class TestIsSystemClass:
    """Tests for is_system_class() wrapper."""

    def test_system_prefix(self):
        assert is_system_class("SI$android.view.Choreographer.doFrame")
        assert is_system_class("SI$androidx.recyclerview.widget.RecyclerView.onDraw")

    def test_system_pattern(self):
        assert is_system_class("SI$view#Choreographer.doFrame")
        assert is_system_class("SI$view#FragmentManager$5")

    def test_user_class(self):
        assert not is_system_class("SI$com.example.MyClass.doWork")
        assert not is_system_class("SI$view#com.example.CustomView.onDraw")


# ---------------------------------------------------------------------------
# _split_fqn_method helper
# ---------------------------------------------------------------------------


class TestSplitFqnMethod:
    """Tests for _split_fqn_method() helper."""

    def test_fqn_with_method(self):
        fqn, method = _split_fqn_method("com.example.Class.method")
        assert fqn == "com.example.Class"
        assert method == "method"

    def test_fqn_without_method(self):
        """'ClassName' starts uppercase → treated as FQN, no method."""
        fqn, method = _split_fqn_method("com.example.Class")
        assert fqn == "com.example.Class"
        assert method == ""

    def test_inner_class_no_method(self):
        fqn, method = _split_fqn_method("com.example.Class$Inner")
        assert fqn == "com.example.Class$Inner"
        assert method == ""

    def test_anonymous_inner_class(self):
        """No dot → no FQN/method split possible."""
        fqn, method = _split_fqn_method("Class$Method$1")
        assert fqn == ""
        assert method == "Class$Method$1"


# ---------------------------------------------------------------------------
# _extract_method_from_anonymous helper
# ---------------------------------------------------------------------------


class TestExtractMethodFromAnonymous:
    """Tests for _extract_method_from_anonymous() helper."""

    def test_kotlin_method_scoped(self):
        assert (
            _extract_method_from_anonymous(
                "com.smartinspector.hook.worker.CpuBurnWorker$startMainThreadWork$1"
            )
            == "startMainThreadWork"
        )

    def test_java_anonymous_no_context(self):
        assert _extract_method_from_anonymous("com.example.OuterClass$1") == ""

    def test_kotlin_inlined_lambda(self):
        assert _extract_method_from_anonymous("com.example.Outer$$inlined$lambda$0") == ""

    def test_multi_level_anonymous(self):
        assert _extract_method_from_anonymous("com.example.Outer$doWork$1$2") == "doWork"

    def test_no_trailing_number(self):
        assert _extract_method_from_anonymous("com.example.Outer$method") == ""


# ---------------------------------------------------------------------------
# Integration: extract_attributable_slices
# ---------------------------------------------------------------------------


class TestExtractAttributableSlices:
    """Integration tests for extract_attributable_slices using parse_si_tag."""

    def test_basic_view_slices(self):
        from smartinspector.commands.attribution import extract_attributable_slices

        data = {
            "view_slices": {
                "slowest_slices": [
                    {
                        "name": "SI$RV#recycler#com.example.DemoAdapter.onBindViewHolder",
                        "dur_ms": 75.0,
                    },
                    {
                        "name": "SI$view#com.example.HeavyDrawView.measure",
                        "dur_ms": 50.0,
                    },
                ],
                "summary": [],
                "rv_instances": [],
            },
        }
        result = extract_attributable_slices(json.dumps(data))
        assert len(result) == 2
        class_names = {r["class_name"] for r in result}
        assert "DemoAdapter" in class_names
        assert "HeavyDrawView" in class_names

    def test_inflate_slice(self):
        from smartinspector.commands.attribution import extract_attributable_slices

        data = {
            "view_slices": {
                "slowest_slices": [
                    {
                        "name": "SI$inflate#item_complex#recycler_view",
                        "dur_ms": 30.0,
                    },
                ],
                "summary": [],
                "rv_instances": [],
            },
        }
        result = extract_attributable_slices(json.dumps(data))
        assert len(result) == 1
        assert result[0]["search_type"] == "xml"
        assert result[0]["class_name"] == "item_complex"

    def test_io_slices(self):
        from smartinspector.commands.attribution import extract_attributable_slices

        data = {
            "view_slices": {"slowest_slices": [], "summary": [], "rv_instances": []},
            "io_slices": {
                "summary": [
                    {
                        "name": "SI$net#com.example.ApiClient.get",
                        "max_ms": 200.0,
                        "count": 3,
                        "total_ms": 500.0,
                    },
                ],
            },
        }
        result = extract_attributable_slices(json.dumps(data))
        assert len(result) == 1
        assert result[0]["io_type"] == "network"
        assert result[0]["class_name"] == "ApiClient"

    def test_system_class_filtered(self):
        from smartinspector.commands.attribution import extract_attributable_slices

        data = {
            "view_slices": {
                "slowest_slices": [
                    {
                        "name": "SI$view#android.view.Choreographer.doFrame",
                        "dur_ms": 20.0,
                    },
                    {
                        "name": "SI$view#com.example.MyView.customDraw",
                        "dur_ms": 15.0,
                    },
                ],
                "summary": [],
                "rv_instances": [],
            },
        }
        result = extract_attributable_slices(json.dumps(data))
        class_names = [r["class_name"] for r in result]
        assert "Choreographer" not in class_names
        assert "MyView" in class_names


if __name__ == "__main__":
    pytest.main([__file__, "-v"])
