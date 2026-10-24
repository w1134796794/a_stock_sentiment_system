"""Render isolated evidence UI fixtures for the browser smoke test."""

import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))
from starlette.requests import Request

from web.app import templates


def main():
    folder = ROOT / "outputs" / "market-evidence-ui"
    folder.mkdir(parents=True, exist_ok=True)
    templates.env.globals["visible_menu_groups"] = lambda user: []
    for role in ("viewer", "admin"):
        request = Request(
            dict(
                type="http",
                method="GET",
                scheme="http",
                path="/market-evidence",
                root_path="",
                query_string=b"",
                headers=[],
                server=("localhost", 80),
                client=("localhost", 1),
            )
        )
        request.state.user = dict(role=role, username="test")
        html = templates.get_template("market_evidence.html").render(request=request, market_date="20261023")
        (folder / f"{role}.html").write_text(html, encoding="utf-8")


if __name__ == "__main__":
    main()
