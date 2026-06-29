"""盘后短线增强因子：资金流共识、关注度、龙头确认与事件风险。"""
from __future__ import annotations

import pandas as pd

from core.factors.jobs.gold_utils import (
    FactorJobResult,
    now_iso,
    percentile_score,
    read_recent_trade_dates,
    read_table,
    write_replace_partition,
)


STOCK_SIGNAL_COLUMNS = [
    "trade_date", "signal_date", "effective_date", "code",
    "capital_flow_consensus_score", "capital_flow_persistence_score", "capital_flow_adjustment",
    "attention_score", "attention_crowding_penalty", "attention_adjustment",
    "leader_quality_score", "leader_adjustment", "margin_score", "margin_adjustment",
    "event_risk_score", "risk_adjustment", "signal_total_adjustment",
    "flow_source_count", "attention_source_count", "kpl_present", "computed_at",
]

SECTOR_SIGNAL_COLUMNS = [
    "trade_date", "signal_date", "effective_date", "sector_code", "sector_name",
    "sector_flow_score", "sector_flow_price_resonance", "net_amount_yuan", "computed_at",
]


def _number(frame: pd.DataFrame, column: str, default: float = 0.0) -> pd.Series:
    if column not in frame.columns:
        return pd.Series(default, index=frame.index, dtype=float)
    return pd.to_numeric(frame[column], errors="coerce").fillna(default)


def _empty(columns: list[str]) -> pd.DataFrame:
    return pd.DataFrame(columns=columns)


