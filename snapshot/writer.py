"""
SnapshotWriter（P0）

把报表层的 ``data_dict`` 规整成前端友好的快照结构（``build_snapshot``），并持久化到：
- ``snapshots/{date}.json``
- ``app.sqlite``（daily_snapshot / trade_plans / signals）
- ``factors.duckdb``（可选）

调用方（``main._generate_reports``）应当用 try/except 包裹，确保快照失败不影响 Excel 产出。
"""
from __future__ import annotations

import json
import sqlite3
from datetime import datetime
from pathlib import Path
from typing import Any, Dict, List, Optional

import loguru

from snapshot.serialize import to_jsonable, tabulate
from snapshot.section_format import SECTION_FORMATTERS

logger = loguru.logger

SCHEMA_VERSION = 1


# ======================================================================
# 1. 规整：data_dict -> 前端快照
# ======================================================================
def _safe(fn, default=None):
    try:
        return fn()
    except Exception:  # noqa: BLE001
        return default


def _extract_market(data_dict: Dict) -> Dict[str, Any]:
    emotion = data_dict.get("emotion_result", {}) or {}
    strategy = emotion.get("strategy", {}) if isinstance(emotion, dict) else {}

    def _from_strategy(key):
        if isinstance(strategy, dict):
            return strategy.get(key)
        return getattr(strategy, key, None)

    return {
        "cycle_name": emotion.get("cycle_name") if isinstance(emotion, dict) else None,
        "position": to_jsonable(_from_strategy("position")),
        "strategy": to_jsonable(_from_strategy("strategy")),
        "scores": to_jsonable(emotion.get("scores") if isinstance(emotion, dict) else None),
        "metrics": to_jsonable(emotion.get("metrics") if isinstance(emotion, dict) else None),
        "env": to_jsonable(data_dict.get("market_env", {})),
        "phase": to_jsonable(data_dict.get("emotion_phase")),
        "similar_days": to_jsonable(data_dict.get("similar_days")),
    }


def _extract_trade_plans(data_dict: Dict) -> Dict[str, Any]:
    columns, rows = tabulate(data_dict.get("trade_plans_df"))
    return {"columns": columns, "rows": rows}


def _extract_risk_gate(data_dict: Dict) -> Optional[Dict[str, Any]]:
    rg = data_dict.get("risk_gate_result")
    if rg is None:
        return None
    j = to_jsonable(rg)
    if not isinstance(j, dict):
        return {"raw": j}

    decisions = j.get("decisions") or []
    for d in decisions:
        if isinstance(d, dict) and "reason_text" not in d:
            reasons = d.get("reasons") or []
            d["reason_text"] = "; ".join(str(r) for r in reasons) if reasons else "通过"

    def _count(action: str) -> int:
        return sum(1 for d in decisions if isinstance(d, dict) and d.get("action") == action)

    j["passed"] = _count("PASS")
    j["downgraded"] = _count("DOWNGRADE")
    j["rejected"] = _count("REJECT")
    return j


def _extract_patterns(data_dict: Dict) -> Dict[str, Any]:
    patterns = data_dict.get("patterns", {}) or {}
    out: Dict[str, Any] = {}
    if isinstance(patterns, dict):
        for name, signals in patterns.items():
            columns, rows = tabulate(signals)
            out[str(name)] = {"columns": columns, "rows": rows, "count": len(rows)}
    return out


# Excel sheet 顺序 -> data_dict 取值器（用于通用 sheet 浏览）。
# 形如 (展示名, 取值函数)；取值函数失败时该 section 跳过。
_SECTION_SPEC = [
    ("热点概念", lambda d: d.get("hot_concepts_df")),
    ("热点行业", lambda d: d.get("hot_industries_df")),
    ("概念持续性", lambda d: d.get("concept_persistence_df")),
    ("行业持续性", lambda d: d.get("industry_persistence_df")),
    ("主线主题", lambda d: d.get("mainline_df")),
    ("涨停梯队", lambda d: d.get("hierarchy_df")),
    ("概念连板梯队", lambda d: d.get("concept_hierarchy")),
    ("龙头池", lambda d: d.get("dragon_pool")),
    ("走弱池", lambda d: d.get("weakening_pool")),
    ("龙虎榜", lambda d: d.get("lhb_result")),
    ("资金流向", lambda d: d.get("moneyflow_analysis")),
    ("筹码结构", lambda d: d.get("chip_analysis")),
    ("复盘总结", lambda d: d.get("review_result")),
    ("周期模式胜率", lambda d: d.get("cycle_pattern_matrix")),
]


