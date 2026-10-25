"""Collection-only controls and storage-backed status for the operations UI."""
from __future__ import annotations

import subprocess
import sys
from datetime import datetime
from pathlib import Path
from threading import Thread

from core.infrastructure.shared_state import TaskLease, TaskStateStore, get_shared_state_backend
from core.realtime.trading_session import shanghai_now


def date_key(value):
    text = str(value or "").replace("-", "")
    datetime.strptime(text, "%Y%m%d")
    if len(text) != 8:
        raise ValueError("日期须为 YYYYMMDD")
    return text


def _health(value, now, max_age=60):
    result = dict(value or {})
    stamp = result.get("updated_at") or result.get("heartbeat_at")
    try:
        age = (now - shanghai_now(datetime.fromisoformat(stamp))).total_seconds()
    except (TypeError, ValueError):
        age = None
    result.update(age_seconds=round(age, 1) if age is not None else None,
                  fresh=age is not None and 0 <= age <= max_age)
    return result


def collection_status(phase, date, *, root=None, db_path=None, backend=None, now=None):
    """No provider construction, data repair, strategy evaluation or publication."""
    from backtest.trade_calendar import TradeCalendar
    from config.settings import FACTOR_DB_PATH, WEB_DATA_DIR

    date = date_key(date)
    root = Path(root or WEB_DATA_DIR)
    now = shanghai_now(now)
    backend = backend or get_shared_state_backend()
    calendar = TradeCalendar()
    result = {"phase": phase, "date": date, "as_of": now.isoformat(),
              "calendar_verified": calendar.is_real,
              "trading_day": calendar.is_real and calendar.is_trade_date(date),
              "notification_sent": False, "strategy_executed": False}
    if phase == "postclose":
        from core.etl.stage_status import fetch_status
        from desktop.runner import FETCH_CONTROLLER
        result.update(data=fetch_status(date, db_path=db_path or FACTOR_DB_PATH, web_data_dir=root),
                      task=FETCH_CONTROLLER.status(0))
        scheduled = TaskStateStore(f"automation_daily_{date}").load()
        manual = result["task"]
        if (scheduled and (manual.get("date") != date or
                str(scheduled.get("started_at") or "").replace("T", " ") > str(manual.get("started_at") or ""))):
            result["task"] = {**scheduled, "state": scheduled.get("status"), "date": date,
                              "source": "scheduled", "error": scheduled.get("message")}
    elif phase in {"preopen", "intraday"}:
        from core.realtime.collection_plan import DailyCollectionPlan
        plan = DailyCollectionPlan(root=root, backend=backend).load(date) or {}
        result.update(plan=plan, task=TaskStateStore("automation_preopen").load())
        from core.realtime.board_plan import BoardCollectionPlan
        result["boards"] = BoardCollectionPlan(root=root).load(date, parent_id=plan.get("plan_id")) or {}
        result["missing"] = [] if plan else ["当日冻结采集名单缺失"]
        if phase == "intraday":
            from config.settings import ELTDX_MINUTE_SYNC_SECONDS, ELTDX_POLL_INTERVAL_SECONDS
            shared = bool(backend.is_shared)
            collector = _health(backend.get_json("realtime:collector:health"), now)
            archive = _health(backend.get_json("realtime:archive:health"), now)
            result.update(storage="redis" if shared else "memory", collector=collector, archive=archive,
                          quotes=backend.get_json("realtime:quotes:meta") or {},
                          minute_sync=_health(backend.get_json("realtime:minute-sync:health"), now, 120))
            result["intervals"] = {"stocks": ELTDX_POLL_INTERVAL_SECONDS, "minutes": ELTDX_MINUTE_SYNC_SECONDS}
            if not shared:
                result["missing"].append("Redis 共享行情存储不可用")
            if not collector["fresh"]:
                result["missing"].append("未读取到新鲜的独立采集器心跳")
            elif not collector.get("lease_owned"):
                result["missing"].append("采集器未持有有效租约")
            elif collector.get("ok") is False:
                result["missing"].append(collector.get("message") or "最新采集批次失败")
            if not archive["fresh"] or not archive.get("ok"):
                result["missing"].append("增量归档未就绪")
            # Heartbeats and live quotes are not historical evidence for another day.
            result["live_date_matches"] = date == now.strftime("%Y%m%d")
    else:
        raise ValueError("未知采集阶段")
    return result


def start_preopen(date):
    from backtest.trade_calendar import TradeCalendar
    from config.settings import BASE_DIR, WEB_DATA_DIR
    date = date_key(date)
    now = shanghai_now()
    calendar = TradeCalendar()
    if not calendar.is_real or not calendar.is_trade_date(date):
        raise ValueError("缺少真实交易日历或所选日期不是交易日")
    if date < now.strftime("%Y%m%d") or (date == now.strftime("%Y%m%d") and now.strftime("%H:%M") >= "09:25"):
        raise ValueError("已过盘前截止时间，不能重建历史盘前证据")
    lease = TaskLease.acquire(f"preopen-collection-{date}", 60)
    if lease is None:
        raise ValueError("该交易日的盘前采集已在运行")
    store = TaskStateStore("automation_preopen")
    path = Path(WEB_DATA_DIR) / "logs" / f"preopen_collection_{date}.log"
    state = {"job": "preopen", "market_date": date, "status": "running",
             "started_at": now.isoformat(), "log_path": str(path)}
    store.save(state)

    def worker():
        try:
            path.parent.mkdir(parents=True, exist_ok=True)
            with path.open("a", encoding="utf-8") as output:
                process = subprocess.Popen([sys.executable, "-X", "utf8", str(Path(BASE_DIR) / "scripts" /
                    "prepare_realtime_collection.py"), "--market-date", date], cwd=BASE_DIR,
                    stdout=output, stderr=subprocess.STDOUT,
                    creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0))
                try:
                    cutoff = shanghai_now(datetime.strptime(date + "092500", "%Y%m%d%H%M%S"))
                    code = process.wait(timeout=max(1, min(600, (cutoff - shanghai_now()).total_seconds())))
                except subprocess.TimeoutExpired:
                    process.terminate()
                    try:
                        process.wait(timeout=5)
                    except subprocess.TimeoutExpired:
                        process.kill()
                        process.wait()
                    raise RuntimeError("盘前采集超时，查看日志") from None
            store.save({**state, "status": "done" if code == 0 else "failed", "exit_code": code,
                        "finished_at": shanghai_now().isoformat(),
                        "reason": "" if code == 0 else "盘前准备失败，请展开日志"})
        except Exception as exc:
            store.save({**state, "status": "failed", "reason": str(exc)})
        finally:
            lease.release()

    try:
        Thread(target=worker, daemon=True, name="preopen-collection-control").start()
    except Exception:
        lease.release()
        raise
    return state


def preopen_log(date, phase="preopen"):
    from config.settings import BASE_DIR, WEB_DATA_DIR
    date = date_key(date)
    if phase not in {"preopen", "postclose"}:
        raise ValueError("未知采集日志类型")
    path = (Path(WEB_DATA_DIR) / "logs" / f"preopen_collection_{date}.log" if phase == "preopen" else
            Path(BASE_DIR) / "logs" / f"automation_postclose_{date}.log")
    if not path.is_file():
        return ""
    with path.open("rb") as stream:
        stream.seek(max(0, path.stat().st_size - 24000))
        return stream.read().decode("utf-8", errors="replace")
