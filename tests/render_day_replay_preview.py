"""Render the actual diagnostic template without starting live automation."""
from pathlib import Path
from types import SimpleNamespace

from jinja2 import Environment, FileSystemLoader

root = Path(__file__).resolve().parents[1]
env = Environment(loader=FileSystemLoader(root / "web/templates"), autoescape=True)
env.globals["visible_menu_groups"] = lambda user: []
context = {"current_user":{"role":"admin", "username":"preview"}, "is_admin":True,
           "request":SimpleNamespace(state=SimpleNamespace(user={"role":"admin", "username":"preview"})),
           "market_date":"20261009"}
directory = root / "outputs/intraday-replay-ui"
directory.mkdir(parents=True, exist_ok=True)
(directory / "page.html").write_text(env.get_template("intraday_replay.html").render(**context), encoding="utf-8")
print(directory)
