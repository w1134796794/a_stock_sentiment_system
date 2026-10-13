"""Render real templates with read-only fixtures for browser regression checks."""
from pathlib import Path
import sys
from types import SimpleNamespace

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from jinja2 import Environment, FileSystemLoader
from core.screening.strategy_profiles import StrategyProfileRepository, MARKET_REGIMES, EMOTION_PHASES, ENTRY_MODES, SUPPORTED_OPERATORS, STOCK_POOLS, WEIGHT_SOURCES

env = Environment(loader=FileSystemLoader(ROOT / "web/templates"), autoescape=True)
env.globals["visible_menu_groups"] = lambda user: []
context = {"current_user": {"role": "admin", "username": "preview"}, "nav_sections": [],
           "request": SimpleNamespace(state=SimpleNamespace(user={"role": "admin", "username": "preview"})),
           "paper_initial_capital": 1000000, "paper_max_positions": 3,
           "paper_position_pct": 34, "paper_rotation_min_edge": 6, "latest_trade_date": "20261009",
           "strategy_state": {"profiles": StrategyProfileRepository().list_profiles(),
               "factors": [{"id": "tech_score", "name": "技术综合分", "category": "stock"}],
               "base_profiles": ["default"], "market_regimes": MARKET_REGIMES,
               "emotion_phases": EMOTION_PHASES, "entry_modes": ENTRY_MODES,
               "operators": SUPPORTED_OPERATORS, "stock_pools": STOCK_POOLS,
               "weight_sources": WEIGHT_SOURCES, "model_dependencies": {}}}
output = ROOT / "outputs/config-sync-ui"
output.mkdir(parents=True, exist_ok=True)
for name in ("strategies", "portfolio"):
    (output / f"{name}.html").write_text(env.get_template(f"{name}.html").render(**context), encoding="utf-8")
print(output)
