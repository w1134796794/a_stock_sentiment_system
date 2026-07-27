from config.config_registry import build_simple_registry


def test_simple_config_registry_only_exposes_daily_controls():
    registry = build_simple_registry()
    scopes = {section["scope"] for section in registry["sections"]}
    assert scopes == {"settings", "risk"}

    paths = {
        field["path"]
        for section in registry["sections"]
        for group in section["groups"]
        for field in group["fields"]
    }
    assert "initial_capital" in paths
    assert "hard_stop_loss" in paths
    assert "market_entry_threshold" in paths
    assert "take_profit" not in paths
    assert not any("factor_registry" in path for path in paths)
