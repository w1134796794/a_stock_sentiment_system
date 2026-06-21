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
import threading
import traceback
from datetime import datetime, timedelta
from pathlib import Path
from typing import Dict, List, Optional, Tuple


class LogBuffer:
    """线程安全的行缓冲：支持按行增量读取（前端轮询用）。"""

    def __init__(self) -> None:
        self._lock = threading.Lock()
        self._lines: List[str] = []
        self._partial = ""  # 尚未遇到换行的残段（print 分片写入时用）

    def clear(self) -> None:
        with self._lock:
            self._lines.clear()
            self._partial = ""

    def append_line(self, line: str) -> None:
        with self._lock:
            self._lines.append(line.rstrip("\n"))

    def append_text(self, text: str) -> None:
        """写入任意文本（可能不含/含多个换行），按换行切分成行。"""
        if not text:
            return
        with self._lock:
            buf = self._partial + text
            parts = buf.split("\n")
            self._partial = parts.pop()  # 最后一段可能是半行
            self._lines.extend(parts)

    def read_from(self, since: int) -> Tuple[List[str], int]:
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
        self.buffer = LogBuffer()
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
            self._thread.start()
        return True, f"已启动历史批量生成：{start} ~ {end}。"

    def status(self, since: int = 0) -> Dict:
        lines, nxt = self.buffer.read_from(since)
        return {
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
        except Exception as exc:  # noqa: BLE001
            self.error = repr(exc)
            self.buffer.append_line(f"!!! 运行失败: {exc!r}")
            self.buffer.append_text(traceback.format_exc())
            self.state = "error"
        finally:
            sys.stdout, sys.stderr = old_out, old_err
            try:
                loguru.logger.remove(sink_id)
            except Exception:  # noqa: BLE001
                pass
            self.finished_at = datetime.now().strftime("%Y-%m-%d %H:%M:%S")

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
            self.buffer.append_line(
                f"=== 开始历史批量生成 · {start_date} ~ {end_date} · 交易日 {len(trade_dates)} 个 ==="
            )
            if not trade_dates:
                raise RuntimeError("区间内没有可运行的交易日，请检查日期范围。")

            system = SentimentSystem()
            for idx, trade_date in enumerate(trade_dates, 1):
                self.buffer.append_line("")
                self.buffer.append_line(f"--- [{idx}/{len(trade_dates)}] {trade_date} 开始 ---")
                try:
                    system.run_daily_analysis(trade_date)
                    self.completed = idx
                    self.date = trade_date
                    self.buffer.append_line(f"--- [{idx}/{len(trade_dates)}] {trade_date} 完成 ---")
                except Exception as exc:  # noqa: BLE001
                    failures.append(trade_date)
                    self.failed = list(failures)
                    self.completed = idx
                    self.date = trade_date
                    self.buffer.append_line(f"!!! [{idx}/{len(trade_dates)}] {trade_date} 失败: {exc!r}")
                    self.buffer.append_text(traceback.format_exc())

            if failures:
                self.error = f"批量生成完成，失败 {len(failures)} 个交易日: {', '.join(failures)}"
                self.buffer.append_line(f"=== 历史批量生成结束：成功 {len(trade_dates) - len(failures)}，失败 {len(failures)} ===")
                self.state = "partial"
            else:
                self.buffer.append_line(f"=== 历史批量生成完成：{len(trade_dates)} 个交易日全部成功 ===")
                self.state = "done"
        except Exception as exc:  # noqa: BLE001
            self.error = repr(exc)
            self.buffer.append_line(f"!!! 批量生成失败: {exc!r}")
            self.buffer.append_text(traceback.format_exc())
            self.state = "error"
        finally:
            sys.stdout, sys.stderr = old_out, old_err
            try:
                loguru.logger.remove(sink_id)
            except Exception:  # noqa: BLE001
                pass
            self.failed = list(failures)
            self.finished_at = datetime.now().strftime("%Y-%m-%d %H:%M:%S")

    def _loguru_sink(self, message) -> None:
        # loguru sink 收到已格式化字符串（带换行）
        try:
            self.buffer.append_text(str(message))
        except Exception:  # noqa: BLE001
            pass


class BacktestController:
    """单例回测控制器：在进程内重跑回测（生成净值/交易/回撤），并实时缓冲日志。

    基于 webdata/snapshots 当前交易计划生成回测输入，回测结果通过 run_backtest.save_backtest_results
    落到 output/backtest_results，「模拟交易」「回撤分析」两页直接读取最新批次。
    """

    def __init__(self) -> None:
        self._lock = threading.Lock()
        self.buffer = LogBuffer()
        self.state: str = "idle"
        self.started_at: Optional[str] = None
        self.finished_at: Optional[str] = None
        self.error: Optional[str] = None
        self.params: Dict[str, object] = {}
        self._thread: Optional[threading.Thread] = None

    def start(self, start_date: Optional[str], end_date: Optional[str],
              initial_capital: object = None,
              risk_control: object = None) -> Tuple[bool, str]:
        start_date = (str(start_date).strip() if start_date else "") or None
        end_date = (str(end_date).strip() if end_date else "") or None
        try:
            capital = float(initial_capital) if initial_capital not in (None, "") else 100000.0
        except (TypeError, ValueError):
            capital = 100000.0

        # 风控闸门开关：未显式指定时回退到全局 RiskConfig.enabled
        if risk_control is None:
            try:
                from risk.risk_config import RiskConfig
                risk_on = bool(RiskConfig.load().enabled)
            except Exception:
                risk_on = True
        else:
            risk_on = bool(risk_control)

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
            if self.state == "running":
                return False, "已有回测任务在运行中，请等待完成。"
            self.buffer.clear()
            self.state = "running"
            self.error = None
            self.params = {"start_date": start, "end_date": end,
                           "initial_capital": capital, "risk_control": risk_on}
            self.started_at = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
            self.finished_at = None
            self._thread = threading.Thread(
                target=self._worker, args=(start, end, capital, risk_on),
                daemon=True, name="backtest-run"
            )
            self._thread.start()
        mode = "开启风控" if risk_on else "关闭风控"
        return True, f"已启动回测：{start} ~ {end}（{mode}）"

    def status(self, since: int = 0) -> Dict:
        lines, nxt = self.buffer.read_from(since)
        return {
            "state": self.state,
            "params": self.params,
            "started_at": self.started_at,
            "finished_at": self.finished_at,
            "error": self.error,
            "lines": lines,
            "next": nxt,
        }

    def _worker(self, start: str, end: str, capital: float,
                risk_control: bool = True) -> None:
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
            self.buffer.append_line(
                f"=== 开始回测 · {start} ~ {end} · 初始资金 {capital:,.0f} · {mode_txt} ===")

            from config.settings import CACHE_DIR, OUTPUT_DIR, SNAPSHOT_DIR, TUSHARE_TOKEN, WEB_DATA_DIR
            from backtest.plan_source import build_backtest_plan_dir
            from core.data.data_manager_main import DataManager
            from backtest.backtest_engine import BacktestConfig, BacktestEngine
            from backtest.performance_analyzer import PerformanceAnalyzer

            if not (TUSHARE_TOKEN or "").strip():
                self.buffer.append_line("[提示] 未配置 TUSHARE_TOKEN，将仅依赖本地缓存数据，缺数据的票会被跳过。")

            trade_plans_dir, file_count, row_count = build_backtest_plan_dir(
                snapshot_dir=Path(SNAPSHOT_DIR),
                output_dir=Path(WEB_DATA_DIR),
                screening_dir=Path(WEB_DATA_DIR) / "screening",
                start_date=start,
                end_date=end,
            )
            if file_count <= 0:
                self.buffer.append_line(f"!!! 当前数据快照中没有可回测的交易计划：{SNAPSHOT_DIR}")
                self.buffer.append_line("请先到「生成数据」生成每日交易计划后再回测。")
                self.error = "当前交易计划为空"
                self.state = "error"
                return
            self.buffer.append_line(
                f"已从当前数据快照生成回测计划：{file_count} 个交易日，{row_count} 条候选（仅执行每日前3名），目录 {trade_plans_dir}"
            )

            dm = DataManager(TUSHARE_TOKEN, CACHE_DIR)
            config = BacktestConfig(initial_capital=capital, risk_control=risk_control)
            engine = BacktestEngine(dm, config)
            result = engine.run_backtest(
                start_date=start, end_date=end, trade_plans_dir=str(trade_plans_dir))

            report = PerformanceAnalyzer().generate_performance_report(result)
            self.buffer.append_text("\n" + report + "\n")

            from run_backtest import save_backtest_results  # 复用同一套 CSV 落盘逻辑
            save_backtest_results(result, OUTPUT_DIR)

            self.buffer.append_line("=== 回测完成，结果已保存，可在「模拟交易 / 回撤分析」查看 ===")
            self.state = "done"
        except Exception as exc:  # noqa: BLE001
            self.error = repr(exc)
            self.buffer.append_line(f"!!! 回测失败: {exc!r}")
            self.buffer.append_text(traceback.format_exc())
            self.state = "error"
        finally:
            sys.stdout, sys.stderr = old_out, old_err
            try:
                loguru.logger.remove(sink_id)
            except Exception:  # noqa: BLE001
                pass
            self.finished_at = datetime.now().strftime("%Y-%m-%d %H:%M:%S")

    def _loguru_sink(self, message) -> None:
        try:
            self.buffer.append_text(str(message))
        except Exception:  # noqa: BLE001
            pass


# 进程级单例
CONTROLLER = RunController()
BACKTEST_CONTROLLER = BacktestController()
