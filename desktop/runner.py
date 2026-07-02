"""在管理工具进程内执行「数据生成」，并把日志实时缓冲给前端轮询。

设计要点：
  - 单例 ``CONTROLLER``：同一时刻只允许一个分析在跑。
  - 分析在后台线程里直接调用 ``main.SentimentSystem``（与命令行 ``python main.py``
    等价），因此打包成 exe 后无需再依赖外部 Python 解释器。
  - 同时捕获两路输出：loguru 日志 + 普通 ``print``，统一写入行缓冲。
    前端用 ``/api/run/status?since=N`` 增量拉取，避免 SSE 在 webview 里的缓冲问题。
  - 重依赖（pandas / tushare / akshare 等）只在点击「运行」时才惰性导入。
"""
from __future__ import annotations

import sys
import json
import time
import threading
import traceback
from datetime import datetime, timedelta
from pathlib import Path
from typing import Dict, List, Optional, Tuple

from core.infrastructure.shared_state import TaskLease, TaskStateStore


class LogBuffer:
    """线程安全的行缓冲：支持按行增量读取（前端轮询用）。"""

    def __init__(self, store: Optional[TaskStateStore] = None) -> None:
        self._lock = threading.Lock()
        self._lines: List[str] = []
        self._partial = ""  # 尚未遇到换行的残段（print 分片写入时用）
        self._store = store

    def clear(self) -> None:
        with self._lock:
            self._lines.clear()
            self._partial = ""
        if self._store and self._store.shared:
            self._store.clear_logs()

    def append_line(self, line: str) -> None:
        clean = line.rstrip("\n")
        with self._lock:
            self._lines.append(clean)
        if self._store and self._store.shared:
            self._store.append_log(clean)

    def append_text(self, text: str) -> None:
        """写入任意文本（可能不含/含多个换行），按换行切分成行。"""
        if not text:
            return
        with self._lock:
            buf = self._partial + text
            parts = buf.split("\n")
            self._partial = parts.pop()  # 最后一段可能是半行
            self._lines.extend(parts)
        if self._store and self._store.shared:
            for line in parts:
                self._store.append_log(line)

    def read_from(self, since: int) -> Tuple[List[str], int]:
        if self._store and self._store.shared:
            return self._store.read_logs(since)
        with self._lock:
            since = max(0, int(since or 0))
            return list(self._lines[since:]), len(self._lines)


class _StreamTee:
    """把 sys.stdout / sys.stderr 同时写到原流和日志缓冲。"""

    def __init__(self, original, buffer: LogBuffer) -> None:
        self._original = original
        self._buffer = buffer

    def write(self, text: str):
        try:
            if self._original is not None:
                self._original.write(text)
        except Exception:
            pass
        self._buffer.append_text(text)
        return len(text)

    def flush(self):
        try:
            if self._original is not None:
                self._original.flush()
        except Exception:
            pass


_FILE_SINK_READY = False


def _ensure_file_sink() -> None:
    """首次运行时挂上 logs/system.log 文件日志（进程级，只加一次，避免重复行）。"""
    global _FILE_SINK_READY
    if _FILE_SINK_READY:
        return
    try:
        import loguru

        from config.settings import BASE_DIR

        log_dir = Path(BASE_DIR) / "logs"
        log_dir.mkdir(parents=True, exist_ok=True)
        loguru.logger.add(
            log_dir / "system.log",
            rotation="1 day",
            retention="30 days",
            encoding="utf-8",
            level="DEBUG",
            enqueue=True,
        )
    except Exception:  # noqa: BLE001
        pass
    _FILE_SINK_READY = True


def _normalize_date(value: Optional[str]) -> Optional[str]:
    text = "".join(ch for ch in str(value or "") if ch.isdigit())
    if len(text) != 8:
        return None
    try:
        datetime.strptime(text, "%Y%m%d")
    except ValueError:
        return None
    return text


