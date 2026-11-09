"""Bounded, local-only queries over registered datasets."""
from datetime import datetime
from pathlib import Path
import math
import duckdb

TABLES = {
 "stock_daily_silver": "个股日线", "sector_daily_silver": "板块日线",
 "index_daily_silver": "指数日线", "limit_up_pool_silver": "涨停池",
 "limit_down_pool_silver": "跌停池", "lhb_daily_silver": "龙虎榜",
 "lhb_institution_silver": "机构交易", "lhb_hot_money_silver": "游资明细",
 "stock_capital_flow_silver": "个股资金流", "sector_capital_flow_silver": "板块资金流",
 "stock_attention_silver": "个股关注度", "stock_leader_signal_silver": "龙头信号",
 "stock_margin_silver": "融资融券", "stock_event_silver": "股票事件",
}
RAW = {
 "daily": ("全市场日线", "stock/all_daily/{date}.csv"),
 "limit_up": ("涨停池", "market/limit_up/{date}.csv"),
 "limit_down": ("跌停池", "market/limit_down/{date}.csv"),
 "sector_daily": ("板块日线", "sector/ths_daily/all_{date}.csv"),
 "moneyflow_ths": ("个股资金流", "signals/moneyflow_ths/{date}.csv"),
 "ths_hot": ("同花顺热榜", "signals/ths_hot/{date}.csv"),
 "dc_hot": ("东方财富热榜", "signals/dc_hot/{date}.csv"),
 "limit_cpt": ("最强板块", "market/limit_cpt/{date}.csv"),
 "daily_basic": ("每日基本指标", "market/daily_basic/{date}.csv"),
 "top_list": ("龙虎榜", "top_list/top_list_{date}.csv"),
 "top_inst": ("机构交易", "top_list/top_inst_{date}_all.csv"),
 "sector_moneyflow_ths": ("板块资金流", "signals/sector_moneyflow_ths/{date}.csv"),
 "moneyflow_dc": ("东财个股资金流", "signals/moneyflow_dc/{date}.csv"),
 "kpl_list": ("开盘啦榜单", "signals/kpl_list/{date}.csv"),
 "margin_detail": ("融资融券", "signals/margin_detail/{date}.csv"),
 "block_trade": ("大宗交易", "signals/block_trade/{date}.csv"),
}
LABELS = dict(trade_date="交易日", code="代码", ts_code="证券代码", name="名称",
 sector_code="板块代码", sector_name="板块名称", sector_type="板块类型",
 index_code="指数代码", index_name="指数名称", open="开盘价", high="最高价",
 low="最低价", close="收盘价", pre_close="昨收价", pct_chg="涨跌幅（%）",
 pct_change="涨跌幅（%）", change="涨跌额", vol="成交量（源口径）",
 vol_hand="成交量（手）", amount="成交额（源口径）", amount_yuan="成交额（元）",
 source="来源", as_of_date="数据基准日", ingested_at="入库时间",
 limit_times="连板数", first_time="首次封板时间", last_time="最后封板时间",
 open_times="开板次数", fd_amount="封单金额（元）", float_mv="流通市值",
 total_mv="总市值", turnover_ratio="换手率", exchange="交易所", rank="排名",
 reason="原因", net_amount="净额", seat_name="营业部", actor_name="游资名称")


def catalog():
    return [{"id": k, "label": v, "layer": "silver"} for k,v in TABLES.items()] + [
        {"id": k, "label": v[0], "layer": "raw"} for k,v in RAW.items()]


def ident(value):
    return '"' + value.replace('"', '""') + '"'


def query(*, db_path, cache_dir, layer, dataset, start, end, code="", name="", source="",
          page=1, page_size=50, sort="", descending=False):
    from core.utils.field_names import ColumnSchema
    labels = {**ColumnSchema.INTERNAL_TO_ZH, **LABELS}
    for date in (start,end):
        datetime.strptime(date, "%Y%m%d")
        if len(date) != 8 or not date.isdigit():
            raise ValueError("日期格式应为YYYYMMDD")
    if start > end or not 1 <= page_size <= 200 or not 1 <= page <= 10000:
        raise ValueError("日期区间或分页参数无效")
    if any(len(x) > 100 for x in (code,name,source)):
        raise ValueError("查询条件过长")
    if layer not in {"raw", "silver"} or dataset not in (TABLES if layer == "silver" else RAW):
        raise ValueError("不支持的数据集")
    if layer == "raw" and start != end:
        raise ValueError("原始缓存请按单个交易日查询")
    if layer == "silver" and not Path(db_path).is_file():
        return {"columns": [], "rows": [], "total": 0, "page": page, "page_size": page_size, "message": "Silver数据库尚未生成"}
    con = duckdb.connect(str(db_path), read_only=True) if layer == "silver" else duckdb.connect()
    try:
        con.execute("SET threads=1")
        con.execute("SET memory_limit='128MB'")
        if layer == "raw":
            path = Path(cache_dir) / RAW[dataset][1].format(date=start)
            if not path.is_file():
                return {"columns": [], "rows": [], "total": 0, "page": page, "page_size": page_size, "message": "该日原始缓存不存在"}
            con.read_csv(str(path), all_varchar=True).create_view("data_query")
            table = "data_query"
        else:
            table = dataset
            if not con.execute("SELECT count(*) FROM information_schema.tables WHERE table_name=?", [table]).fetchone()[0]:
                return {"columns": [], "rows": [], "total": 0, "page": page, "page_size": page_size, "message": "该标准表尚未生成"}
        columns = [r[0] for r in con.execute(f"DESCRIBE {ident(table)}").fetchall()]
        conditions, args = [], []
        if "trade_date" in columns:
            conditions.append("replace(substr(CAST(trade_date AS VARCHAR),1,10),'-','') BETWEEN ? AND ?")
            args += [start,end]
        for value, keys in ((code,("code","ts_code","代码","股票代码","sector_code","index_code")),
                            (name,("name","名称","股票名称","sector_name","index_name")),
                            (source,("source",))):
            if not value:
                continue
            found = [k for k in keys if k in columns]
            if not found:
                return {"columns": [{"key":k,"label":labels.get(k,k)} for k in columns], "rows":[], "total":0, "page":page,"page_size":page_size,"message":"该数据集不包含所选筛选字段"}
            conditions.append("(" + " OR ".join(f"contains(CAST({ident(k)} AS VARCHAR), ?)" for k in found) + ")")
            args += [value] * len(found)
        where = " WHERE " + " AND ".join(conditions) if conditions else ""
        if sort and sort not in columns:
            raise ValueError("排序字段无效")
        order = [sort] if sort else []
        order += [k for k in columns if k not in order]
        ordering = ",".join(ident(k) + (" DESC NULLS LAST" if descending else " ASC NULLS LAST") for k in order)
        total = con.execute(f"SELECT count(*) FROM {ident(table)}{where}", args).fetchone()[0]
        rows = con.execute(f"SELECT * FROM {ident(table)}{where} ORDER BY {ordering} LIMIT ? OFFSET ?", args+[page_size,(page-1)*page_size]).fetchall()
        def safe(v):
            if isinstance(v,float) and not math.isfinite(v): return None
            if v is None or isinstance(v,(str,int,float,bool)): return v
            return str(v)
        return {"columns":[{"key":k,"label":labels.get(k,k)} for k in columns],
                "rows":[{k:safe(v) for k,v in zip(columns,row)} for row in rows],
                "total":total,"page":page,"page_size":page_size,"message":""}
    finally:
        con.close()
