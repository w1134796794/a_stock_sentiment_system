import pytest

from web import auth_store
from web.navigation import product_navigation
from web.page_highlights import data_highlights
from web.permissions import can_access_path, update_permission_matrix


@pytest.fixture(autouse=True)
def isolated_permissions(tmp_path, monkeypatch):
    monkeypatch.setattr(auth_store, "AUTH_DB_PATH", tmp_path / "auth.sqlite")
    auth_store.get_role_permission_overrides.cache_clear()
    yield
    auth_store.get_role_permission_overrides.cache_clear()


def test_six_product_modules_replace_flat_admin_navigation():
    state = product_navigation({"role": "admin"}, "/data/limitup/20261023")
    assert [module["key"] for module in state["modules"]] == ["workspace", "market", "research", "validation", "collection", "settings"]
    assert state["current"]["key"] == "market"
    assert [tab["key"] for tab in state["current"]["tabs"] if tab["active"]] == ["limitup"]
    assert state["current"]["tabs"][1]["href"] == "/data/sector/20261023"
    assert state["current"]["tabs"][2]["href"] == "/data/lhb/20261023"


def test_read_only_users_do_not_gain_admin_tabs():
    user = {"role": "viewer"}
    state = product_navigation(user, "/portfolio/backtest")
    keys = {module["key"] for module in state["modules"]}
    assert "research" not in keys and "collection" not in keys
    assert [module for module in state["modules"] if module["key"] == "settings"][0]["tabs"][0]["key"] == "about"
    for module in state["modules"]:
        for tab in module["tabs"]:
            assert can_access_path(user, tab["href"].split("?")[0])


def test_specific_replay_and_history_paths_win_over_parent_paths():
    replay = product_navigation({"role": "admin"}, "/intraday/replay")
    assert replay["current"]["key"] == "validation"
    assert next(tab for tab in replay["current"]["tabs"] if tab["active"])["key"] == "replay"
    history = product_navigation({"role": "admin"}, "/portfolio/backtest")
    assert [tab["key"] for tab in history["current"]["tabs"] if tab["active"]] == ["history"]
    assert product_navigation({"role": "admin"}, "/intraday-malicious")["current"] is None


def test_candidate_date_is_preserved_without_reinterpreting_it_as_market_date():
    result = product_navigation({"role": "admin"}, "/dragon", query={"date": "20261023"})
    assert next(tab for tab in result["current"]["tabs"] if tab["key"] == "signals")["href"] == "/intraday?date=20261023"


def test_invalid_dates_do_not_enter_links():
    result = product_navigation({"role": "admin"}, "/data/limitup", date="20260230")
    assert result["date"] == ""
    assert result["current"]["tabs"][1]["href"] == "/data/sector"


def test_explicit_permission_overrides_hide_disallowed_queries():
    update_permission_matrix([{"role": "viewer", "permission_key": "lhb", "can_access": False, "menu_visible": False}])
    result = product_navigation({"role": "viewer"}, "/data/limitup/20261023")
    assert not any(tab["key"] == "lhb" for tab in result["current"]["tabs"])


def test_navigation_has_no_market_dependency(monkeypatch):
    monkeypatch.setattr("core.data.data_manager_main.DataManager.__init__", lambda *a, **kw: pytest.fail("Navigation cannot fetch"))
    assert product_navigation({"role": "admin"}, "/market-collection")["current"]["key"] == "collection"


def test_limitup_highlights_use_supplied_rows_not_synthetic_signals():
    result = data_highlights("limitup", [{"name": "涨停梯队", "rows": [
        {"股票名称": "甲", "连板数": 4}, {"股票名称": "乙", "连板数": 2}, {"股票名称": "丙", "连板数": 1}]}])
    assert result["headline"] == "最高 4 板"
    assert result["metrics"][0][1] == 3
    assert result["metrics"][1][1] == 2
    assert "甲" in result["detail"]


def test_missing_rows_are_not_displayed_as_zero_or_confirmations():
    assert data_highlights("limitup", []) is None
    value = data_highlights("limitup", [{"name": "涨停梯队", "rows": [{"股票名称": "甲"}]}])
    assert "待核验" in value["headline"]
    assert value["metrics"][1][1] == "--"


def test_sector_summary_keeps_snapshot_order_and_scope():
    value = data_highlights("sector", [{"name": "主线主题", "rows": [{"sector_name": "软件"}, {"sector_name": "电力"}]}])
    assert value["headline"] == "软件"
    assert "非盘中买点" in value["detail"]
    assert value["metrics"][0][1] == 2


def test_market_removes_latest_overview_tab():
    state = product_navigation({"role": "admin"}, "/data/sector/20261023")
    assert [tab["key"] for tab in state["current"]["tabs"]] == ["limitup", "sectors", "lhb"]


def test_v2_sector_summary_does_not_call_rotation_a_mainline():
    value = data_highlights("sector", [{"name": "主线主题", "rows": [
        {"sector_name": "短暂热点", "_review_version": "sector_review_v2", "_review_status": "轮动观察"}]}])
    assert value["headline"] == "轮动观察"
    assert value["metrics"][0][1] == 0
