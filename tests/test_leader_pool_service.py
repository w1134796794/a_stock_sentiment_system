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
