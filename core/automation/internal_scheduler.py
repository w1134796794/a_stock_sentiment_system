"""Built-in trading-day scheduler; no cron or systemd timer is required."""
from __future__ import annotations

import gc
import os
import threading
from datetime import datetime
from typing import Any, Dict, Optional

import schedule
from loguru import logger

from backtest.trade_calendar import TradeCalendar
from core.infrastructure.shared_state import TaskLease, TaskStateStore


def _env_bool(name: str, default: bool = True) -> bool:
    raw = os.getenv(name)
    if raw is None:
        return default
    return raw.strip().lower() not in {"0", "false", "no", "off"}


class InternalScheduler:
    def __init__(self) -> None:
        self.enabled = _env_bool("AUTOMATION_ENABLED", True)
        self.daily_time = os.getenv("AUTOMATION_DAILY_TIME", "18:30").strip() or "18:30"
        self.auction_time = os.getenv("AUTOMATION_AUCTION_TIME", "09:25:10").strip() or "09:25:10"
        self.auto_backtest = _env_bool("AUTOMATION_AUTO_BACKTEST", True)
        self.capital = float(os.getenv("AUTOMATION_CAPITAL", "100000") or 100000)
        self.scheduler = schedule.Scheduler()
        self.calendar = TradeCalendar()
        self.state = TaskStateStore("automation")
        self._stop = threading.Event()
        self._thread: Optional[threading.Thread] = None
        self._lock = threading.RLock()

    def start(self) -> None:
        with self._lock:
            if not self.enabled:
                logger.info("[Automation] 内置定时任务已禁用")
                return
            if self._thread and self._thread.is_alive():
                return
            self.scheduler.clear()
            self.scheduler.every().day.at(self.auction_time).do(self._auction_job).tag("auction")
            self.scheduler.every().day.at(self.daily_time).do(self._daily_job).tag("daily")
            self._stop.clear()
            self._thread = threading.Thread(target=self._loop, daemon=True, name="internal-automation")
            self._thread.start()
            if not self.state.load():
                self.state.save({"status": "idle", "message": "内置定时任务运行中"})
            logger.info(f"[Automation] 已启动：竞价 {self.auction_time}，每日流水线 {self.daily_time}")

    def stop(self) -> None:
        self._stop.set()
        thread = self._thread
        if thread and thread.is_alive():
            thread.join(timeout=3.0)
        self._thread = None

    def status(self) -> Dict[str, Any]:
        jobs = [
            {
                "tags": sorted(job.tags),
                "next_run": job.next_run.isoformat(timespec="seconds") if job.next_run else None,
            }
            for job in self.scheduler.jobs
        ]
        return {
            "ok": True,
            "enabled": self.enabled,
            "running": bool(self._thread and self._thread.is_alive()),
            "daily_time": self.daily_time,
            "auction_time": self.auction_time,
            "auto_backtest": self.auto_backtest,
            "capital": self.capital,
            "jobs": jobs,
            "latest": self.state.load(),
        }

    def _loop(self) -> None:
        while not self._stop.wait(1.0):
            try:
                self.scheduler.run_pending()
            except Exception as exc:  # noqa: BLE001
                logger.exception(f"[Automation] 调度循环异常: {exc}")

    def _daily_job(self) -> None:
        trade_date = datetime.now().strftime("%Y%m%d")
        if not self.calendar.is_trade_date(trade_date):
            self.state.save({"status": "skipped", "job": "daily", "trade_date": trade_date, "message": "非交易日，已跳过"})
            return
        lease = TaskLease.acquire(f"automatic-daily-{trade_date}", 6 * 60 * 60)
        if lease is None:
            logger.info(f"[Automation] {trade_date} 每日任务已由其他进程执行")
            return
        self.state.save({"status": "running", "job": "daily", "trade_date": trade_date, "started_at": datetime.now().isoformat(timespec="seconds")})
        try:
            from main import SentimentSystem
            from core.notifications.notifier import NotificationService
            from core.reports.daily_journal import DailyJournalService

            system = SentimentSystem()
            fetched = system.fetch_post_close_data(trade_date, skip_existing=True)
            factors = system.run_factor_calculation(trade_date)
            result = system.run_screening_strategy(trade_date)
            journal = DailyJournalService().generate(trade_date, capital=self.capital)
            backtest: Dict[str, Any] = {"ok": False, "reason": "自动回测已关闭"}
            if self.auto_backtest:
                gc.collect()
                from core.reports.auto_backtest import AutoBacktestReportService

                backtest = AutoBacktestReportService().run(trade_date, capital=self.capital)
            payload = {
                "status": "done",
                "job": "daily",
                "trade_date": trade_date,
                "finished_at": datetime.now().isoformat(timespec="seconds"),
                "pipeline_ok": bool(fetched.ok and factors.ok and result.ok),
                "stages": {
                    "fetch": bool(fetched.ok),
                    "factors": bool(factors.ok),
                    "screening": bool(result.ok),
                },
                "journal": journal,
                "backtest": backtest,
            }
            self.state.save(payload)
            NotificationService().send(
                "每日复盘数据已生成",
                f"{trade_date} 五阶段流水线完成；候选{journal.get('candidate_count', 0)}只。"
                f"近3个月回测胜率{backtest.get('win_rate_pct', '--')}%，最大回撤{backtest.get('max_drawdown_pct', '--')}%。",
                event_key=f"daily-complete:{trade_date}",
            )
        except Exception as exc:  # noqa: BLE001
            logger.exception(f"[Automation] {trade_date} 每日任务失败: {exc}")
            self.state.save({
                "status": "error", "job": "daily", "trade_date": trade_date,
                "message": str(exc), "finished_at": datetime.now().isoformat(timespec="seconds"),
            })
            try:
                from core.notifications.notifier import NotificationService

                NotificationService().send("每日数据生成失败", f"{trade_date}：{exc}", event_key=f"daily-error:{trade_date}")
            except Exception:
                pass
        finally:
            lease.release()
            gc.collect()

    def _auction_job(self) -> None:
        market_date = datetime.now().strftime("%Y%m%d")
        if not self.calendar.is_trade_date(market_date):
            return
        lease = TaskLease.acquire(f"auction-alert-{market_date}", 30 * 60)
        if lease is None:
            return
        try:
            from config.settings import CACHE_DIR, TUSHARE_TOKEN
            from core.data.data_manager_main import DataManager
            from core.notifications.notifier import NotificationService
            from core.realtime.auction_alert_service import AuctionAlertService

            candidate_date = self.calendar.prev(market_date)
            payload = AuctionAlertService(DataManager(TUSHARE_TOKEN, CACHE_DIR)).build(candidate_date, market_date)
            groups: Dict[str, int] = {}
            for row in payload.get("rows") or []:
                name = str(row.get("category") or "数据不足")
                groups[name] = groups.get(name, 0) + 1
            summary = "；".join(f"{name}{count}只" for name, count in groups.items()) or "没有可用候选"
            NotificationService().send(
                "09:25 竞价预警",
                f"观察{candidate_date}候选：{summary}。请在09:30后等待分钟条件确认。",
                event_key=f"auction:{market_date}", ttl_seconds=60 * 60 * 10,
            )
            self.state.save({
                "status": "done", "job": "auction", "market_date": market_date,
                "candidate_date": candidate_date, "summary": groups,
                "finished_at": datetime.now().isoformat(timespec="seconds"),
            })
        except Exception as exc:  # noqa: BLE001
            logger.exception(f"[Automation] {market_date} 竞价预警失败: {exc}")
            self.state.save({"status": "error", "job": "auction", "market_date": market_date, "message": str(exc)})
        finally:
            lease.release()


AUTOMATION_SCHEDULER = InternalScheduler()

__all__ = ["AUTOMATION_SCHEDULER", "InternalScheduler"]