class RunController:
    """单例分析控制器。"""

    def __init__(self) -> None:
        self._lock = threading.Lock()
        self._store = TaskStateStore("data_generation")
        self.buffer = LogBuffer(self._store)
        self._lease: Optional[TaskLease] = None
        self.state: str = "idle"  # idle / running / done / partial / error
        self.mode: str = "single"
        self.date: Optional[str] = None
        self.start_date: Optional[str] = None
        self.end_date: Optional[str] = None
        self.total: int = 0
        self.completed: int = 0
        self.failed: List[str] = []
        self.started_at: Optional[str] = None
        self.finished_at: Optional[str] = None
        self.error: Optional[str] = None
        self._thread: Optional[threading.Thread] = None

    # ---- 对外 API -----------------------------------------------------
    def start(self, date: Optional[str]) -> Tuple[bool, str]:
        date = (date or "").strip() or None
        with self._lock:
            if self.state == "running":
                return False, "已有分析任务在运行中，请等待完成。"
            from config.settings import TASK_LOCK_TTL_SECONDS

            lease = TaskLease.acquire("data_generation", TASK_LOCK_TTL_SECONDS)
            if lease is None:
                return False, "已有其他 Web Worker 启动了数据生成任务，请等待完成。"
            self._lease = lease
            self.buffer.clear()
            self.state = "running"
            self.mode = "single"
            self.error = None
            self.date = date
            self.start_date = None
            self.end_date = None
            self.total = 1
            self.completed = 0
            self.failed = []
            self.started_at = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
            self.finished_at = None
            self._thread = threading.Thread(
                target=self._worker, args=(date,), daemon=True, name="analysis-run"
            )
            self._publish_state()
            self._thread.start()
        return True, "已启动分析任务。"

    def start_batch(self, start_date: Optional[str], end_date: Optional[str]) -> Tuple[bool, str]:
        start = _normalize_date(start_date)
        end = _normalize_date(end_date)
        if not start or not end:
            return False, "请填写完整的开始日期和结束日期，格式为 YYYYMMDD。"
        if end < start:
            return False, "结束日期不能早于开始日期。"

        with self._lock:
            if self.state == "running":
                return False, "已有分析任务在运行中，请等待完成。"
            from config.settings import TASK_LOCK_TTL_SECONDS

            lease = TaskLease.acquire("data_generation", TASK_LOCK_TTL_SECONDS)
            if lease is None:
                return False, "已有其他 Web Worker 启动了数据生成任务，请等待完成。"
            self._lease = lease
            self.buffer.clear()
            self.state = "running"
            self.mode = "batch"
            self.error = None
            self.date = end
            self.start_date = start
            self.end_date = end
            self.total = 0
            self.completed = 0
            self.failed = []
            self.started_at = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
            self.finished_at = None
            self._thread = threading.Thread(
                target=self._batch_worker, args=(start, end), daemon=True, name="analysis-batch-run"
            )
            self._publish_state()
            self._thread.start()
        return True, f"已启动历史批量生成：{start} ~ {end}。"

    def status(self, since: int = 0) -> Dict:
        lines, nxt = self.buffer.read_from(since)
        current = {
            "state": self.state,
            "mode": self.mode,
            "date": self.date,
            "start_date": self.start_date,
            "end_date": self.end_date,
            "total": self.total,
            "completed": self.completed,
            "failed": list(self.failed),
            "started_at": self.started_at,
            "finished_at": self.finished_at,
            "error": self.error,
            "lines": lines,
            "next": nxt,
        }
        if self._store.shared:
            remote = self._store.load()
            if remote:
                remote.update({"lines": lines, "next": nxt})
                return remote
        current["storage"] = "memory"
        return current

    def _publish_state(self) -> None:
        self._store.save({
            "state": self.state,
            "mode": self.mode,
            "date": self.date,
            "start_date": self.start_date,
            "end_date": self.end_date,
            "total": self.total,
            "completed": self.completed,
            "failed": list(self.failed),
            "started_at": self.started_at,
            "finished_at": self.finished_at,
            "error": self.error,
        })

    def _finish_lease(self) -> None:
        lease, self._lease = self._lease, None
        if lease is not None:
            lease.release()

    # ---- 内部实现 -----------------------------------------------------
    def _worker(self, date: Optional[str]) -> None:
        import loguru

        _ensure_file_sink()

        sink_id = loguru.logger.add(
            self._loguru_sink,
            level="INFO",
            format="{time:HH:mm:ss} | {level: <7} | {message}",
            enqueue=False,
        )
        old_out, old_err = sys.stdout, sys.stderr
        sys.stdout = _StreamTee(old_out, self.buffer)
        sys.stderr = _StreamTee(old_err, self.buffer)
        try:
            self.buffer.append_line(f"=== 开始指标数据生成 · 日期={date or '今日(自动取最近交易日)'} ===")
            from main import SentimentSystem  # 惰性导入重依赖

            system = SentimentSystem()
            system.run_daily_analysis(date)
            self.buffer.append_line("=== 指标数据生成完成，数据仓库、候选池和快照已生成 ===")
            self.state = "done"
            self._publish_state()
        except Exception as exc:  # noqa: BLE001
            self.error = repr(exc)
            self.buffer.append_line(f"!!! 运行失败: {exc!r}")
            self.buffer.append_text(traceback.format_exc())
            self.state = "error"
            self._publish_state()
        finally:
            sys.stdout, sys.stderr = old_out, old_err
            try:
                loguru.logger.remove(sink_id)
            except Exception:  # noqa: BLE001
                pass
            self.finished_at = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
            self._publish_state()
            self._finish_lease()

    def _batch_worker(self, start_date: str, end_date: str) -> None:
        import loguru

        _ensure_file_sink()

        sink_id = loguru.logger.add(
            self._loguru_sink,
            level="INFO",
            format="{time:HH:mm:ss} | {level: <7} | {message}",
            enqueue=False,
        )
        old_out, old_err = sys.stdout, sys.stderr
        sys.stdout = _StreamTee(old_out, self.buffer)
        sys.stderr = _StreamTee(old_err, self.buffer)
        failures: List[str] = []
        try:
            from backtest.trade_calendar import TradeCalendar
            from main import SentimentSystem

            trade_dates = TradeCalendar().get_trade_dates(start_date, end_date)
            self.total = len(trade_dates)
            self._publish_state()
            self.buffer.append_line(
                f"=== 开始历史批量生成 · {start_date} ~ {end_date} · 交易日 {len(trade_dates)} 个 ==="
            )
            if not trade_dates:
                raise RuntimeError("区间内没有可运行的交易日，请检查日期范围。")

            system = SentimentSystem()
            for idx, trade_date in enumerate(trade_dates, 1):
                date_started = time.monotonic()
                heartbeat_stop = threading.Event()

                def heartbeat(current_date=trade_date, started=date_started):
                    while not heartbeat_stop.wait(60.0):
                        loguru.logger.info(
                            f"[批量生成] {current_date} 仍在运行，累计耗时 "
                            f"{time.monotonic() - started:.0f}s"
                        )

                heartbeat_thread = threading.Thread(
                    target=heartbeat,
                    daemon=True,
                    name=f"batch-heartbeat-{trade_date}",
                )
                heartbeat_thread.start()
                self.buffer.append_line("")
                self.buffer.append_line(f"--- [{idx}/{len(trade_dates)}] {trade_date} 开始 ---")
                try:
                    system.run_daily_analysis(trade_date)
                    self.completed = idx
                    self.date = trade_date
                    self._publish_state()
                    self.buffer.append_line(
                        f"--- [{idx}/{len(trade_dates)}] {trade_date} 完成 · "
                        f"耗时 {time.monotonic() - date_started:.1f}s ---"
                    )
                except Exception as exc:  # noqa: BLE001
                    failures.append(trade_date)
                    self.failed = list(failures)
                    self.completed = idx
                    self.date = trade_date
                    self._publish_state()
                    self.buffer.append_line(
                        f"!!! [{idx}/{len(trade_dates)}] {trade_date} 失败 · "
                        f"耗时 {time.monotonic() - date_started:.1f}s: {exc!r}"
                    )
                    self.buffer.append_text(traceback.format_exc())
                finally:
                    heartbeat_stop.set()
                    heartbeat_thread.join(timeout=0.2)

            if failures:
                self.error = f"批量生成完成，失败 {len(failures)} 个交易日: {', '.join(failures)}"
                self.buffer.append_line(f"=== 历史批量生成结束：成功 {len(trade_dates) - len(failures)}，失败 {len(failures)} ===")
                self.state = "partial"
            else:
                self.buffer.append_line(f"=== 历史批量生成完成：{len(trade_dates)} 个交易日全部成功 ===")
                self.state = "done"
            self._publish_state()
        except Exception as exc:  # noqa: BLE001
            self.error = repr(exc)
            self.buffer.append_line(f"!!! 批量生成失败: {exc!r}")
            self.buffer.append_text(traceback.format_exc())
            self.state = "error"
            self._publish_state()
        finally:
            sys.stdout, sys.stderr = old_out, old_err
            try:
                loguru.logger.remove(sink_id)
            except Exception:  # noqa: BLE001
                pass
            self.failed = list(failures)
            self.finished_at = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
            self._publish_state()
            self._finish_lease()

    def _loguru_sink(self, message) -> None:
        # loguru sink 收到已格式化字符串（带换行）
        try:
            self.buffer.append_text(str(message))
        except Exception:  # noqa: BLE001
            pass


