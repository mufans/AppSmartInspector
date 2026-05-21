# src/smartinspector/collector/dimensions/__init__.py

"""Analysis dimension registry with auto-discovery."""

import importlib
import pkgutil

from smartinspector.collector.dimensions.base import AnalysisDimension, HintContext


class DimensionRegistry:
    """分析维度注册表。支持自动发现和按名获取。"""

    _dimensions: dict[str, AnalysisDimension] = {}

    @classmethod
    def register(cls, dim: AnalysisDimension) -> None:
        cls._dimensions[dim.name] = dim

    @classmethod
    def all(cls) -> list[AnalysisDimension]:
        return list(cls._dimensions.values())

    @classmethod
    def get(cls, name: str) -> AnalysisDimension | None:
        return cls._dimensions.get(name)

    @classmethod
    def discover(cls) -> None:
        """自动发现 dimensions/ 包下的所有维度模块。"""
        from smartinspector.collector import dimensions as pkg

        for _, module_name, _ in pkgutil.iter_modules(pkg.__path__):
            importlib.import_module(
                f"smartinspector.collector.dimensions.{module_name}"
            )

    @classmethod
    def clear(cls) -> None:
        """清空注册表（仅用于测试）。"""
        cls._dimensions.clear()


def register_dimension(cls_or_dim):
    """类装饰器或实例注册：注册维度到 Registry。

    用作类装饰器时：@register_dimension → 自动实例化并注册。
    用作函数调用时：register_dimension(instance) → 直接注册。
    """
    if isinstance(cls_or_dim, type):
        # 类装饰器用法: @register_dimension → 实例化并注册
        dim = cls_or_dim()
        DimensionRegistry.register(dim)
        return cls_or_dim
    else:
        # 实例注册用法: register_dimension(instance)
        DimensionRegistry.register(cls_or_dim)
        return cls_or_dim
