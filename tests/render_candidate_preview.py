"""Render both SSR and fragment candidate views for interaction regression tests."""
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from starlette.requests import Request

from web.app import templates


def main():
    output = ROOT / "outputs/candidate-ui"
    output.mkdir(parents=True, exist_ok=True)
    names = ["主线龙头候选", "弱转强修复", "首板启动", "暂无匹配策略"]
    rows = []
    for code, name, strategies in [("300369", "绿盟科技", names[:2]), ("002322", "理工能科", [names[2]]),
                                    ("603721", "中广天择", names[1:3]), ("000001", "暂不参与样本", ["已停用策略"])]:
        rows.append({"code": code, "name": name, "命中策略": strategies, "规则等级": "A", "行动分组": "观察",
                     "策略共识显示": "2/5", "增强证据": [], "否决条件": [], "所属主线": "软件",
                     "一句话结论": "等待盘中条件确认", "_detail_url": f"/stock/{code}?date=20261023&context_date=20261023"})
    section = {"name": "今日决策池", "category": "strategy", "kind": "decision_pool", "tab_count": 3,
               "market_regime_label": "强市", "active_strategy_names": names, "hidden_strategy_names": [],
               "rows": rows, "columns": ["code", "name"], "groups": [
                   {"key": "focus", "name": "重点确认", "meaning": "待确认", "rows": rows[:2]},
                   {"key": "watch", "name": "盘中观察", "meaning": "继续观察", "rows": rows[2:3]},
                   {"key": "inactive", "name": "暂不参与", "meaning": "不参与", "rows": rows[3:]}]}
    request = Request(dict(type="http", method="GET", scheme="http", path="/data/strategy/20261023", root_path="",
                           query_string=b"", headers=[], server=("localhost", 80), client=("localhost", 1)))
    request.state.user = {"role": "admin", "username": "preview", "label": "管理员"}
    (output / "page.html").write_text(templates.get_template("data_browse.html").render(request=request,
        category={"label": "候选股", "names": [], "signals": False}, date="20261023", dates=["20261023"],
        cat="strategy", snapshot={}, sections=[section], highlights=None), encoding="utf-8")
    (output / "fragment.html").write_text(templates.get_template("partials/section.html").render(section=section, view="detail"), encoding="utf-8")
    request = Request({**request.scope, "path": "/stock/300369"})
    (output / "stock.html").write_text(templates.get_template("stock_detail.html").render(request=request,
        code="300369", stock_name="绿盟科技", date="20261023", market_date="20261023", context_date="20261023",
        dates=["20261023"], context={}), encoding="utf-8")


if __name__ == "__main__":
    main()
