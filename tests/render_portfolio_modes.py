"""Render both real portfolio views without starting trading or touching accounts."""
from pathlib import Path
from types import SimpleNamespace
from jinja2 import Environment, FileSystemLoader

root = Path(__file__).resolve().parents[1]
env = Environment(loader=FileSystemLoader(root / 'web/templates'), autoescape=True)
env.globals['visible_menu_groups'] = lambda user: []
context = {'current_user': {'role': 'admin', 'username': 'preview'}, 'is_admin': True,
           'request': SimpleNamespace(state=SimpleNamespace(user={'role': 'admin', 'username': 'preview'})),
           'paper_initial_capital': 1000000, 'paper_max_positions': 3, 'paper_position_pct': 34,
           'paper_rotation_min_edge': 6, 'latest_trade_date': '20261014'}
out = root / 'outputs/portfolio-parity-ui'
out.mkdir(parents=True, exist_ok=True)
for mode in ('paper', 'backtest'):
    (out / f'{mode}.html').write_text(env.get_template('portfolio.html').render(
        **context, portfolio_mode=mode), encoding='utf-8')
print(out)
