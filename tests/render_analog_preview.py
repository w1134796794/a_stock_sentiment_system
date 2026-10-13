"""Render the real report template into an isolated browser-test shell."""
from pathlib import Path

from jinja2 import ChoiceLoader, DictLoader, Environment, FileSystemLoader

root = Path(__file__).resolve().parents[1]
env = Environment(loader=ChoiceLoader([
    DictLoader({"base.html": '<!doctype html><meta charset="utf-8"><meta name="viewport" content="width=device-width, initial-scale=1"><link rel="stylesheet" href="/static/css/app.css"><body class="bg-slate-950 text-slate-100"><main style="padding:16px;max-width:1400px;margin:auto">{% block content %}{% endblock %}</main></body>'}),
    FileSystemLoader(root / "web" / "templates"),
]), autoescape=True)
directory = root / "outputs" / "analog-ui"
directory.mkdir(parents=True, exist_ok=True)
(directory / "evaluation.html").write_text(env.get_template("strategy_evaluation.html").render(latest_trade_date="20260901"), encoding="utf-8")
