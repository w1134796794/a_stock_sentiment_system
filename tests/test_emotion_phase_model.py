"""循环相位模型单测（情绪周期权威来源，纯函数）。"""
from core.analysis.emotion_phase_model import compute_phase_model, LEGACY_MAP


def _base(**kw):
    m = {
        "limit_up_count": 60, "max_board_height": 4, "broken_rate": 20,
        "continuous_rate": 20, "limit_down_ratio": 0.1, "win_rate": 55,
        "avg_profit": 2.0, "prev_limit_up_premium": 1.5,
        "promotion": {"overall": 30.0},
    }
    m.update(kw)
    return m


def test_returns_none_on_empty():
    assert compute_phase_model({}) is None
    assert compute_phase_model({"limit_up_count": 0}) is None


def test_freeze():
    r = compute_phase_model(_base(limit_up_count=15, max_board_height=2,
                                  limit_down_ratio=0.6, win_rate=25,
                                  continuous_rate=3, promotion={"overall": 5.0}))
    assert r["phase"] == "冰点"
    assert r["legacy_cycle_name"] == "冰点期"


def test_climax():
    r = compute_phase_model(_base(limit_up_count=120, max_board_height=8,
                                  broken_rate=30, win_rate=65,
                                  promotion={"overall": 40.0}))
    assert r["phase"] == "高潮"
    assert r["legacy_cycle_name"] == "高潮期"
    assert r["momentum"] == "见顶"


def test_ferment():
    r = compute_phase_model(_base(limit_up_count=70, max_board_height=5,
                                  continuous_rate=25, broken_rate=15,
                                  win_rate=55, promotion={"overall": 35.0}))
    assert r["phase"] == "发酵"
    assert r["legacy_cycle_name"] == "上升期"


def test_decline():
    r = compute_phase_model(_base(limit_up_count=55, max_board_height=4,
                                  win_rate=35, limit_down_ratio=0.5,
                                  broken_rate=50, avg_profit=-1.0,
                                  promotion={"overall": 10.0}))
    assert r["phase"] == "退潮"
    assert r["legacy_cycle_name"] == "退潮期"
    assert r["momentum"] == "降温"


def test_momentum_real_env_warming():
    """有昨日 metrics 且晋级率/赚钱效应同步走高 → 升温。"""
    prev = _base(win_rate=45, promotion={"overall": 20.0}, max_board_height=4)
    today = _base(limit_up_count=70, max_board_height=5, continuous_rate=25,
                  broken_rate=15, win_rate=58, promotion={"overall": 35.0})
    r = compute_phase_model(today, prev_metrics=prev)
    assert r["phase"] == "发酵"
    assert r["momentum"] == "升温"


def test_momentum_real_env_cooling():
    """晋级率/赚钱效应环比走低 → 降温。"""
    prev = _base(win_rate=60, promotion={"overall": 40.0}, max_board_height=5)
    today = _base(limit_up_count=55, max_board_height=4, win_rate=35,
                  limit_down_ratio=0.5, broken_rate=50, avg_profit=-1.0,
                  promotion={"overall": 10.0})
    r = compute_phase_model(today, prev_metrics=prev)
    assert r["momentum"] == "降温"


def test_config_thresholds_loaded():
    """阈值应能从 YAML 单一真源加载（存在 thresholds 键）。"""
    from core.analysis.emotion_phase_model import _load_thresholds
    cfg = _load_thresholds()
    assert "th" in cfg and "mom" in cfg
    assert cfg["th"]["high_lu"] == 90  # 与 YAML 默认一致


def test_engine_cycle_name_comes_from_phase_model():
    """情绪周期的权威 cycle_name 直接来自循环相位模型的 legacy 映射。"""
    import pandas as pd
    from core.analysis.emotion_cycle_engine import EmotionCycleEngine
    eng = EmotionCycleEngine()
    zt = pd.DataFrame({
        "ts_code": [f"0000{i:02d}.SZ" for i in range(40)],
        "limit_times": [1] * 40,
        "float_mv": [3e9] * 40,
        "open_times": [0] * 40,
    })
    res = eng.analyze_market_data(limit_up_df=zt)
    assert res["phase_model"] is not None
    assert res["cycle_name"] == res["phase_model"]["legacy_cycle_name"]
    # 旧引擎相关字段已彻底移除
    assert "authoritative_source" not in res
    assert "cycle_name_rule_engine" not in res


def test_emotion_phase_consumes_phase_model():
    """相位子态分析消费 phase_model：高潮相位应给出转入「退潮」预警。"""
    from core.analysis.emotion_phase import analyze_emotion_phase
    pm = compute_phase_model(_base(limit_up_count=120, max_board_height=8,
                                   broken_rate=30, win_rate=65,
                                   promotion={"overall": 40.0}))
    res = analyze_emotion_phase({"phase_model": pm, "cycle_name": pm["legacy_cycle_name"]})
    assert res is not None
    assert res.cycle_name == "高潮期"
    assert "退潮" in res.transition_warning
    assert analyze_emotion_phase({}) is None


def test_output_shape():
    r = compute_phase_model(_base())
    assert set(["phase", "momentum", "trunk_clarity", "legacy_cycle_name",
                "scores", "score_gap"]).issubset(r.keys())
    assert 0.0 <= r["trunk_clarity"] <= 1.0
    # legacy 映射闭合
    assert r["legacy_cycle_name"] in set(LEGACY_MAP.values())