def _build_factor_table(data_dict: Dict) -> List[Dict[str, Any]]:
    """合并 per-stock 技术因子 + 资金流因子，一行一只股票。"""
    tech = data_dict.get("stock_tech_factors", {}) or {}
    mf = data_dict.get("moneyflow_factors", {}) or {}
    if not isinstance(tech, dict):
        tech = {}
    if not isinstance(mf, dict):
        mf = {}

    codes = list(dict.fromkeys(list(tech.keys()) + list(mf.keys())))
    rows: List[Dict[str, Any]] = []
    for code in codes:
        row: Dict[str, Any] = {"stock_code": str(code)}
        t = to_jsonable(tech.get(code, {}))
        m = to_jsonable(mf.get(code, {}))
        if isinstance(t, dict):
            for k, v in t.items():
                row[f"tech_{k}"] = v
        if isinstance(m, dict):
            for k, v in m.items():
                row[f"mf_{k}"] = v
        rows.append(row)
    return rows


def build_snapshot(data_dict: Dict) -> Dict[str, Any]:
    """把报表 ``data_dict`` 规整成前端快照（纯 JSON 可序列化）。"""
    date = str(data_dict.get("date") or datetime.now().strftime("%Y%m%d"))

    market = _safe(lambda: _extract_market(data_dict), {})
    trade_plans = _safe(lambda: _extract_trade_plans(data_dict), {"columns": [], "rows": []})
    risk_gate = _safe(lambda: _extract_risk_gate(data_dict), None)
    patterns = _safe(lambda: _extract_patterns(data_dict), {})

    # 通用浏览 sections（含 4 大模式 + 交易计划 + 因子）
    sections: List[Dict[str, Any]] = []

    # 4 大模式优先排前
    for name, blk in patterns.items():
        sections.append({
            "name": name, "kind": "signals",
            "columns": blk.get("columns", []), "rows": blk.get("rows", []),
        })

    for name, resolver in _SECTION_SPEC:
        src = _safe(lambda: resolver(data_dict))
        if src is None:
            continue
        # 富格式化 section（梯队/龙虎榜/资金流向/复盘/周期矩阵等）：把结构化数据
        # 规整成干净表格，替代把整段嵌套 JSON 塞进单格的 tabulate。
        formatter = SECTION_FORMATTERS.get(name)
        if formatter is not None:
            section = _safe(lambda: formatter(to_jsonable(src)))
            if section and section.get("rows"):
                sections.append(section)
            continue
        columns, rows = _safe(lambda: tabulate(src), ([], []))
        if not rows:
            continue
        sections.append({"name": name, "kind": "table", "columns": columns, "rows": rows})

    # 交易计划 / 风控闸门也加入浏览
    if trade_plans.get("rows"):
        sections.append({
            "name": "交易计划", "kind": "table",
            "columns": trade_plans["columns"], "rows": trade_plans["rows"],
        })
    if risk_gate and risk_gate.get("decisions"):
        dcols = ["stock_code", "stock_name", "pattern_type",
                 "original_position_pct", "final_position_pct", "action", "reason_text"]
        sections.append({
            "name": "风控闸门", "kind": "table",
            "columns": dcols, "rows": risk_gate["decisions"],
        })

    # 因子原始数据
    factor_rows = _safe(lambda: _build_factor_table(data_dict), [])
    if factor_rows:
        cols: List[str] = []
        seen = set()
        for r in factor_rows:
            for k in r.keys():
                if k not in seen:
                    seen.add(k)
                    cols.append(k)
        sections.append({"name": "因子原始数据", "kind": "table", "columns": cols, "rows": factor_rows})

    snapshot = {
        "meta": {
            "date": date,
            "generated_at": datetime.now().isoformat(timespec="seconds"),
            "schema_version": SCHEMA_VERSION,
        },
        "market": market,
        "trade_plans": trade_plans,
        "risk_gate": risk_gate,
        "patterns": patterns,
        "sections": sections,
    }
    return snapshot


