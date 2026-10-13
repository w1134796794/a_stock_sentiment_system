from copy import deepcopy
import json

import pytest
import yaml

from core.screening.strategy_profiles import StrategyProfileRepository
from core.screening.config_transfer import StrategyConfigTransfer, _fingerprint


def repository(tmp_path, name):
    root = tmp_path / name
    root.mkdir()
    base = root / "bases.yaml"
    base.write_text(yaml.safe_dump({"screening_profiles": {"default": {
        "ranking": {"prior_weights": {"tech_score": 1.0}},
        "liquidity_filter": {"minimum": 123},
    }}}), encoding="utf-8")
    repo = StrategyProfileRepository(root / "runtime.yaml", base)
    repo.default_path = root / "missing-default.yaml"
    repo.save("weak_to_strong", {"name": "弱转强", "scope": "production",
        "base_profile": "default", "ranking_factors": [{"factor": "tech_score", "weight": 1}],
        "weight_source": "manual", "market_regimes": ["strong", "neutral", "weak"],
        "execution": {"confirmation_deadline": "10:00"}})
    return repo


def test_round_trip_freezes_inherited_base_and_diffs_deadline(tmp_path):
    source, target = repository(tmp_path, "source"), repository(tmp_path, "target")
    row = source.get_profile("weak_to_strong")
    row["execution"]["confirmation_deadline"] = "10:30"
    source.save(row["id"], row)
    base = yaml.safe_load(target.base_profile_path.read_text())
    base["screening_profiles"]["default"]["liquidity_filter"]["minimum"] = 456
    target.base_profile_path.write_text(yaml.safe_dump(base), encoding="utf-8")
    transfer = StrategyConfigTransfer(target)
    bundle = StrategyConfigTransfer(source).export()
    before = target.path.read_bytes()
    diff = transfer.compare(bundle)
    assert target.path.read_bytes() == before
    assert diff["changed_count"] == 1
    assert any("confirmation_deadline" in c["field"] for c in diff["strategies"][0]["changes"])
    assert any("liquidity_filter" in c["field"] for c in diff["strategies"][0]["changes"])
    result = transfer.apply(bundle, diff["current_version"])
    assert result["effective_version"] == bundle["effective_version"]
    assert target.resolve("weak_to_strong") == source.resolve("weak_to_strong")
    assert target.path.parent.joinpath(result["backup_path"]).exists()


def test_invalid_bundle_is_atomic_and_stale_preview_is_rejected(tmp_path):
    repo = repository(tmp_path, "source")
    transfer = StrategyConfigTransfer(repo)
    bundle = transfer.export()
    before = repo.path.read_bytes()
    invalid = deepcopy(bundle)
    invalid["strategies"]["weak_to_strong"]["execution"]["confirmation_deadline"] = "25:30"
    invalid["bundle_version"] = _fingerprint({key: invalid[key] for key in ("strategies", "base_profiles")})
    with pytest.raises(ValueError): transfer.apply(invalid, bundle["effective_version"])
    assert repo.path.read_bytes() == before
    row = repo.get_profile("weak_to_strong")
    row["top_n"] = 7
    repo.save(row["id"], row)
    with pytest.raises(ValueError, match="重新比较"): transfer.apply(bundle, bundle["effective_version"])


def test_tampering_and_nonfinite_values_are_rejected(tmp_path):
    transfer = StrategyConfigTransfer(repository(tmp_path, "source"))
    bundle = transfer.export()
    bundle["strategies"]["weak_to_strong"]["top_n"] = 9
    with pytest.raises(ValueError, match="校验失败"): transfer.compare(bundle)
    bundle["strategies"]["weak_to_strong"]["top_n"] = float("nan")
    with pytest.raises(ValueError): transfer.compare(bundle)
    with pytest.raises(ValueError, match="JSON对象"): transfer.compare([])


def test_all_builtin_profiles_can_be_exported_and_compared_without_changes():
    transfer = StrategyConfigTransfer()
    assert transfer.compare(transfer.export())["changed_count"] == 0


def test_replay_fingerprint_includes_runtime_configuration(tmp_path, monkeypatch):
    from core.portfolio.incremental_replay import IncrementalPaperReplay
    import core.screening.strategy_profiles as module
    repo = repository(tmp_path, "source")
    monkeypatch.setattr(module, "StrategyProfileRepository", lambda: repo)
    before = IncrementalPaperReplay.config_version()
    row = repo.get_profile("weak_to_strong")
    row["execution"]["confirmation_deadline"] = "10:30"
    repo.save(row["id"], row)
    assert IncrementalPaperReplay.config_version() != before
