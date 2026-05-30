"""
DataFrame 列名单一来源（P2-4）

设计目标：
1. 提供整个项目的字段名常量，避免硬编码 + 多语言混用。
2. 提供 snake_case ↔ 中文显示名的双向翻译工具。
3. 兼容多种历史命名（'代码' / '股票代码' / 'ts_code' / 'code'），统一收口。

使用：
    from core.utils.field_names import FieldNames as F, ColumnSchema

    code_col = F.detect_column(df, F.STOCK_CODE_CANDIDATES)
    df = ColumnSchema.normalize(df)               # 各种历史命名 -> snake_case
    df_zh = ColumnSchema.to_display(df, 'zh')     # snake_case -> 中文显示
"""
from typing import Dict, List, Optional

import pandas as pd


class FieldNames:
    """
    项目内部规范字段名（snake_case）+ 各类候选别名。

    规范：
        - 内部 DataFrame 一律采用 snake_case
        - 输出到报表 / Excel 时再走 `ColumnSchema.to_display(df, lang)` 翻译
    """

    # -------- 核心标识 --------
    TS_CODE = "ts_code"          # 带后缀，例如 000001.SZ
    CODE = "code"                # 6 位裸代码，例如 000001
    NAME = "name"                # 股票/板块名称
    TRADE_DATE = "trade_date"    # YYYYMMDD

    # -------- 板块 --------
    SECTOR_CODE = "ts_code"
    SECTOR_NAME = "name"

    # -------- 行情 --------
    OPEN = "open"
    HIGH = "high"
    LOW = "low"
    CLOSE = "close"
    PRE_CLOSE = "pre_close"
    CHANGE = "change"
    PCT_CHG = "pct_chg"
    VOL = "vol"
    AMOUNT = "amount"
    TURNOVER_RATE = "turnover_rate"
    VOL_RATIO = "vol_ratio"
    PE = "pe"
    PB = "pb"

    # -------- 涨停 / 连板 --------
    FIRST_TIME = "first_time"            # 首次封板时间
    LAST_TIME = "last_time"              # 最后封板时间
    UP_STAT = "up_stat"                  # 连板高度
    FD_AMOUNT = "fd_amount"              # 封单额
    LU_DESC = "lu_desc"                  # 涨停原因
    LU_TIME = "lu_time"                  # 封板时间
    LIMIT_TIMES = "limit_times"          # 涨停次数

    # -------- 资金流向 --------
    BUY_ELG_AMOUNT = "buy_elg_amount"
    SELL_ELG_AMOUNT = "sell_elg_amount"
    BUY_LG_AMOUNT = "buy_lg_amount"
    SELL_LG_AMOUNT = "sell_lg_amount"
    BUY_MD_AMOUNT = "buy_md_amount"
    SELL_MD_AMOUNT = "sell_md_amount"
    BUY_SM_AMOUNT = "buy_sm_amount"
    SELL_SM_AMOUNT = "sell_sm_amount"
    NET_MF_AMOUNT = "net_mf_amount"
    NET_MF_VOL = "net_mf_vol"

    # -------- 交易计划 / 回测 --------
    STOCK_CODE = "stock_code"
    STOCK_NAME = "stock_name"
    ENTRY_PRICE = "entry_price"
    TARGET_PRICE = "target_price"
    STOP_LOSS = "stop_loss"
    POSITION = "position"

    # -------- 候选别名（用于 detect_column / 历史兼容） --------
    STOCK_CODE_CANDIDATES = [
        "code", "ts_code", "con_code", "股票代码", "代码", "stock_code",
    ]
    STOCK_NAME_CANDIDATES = [
        "name", "股票名称", "名称", "stock_name",
    ]
    SECTOR_CODE_CANDIDATES = [
        "ts_code", "sector_code", "板块代码",
    ]
    SECTOR_NAME_CANDIDATES = [
        "name", "sector_name", "板块名称", "概念名称",
    ]
    TRADE_DATE_CANDIDATES = [
        "trade_date", "date", "交易日期", "日期",
    ]

    # 向后兼容旧 stock_code_utils.FieldNames 的别名常量
    STOCK_CODE_FIELDS = STOCK_CODE_CANDIDATES
    STOCK_NAME_FIELDS = STOCK_NAME_CANDIDATES
    SECTOR_CODE_FIELDS = SECTOR_CODE_CANDIDATES
    SECTOR_NAME_FIELDS = SECTOR_NAME_CANDIDATES

    @staticmethod
    def detect_column(df: pd.DataFrame, candidates: List[str]) -> Optional[str]:
        """在 df 中按候选列表顺序找到第一个存在的列名"""
        if df is None or df.empty:
            return None
        for col in candidates:
            if col in df.columns:
                return col
        return None


