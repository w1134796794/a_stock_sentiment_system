from argparse import Namespace
from pathlib import Path

import pandas as pd
import pytest

from core.factors.factor_library import FactorLibraryTrainer, TrainingPrerequisiteError
from scripts.train_factor_library import build_child_command


def test_all_strategy_child_command_preserves_walk_forward_arguments(tmp_path: Path):
    args = Namespace(
        start="20250603",
        end="20260714",
        effective_date="",
        walk_forward=True,
        train_months=12,
    )

    command = build_child_command(args, "mainline_leader", tmp_path / "result.json")

    assert "--walk-forward" in command
    assert command[command.index("--train-months") + 1] == "12"


def test_walk_forward_uses_strategy_horizon_and_reports_expected_skip(monkeypatch, tmp_path: Path):
    trainer = FactorLibraryTrainer(duckdb_path=tmp_path / "factor.duckdb")
    captured = {}
    monkeypatch.setattr(trainer, "prior_weights", lambda profile: {"tech_score": 1.0})
    monkeypatch.setattr(trainer, "training_scope", lambda profile: "all")

    def fake_load(start, end, factors, **kwargs):
        captured.update(kwargs)
        trainer._last_strategy_training_audit = {
            "candidate_rows": 10,
            "filled_rows": 0,
            "excluded": {"missing_minutes": 10},
        }
        return pd.DataFrame()

    monkeypatch.setattr(trainer, "load_training_frame", fake_load)

    with pytest.raises(TrainingPrerequisiteError) as error:
        trainer.walk_forward("20250603", "20260714", profile="trend_follow", train_months=12)

    assert captured["profile"] == "trend_follow"
    assert captured["horizon_days"] == 5
    assert error.value.reason_code == "insufficient_executable_samples"
    assert error.value.audit["excluded"]["missing_minutes"] == 10
