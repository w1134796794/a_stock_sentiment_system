"""在管理工具进程内执行「收盘分析」，并把日志实时缓冲给前端轮询。

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
from datetime import datetime
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


class RunController:
    """单例分析控制器。"""

    def __init__(self) -> None:
        self._lock = threading.Lock()
        self.buffer = LogBuffer()
        self.state: str = "idle"  # idle / running / done / error
        self.date: Optional[str] = None
        self.started_at: Optional[str] = None
        self.finished_at: Optional[str] = None
        self.error: Optional[str] = None
        self._thread: Optional[threading.Thread] = None
        self._file_sink_ready = False

    # ---- 对外 API -----------------------------------------------------
    def start(self, date: Optional[str]) -> Tuple[bool, str]:
        date = (date or "").strip() or None
        with self._lock:
            if self.state == "running":
                return False, "已有分析任务在运行中，请等待完成。"
            self.buffer.clear()
            self.state = "running"
            self.error = None
            self.date = date
            self.started_at = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
            self.finished_at = None
            self._thread = threading.Thread(
                target=self._worker, args=(date,), daemon=True, name="analysis-run"
            )
            self._thread.start()
        return True, "已启动分析任务。"

    def status(self, since: int = 0) -> Dict:
        lines, nxt = self.buffer.read_from(since)
        return {
            "state": self.state,
            "date": self.date,
            "started_at": self.started_at,
            "finished_at": self.finished_at,
            "error": self.error,
            "lines": lines,
            "next": nxt,
        }

    # ---- 内部实现 -----------------------------------------------------
    def _ensure_file_sink(self) -> None:
        """首次运行时挂上 logs/system.log 文件日志（与命令行运行表现一致）。"""
        if self._file_sink_ready:
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
        self._file_sink_ready = True

    def _worker(self, date: Optional[str]) -> None:
        import loguru

        self._ensure_file_sink()

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
            self.buffer.append_line(f"=== 开始收盘分析 · 日期={date or '今日(自动取最近交易日)'} ===")
            from main import SentimentSystem  # 惰性导入重依赖

            system = SentimentSystem()
            system.run_daily_analysis(date)
            self.buffer.append_line("=== 分析完成，报告与快照已生成 ===")
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

    def _loguru_sink(self, message) -> None:
        # loguru sink 收到已格式化字符串（带换行）
        try:
            self.buffer.append_text(str(message))
        except Exception:  # noqa: BLE001
            pass


# 进程级单例
CONTROLLER = RunController()


