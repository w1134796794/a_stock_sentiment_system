from desktop.backtest import _pattern_text, _trade_text
import pandas as pd

from backtest.backtest_engine import BacktestConfig, BacktestEngine


def test_trade_enums_are_rendered_in_chinese():
    assert _trade_text("trailing_stop") == "回撤止盈"
    assert _trade_text("stop_loss_gap") == "跳空止损"
    assert _trade_text("time_stop") == "时间止损"
    assert _trade_text("buy") == "买入成交"
    assert _pattern_text("指标筛选/default") == "指标筛选/默认"


def _write_plans(path, market_score, scores):
    pd.DataFrame([
        {
            "动作": "买入", "代码": f"00000{index + 1}", "名称": f"股票{index + 1}",
            "综合评分": score, "原始_mkt_market_score": market_score,
        }
        for index, score in enumerate(scores)
    ]).to_csv(path / "交易计划_20260626.csv", index=False)


def test_market_layers_use_quality_gates_without_fixed_rank_cap(tmp_path):
    engine = BacktestEngine(None, BacktestConfig())

    _write_plans(tmp_path, 62, [85, 79])
    assert engine._load_trade_plans("20260626", str(tmp_path))["综合评分"].tolist() == [85]

    _write_plans(tmp_path, 67, [78, 75])
    assert engine._load_trade_plans("20260626", str(tmp_path))["综合评分"].tolist() == [78]

    _write_plans(tmp_path, 72, [78, 75])
    assert engine._load_trade_plans("20260626", str(tmp_path))["综合评分"].tolist() == [78, 75]
