"""Render real navigation/data templates using isolated, deterministic fixtures."""
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from starlette.requests import Request

from web import auth_store
from web.app import templates
from web.page_highlights import data_highlights


def main():
    output = ROOT / "outputs/product-navigation"
    output.mkdir(parents=True, exist_ok=True)
    auth_store.AUTH_DB_PATH = output / "fixture-auth.sqlite"
    auth_store.get_role_permission_overrides.cache_clear()
    limitup = {"name": "涨停梯队", "kind": "table", "columns": ["股票名称", "连板数", "股票代码"], "rows": [
        {"股票名称": name, "股票代码": f"00000{i}", "连板数": level, "行业": sector, "涨幅%": 10,
         "首次涨停时间": "09:36:00", "炸板次数": 0, "_detail_url": f"/stock/00000{i}"}
        for i, (name, level, sector) in enumerate([("海洋王", 7, "照明设备"), ("东安动力", 4, "汽车零部件"), ("科森科技", 2, "电子元件"), ("九阳股份", 1, "家用电器"), ("南威软件", 1, "软件服务")], 1)]}
    concept = {"name": "概念连板梯队", "kind": "table", "columns": ["概念名称", "涨停总数", "最高连板"], "rows": [{"概念名称": "机器人概念", "涨停总数": 3, "最高连板": 4, "梯队分布": "4板1家 · 2板1家 · 1板1家"}]}
    limitup["rows"][1]["炸板次数"] = 3
    limitup["rows"][4]["炸板次数"] = None
    sector = {"name": "主线主题", "kind": "table", "columns": ["sector_name"], "rows": [
        {"sector_name": name, "sector_code": f"88600{i}.TI", "_review_version": "sector_review_v2",
         "_review_rank": i + 1, "_review_score": score, "_review_status": state,
         "_review_qualified": i == 0, "_review_history_coverage": 100 if i < 2 else 60,
         "_review_score_coverage": 85, "_review_days": 10 if i < 2 else 6, "_review_expected_days": 10,
         "_review_excess_pct": 12.34, "_review_amount_ratio": 1.4, "近10日上榜天数": 7,
         "近10日累计涨幅%": 18.5,
         "_review_parts": [{"name": "持续活跃", "score": score, "weight": .35}, {"name": "相对强度", "score": 80, "weight": .3}, {"name": "量能持续", "score": 75, "weight": .2}, {"name": "涨停扩散", "score": None, "weight": .15}]}
        for i, (name, score, state) in enumerate([("机器人概念", 82, "持续主线"), ("信创", 60, "轮动观察"), ("人工智能", 75, "数据不足")])]}
    hotspot = {"name": "热点概念", "kind": "table", "columns": ["sector_name"], "rows": sector["rows"][:1]}
    if "--real" in sys.argv:
        from web.app import _factor_sector_rows, _mainline_theme_rows
        sector["rows"] = _mainline_theme_rows("20261023", limit=20)
        hotspot["rows"] = _factor_sector_rows("20261023", limit=20, review=True)
    for role in ("admin", "viewer"):
        for cat, sections in (("limitup", [limitup, concept]), ("sector", [sector, hotspot])):
            request = Request(dict(type="http",method="GET",scheme="http",path=f"/data/{cat}/20261023",root_path="",query_string=b"",headers=[],server=("localhost",80),client=("localhost",1)))
            request.state.user = {"role":role,"username":"preview","label":"管理员" if role == "admin" else "只读用户","expire_at_display":"--"}
            html = templates.get_template("data_browse.html").render(request=request,category={"label":"涨停数据" if cat == "limitup" else "板块热度","names":[s["name"] for s in sections],"signals":False},cat=cat,date="20261023",dates=["20261023","20261022"],sections=sections,snapshot={},highlights=data_highlights(cat,sections))
            (output / f"{cat}-{role}.html").write_text(html, encoding="utf-8")
    (output / "concept.html").write_text(templates.get_template("partials/section.html").render(section=concept,view="echelon"),encoding="utf-8")
    (output / "hotspot.html").write_text(templates.get_template("partials/section.html").render(section=hotspot),encoding="utf-8")


if __name__ == "__main__":
    main()