# ======================================================================
# 2. 持久化
# ======================================================================
class SnapshotWriter:
    def __init__(self,
                 snapshot_dir: Path,
                 app_db_path: Path,
                 factor_db_path: Optional[Path] = None):
        self.snapshot_dir = Path(snapshot_dir)
        self.app_db_path = Path(app_db_path)
        self.factor_db_path = Path(factor_db_path) if factor_db_path else None
        self.snapshot_dir.mkdir(parents=True, exist_ok=True)
        self.app_db_path.parent.mkdir(parents=True, exist_ok=True)

    # -- public ---------------------------------------------------------
    def write(self, data_dict: Dict) -> Dict[str, str]:
        """规整 + 落盘。返回各产物路径；单项失败不影响其它。"""
        snapshot = build_snapshot(data_dict)
        date = snapshot["meta"]["date"]
        paths: Dict[str, str] = {}

        json_path = self._write_json(snapshot, date)
        if json_path:
            paths["json"] = str(json_path)

        try:
            self._write_sqlite(snapshot, date)
            paths["sqlite"] = str(self.app_db_path)
        except Exception as e:  # noqa: BLE001
            logger.warning(f"[Snapshot] 写 SQLite 失败（已跳过）: {e}")

        if self.factor_db_path is not None:
            try:
                wrote = self._write_duckdb(data_dict, date)
                if wrote:
                    paths["duckdb"] = str(self.factor_db_path)
            except Exception as e:  # noqa: BLE001
                logger.warning(f"[Snapshot] 写 DuckDB 失败（已跳过）: {e}")

        logger.info(f"[Snapshot] {date} 快照已落盘: {', '.join(paths) or '无'}")
        return paths

    # -- json -----------------------------------------------------------
    def _write_json(self, snapshot: Dict, date: str) -> Optional[Path]:
        path = self.snapshot_dir / f"{date}.json"
        try:
            text = json.dumps(snapshot, ensure_ascii=False, indent=2)
        except Exception as e:  # noqa: BLE001
            logger.warning(f"[Snapshot] JSON 序列化失败，回退 default=str: {e}")
            text = json.dumps(snapshot, ensure_ascii=False, indent=2, default=str)
        try:
            path.write_text(text, encoding="utf-8")
            # 同步维护一个 latest 指针，便于前端默认进入最新一天
            (self.snapshot_dir / "latest.txt").write_text(date, encoding="utf-8")
            return path
        except Exception as e:  # noqa: BLE001
            logger.warning(f"[Snapshot] 写 JSON 失败: {e}")
            return None

    # -- sqlite ---------------------------------------------------------
    def _write_sqlite(self, snapshot: Dict, date: str) -> None:
        conn = sqlite3.connect(self.app_db_path)
        try:
            self._ensure_sqlite_schema(conn)
            market = snapshot.get("market", {}) or {}
            plans = snapshot.get("trade_plans", {}) or {}
            plan_rows = plans.get("rows", []) or []

            payload = json.dumps(snapshot, ensure_ascii=False, default=str)
            conn.execute(
                """INSERT INTO daily_snapshot(date, generated_at, cycle_name, position, plan_count, payload_json)
                   VALUES(?,?,?,?,?,?)
                   ON CONFLICT(date) DO UPDATE SET
                     generated_at=excluded.generated_at,
                     cycle_name=excluded.cycle_name,
                     position=excluded.position,
                     plan_count=excluded.plan_count,
                     payload_json=excluded.payload_json""",
                (date, snapshot["meta"]["generated_at"], market.get("cycle_name"),
                 market.get("position"), len(plan_rows), payload),
            )

            conn.execute("DELETE FROM trade_plans WHERE date=?", (date,))
            for r in plan_rows:
                conn.execute(
                    """INSERT OR REPLACE INTO trade_plans(
                        date, stock_code, stock_name, pattern_type, priority, score,
                        position_level, suggested_position, position_basis, entry_range,
                        stop_loss, take_profit, next_day_expectation, risk_hint,
                        gate_action, gate_position, gate_hint)
                       VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)""",
                    (
                        date,
                        _s(r.get("股票代码")), _s(r.get("股票名称")), _s(r.get("模式类型")),
                        _s(r.get("优先级")), _f(r.get("综合评分")),
                        _s(r.get("仓位等级")), _s(r.get("建议仓位")), _s(r.get("仓位依据")),
                        _s(r.get("入场区间")), _s(r.get("止损")), _s(r.get("止盈")),
                        _s(r.get("次日预期")), _s(r.get("风险提示")),
                        _s(r.get("风控动作")), _s(r.get("风控后仓位")), _s(r.get("风控提示")),
                    ),
                )

            conn.execute("DELETE FROM signals WHERE date=?", (date,))
            for name, blk in (snapshot.get("patterns", {}) or {}).items():
                for r in blk.get("rows", []) or []:
                    conn.execute(
                        """INSERT INTO signals(date, pattern_type, stock_code, stock_name, confidence, description)
                           VALUES(?,?,?,?,?,?)""",
                        (date, str(name), _s(r.get("stock_code")), _s(r.get("stock_name")),
                         _f(r.get("confidence")), _s(r.get("description"))),
                    )
            conn.commit()
        finally:
            conn.close()

    @staticmethod
    def _ensure_sqlite_schema(conn: sqlite3.Connection) -> None:
        conn.executescript(
            """
            CREATE TABLE IF NOT EXISTS daily_snapshot(
                date TEXT PRIMARY KEY,
                generated_at TEXT,
                cycle_name TEXT,
                position TEXT,
                plan_count INTEGER,
                payload_json TEXT
            );
            CREATE TABLE IF NOT EXISTS trade_plans(
                date TEXT, stock_code TEXT, stock_name TEXT, pattern_type TEXT,
                priority TEXT, score REAL, position_level TEXT, suggested_position TEXT,
                position_basis TEXT, entry_range TEXT, stop_loss TEXT, take_profit TEXT,
                next_day_expectation TEXT, risk_hint TEXT,
                gate_action TEXT, gate_position TEXT, gate_hint TEXT,
                PRIMARY KEY(date, stock_code, pattern_type)
            );
            CREATE TABLE IF NOT EXISTS signals(
                date TEXT, pattern_type TEXT, stock_code TEXT, stock_name TEXT,
                confidence REAL, description TEXT
            );
            CREATE INDEX IF NOT EXISTS idx_signals_date ON signals(date);
            CREATE INDEX IF NOT EXISTS idx_plans_date ON trade_plans(date);
            """
        )

    # -- duckdb ---------------------------------------------------------
    def _write_duckdb(self, data_dict: Dict, date: str) -> bool:
        rows = _build_factor_table(data_dict)
        if not rows:
            return False
        try:
            import duckdb  # type: ignore
            import pandas as pd
        except Exception:  # pragma: no cover
            logger.debug("[Snapshot] 未安装 duckdb，跳过因子大表落盘")
            return False

        df = pd.DataFrame(rows)
        df.insert(0, "date", date)
        # 非标量列转成字符串，避免 DuckDB 类型推断失败
        for col in df.columns:
            df[col] = df[col].apply(lambda v: json.dumps(v, ensure_ascii=False)
                                    if isinstance(v, (dict, list)) else v)

        con = duckdb.connect(str(self.factor_db_path))
        try:
            con.register("df_new", df)
            exists = con.execute(
                "SELECT count(*) FROM information_schema.tables WHERE table_name='factors'"
            ).fetchone()[0]
            if not exists:
                con.execute("CREATE TABLE factors AS SELECT * FROM df_new WHERE 1=0")
            # 补齐新出现的列，保证 INSERT BY NAME 不报错
            # PRAGMA table_info 返回 (cid, name, type, notnull, dflt_value, pk)，列名在索引 1。
            existing_cols = {r[1] for r in con.execute("PRAGMA table_info('factors')").fetchall()}
            for col, dtype in zip(df.columns, df.dtypes):
                if col not in existing_cols:
                    sql_type = "DOUBLE" if str(dtype).startswith(("float", "int")) else "VARCHAR"
                    con.execute(f'ALTER TABLE factors ADD COLUMN "{col}" {sql_type}')
            con.execute("DELETE FROM factors WHERE date = ?", [date])
            con.execute("INSERT INTO factors BY NAME SELECT * FROM df_new")
            return True
        finally:
            con.unregister("df_new")
            con.close()


def _s(v: Any) -> Optional[str]:
    if v is None:
        return None
    return str(v)


def _f(v: Any) -> Optional[float]:
    try:
        if v is None or v == "" or v == "--":
            return None
        return float(v)
    except (TypeError, ValueError):
        return None
