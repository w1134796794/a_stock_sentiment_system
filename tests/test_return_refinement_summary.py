from copy import deepcopy

import pytest

from scripts.summarize_return_refinement import control_reconciliation
from scripts.validate_confirmation_quality import source_plans


def account():
    return {"daily_nav": [{"date": "20261009", "total_value": 1000000}],
            "trade_history": [{"date": "20261009", "stock_code": "000001", "action": "BUY",
                               "entry_price": 10, "entry_time": "09:46:00", "shares": 1000}]}


@pytest.mark.parametrize("field,value", [("stock_code", "000002"), ("entry_time", "09:47:00"),
                                       ("entry_price", 10.01), ("shares", 1100)])
def test_control_audit_refuses_economic_trade_changes(field, value):
    original = account()
    changed = deepcopy(original)
    changed["trade_history"][0][field] = value
    assert not control_reconciliation(changed, original)


def test_control_audit_checks_nav_and_count_but_ignores_new_diagnostics():
    original = account()
    changed = deepcopy(original)
    changed["trade_history"][0]["research_only_metadata"] = "new"
    assert control_reconciliation(changed, original)
    changed["daily_nav"][0]["total_value"] -= 1
    assert not control_reconciliation(changed, original)
    changed = deepcopy(original)
    changed["trade_history"] = []
    assert not control_reconciliation(changed, original)


@pytest.mark.parametrize("arm", ["baseline", "strategy_quality"])
def test_research_source_requires_sealed_candidate_coverage(tmp_path, arm):
    manifest = {"arms": {arm: {}}, "calendar": ["20261008", "20261009"],
                "dates": ["20261009"], "files": {}}
    with pytest.raises(ValueError, match="目录"):
        source_plans(tmp_path, manifest)
    folder = tmp_path / "plans" / arm
    folder.mkdir(parents=True)
    with pytest.raises(ValueError, match="覆盖"):
        source_plans(tmp_path, manifest)
    file = folder / "交易计划_20261008.csv"
    file.write_text("代码\n", encoding="utf-8")
    with pytest.raises(ValueError, match="覆盖"):
        source_plans(tmp_path, manifest)
    manifest["files"][file.relative_to(tmp_path).as_posix()] = "sealed_hash"
    assert source_plans(tmp_path, manifest) == folder