class ColumnSchema:
    """列名规范化 + 显示翻译"""

    # 各类历史 / 别名 → 内部规范 snake_case
    ALIAS_TO_INTERNAL: Dict[str, str] = {
        # 代码
        "ts_code": "ts_code",
        "con_code": "code",
        "股票代码": "code",
        "代码": "code",
        "stock_code": "stock_code",
        # 名称
        "股票名称": "name",
        "名称": "name",
        "stock_name": "stock_name",
        "板块名称": "name",
        "概念名称": "name",
        # 日期
        "交易日期": "trade_date",
        "日期": "trade_date",
        "date": "trade_date",
        # 行情
        "开盘": "open",
        "收盘": "close",
        "最高": "high",
        "最低": "low",
        "涨跌额": "change",
        "涨跌幅": "pct_chg",
        "成交量": "vol",
        "成交额": "amount",
        "换手率": "turnover_rate",
        "量比": "vol_ratio",
        # 涨停
        "首次封板时间": "first_time",
        "封板时间": "first_time",
        "最后封板时间": "last_time",
        "连板高度": "up_stat",
        "封单额": "fd_amount",
        "涨停原因": "lu_desc",
        "涨停次数": "limit_times",
    }

    # 内部 snake_case → 中文显示
    INTERNAL_TO_ZH: Dict[str, str] = {
        "ts_code": "代码",
        "code": "代码",
        "name": "名称",
        "stock_code": "股票代码",
        "stock_name": "股票名称",
        "trade_date": "交易日期",
        "open": "开盘",
        "high": "最高",
        "low": "最低",
        "close": "收盘",
        "pre_close": "前收",
        "change": "涨跌额",
        "pct_chg": "涨跌幅",
        "vol": "成交量",
        "amount": "成交额",
        "turnover_rate": "换手率",
        "vol_ratio": "量比",
        "first_time": "首次封板",
        "last_time": "最后封板",
        "up_stat": "连板",
        "fd_amount": "封单额",
        "lu_desc": "涨停原因",
        "limit_times": "涨停次数",
        "buy_elg_amount": "特大单买入额",
        "sell_elg_amount": "特大单卖出额",
        "buy_lg_amount": "大单买入额",
        "sell_lg_amount": "大单卖出额",
        "net_mf_amount": "资金净流入",
        "entry_price": "买入价",
        "target_price": "目标价",
        "stop_loss": "止损价",
        "position": "仓位",
    }

    @classmethod
    def normalize(cls, df: pd.DataFrame,
                  extra_mapping: Optional[Dict[str, str]] = None) -> pd.DataFrame:
        """
        把 df 列名从历史 / 中文 / 别名统一规范化为 snake_case。

        如果目标列名已存在则跳过该映射以避免重名冲突。
        """
        if df is None or df.empty:
            return df

        mapping = dict(cls.ALIAS_TO_INTERNAL)
        if extra_mapping:
            mapping.update(extra_mapping)

        rename = {}
        for src, dst in mapping.items():
            if src in df.columns and dst not in df.columns:
                rename[src] = dst
        if rename:
            df = df.rename(columns=rename)
        return df

    @classmethod
    def to_display(cls, df: pd.DataFrame, lang: str = "zh",
                   extra_mapping: Optional[Dict[str, str]] = None) -> pd.DataFrame:
        """
        把 snake_case 列名翻译为显示语言（默认中文）。

        Args:
            df: 内部规范列名的 DataFrame
            lang: 目标语言，目前支持 'zh'
            extra_mapping: 额外的 snake_case -> display 映射（覆盖默认）
        """
        if df is None or df.empty:
            return df
        if lang != "zh":
            return df

        mapping = dict(cls.INTERNAL_TO_ZH)
        if extra_mapping:
            mapping.update(extra_mapping)

        rename = {c: mapping[c] for c in df.columns if c in mapping}
        return df.rename(columns=rename) if rename else df

    @classmethod
    def register(cls, internal: str, display_zh: Optional[str] = None,
                 aliases: Optional[List[str]] = None):
        """在运行时扩展 schema（供新模块自定义字段）"""
        if display_zh:
            cls.INTERNAL_TO_ZH[internal] = display_zh
        if aliases:
            for a in aliases:
                cls.ALIAS_TO_INTERNAL[a] = internal


__all__ = ["FieldNames", "ColumnSchema"]
