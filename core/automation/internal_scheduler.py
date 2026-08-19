"""Built-in trading-day scheduler; no cron or systemd timer is required."""
from __future__ import annotations

import json
import os
import signal
import subprocess
import sys
import threading
import time
from datetime import datetime
from pathlib import Path
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
        self._job_threads: Dict[str, threading.Thread] = {}
        self._last_heartbeat_at = ""
        self._catchup_checked = False
        self.catch_up_enabled = _env_bool("AUTOMATION_DAILY_CATCH_UP", True)
        self.max_daily_attempts = max(int(os.getenv("AUTOMATION_DAILY_MAX_ATTEMPTS", "3") or 3), 1)
        self.daily_timeout = max(int(os.getenv("AUTOMATION_DAILY_TIMEOUT", "21600") or 21600), 600)
        self.worker_memory_limit_mb = max(int(os.getenv("AUTOMATION_WORKER_MEMORY_MB", "2400") or 2400), 512)

    def start(self) -> None:
        with self._lock:
            if not self.enabled:
                logger.info("[Automation] 内置定时任务已禁用")
                return
            if self._thread and self._thread.is_alive():
                return
            self.scheduler.clear()
            self.scheduler.every().day.at(self.auction_time).do(
                self._dispatch_job, "auction", self._auction_job,
            ).tag("auction")
            self.scheduler.every().day.at(self.daily_time).do(
                self._dispatch_job, "daily", self._daily_job,
            ).tag("daily")
            self.scheduler.every(10).minutes.do(self._recover_due_daily_job).tag("daily")
            self._stop.clear()
            self._catchup_checked = False
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
            "catch_up_enabled": self.catch_up_enabled,
            "max_daily_attempts": self.max_daily_attempts,
            "scheduler_heartbeat_at": self._last_heartbeat_at,
            "active_jobs": sorted(
                name for name, thread in self._job_threads.items() if thread.is_alive()
            ),
            "jobs": jobs,
            "latest": self.state.load(),
        }

    def _loop(self) -> None:
        while not self._stop.wait(1.0):
            try:
                self._last_heartbeat_at = datetime.now().isoformat(timespec="seconds")
                self.scheduler.run_pending()
                if not self._catchup_checked:
                    # Give application startup a few seconds to finish creating
                    # database connections before a missed daily job is resumed.
                    self._catchup_checked = True
                    if self._stop.wait(3.0):
                        break
                    self._recover_due_daily_job()
            except Exception as exc:  # noqa: BLE001
                logger.exception(f"[Automation] 调度循环异常: {exc}")

    def _dispatch_job(self, name: str, target) -> bool:
        with self._lock:
            current = self._job_threads.get(name)
            if current and current.is_alive():
                logger.info(f"[Automation] {name} 任务仍在运行，本次触发已跳过")
                return False

            def run() -> None:
                try:
                    target()
                finally:
                    with self._lock:
                        self._job_threads.pop(name, None)

            thread = threading.Thread(
                target=run, daemon=True, name=f"internal-automation-{name}",
            )
            self._job_threads[name] = thread
            thread.start()
            return True

    def _recover_due_daily_job(self) -> bool:
        if not self.catch_up_enabled or self._stop.is_set():
            return False
        now = datetime.now()
        try:
            due = datetime.strptime(
                f"{now:%Y-%m-%d} {self.daily_time}", "%Y-%m-%d %H:%M",
            )
        except ValueError:
            logger.error(f"[Automation] 每日任务时间格式无效: {self.daily_time}")
            return False
        trade_date = now.strftime("%Y%m%d")
        if now < due or not self.calendar.is_trade_date(trade_date):
            return False
        latest = self.state.load()
        completed = bool(
            str(latest.get("trade_date") or "") == trade_date
            and latest.get("status") == "done"
            and latest.get("pipeline_ok")
        )
        attempts = int(latest.get("attempt") or 0) if str(latest.get("trade_date") or "") == trade_date else 0
        if completed or attempts >= self.max_daily_attempts:
            return False
        logger.warning(
            f"[Automation] 检测到 {trade_date} 每日流水线未完成，"
            f"自动恢复第 {attempts + 1}/{self.max_daily_attempts} 次"
        )
        return self._dispatch_job("daily", self._daily_job)

    def _daily_job(self) -> None:
        trade_date = datetime.now().strftime("%Y%m%d")
        if not self.calendar.is_trade_date(trade_date):
            self.state.save({"status": "skipped", "job": "daily", "trade_date": trade_date, "message": "非交易日，已跳过"})
            return
        lease = TaskLease.acquire(f"automatic-daily-{trade_date}", 6 * 60 * 60)
        if lease is None:
            logger.info(f"[Automation] {trade_date} 每日任务已由其他进程执行")
            return
        previous = self.state.load()
        attempt = (
            int(previous.get("attempt") or 0) + 1
            if str(previous.get("trade_date") or "") == trade_date else 1
        )
        self.state.save({
            "status": "running", "job": "daily", "trade_date": trade_date,
            "attempt": attempt,
            "started_at": datetime.now().isoformat(timespec="seconds"),
        })
        try:
            from config.settings import BASE_DIR, WEB_DATA_DIR
            from core.notifications.notifier import NotificationService

            result_path = Path(WEB_DATA_DIR) / "automation" / f"daily_{trade_date}.json"
            result_path.unlink(missing_ok=True)
            command = [
                sys.executable,
                "-X",
                "faulthandler",
                str(Path(BASE_DIR) / "scripts" / "automation_daily_worker.py"),
                "--date", trade_date,
                "--capital", str(self.capital),
                "--result", str(result_path),
            ]
            if self.auto_backtest:
                command.append("--auto-backtest")
            log_path = Path(BASE_DIR) / "logs" / f"automation_{trade_date}.log"
            log_path.parent.mkdir(parents=True, exist_ok=True)
            with log_path.open("a", encoding="utf-8") as stream:
                process = subprocess.Popen(
                    command,
                    cwd=str(BASE_DIR),
                    stdout=stream,
                    stderr=subprocess.STDOUT,
                    env={**os.environ, "PYTHONFAULTHANDLER": "1"},
                    start_new_session=(os.name != "nt"),
                )
                exit_code, guard_error = self._wait_worker(process, result_path)
            payload = json.loads(result_path.read_text(encoding="utf-8")) if result_path.exists() else {
                "status": "error",
                "job": "daily",
                "trade_date": trade_date,
                "message": f"隔离任务退出码 {exit_code}，但未生成结果文件",
            }
            if guard_error:
                payload.update({"status": "error", "message": guard_error})
            payload["worker_exit_code"] = exit_code
            payload["worker_log"] = str(log_path)
            payload["attempt"] = attempt
            self.state.save(payload)
            if payload.get("status") != "done" or not payload.get("pipeline_ok"):
                message = str(payload.get("message") or self._exit_code_message(exit_code))
                logger.error(f"[Automation] {trade_date} 隔离任务失败: {message}; 日志: {log_path}")
                NotificationService().send(
                    "每日数据生成失败",
                    f"{trade_date}：{message}；日志：{log_path}",
                    event_key=f"daily-error:{trade_date}",
                )
                return
            journal = payload.get("journal") or {}
            backtest = payload.get("backtest") or {}
            if self.auto_backtest and not backtest.get("ok"):
                logger.warning(
                    f"[Automation] {trade_date} 主流水线成功，但自动回测失败: "
                    f"{backtest.get('reason', '未知原因')}"
                )
            backtest_text = (
                f"近3个月回测胜率{backtest.get('win_rate_pct', '--')}%，"
                f"最大回撤{backtest.get('max_drawdown_pct', '--')}%。"
                if backtest.get("ok")
                else f"自动回测未完成：{backtest.get('reason', '已跳过')}。"
            )
            NotificationService().send(
                "每日复盘数据已生成",
                f"{trade_date} 五阶段流水线完成；候选{journal.get('candidate_count', 0)}只。"
                f"{backtest_text}",
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

    def _wait_worker(self, process: subprocess.Popen, result_path: Path) -> tuple[int, str]:
        """Monitor timeout, RSS and heartbeat without loading worker data in Web."""
        started = time.monotonic()
        while process.poll() is None:
            if time.monotonic() - started > self.daily_timeout:
                self._terminate_worker(process)
                return -9, f"任务超过{self.daily_timeout}秒，已终止"
            rss_mb = self._worker_rss_mb(process.pid)
            if rss_mb > self.worker_memory_limit_mb:
                self._terminate_worker(process)
                return -9, f"任务内存{rss_mb:.0f}MB超过限制{self.worker_memory_limit_mb}MB，已终止并保留进度"
            if result_path.exists():
                try:
                    payload = json.loads(result_path.read_text(encoding="utf-8"))
                    self.state.save({**payload, "worker_rss_mb": round(rss_mb, 1)})
                except (OSError, json.JSONDecodeError):
                    pass
            time.sleep(5.0)
        return int(process.returncode or 0), ""

    @staticmethod
    def _exit_code_message(exit_code: int) -> str:
        if exit_code >= 0:
            return f"隔离任务退出码 {exit_code}"
        try:
            signal_name = signal.Signals(-exit_code).name
        except ValueError:
            signal_name = f"SIGNAL_{-exit_code}"
        detail = "，通常表示 DuckDB/Arrow 等原生扩展发生段错误" if signal_name == "SIGSEGV" else ""
        return f"隔离任务收到 {signal_name}（{-exit_code}）{detail}"

    @staticmethod
    def _worker_rss_mb(pid: int) -> float:
        try:
            import psutil  # type: ignore

            parent = psutil.Process(pid)
            rss = parent.memory_info().rss
            rss += sum(child.memory_info().rss for child in parent.children(recursive=True) if child.is_running())
            return rss / 1024.0 / 1024.0
        except Exception:
            return 0.0

    @staticmethod
    def _terminate_worker(process: subprocess.Popen) -> None:
        try:
            import psutil  # type: ignore

            parent = psutil.Process(process.pid)
            children = parent.children(recursive=True)
            processes = [*children, parent]
            for item in processes:
                try:
                    item.terminate()
                except (psutil.NoSuchProcess, psutil.AccessDenied):
                    continue
            _, alive = psutil.wait_procs(processes, timeout=5)
            for item in alive:
                try:
                    item.kill()
                except (psutil.NoSuchProcess, psutil.AccessDenied):
                    continue
        except Exception:
            if process.poll() is None:
                process.kill()

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
