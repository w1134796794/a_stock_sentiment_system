from core.utils.price_limit import (
    get_price_limit_pct_points,
    limit_down_price,
    limit_progress,
    limit_up_price,
)


def test_price_limit_thresholds_by_board():
    assert get_price_limit_pct_points("000001", "平安银行") == 10.0
    assert get_price_limit_pct_points("300059", "东方财富") == 20.0
    assert get_price_limit_pct_points("688001", "华兴源创") == 20.0
    assert get_price_limit_pct_points("430047", "北交测试") == 30.0
    assert get_price_limit_pct_points("600001", "*ST示例") == 5.0


def test_limit_progress_uses_board_specific_limit_without_official_judgement():
    assert round(limit_progress(12.74, "300059", "东方财富"), 4) == 0.637
    assert round(limit_progress(9.8, "000001", "平安银行"), 2) == 0.98


def test_theoretical_limit_prices_are_board_specific():
    assert limit_up_price(10.0, "300059", "东方财富") == 12.0
    assert limit_down_price(10.0, "300059", "东方财富") == 8.0
    assert limit_up_price(10.0, "600001", "*ST示例") == 10.5
    assert limit_down_price(10.0, "600001", "*ST示例") == 9.5