class ShortSignalFactorJob:
    name = "short_signal_factors"

    def run(self, con, trade_date: str) -> FactorJobResult:
        result = FactorJobResult(name=self.name, trade_date=str(trade_date))
        stock = self._stock_factors(con, str(trade_date))
        sector = self._sector_factors(con, str(trade_date))
        result.rows["factor_signal_stock_wide"] = write_replace_partition(
            con, "factor_signal_stock_wide", stock,
            where="CAST(trade_date AS VARCHAR) = ?", params=[str(trade_date)],
        )
        result.rows["factor_signal_sector_wide"] = write_replace_partition(
            con, "factor_signal_sector_wide", sector,
            where="CAST(trade_date AS VARCHAR) = ?", params=[str(trade_date)],
        )
        return result

    def _stock_factors(self, con, trade_date: str) -> pd.DataFrame:
        flow = read_table(
            con, "stock_capital_flow_silver",
            where="CAST(trade_date AS VARCHAR) = ?", params=[trade_date],
        )
        attention = read_table(
            con, "stock_attention_silver",
            where="CAST(trade_date AS VARCHAR) = ?", params=[trade_date],
        )
        leader = read_table(
            con, "stock_leader_signal_silver",
            where="CAST(trade_date AS VARCHAR) = ?", params=[trade_date],
        )
        margin = read_recent_trade_dates(
            con, "stock_margin_silver", trade_date, days=2,
        )
        events = read_table(
            con, "stock_event_silver",
            where="CAST(trade_date AS VARCHAR) = ?", params=[trade_date],
        )
        daily = read_table(
            con, "stock_daily_silver",
            where="CAST(trade_date AS VARCHAR) = ?", params=[trade_date],
        )

        code_sets = []
        for frame in (flow, attention, leader, margin, events):
            if not frame.empty and "code" in frame.columns:
                code_sets.extend(frame["code"].astype(str).str.zfill(6).tolist())
        codes = sorted(set(code_sets))
        if not codes:
            return _empty(STOCK_SIGNAL_COLUMNS)
        out = pd.DataFrame({"code": codes})

        flow_features = self._flow_features(flow)
        attention_features = self._attention_features(attention)
        leader_features = self._leader_features(leader)
        margin_features = self._margin_features(margin, trade_date)
        event_features = self._event_features(events, daily)
        for frame in (flow_features, attention_features, leader_features, margin_features, event_features):
            out = out.merge(frame, on="code", how="left")

        defaults = {
            "capital_flow_consensus_score": 50.0, "capital_flow_persistence_score": 50.0,
            "flow_source_count": 0.0, "attention_score": 50.0, "attention_source_count": 0.0,
            "leader_quality_score": 50.0, "kpl_present": 0.0, "margin_score": 50.0,
            "event_risk_score": 0.0,
        }
        for column, default in defaults.items():
            out[column] = _number(out, column, default)

        out["capital_flow_adjustment"] = (
            (out["capital_flow_consensus_score"] - 50.0) / 50.0 * 3.5
            + (out["capital_flow_persistence_score"] - 50.0) / 50.0 * 1.0
        ).clip(-4.5, 4.5)
        weak_confirmation = (1.0 - ((out["capital_flow_consensus_score"] - 45.0) / 25.0).clip(0, 1))
        out["attention_crowding_penalty"] = (
            ((out["attention_score"] - 80.0) / 20.0).clip(0, 1) * weak_confirmation * 10.0
        )
        out["attention_adjustment"] = (
            (out["attention_score"] - 50.0) / 50.0 * 1.5
            - out["attention_crowding_penalty"] * 0.25
        ).clip(-3.0, 2.0)
        out["leader_adjustment"] = (
            (out["leader_quality_score"] - 50.0) / 50.0 * 3.0
        ).where(out["kpl_present"] > 0, 0.0).clip(-2.0, 3.0)
        out["margin_adjustment"] = ((out["margin_score"] - 50.0) / 50.0).clip(-1.0, 1.0)
        out["risk_adjustment"] = -(out["event_risk_score"] / 100.0 * 3.0).clip(0, 3.0)
        out["signal_total_adjustment"] = out[
            ["capital_flow_adjustment", "attention_adjustment", "leader_adjustment", "margin_adjustment", "risk_adjustment"]
        ].sum(axis=1).clip(-10.0, 10.0)
        effective = ""
        for frame in (flow, attention, leader, margin, events):
            if not frame.empty and "effective_date" in frame.columns:
                values = frame["effective_date"].dropna().astype(str)
                if not values.empty:
                    effective = values.iloc[0]
                    break
        out["trade_date"] = trade_date
        out["signal_date"] = trade_date
        out["effective_date"] = effective
        out["computed_at"] = now_iso()
        return out[STOCK_SIGNAL_COLUMNS]

    @staticmethod
    def _flow_features(flow: pd.DataFrame) -> pd.DataFrame:
        columns = ["code", "capital_flow_consensus_score", "capital_flow_persistence_score", "flow_source_count"]
        if flow.empty:
            return _empty(columns)
        data = flow.copy()
        data["code"] = data["code"].astype(str).str.zfill(6)
        data["net_signal"] = _number(data, "net_amount_yuan") + _number(data, "large_net_yuan") * 0.35
        data["source_score"] = data.groupby("source", group_keys=False)["net_signal"].transform(percentile_score)
        grouped = data.groupby("code", as_index=False).agg(
            capital_flow_consensus_score=("source_score", "mean"),
            flow_source_count=("source", "nunique"),
            positive_sources=("net_signal", lambda values: int((values > 0).sum())),
            negative_sources=("net_signal", lambda values: int((values < 0).sum())),
            net_5d_amount_yuan=("net_5d_amount_yuan", "max"),
        )
        both_positive = (grouped["flow_source_count"] >= 2) & (grouped["positive_sources"] == grouped["flow_source_count"])
        both_negative = (grouped["flow_source_count"] >= 2) & (grouped["negative_sources"] == grouped["flow_source_count"])
        grouped.loc[both_positive, "capital_flow_consensus_score"] += 8.0
        grouped.loc[both_negative, "capital_flow_consensus_score"] -= 8.0
        grouped["capital_flow_consensus_score"] = grouped["capital_flow_consensus_score"].clip(0, 100)
        grouped["capital_flow_persistence_score"] = percentile_score(grouped["net_5d_amount_yuan"])
        return grouped[columns]

    @staticmethod
    def _attention_features(attention: pd.DataFrame) -> pd.DataFrame:
        columns = ["code", "attention_score", "attention_source_count"]
        if attention.empty:
            return _empty(columns)
        data = attention.copy()
        data["code"] = data["code"].astype(str).str.zfill(6)
        data["rank"] = _number(data, "rank", 9999.0)
        data["source_rank_score"] = data.groupby("source", group_keys=False)["rank"].transform(
            lambda values: percentile_score(values, higher_better=False)
        )
        grouped = data.groupby("code", as_index=False).agg(
            attention_score=("source_rank_score", "mean"), attention_source_count=("source", "nunique"),
        )
        grouped.loc[grouped["attention_source_count"] >= 2, "attention_score"] += 5.0
        grouped["attention_score"] = grouped["attention_score"].clip(0, 100)
        return grouped[columns]

    @staticmethod
    def _leader_features(leader: pd.DataFrame) -> pd.DataFrame:
        columns = ["code", "leader_quality_score", "kpl_present"]
        if leader.empty:
            return _empty(columns)
        data = leader.copy()
        data["code"] = data["code"].astype(str).str.zfill(6)
        text = (
            data.get("tag", pd.Series("", index=data.index)).fillna("").astype(str) + " "
            + data.get("status", pd.Series("", index=data.index)).fillna("").astype(str) + " "
            + data.get("lu_desc", pd.Series("", index=data.index)).fillna("").astype(str)
        )
        score = pd.Series(62.0, index=data.index)
        score += text.str.contains("龙头|核心|连板", regex=True).astype(float) * 14.0
        score += text.str.contains("强势|反包|晋级", regex=True).astype(float) * 8.0
        score -= text.str.contains("炸板|开板|弱", regex=True).astype(float) * 12.0
        score += percentile_score(_number(data, "limit_order")) * 0.12 - 6.0
        data["leader_quality_score"] = score.clip(0, 100)
        data["kpl_present"] = 1.0
        return data.groupby("code", as_index=False).agg(
            leader_quality_score=("leader_quality_score", "max"), kpl_present=("kpl_present", "max"),
        )[columns]

    @staticmethod
    def _margin_features(margin: pd.DataFrame, trade_date: str) -> pd.DataFrame:
        columns = ["code", "margin_score"]
        if margin.empty:
            return _empty(columns)
        data = margin.copy()
        data["code"] = data["code"].astype(str).str.zfill(6)
        data["trade_date"] = data["trade_date"].astype(str)
        data["net_financing"] = _number(data, "rzmre_yuan") - _number(data, "rzche_yuan")
        pivot = data.pivot_table(index="code", columns="trade_date", values="net_financing", aggfunc="sum")
        current = pivot[str(trade_date)] if str(trade_date) in pivot.columns else pd.Series(0.0, index=pivot.index)
        older_cols = [column for column in pivot.columns if str(column) < str(trade_date)]
        previous = pivot[max(older_cols)] if older_cols else pd.Series(0.0, index=pivot.index)
        acceleration = current - previous.fillna(0.0)
        return pd.DataFrame({"code": pivot.index.astype(str), "margin_score": percentile_score(acceleration).values})

    @staticmethod
    def _event_features(events: pd.DataFrame, daily: pd.DataFrame) -> pd.DataFrame:
        columns = ["code", "event_risk_score"]
        if events.empty:
            return _empty(columns)
        data = events.copy()
        data["code"] = data["code"].astype(str).str.zfill(6)
        if not daily.empty:
            close = daily[["code", "close"]].copy()
            close["code"] = close["code"].astype(str).str.zfill(6)
            close = close.drop_duplicates("code")
            data = data.merge(close, on="code", how="left")
        data["discount_pct"] = (_number(data, "price") / _number(data, "close").replace(0, pd.NA) - 1.0) * 100.0
        data["discount_severity"] = (-data["discount_pct"].fillna(0.0) - 2.0).clip(lower=0.0)
        grouped = data.groupby("code", as_index=False).agg(
            discount_severity=("discount_severity", "max"), amount_yuan=("amount_yuan", "sum"),
        )
        amount_score = percentile_score(grouped["amount_yuan"])
        grouped["event_risk_score"] = (grouped["discount_severity"] * 10.0 + amount_score * 0.35).clip(0, 100)
        return grouped[columns]

    @staticmethod
    def _sector_factors(con, trade_date: str) -> pd.DataFrame:
        flow = read_table(
            con, "sector_capital_flow_silver",
            where="CAST(trade_date AS VARCHAR) = ?", params=[trade_date],
        )
        if flow.empty:
            return _empty(SECTOR_SIGNAL_COLUMNS)
        out = flow.copy()
        out["net_amount_yuan"] = _number(out, "net_amount_yuan")
        out["pct_chg"] = _number(out, "pct_chg")
        amount_score = percentile_score(out["net_amount_yuan"])
        price_score = percentile_score(out["pct_chg"])
        out["sector_flow_score"] = (amount_score * 0.75 + price_score * 0.25).clip(0, 100)
        same_direction = ((out["net_amount_yuan"] >= 0) == (out["pct_chg"] >= 0)).astype(float)
        out["sector_flow_price_resonance"] = (out["sector_flow_score"] * 0.7 + same_direction * 30.0).clip(0, 100)
        out["signal_date"] = trade_date
        out["effective_date"] = out.get("effective_date", "")
        out["computed_at"] = now_iso()
        out = out.rename(columns={"trade_date": "_trade_date"})
        out["trade_date"] = trade_date
        return out[SECTOR_SIGNAL_COLUMNS].drop_duplicates("sector_code", keep="last")
