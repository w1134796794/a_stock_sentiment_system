"""Render real portfolio templates without starting market workers or API calls."""

import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from starlette.requests import Request

from web.app import templates


def main():
    output = ROOT / "outputs/history-workflow-ui"
    output.mkdir(parents=True, exist_ok=True)
    templates.env.globals["visible_menu_groups"] = lambda user: []
    for role in ("admin", "viewer"):
        request = Request(
            dict(
                type="http",
                method="GET",
                scheme="http",
                path="/portfolio/backtest",
                root_path="",
                query_string=b"",
                headers=[],
                server=("localhost", 80),
                client=("localhost", 1),
            )
        )
        request.state.user = dict(role=role, username="preview")
        html = templates.get_template("portfolio.html").render(
            request=request,
            portfolio_mode="backtest",
            paper_initial_capital=1000000,
            paper_max_positions=3,
            paper_position_pct=34,
            paper_rotation_min_edge=6,
            latest_trade_date="20261023",
        )
        (output / f"{role}.html").write_text(html, encoding="utf-8")
        if role == "admin":
            request = Request({**request.scope, "path": "/market-collection"})
            html = templates.get_template("market_backfill.html").render(
                request=request, backfill_view=True, paper_initial_capital=1000000
            )
            (output / "backfill.html").write_text(html, encoding="utf-8")


if __name__ == "__main__":
    main()