class BacktestController:
    """单例回测控制器：在进程内重跑回测（生成净值/交易/回撤），并实时缓冲日志。

    基于 webdata/snapshots 当前交易计划生成回测输入，回测结果通过 run_backtest.save_backtest_results
    落到可配置 OUTPUT_DIR/backtest_results，「模拟交易」「回撤分析」两页直接读取最新批次。
    """

    def __init__(self) -> None:
        self._lock = threading.Lock()
        self._store = TaskStateStore("backtest")
        self.buffer = LogBuffer(self._store)
        self._lease: Optional[TaskLease] = None
        self.state: str = "idle"
        self.started_at: Optional[str] = None
        self.finished_at: Optional[str] = None
        self.error: Optional[str] = None
        self.params: Dict[str, object] = {}
        self._thread: Optional[threading.Thread] = None

    def _try_acquire(self) -> bool:
        if self.state == "running":
            return False
        from config.settings import TASK_LOCK_TTL_SECONDS

        self._lease = TaskLease.acquire("backtest", TASK_LOCK_TTL_SECONDS)
        return self._lease is not None

    def _publish_state(self) -> None:
        self._store.save({
            "state": self.state,
            "params": self.params,
            "started_at": self.started_at,
            "finished_at": self.finished_at,
            "error": self.error,
        })

    def _finish_lease(self) -> None:
        lease, self._lease = self._lease, None
        if lease is not None:
            lease.release()

    def start(self, start_date: Optional[str], end_date: Optional[str],
              initial_capital: object = None,
              risk_control: object = None,
              max_plan_rank: object = 0,
              mode: object = "range",
              trade_date: Optional[str] = None,
              reset_state: object = False,
              enhancements: object = None) -> Tuple[bool, str]:
        from core.screening.enhancements import enhancement_label, normalize_enhancements

        selected_enhancements = normalize_enhancements(enhancements)
        combination_label = enhancement_label(selected_enhancements)
        start_date = (str(start_date).strip() if start_date else "") or None
        end_date = (str(end_date).strip() if end_date else "") or None
        trade_date = (str(trade_date).strip() if trade_date else "") or None
        mode = (str(mode or "range").strip().lower() or "range")
        if mode not in {"range", "daily"}:
            mode = "range"
        try:
            capital = float(initial_capital) if initial_capital not in (None, "") else 100000.0
        except (TypeError, ValueError):
            capital = 100000.0
        # 全部候选交给买点/盘中转强规则，保留参数仅兼容旧 API 请求。
        max_rank = 0

        # 风控闸门开关：未显式指定时回退到全局 RiskConfig.enabled
        if risk_control is None:
            try:
                from risk.risk_config import RiskConfig
                risk_on = bool(RiskConfig.load().enabled)
            except Exception:
                risk_on = True
        else:
            risk_on = bool(risk_control)

        if mode == "daily":
            target = trade_date or end_date or start_date or datetime.now().strftime("%Y%m%d")
            with self._lock:
                if not self._try_acquire():
                    return False, "已有回测任务在运行中，请等待完成。"
                self.buffer.clear()
                self.state = "running"
                self.error = None
                self.params = {
                    "mode": "daily",
                    "trade_date": target,
                    "initial_capital": capital,
                    "risk_control": risk_on,
                    "max_plan_rank": max_rank,
                    "reset_state": bool(reset_state),
                    "enhancements": selected_enhancements,
                }
                self.started_at = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
                self.finished_at = None
                self._thread = threading.Thread(
                    target=self._worker_daily,
                    args=(target, capital, risk_on, bool(reset_state), max_rank, selected_enhancements),
                    daemon=True,
                    name="backtest-run-daily",
                )
                self._publish_state()
                self._thread.start()
            mode_txt = "开启风控" if risk_on else "关闭风控"
            reset_txt = "重置接力账户" if reset_state else "承接上一交易日账户"
            return True, f"已启动单日接力回测：{target}（{combination_label}，{mode_txt}，{reset_txt}）"

        # 默认区间：结束=今日，开始=结束前 90 天（交易日历会自动剔除非交易日）
        end = end_date or datetime.now().strftime("%Y%m%d")
        if start_date:
            start = start_date
        else:
            try:
                start = (datetime.strptime(end, "%Y%m%d") - timedelta(days=90)).strftime("%Y%m%d")
            except ValueError:
                start = (datetime.now() - timedelta(days=90)).strftime("%Y%m%d")

        with self._lock:
            if not self._try_acquire():
                return False, "已有回测任务在运行中，请等待完成。"
            self.buffer.clear()
            self.state = "running"
            self.error = None
            self.params = {"mode": "range", "start_date": start, "end_date": end,
                           "initial_capital": capital, "risk_control": risk_on,
                           "max_plan_rank": max_rank, "enhancements": selected_enhancements}
            self.started_at = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
            self.finished_at = None
            self._thread = threading.Thread(
                target=self._worker, args=(start, end, capital, risk_on, max_rank, selected_enhancements),
                daemon=True, name="backtest-run"
            )
            self._publish_state()
            self._thread.start()
        mode = "开启风控" if risk_on else "关闭风控"
        return True, f"已启动回测：{start} ~ {end}（{combination_label}，{mode}）"

    def status(self, since: int = 0) -> Dict:
        lines, nxt = self.buffer.read_from(since)
        current = {
            "state": self.state,
            "params": self.params,
            "started_at": self.started_at,
            "finished_at": self.finished_at,
            "error": self.error,
            "lines": lines,
            "next": nxt,
        }
        if self._store.shared:
            remote = self._store.load()
            if remote:
                remote.update({"lines": lines, "next": nxt})
                return remote
        current["storage"] = "memory"
        return current

    def _worker(self, start: str, end: str, capital: float,
                risk_control: bool = True, max_plan_rank: int = 0,
                enhancements: object = None) -> None:
        import loguru

        _ensure_file_sink()
        sink_id = loguru.logger.add(
            self._loguru_sink, level="INFO",
            format="{time:HH:mm:ss} | {level: <7} | {message}", enqueue=False,
        )
        old_out, old_err = sys.stdout, sys.stderr
        sys.stdout = _StreamTee(old_out, self.buffer)
        sys.stderr = _StreamTee(old_err, self.buffer)
        try:
            mode_txt = "开启风控闸门" if risk_control else "关闭风控闸门（无风控）"
            from core.screening.enhancements import enhancement_label, normalize_enhancements
            selected_enhancements = normalize_enhancements(enhancements)
            combination_label = enhancement_label(selected_enhancements)
            self.buffer.append_line(
                f"=== 开始回测 · {start} ~ {end} · {combination_label} · 初始资金 {capital:,.0f} · {mode_txt} ===")

            from config.settings import CACHE_DIR, OUTPUT_DIR, SNAPSHOT_DIR, TUSHARE_TOKEN, WEB_DATA_DIR
            from backtest.plan_source import build_backtest_plan_dir
            from core.data.data_manager_main import DataManager
            from backtest.backtest_engine import BacktestConfig, BacktestEngine
            from backtest.performance_analyzer import PerformanceAnalyzer
            from risk.risk_config import RiskConfig

            if not (TUSHARE_TOKEN or "").strip():
                self.buffer.append_line("[提示] 未配置 TUSHARE_TOKEN，将仅依赖本地缓存数据，缺数据的票会被跳过。")

            trade_plans_dir, file_count, row_count = build_backtest_plan_dir(
                snapshot_dir=Path(SNAPSHOT_DIR),
                output_dir=Path(WEB_DATA_DIR),
                screening_dir=Path(WEB_DATA_DIR) / "screening",
                start_date=start,
                end_date=end,
                max_rank=max_plan_rank,
                enhancements=selected_enhancements,
            )
            if file_count <= 0:
                self.buffer.append_line(f"!!! 当前数据快照中没有可回测的交易计划：{SNAPSHOT_DIR}")
                self.buffer.append_line("请先到「生成数据」生成每日交易计划后再回测。")
                self.error = "当前交易计划为空"
                self.state = "error"
                return
            self.buffer.append_line(
                f"已从当前数据快照生成回测计划：{file_count} 个交易日，{row_count} 条候选（不按名次截断），目录 {trade_plans_dir}"
            )

            dm = DataManager(TUSHARE_TOKEN, CACHE_DIR, allow_remote_history=False)
            config = BacktestConfig.from_risk_config(
                RiskConfig.load(), initial_capital=capital, risk_control=risk_control,
            )
            config.max_plan_rank = max_plan_rank
            self.buffer.append_line(
                f"退出策略：盈利5%-10%/10%-20%/20%以上分别从高点回撤 "
                f"{config.trailing_early_stop_pct:.0%}/{config.trailing_mid_stop_pct:.0%}/"
                f"{config.trailing_stop_pct:.0%} 退出；不设固定止盈"
            )
            engine = BacktestEngine(dm, config)
            result = engine.run_backtest(
                start_date=start, end_date=end, trade_plans_dir=str(trade_plans_dir))

            report = PerformanceAnalyzer().generate_performance_report(result)
            self.buffer.append_text("\n" + report + "\n")

            from run_backtest import save_backtest_results  # 复用同一套 CSV 落盘逻辑
            run_id = save_backtest_results(result, OUTPUT_DIR, metadata={
                "run_mode": "range",
                "start_date": start,
                "end_date": end,
                "risk_control": risk_control,
                "max_plan_rank": max_plan_rank,
                "enhancements": selected_enhancements,
                "enhancement_label": combination_label,
            })
            if not selected_enhancements:
                from backtest.lhb_comparison import run_lhb_comparison, save_lhb_comparison

                self.buffer.append_line("开始龙虎榜四组对照回测：无龙虎榜 / 净买入 / 机构 / 龙虎榜＋板块共振")
                comparison = run_lhb_comparison(
                    data_manager=dm,
                    config=config,
                    start_date=start,
                    end_date=end,
                    snapshot_dir=Path(SNAPSHOT_DIR),
                    web_data_dir=Path(WEB_DATA_DIR),
                    baseline_result=result,
                )
                comparison_path = save_lhb_comparison(comparison, Path(OUTPUT_DIR), run_id)
                self.buffer.append_line(f"龙虎榜对照报表已保存：{comparison_path}")
            state_path = self._save_rolling_state(
                engine, "range", enhancements=selected_enhancements,
            )

            self.buffer.append_line(f"接力账户状态已同步到 {state_path}，后续可用单日接力继续。")
            self.buffer.append_line("=== 回测完成，结果已保存，可在「模拟交易 / 回撤分析」查看 ===")
            self.state = "done"
            self._publish_state()
        except Exception as exc:  # noqa: BLE001
            self.error = repr(exc)
            self.buffer.append_line(f"!!! 回测失败: {exc!r}")
            self.buffer.append_text(traceback.format_exc())
            self.state = "error"
            self._publish_state()
        finally:
            sys.stdout, sys.stderr = old_out, old_err
            try:
                loguru.logger.remove(sink_id)
            except Exception:  # noqa: BLE001
                pass
            self.finished_at = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
            self._publish_state()
            self._finish_lease()

    def _worker_daily(self, trade_date: str, capital: float,
                      risk_control: bool = True, reset_state: bool = False,
                      max_plan_rank: int = 0, enhancements: object = None) -> None:
        import loguru

        _ensure_file_sink()
        sink_id = loguru.logger.add(
            self._loguru_sink, level="INFO",
            format="{time:HH:mm:ss} | {level: <7} | {message}", enqueue=False,
        )
        old_out, old_err = sys.stdout, sys.stderr
        sys.stdout = _StreamTee(old_out, self.buffer)
        sys.stderr = _StreamTee(old_err, self.buffer)
        try:
            mode_txt = "开启风控闸门" if risk_control else "关闭风控闸门（无风控）"
            from core.screening.enhancements import enhancement_label, normalize_enhancements
            selected_enhancements = normalize_enhancements(enhancements)
            combination_label = enhancement_label(selected_enhancements)
            self.buffer.append_line(
                f"=== 开始单日接力回测 · {trade_date} · {combination_label} · 初始资金 {capital:,.0f} · {mode_txt} ===")

            from config.settings import CACHE_DIR, OUTPUT_DIR, SNAPSHOT_DIR, TUSHARE_TOKEN, WEB_DATA_DIR
            from backtest.plan_source import build_backtest_plan_dir
            from backtest.trade_calendar import TradeCalendar
            from core.data.data_manager_main import DataManager
            from backtest.backtest_engine import BacktestConfig, BacktestEngine
            from backtest.performance_analyzer import PerformanceAnalyzer
            from risk.risk_config import RiskConfig

            calendar = TradeCalendar()
            if not calendar.is_trade_date(trade_date):
                self.buffer.append_line(f"!!! {trade_date} 不是交易日，单日接力已取消。")
                self.error = f"{trade_date} 不是交易日"
                self.state = "error"
                return

            prev_date = calendar.prev(trade_date)
            if not (TUSHARE_TOKEN or "").strip():
                self.buffer.append_line("[提示] 未配置 TUSHARE_TOKEN，将仅依赖本地缓存数据，缺数据的票会被跳过。")

            trade_plans_dir, file_count, row_count = build_backtest_plan_dir(
                snapshot_dir=Path(SNAPSHOT_DIR),
                output_dir=Path(WEB_DATA_DIR),
                screening_dir=Path(WEB_DATA_DIR) / "screening",
                start_date=prev_date,
                end_date=prev_date,
                max_rank=max_plan_rank,
                enhancements=selected_enhancements,
            )
            if file_count <= 0:
                self.buffer.append_line(f"!!! 未找到上一交易日 {prev_date} 的交易计划，无法执行 {trade_date}。")
                self.error = f"缺少 {prev_date} 交易计划"
                self.state = "error"
                return
            self.buffer.append_line(
                f"已加载上一交易日 {prev_date} 的回测计划：{row_count} 条候选（按买点/盘中转强确认）。")

            dm = DataManager(TUSHARE_TOKEN, CACHE_DIR, allow_remote_history=False)
            config = BacktestConfig.from_risk_config(
                RiskConfig.load(), initial_capital=capital, risk_control=risk_control,
            )
            config.max_plan_rank = max_plan_rank
            self.buffer.append_line(
                f"退出策略：盈利5%-10%/10%-20%/20%以上分别从高点回撤 "
                f"{config.trailing_early_stop_pct:.0%}/{config.trailing_mid_stop_pct:.0%}/"
                f"{config.trailing_stop_pct:.0%} 退出；不设固定止盈"
            )
            engine = BacktestEngine(dm, config)
            state_source = "reset"

            if reset_state:
                self.buffer.append_line("已按空账户初始化接力状态；这一天不会继承历史持仓。")
            else:
                state = self._load_rolling_state()
                if state:
                    state_enhancements = normalize_enhancements(state.get("enhancements"))
                    if state_enhancements != selected_enhancements:
                        self.buffer.append_line(
                            f"!!! 接力账户使用 {enhancement_label(state_enhancements)}，本次选择 {combination_label}，口径不一致。"
                        )
                        self.error = "接力账户增强组合不一致"
                        self.state = "error"
                        return
                    last_date = str(state.get("last_date") or "")
                    if last_date != prev_date:
                        self.buffer.append_line(
                            f"!!! 当前接力状态停在 {last_date or '空'}，而 {trade_date} 的上一交易日是 {prev_date}。")
                        self.buffer.append_line("请先补跑缺失交易日，或用区间重算同步状态后再接力。")
                        self.error = f"接力状态日期不匹配：{last_date} != {prev_date}"
                        self.state = "error"
                        return
                    engine.import_state(state)
                    state_source = "rolling_state"
                    self.buffer.append_line(f"已读取接力账户状态：截至 {last_date}，持仓 {len(engine.current_positions)} 只。")
                else:
                    boot = self._bootstrap_state_from_latest_run(prev_date, engine)
                    if not boot:
                        self.buffer.append_line(f"!!! 没有找到截至 {prev_date} 的接力状态或历史回测批次。")
                        self.buffer.append_line("请先运行区间回测到上一交易日，或勾选“重置接力账户”从空仓开始。")
                        self.error = f"缺少截至 {prev_date} 的接力状态"
                        self.state = "error"
                        return
                    state_source = f"bootstrap:{boot}"
                    self.buffer.append_line(f"已从历史回测批次 {boot} 引导接力账户。")

            result = engine.run_one_day(trade_date=trade_date, trade_plans_dir=str(trade_plans_dir))

            report = PerformanceAnalyzer().generate_performance_report(result)
            self.buffer.append_text("\n" + report + "\n")

            from run_backtest import save_backtest_results
            save_backtest_results(result, OUTPUT_DIR, metadata={
                "run_mode": "daily",
                "trade_date": trade_date,
                "continued_from": prev_date,
                "state_source": state_source,
                "risk_control": risk_control,
                "max_plan_rank": max_plan_rank,
                "enhancements": selected_enhancements,
                "enhancement_label": combination_label,
            })
            state_path = self._save_rolling_state(
                engine, "daily", state_source=state_source, enhancements=selected_enhancements,
            )

            self.buffer.append_line(f"接力账户状态已更新到 {trade_date}：{state_path}")
            self.buffer.append_line("=== 单日接力回测完成，结果已保存，可在「模拟交易 / 回撤分析」查看 ===")
            self.state = "done"
            self._publish_state()
        except Exception as exc:  # noqa: BLE001
            self.error = repr(exc)
            self.buffer.append_line(f"!!! 单日接力回测失败: {exc!r}")
            self.buffer.append_text(traceback.format_exc())
            self.state = "error"
            self._publish_state()
        finally:
            sys.stdout, sys.stderr = old_out, old_err
            try:
                loguru.logger.remove(sink_id)
            except Exception:  # noqa: BLE001
                pass
            self.finished_at = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
            self._publish_state()
            self._finish_lease()

    @staticmethod
    def _rolling_state_path() -> Path:
        from config.settings import OUTPUT_DIR

        return Path(OUTPUT_DIR) / "backtest_results" / "rolling_state.json"

    def _load_rolling_state(self) -> Optional[Dict[str, object]]:
        path = self._rolling_state_path()
        if not path.exists():
            return None
        try:
            return json.loads(path.read_text(encoding="utf-8"))
        except Exception as exc:  # noqa: BLE001
            self.buffer.append_line(f"[提示] 接力状态读取失败，将尝试从历史批次引导: {exc}")
            return None

    def _save_rolling_state(
        self, engine, mode: str, state_source: str = "", enhancements: object = None,
    ) -> Path:
        from core.screening.enhancements import normalize_enhancements

        path = self._rolling_state_path()
        path.parent.mkdir(parents=True, exist_ok=True)
        state = engine.export_state()
        state["source_mode"] = mode
        state["state_source"] = state_source
        state["enhancements"] = normalize_enhancements(enhancements)
        path.write_text(json.dumps(state, ensure_ascii=False, indent=2), encoding="utf-8")
        return path

    def _bootstrap_state_from_latest_run(self, prev_date: str, engine) -> Optional[str]:
        """从已有回测 CSV 中找一个净值最后日期为 prev_date 的批次，近似恢复接力状态。"""
        try:
            from desktop.backtest import list_runs, load_nav, load_summary, load_trades
        except Exception:
            return None

        for run in list_runs():
            nav = load_nav(run)
            if not nav or str(nav[-1].get("date") or "") != prev_date:
                continue
            trades = load_trades(run)
            summary = load_summary(run)
            state = self._state_from_result_rows(nav, trades, summary, prev_date, engine)
            engine.import_state(state)
            return run
        return None

    def _state_from_result_rows(self, nav: List[Dict], trades: List[Dict],
                                summary: Dict, prev_date: str, engine) -> Dict[str, object]:
        initial = float(summary.get("initial_capital") or (nav[0].get("total_value") if nav else 100000) or 100000)
        cash = float(nav[-1].get("cash") or initial) if nav else initial
        total = float(nav[-1].get("total_value") or cash) if nav else cash
        positions: Dict[str, Dict] = {}
        for trade in trades:
            code = str(trade.get("stock_code") or "").zfill(6)
            action = str(trade.get("action") or "").upper()
            if not code:
                continue
            shares = int(float(trade.get("shares") or 0))
            if action == "BUY":
                entry_price = float(trade.get("entry_price") or 0)
                cost_basis = float(trade.get("position_size") or (entry_price * shares))
                positions[code] = {
                    "stock_name": trade.get("stock_name") or "",
                    "entry_date": str(trade.get("entry_date") or trade.get("date") or ""),
                    "entry_price": entry_price,
                    "shares": shares,
                    "cost_basis": cost_basis,
                    "market_value": entry_price * shares,
                    "pattern_type": trade.get("pattern_type") or "",
                    "hot_resonance": bool(trade.get("hot_resonance")),
                    "resonance_sectors": trade.get("resonance_sectors") or "",
                    "plan_rank": int(float(trade.get("plan_rank") or 0)),
                    "plan_score": float(trade.get("plan_score") or 0),
                    "plan_reason": trade.get("plan_reason") or "",
                    "factor_metrics_json": trade.get("factor_metrics_json") or "",
                    "factor_context_json": trade.get("factor_context_json") or "",
                    "open_gap_pct": float(trade.get("open_gap_pct") or 0),
                    "market_score": float(trade.get("market_score") or 0),
                    "amount_ratio": float(trade.get("amount_ratio") or 0),
                    "entry_signal": str(trade.get("entry_signal") or ""),
                    "stop_loss_price": entry_price * (1 - engine.config.stop_loss_pct),
                    "highest_price": entry_price,
                }
            elif action == "SELL_PARTIAL" and code in positions:
                pos = positions[code]
                current_shares = int(pos.get("shares") or 0)
                if current_shares <= 0:
                    positions.pop(code, None)
                    continue
                sold = min(shares, current_shares)
                ratio = sold / current_shares if current_shares else 0
                pos["shares"] = current_shares - sold
                pos["cost_basis"] = float(pos.get("cost_basis") or 0) * (1 - ratio)
                reason = str(trade.get("exit_reason") or "")
                if reason == "partial_first":
                    pos["first_partial_sold"] = True
                if reason == "partial_second":
                    pos["second_partial_sold"] = True
                if pos["shares"] <= 0:
                    positions.pop(code, None)
            elif action.startswith("SELL"):
                positions.pop(code, None)

        self._refresh_bootstrap_positions(positions, prev_date, engine)
        return {
            "version": 1,
            "last_date": prev_date,
            "initial_capital": initial,
            "cash": cash,
            "total_capital": total,
            "current_positions": positions,
            "daily_nav": nav,
            "trade_history": trades,
        }

    @staticmethod
    def _refresh_bootstrap_positions(positions: Dict[str, Dict], prev_date: str, engine) -> None:
        for code, pos in positions.items():
            entry = str(pos.get("entry_date") or "")
            dates = engine.calendar.get_trade_dates(entry, prev_date) if entry else [prev_date]
            for d in dates:
                try:
                    row = engine.dm.get_stock_daily_data(engine._standardize_stock_code(code), d)
                except Exception:
                    row = None
                if not row:
                    continue
                close = float(row.get("close") or 0)
                high = float(row.get("high") or close or 0)
                if high > 0:
                    pos["highest_price"] = max(float(pos.get("highest_price") or 0), high)
                if d == prev_date and close > 0:
                    pos["market_value"] = int(pos.get("shares") or 0) * close

    def _loguru_sink(self, message) -> None:
        try:
            self.buffer.append_text(str(message))
        except Exception:  # noqa: BLE001
            pass


# 进程级单例
CONTROLLER = RunController()
BACKTEST_CONTROLLER = BacktestController()
