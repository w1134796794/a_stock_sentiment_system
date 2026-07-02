import json
from pathlib import Path

import pandas as pd

from core.realtime.entry_signal_service import RealtimeEntrySignalService
from core.realtime.leader_pool_service import IntradayStrengthService, LeaderPoolService


def _leader_item(code="002281", name="光迅科技", rank=7, pct_chg=10.0):
    return {
        "code": code,
        "name": name,
        "score": 88.0,
        "rank": rank,
        "metrics": {
            "tech_score": 88,
            "stk_liquidity_percentile": 90,
            "stk_new_high_20d": 86,
            "stk_sector_mainline_score": 85,
            "stk_sector_persistence_score": 80,
            "stk_sector_resonance_score": 82,
            "stk_board_position": 90,
            "stk_seal_time_quality": 85,
            "stk_kpl_leader_quality": 80,
            "stk_attention_consensus": 75,
            "stk_capital_flow_consensus": 75,
            "stk_lhb_composite_score": 70,
            "stk_lhb_institution_score": 65,
            "stk_lhb_crowding_risk": 90,
            "stk_attention_crowding_risk": 90,
            "stk_block_trade_risk": 95,
        },
        "context": {
            "pct_chg": pct_chg,
            "amount_ratio": 1.15,
            "vol_ratio": 1.2,
            "board_height": 2,
            "lhb_present": 1,
        },
        "resonance_sectors": "光通信",
    }


def test_leader_pool_from_screening_json(tmp_path: Path):
    screening_dir = tmp_path / "screening"
    screening_dir.mkdir()
    payload = {
        "trade_date": "20260618",
        "final": [
            {"code": "000001", "name": "仅候选第一", "score": 99, "rank": 1, "metrics": {}, "context": {}},
            _leader_item(),
        ],
    }
    (screening_dir / "screening_20260618.json").write_text(
        json.dumps(payload, ensure_ascii=False),
        encoding="utf-8",
    )

    result = LeaderPoolService(screening_dir=screening_dir).build_pool("20260618")

    assert result["rows"][0]["code"] == "002281"
    assert result["rows"][0]["pool_type"] == "核心龙头"
    assert result["rows"][0]["source_rank"] == 7
    assert result["rows"][0]["resonance_sectors"] == "光通信"
    assert all(row["code"] != "000001" for row in result["rows"])


def test_intraday_strength_uses_weak_to_strong_for_low_open():
    class FakePool:
        def build_pool(self, trade_date, *, lookback=5, limit=30):
            return {
                "trade_date": trade_date,
                "rows": [
                    {
                        "code": "002281",
                        "name": "光迅科技",
                        "pool_rank": 1,
                        "pool_type": "核心龙头",
                        "leader_score": 88,
                        "sector_status_score": 80,
                        "context": {"amount_ratio": 1.2},
                        "action": "按分钟入场条件确认",
                    }
                ],
            }

    class FakeQuotes:
        def get_quotes(self, codes):
            return {
                "quotes": [
                    {
                        "code": "002281",
                        "name": "光迅科技",
                        "pre_close": 10,
                        "open_price": 9.9,
                        "last_price": 10.1,
                        "change_pct": 1.0,
                    }
                ]
            }

    class FakeMinuteData:
        def get_minute_bars_live(self, ts_code, trade_date):
            prices = [
                (9.90, 9.94, 9.86, 9.92),
                (9.92, 9.96, 9.90, 9.94),
                (9.94, 9.98, 9.92, 9.96),
                (9.96, 9.99, 9.94, 9.98),
                (9.98, 10.00, 9.96, 9.99),
                (9.99, 10.06, 9.98, 10.04),
                (10.05, 10.08, 10.02, 10.06),
            ]
            return pd.DataFrame([
                {
                    "time": f"09:{30 + i:02d}:00", "open": op, "high": high,
                    "low": low, "close": close, "volume": 1000, "amount": close * 1000,
                }
                for i, (op, high, low, close) in enumerate(prices)
            ])

        def get_all_stocks_daily(self, trade_date):
            return pd.DataFrame()

    result = IntradayStrengthService(
        quote_service=FakeQuotes(),
        pool_service=FakePool(),
        entry_signal_service=RealtimeEntrySignalService(FakeMinuteData()),
    ).build("20260618", market_date="20260619")

    assert result["candidate_date"] == "20260618"
    assert result["market_date"] == "20260619"
    assert result["rows"][0]["status"] == "confirmed"
    assert result["rows"][0]["entry_mode_text"] == "弱转强"


def test_leader_pool_uses_20cm_limit_progress_for_chinext(tmp_path: Path):
    screening_dir = tmp_path / "screening"
    screening_dir.mkdir()
    payload = {
        "trade_date": "20260616",
        "final": [_leader_item(code="300059", name="东方财富", pct_chg=12.74)],
    }
    (screening_dir / "screening_20260616.json").write_text(
        json.dumps(payload, ensure_ascii=False),
        encoding="utf-8",
    )

    row = LeaderPoolService(screening_dir=screening_dir).build_pool("20260616")["rows"][0]

    assert row["limit_pct"] == 20.0
    assert 0.63 < row["limit_progress"] < 0.64
    assert not any("接近" in reason and "涨停" in reason for reason in row["reasons"])
    assert any("20cm涨停进度" in reason for reason in row["reasons"])


def test_recent_one_day_leader_is_kept_with_source_date(tmp_path: Path):
    screening_dir = tmp_path / "screening"
    screening_dir.mkdir()
    day1 = {
        "trade_date": "20260617",
        "final": [
            _leader_item(code="000001", name="历史龙头", rank=6),
            {"code": "000002", "name": "普通候选", "score": 80, "rank": 4, "metrics": {}, "context": {}},
        ],
    }
    day2 = {
        "trade_date": "20260618",
        "final": [
            {"code": "000001", "name": "历史龙头", "score": 75, "rank": 6, "metrics": {}, "context": {}},
            {"code": "000002", "name": "普通候选", "score": 78, "rank": 4, "metrics": {}, "context": {}},
        ],
    }
    for payload in (day1, day2):
        (screening_dir / f"screening_{payload['trade_date']}.json").write_text(
            json.dumps(payload, ensure_ascii=False), encoding="utf-8"
        )

    result = LeaderPoolService(screening_dir=screening_dir).build_pool("20260618", lookback=10)

    assert [row["code"] for row in result["rows"]] == ["000001"]
    row = result["rows"][0]
    assert row["pool_type"] == "近期龙头"
    assert row["last_leader_date"] == "20260617"
    assert row["source_date"] == "20260618"
    assert row["leader_age_days"] == 1
    assert row["leader_time_label"] == "上一交易日龙头"
