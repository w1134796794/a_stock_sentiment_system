"""Sector-level batch factor job."""
from __future__ import annotations

import pandas as pd

from core.factors.behavior_cycle import BEHAVIOR_STATES, sector_behavior_cycle

from core.factors.jobs.gold_utils import (
    FactorJobResult,
    long_records_to_frame,
    make_long_record,
    percentile_score,
    read_recent_trade_dates,
    read_table,
    safe_weighted_score,
    score_between,
    to_float,
    write_replace_partition,
    now_iso,
)


class SectorFactorJob:
    name = "sector_factor_job"

    def run(self, con, trade_date: str) -> FactorJobResult:
        result = FactorJobResult(name=self.name, trade_date=str(trade_date))
        sector = read_recent_trade_dates(
            con,
            "sector_daily_silver",
            trade_date,
            days=6,
            columns=(
                "trade_date", "sector_code", "sector_name", "sector_type",
                "pct_chg", "amount_yuan", "close", "pre_close", "vol_hand",
            ),
        )
        if sector.empty:
            result.ok = False
            result.add_message("sector_daily_silver 为空，无法计算板块指标")
            return result

        sector["trade_date"] = sector["trade_date"].astype(str)
        for col in ("pct_chg", "amount_yuan", "close", "pre_close", "vol_hand"):
            sector[col] = pd.to_numeric(sector.get(col), errors="coerce").fillna(0)
        missing_pct = sector["pct_chg"].abs() <= 1e-12
        can_calc_pct = (sector["pre_close"] > 0) & (sector["close"] > 0)
        sector.loc[missing_pct & can_calc_pct, "pct_chg"] = (
            (sector.loc[missing_pct & can_calc_pct, "close"] - sector.loc[missing_pct & can_calc_pct, "pre_close"])
            / sector.loc[missing_pct & can_calc_pct, "pre_close"]
            * 100.0
        )
        missing_amount = sector["amount_yuan"] <= 0
        can_calc_amount = (sector["vol_hand"] > 0) & (sector["close"] > 0)
        sector.loc[missing_amount & can_calc_amount, "amount_yuan"] = (
            sector.loc[missing_amount & can_calc_amount, "vol_hand"]
            * sector.loc[missing_amount & can_calc_amount, "close"]
        )
        sector["rank_percentile"] = sector.groupby("trade_date")["pct_chg"].rank(
            method="average", pct=True,
        )
        today = sector[sector["trade_date"] == str(trade_date)].copy()
        if today.empty:
            result.ok = False
            result.add_message(f"sector_daily_silver 无 {trade_date} 数据")
            return result

        today["momentum_score"] = today["pct_chg"].map(lambda v: score_between(v, -5.0, 8.0))
        today["amount_score"] = percentile_score(today["amount_yuan"], higher_better=True)

        hist = sector[sector["trade_date"] < str(trade_date)].sort_values(["sector_code", "trade_date"])
        avg_amount_5 = hist.groupby("sector_code").tail(5).groupby("sector_code")["amount_yuan"].mean()
        positive_days_3 = hist.groupby("sector_code").tail(3).assign(
            positive=lambda x: (x["pct_chg"] > 0).astype(float)
        ).groupby("sector_code")["positive"].sum()

        ratio_scores = []
        ratio_values = []
        persistence_scores = []
        positive_streaks = []
        for _, row in today.iterrows():
            code = str(row.get("sector_code") or "")
            base = to_float(avg_amount_5.get(code), to_float(row.get("amount_yuan")))
            ratio = to_float(row.get("amount_yuan")) / base if base > 0 else 1.0
            ratio_values.append(ratio)
            ratio_scores.append(score_between(ratio, 0.5, 2.5))
            pos_days = to_float(positive_days_3.get(code), 0.0)
            current_pos = 1.0 if to_float(row.get("pct_chg")) > 0 else 0.0
            persistence_scores.append((pos_days + current_pos) / 4.0 * 100.0)
            code_rows = sector[sector["sector_code"].astype(str) == code].sort_values("trade_date")
            streak = 0
            for value in reversed(list(code_rows["pct_chg"])):
                if to_float(value) <= 0:
                    break
                streak += 1
            positive_streaks.append(streak)

        today["amount_ratio"] = ratio_values
        today["amount_ratio_score"] = ratio_scores
        today["persistence_score"] = persistence_scores
        today["positive_streak"] = positive_streaks
        constituent = self._constituent_metrics(con, today, str(trade_date))
        for column in (
            "constituent_count", "constituent_observed", "constituent_breadth",
            "constituent_average_pct", "limit_up_count", "breadth_score",
            "limit_up_diffusion_score", "breadth_acceleration_score",
            "membership_available",
        ):
            today[column] = [row.get(column) for row in constituent]
        result.record_source(
            "sector_constituent_breadth",
            available=bool(pd.to_numeric(today["membership_available"], errors="coerce").fillna(0).max()),
            rows=int(pd.to_numeric(today["constituent_observed"], errors="coerce").fillna(0).sum()),
            freshness_date=str(trade_date), required=True,
        )
        result.record_source(
            "limit_up_pool_silver",
            available=bool(pd.to_numeric(today["limit_up_count"], errors="coerce").fillna(0).sum()),
            rows=int(pd.to_numeric(today["limit_up_count"], errors="coerce").fillna(0).sum()),
            freshness_date=str(trade_date), required=False,
        )
        signal = read_table(
            con, "factor_signal_sector_wide",
            where="CAST(trade_date AS VARCHAR) = ?", params=[str(trade_date)],
        )
        if not signal.empty:
            signal_keep = [
                "sector_code", "sector_flow_score", "sector_flow_price_resonance", "net_amount_yuan",
            ]
            today = today.merge(
                signal[[col for col in signal_keep if col in signal.columns]],
                on="sector_code", how="left",
            )
        lhb = read_table(
            con, "factor_lhb_sector_wide", where="CAST(trade_date AS VARCHAR) = ?", params=[str(trade_date)]
        )
        if not lhb.empty:
            keep = [
                "sector_code", "lhb_stock_count", "sector_lhb_net_buy_ratio",
                "sector_lhb_breadth_score", "sector_lhb_net_buy_score",
                "sector_lhb_institution_score", "sector_lhb_resonance_score",
                "signal_date", "effective_date",
            ]
            today = today.merge(lhb[[col for col in keep if col in lhb.columns]], on="sector_code", how="left")
        defaults = {
            "lhb_stock_count": 0.0,
            "sector_lhb_net_buy_ratio": 0.0,
            "sector_lhb_breadth_score": 50.0,
            "sector_lhb_net_buy_score": 50.0,
            "sector_lhb_institution_score": 50.0,
            "sector_lhb_resonance_score": 50.0,
            "signal_date": "",
            "effective_date": "",
            "sector_flow_score": 50.0,
            "sector_flow_price_resonance": 50.0,
            "net_amount_yuan": 0.0,
        }
        for col, default in defaults.items():
            if col not in today.columns:
                today[col] = default
            elif isinstance(default, float):
                today[col] = pd.to_numeric(today[col], errors="coerce").fillna(default)
            else:
                today[col] = today[col].fillna(default)
        today["mainline_score"] = [self._mainline_score(row) for row in today.itertuples()]
        previous_by_code = (
            hist.drop_duplicates("sector_code", keep="last")
            .set_index("sector_code").to_dict("index")
            if not hist.empty else {}
        )
        behavior_rows = []
        for _, row in today.iterrows():
            previous = previous_by_code.get(row.get("sector_code")) or {}
            behavior_rows.append(sector_behavior_cycle(
                pct_chg=row.get("pct_chg"),
                previous_pct_chg=previous.get("pct_chg"),
                amount_ratio=row.get("amount_ratio"),
                current_rank_percentile=row.get("rank_percentile"),
                previous_rank_percentile=previous.get("rank_percentile"),
                persistence_score=row.get("persistence_score"),
                flow_score=row.get("sector_flow_score"),
                positive_streak=row.get("positive_streak"),
                breadth_acceleration_score=(
                    row.get("breadth_acceleration_score")
                    if bool(row.get("membership_available")) else None
                ),
            ))
        for state in BEHAVIOR_STATES:
            today[f"behavior_{state}_score"] = [item["scores"][state] for item in behavior_rows]
            today[f"behavior_{state}_probability"] = [
                item["probabilities"][state] for item in behavior_rows
            ]
        for atomic in (
            "first_activation", "rank_improvement", "momentum_acceleration",
            "amount_surprise", "breadth_acceleration", "reversal", "fresh_stage",
        ):
            today[f"behavior_{atomic}_score"] = [item["atomic"][atomic] for item in behavior_rows]
        today["behavior_dominant_state"] = [item["dominant_state"] for item in behavior_rows]
        today["behavior_dominant_label"] = [item["dominant_label"] for item in behavior_rows]
        today["behavior_dominant_probability"] = [item["dominant_probability"] for item in behavior_rows]
        today["behavior_data_completeness"] = [item["data_completeness"] for item in behavior_rows]
        today["rank"] = today["mainline_score"].rank(method="dense", ascending=False).astype(int)

        wide = today[[
            "trade_date",
            "sector_code",
            "sector_name",
            "sector_type",
            "momentum_score",
            "amount_score",
            "amount_ratio_score",
            "persistence_score",
            "sector_flow_score",
            "sector_flow_price_resonance",
            "net_amount_yuan",
            "lhb_stock_count",
            "sector_lhb_net_buy_ratio",
            "sector_lhb_breadth_score",
            "sector_lhb_net_buy_score",
            "sector_lhb_institution_score",
            "sector_lhb_resonance_score",
            "signal_date",
            "effective_date",
            "mainline_score",
            "amount_ratio",
            "positive_streak",
            "constituent_count",
            "constituent_observed",
            "constituent_breadth",
            "constituent_average_pct",
            "limit_up_count",
            "breadth_score",
            "limit_up_diffusion_score",
            "breadth_acceleration_score",
            "membership_available",
            "behavior_attention_score",
            "behavior_acceleration_score",
            "behavior_divergence_score",
            "behavior_repair_score",
            "behavior_decay_score",
            "behavior_attention_probability",
            "behavior_acceleration_probability",
            "behavior_divergence_probability",
            "behavior_repair_probability",
            "behavior_decay_probability",
            "behavior_first_activation_score",
            "behavior_rank_improvement_score",
            "behavior_momentum_acceleration_score",
            "behavior_amount_surprise_score",
            "behavior_breadth_acceleration_score",
            "behavior_reversal_score",
            "behavior_fresh_stage_score",
            "behavior_dominant_state",
            "behavior_dominant_label",
            "behavior_dominant_probability",
            "behavior_data_completeness",
            "rank",
        ]].copy()
        wide["computed_at"] = now_iso()

        records = []
        for _, row in today.iterrows():
            entity_id = str(row.get("sector_code") or "")
            pct = to_float(row.get("pct_chg"))
            amount = to_float(row.get("amount_yuan"))
            records.extend([
                make_long_record(
                    trade_date=trade_date, entity_type="sector", entity_id=entity_id,
                    factor_id="sec_pct_chg_1d", raw_value=pct, score=row["momentum_score"],
                    direction="higher_better",
                ),
                make_long_record(
                    trade_date=trade_date, entity_type="sector", entity_id=entity_id,
                    factor_id="sec_amount_percentile", raw_value=amount, score=row["amount_score"],
                    percentile=row["amount_score"], direction="higher_better",
                ),
                make_long_record(
                    trade_date=trade_date, entity_type="sector", entity_id=entity_id,
                    factor_id="sec_amount_ratio_5d_score", raw_value=row["amount_ratio_score"],
                    score=row["amount_ratio_score"], direction="higher_better",
                ),
                make_long_record(
                    trade_date=trade_date, entity_type="sector", entity_id=entity_id,
                    factor_id="sec_persistence_score", raw_value=row["persistence_score"],
                    score=row["persistence_score"], direction="higher_better",
                ),
                make_long_record(
                    trade_date=trade_date, entity_type="sector", entity_id=entity_id,
                    factor_id="sec_capital_flow_score", raw_value=row["net_amount_yuan"],
                    score=row["sector_flow_score"], direction="higher_better",
                ),
                make_long_record(
                    trade_date=trade_date, entity_type="sector", entity_id=entity_id,
                    factor_id="sec_flow_price_resonance", raw_value=row["net_amount_yuan"],
                    score=row["sector_flow_price_resonance"], direction="higher_better",
                ),
                make_long_record(
                    trade_date=trade_date, entity_type="sector", entity_id=entity_id,
                    factor_id="sec_lhb_resonance_score", raw_value=row["sector_lhb_net_buy_ratio"],
                    score=row["sector_lhb_resonance_score"], direction="higher_better",
                ),
                make_long_record(
                    trade_date=trade_date, entity_type="sector", entity_id=entity_id,
                    factor_id="sec_mainline_score", raw_value=row["mainline_score"],
                    score=row["mainline_score"], rank_value=row["rank"], direction="higher_better",
                ),
                make_long_record(
                    trade_date=trade_date, entity_type="sector", entity_id=entity_id,
                    factor_id="sec_constituent_breadth", raw_value=(
                        row["constituent_breadth"] if row["membership_available"] else None
                    ),
                    score=row["breadth_score"], direction="higher_better",
                ),
                make_long_record(
                    trade_date=trade_date, entity_type="sector", entity_id=entity_id,
                    factor_id="sec_limit_up_diffusion", raw_value=(
                        row["limit_up_count"] if row["membership_available"] else None
                    ),
                    score=row["limit_up_diffusion_score"], direction="higher_better",
                ),
            ])
            for state in BEHAVIOR_STATES:
                records.append(make_long_record(
                    trade_date=trade_date, entity_type="sector", entity_id=entity_id,
                    factor_id=f"sec_behavior_{state}",
                    raw_value=row[f"behavior_{state}_probability"],
                    score=row[f"behavior_{state}_score"],
                    direction="higher_better",
                ))
        long = long_records_to_frame(records)

        result.rows["factor_sector_wide"] = write_replace_partition(
            con, "factor_sector_wide", wide, where="trade_date = ?", params=[str(trade_date)]
        )
        result.rows["factor_value_long"] = write_replace_partition(
            con,
            "factor_value_long",
            long,
            where="trade_date = ? AND entity_type = ?",
            params=[str(trade_date), "sector"],
        )
        return result

    @staticmethod
    def _mainline_score(row) -> float:
        values = [
            (row.momentum_score, 0.35),
            (row.amount_score, 0.20),
            (row.amount_ratio_score, 0.10),
            (row.persistence_score, 0.10),
        ]
        if bool(row.membership_available):
            values.extend([
                (row.breadth_score, 0.15),
                (row.limit_up_diffusion_score, 0.10),
            ])
        else:
            values = [
                (row.momentum_score, 0.45), (row.amount_score, 0.25),
                (row.amount_ratio_score, 0.15), (row.persistence_score, 0.15),
            ]
        return safe_weighted_score(values)

    @staticmethod
    def _constituent_metrics(con, sectors: pd.DataFrame, trade_date: str) -> list[dict]:
        """Use prior known memberships with today's stock tape and official limit pool."""
        try:
            previous_date = con.execute(
                "SELECT MAX(CAST(trade_date AS VARCHAR)) FROM factor_stock_wide "
                "WHERE CAST(trade_date AS VARCHAR) < ?", [trade_date],
            ).fetchone()[0]
        except Exception:
            previous_date = None
        if not previous_date:
            return [{"membership_available": 0} for _ in range(len(sectors))]
        try:
            members = con.execute(
                "SELECT code, primary_sector_code, primary_sector_name, resonance_sectors "
                "FROM factor_stock_wide WHERE CAST(trade_date AS VARCHAR)=?",
                [str(previous_date)],
            ).fetchdf()
            daily = con.execute(
                "SELECT code, pct_chg FROM stock_daily_silver "
                "WHERE CAST(trade_date AS VARCHAR)=?", [trade_date],
            ).fetchdf()
            limit_rows = con.execute(
                "SELECT code FROM limit_up_pool_silver "
                "WHERE CAST(trade_date AS VARCHAR)=?", [trade_date],
            ).fetchdf()
        except Exception:
            return [{"membership_available": 0} for _ in range(len(sectors))]
        try:
            previous_sector = con.execute(
                "SELECT sector_code, constituent_breadth FROM factor_sector_wide "
                "WHERE CAST(trade_date AS VARCHAR)=?", [str(previous_date)],
            ).fetchdf()
        except Exception:
            previous_sector = pd.DataFrame()
        if members.empty or daily.empty:
            return [{"membership_available": 0} for _ in range(len(sectors))]
        for frame in (members, daily, limit_rows):
            if "code" in frame:
                frame["code"] = frame["code"].astype(str).str.split(".").str[0].str.zfill(6)
        daily["pct_chg"] = pd.to_numeric(daily.get("pct_chg"), errors="coerce")
        tape = daily.dropna(subset=["pct_chg"]).drop_duplicates("code").set_index("code")
        limit_codes = set(limit_rows.get("code", pd.Series(dtype=str)).astype(str))
        previous_breadth = {
            str(row.get("sector_code") or "").split(".")[0]: to_float(row.get("constituent_breadth"), 0.5)
            for row in previous_sector.to_dict("records")
        } if not previous_sector.empty else {}
        rows: list[dict] = []
        resonance = members.get("resonance_sectors", pd.Series("", index=members.index)).fillna("").astype(str)
        primary_code = members.get("primary_sector_code", pd.Series("", index=members.index)).fillna("").astype(str).str.split(".").str[0]
        primary_name = members.get("primary_sector_name", pd.Series("", index=members.index)).fillna("").astype(str)
        for sector in sectors.to_dict("records"):
            code = str(sector.get("sector_code") or "").split(".")[0]
            name = str(sector.get("sector_name") or "").strip()
            mask = primary_code.eq(code) | primary_name.eq(name)
            if name:
                mask = mask | resonance.str.contains(name, regex=False)
            codes = set(members.loc[mask, "code"].astype(str))
            observed_codes = sorted(codes.intersection(set(tape.index)))
            observed = tape.loc[observed_codes, "pct_chg"] if observed_codes else pd.Series(dtype=float)
            breadth = float((observed > 0).mean()) if len(observed) else None
            average = float(observed.mean()) if len(observed) else None
            limit_count = len(codes.intersection(limit_codes))
            old = previous_breadth.get(code)
            acceleration = (breadth - old) if breadth is not None and old is not None else None
            rows.append({
                "constituent_count": len(codes),
                "constituent_observed": len(observed),
                "constituent_breadth": breadth,
                "constituent_average_pct": average,
                "limit_up_count": limit_count,
                "breadth_score": breadth * 100.0 if breadth is not None else None,
                "limit_up_diffusion_score": score_between(limit_count, 0.0, 8.0) if codes else None,
                "breadth_acceleration_score": score_between(acceleration, -0.20, 0.20) if acceleration is not None else None,
                "membership_available": int(len(observed) >= 3),
            })
        return rows
