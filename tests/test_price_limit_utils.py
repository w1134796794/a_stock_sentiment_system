from core.utils.price_limit import (
    get_price_limit_pct_points,
    is_near_limit_up_pct,
    near_limit_up_threshold_pct,
)


def test_price_limit_thresholds_by_board():
    assert get_price_limit_pct_points("000001", "平安银行") == 10.0
    assert get_price_limit_pct_points("300059", "东方财富") == 20.0
    assert get_price_limit_pct_points("688001", "华兴源创") == 20.0
    assert get_price_limit_pct_points("430047", "北交测试") == 30.0
    assert get_price_limit_pct_points("600001", "*ST示例") == 5.0

    assert near_limit_up_threshold_pct("000001", "平安银行") == 9.5
    assert near_limit_up_threshold_pct("300059", "东方财富") == 19.5
    assert near_limit_up_threshold_pct("600001", "*ST示例") == 4.8


def test_chinext_mid_teens_gain_is_not_near_limit_up():
    assert is_near_limit_up_pct(12.74, "300059", "东方财富") is False
    assert is_near_limit_up_pct(19.8, "300059", "东方财富") is True
    assert is_near_limit_up_pct(9.8, "000001", "平安银行") is True
