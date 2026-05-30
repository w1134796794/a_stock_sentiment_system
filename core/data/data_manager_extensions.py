"""
DataManagerExtensions - 已废弃，保留作为向后兼容 shim。

所有功能已合并至 `DataManager` (通过 `MoneyflowDataManager` Mixin)。
新代码请直接调用 `dm.get_stock_moneyflow(...)` / `dm.get_top_list(...)` 等。

旧用法：
    ext = DataManagerExtensions(dm)
    ext.get_cyq_perf(code, trade_date)

新用法：
    dm.get_cyq_perf(code, trade_date)

本 shim 把 `DataManagerExtensions(dm)` 调用代理给 `dm` 本身，并在使用时打印一次
DeprecationWarning，方便后续逐步迁移。
"""
import warnings

from core.data.data_manager_moneyflow import MoneyflowDataManager  # noqa: F401  (re-export)

_WARNED = False


class DataManagerExtensions:
    """[Deprecated] 代理至 DataManager 的资金流向 / 龙虎榜 / 北向 / 筹码接口。"""

    def __init__(self, data_manager):
        global _WARNED
        if not _WARNED:
            warnings.warn(
                "DataManagerExtensions 已合并至 DataManager。请直接调用 "
                "dm.get_stock_moneyflow / get_top_list / get_hsgt_moneyflow / "
                "get_cyq_perf 等方法。",
                DeprecationWarning,
                stacklevel=2,
            )
            _WARNED = True
        self.dm = data_manager
        self.ts_pro = getattr(data_manager, "ts_pro", None)

    def __getattr__(self, item):
        # 委托给底层 DataManager，避免重复实现。
        return getattr(self.dm, item)


def create_extensions(data_manager) -> DataManagerExtensions:
    """[Deprecated] 创建 DataManagerExtensions 实例。"""
    return DataManagerExtensions(data_manager)


__all__ = ["DataManagerExtensions", "MoneyflowDataManager", "create_extensions"]