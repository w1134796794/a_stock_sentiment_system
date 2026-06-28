import json
from pathlib import Path

from core.realtime.leader_pool_service import IntradayStrengthService, LeaderPoolService


def test_leader_pool_from_screening_json(tmp_path: Path):
    screening_dir = tmp_path / "screening"
    screening_dir.mkdir()
    payload = {
        "trade_date": "20260618",
        "final": [
            {
                "code": "002281",
                "name": "光迅科技",
                "score": 94.8,
                "rank": 1,
                "metrics": {
                    "tech_score": 94,
                    "stk_liquidity_percentile": 99,
                    "stk_new_high_20d": 88,
                },
                "context": {
                    "pct_chg": 10,
                    "amount_ratio": 1.3,
                    "vol_ratio": 1.2,
                },
            }
        ],
    }
    (screening_dir / "screening_20260618.json").write_text(
        json.dumps(payload, ensure_ascii=False),
        encoding="utf-8",
    )

    result = LeaderPoolService(screening_dir=screening_dir).build_pool("20260618")

    assert result["rows"][0]["code"] == "002281"
    assert result["rows"][0]["pool_type"] == "核心龙头"
    assert result["rows"][0]["leader_score"] > 80


def test_intraday_strength_cancels_low_open():
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
                        "action": "只在高开且实时转强时确认；低开直接放弃",
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

    result = IntradayStrengthService(quote_service=FakeQuotes(), pool_service=FakePool()).build("20260618")

    assert result["rows"][0]["status"] == "cancelled"
    assert "低开" in result["rows"][0]["reason"]


def test_leader_pool_uses_20cm_limit_progress_for_chinext(tmp_path: Path):
    screening_dir = tmp_path / "screening"
    screening_dir.mkdir()
    payload = {
        "trade_date": "20260616",
        "final": [
            {
                "code": "300059",
                "name": "东方财富",
                "score": 94.8,
                "rank": 1,
                "metrics": {
                    "tech_score": 94,
                    "stk_liquidity_percentile": 99,
                    "stk_new_high_20d": 88,
                },
                "context": {
                    "pct_chg": 12.74,
                    "amount_ratio": 1.3,
                    "vol_ratio": 1.2,
                },
            }
        ],
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
            {"code": "000001", "name": "历史龙头", "score": 90, "rank": 1, "metrics": {}, "context": {}},
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
