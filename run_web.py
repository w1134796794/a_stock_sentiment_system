"""
启动 Web 看板（P1）。

    python run_web.py            # 默认 127.0.0.1:8000（仅本机）
    python run_web.py --port 9000

仅读取快照产物，无需 tushare 等重依赖。
"""
import argparse
import faulthandler
import os
import signal
import sys

import uvicorn


class _NullStream:
    def write(self, _text):
        return 0

    def flush(self):
        return None

    def isatty(self):
        return False


def _stream_is_usable(stream) -> bool:
    if stream is None:
        return False
    try:
        stream.flush()
        if os.name == "nt" and hasattr(stream, "fileno"):
            try:
                os.fstat(stream.fileno())
            except OSError:
                return False
        return True
    except Exception:
        return False


def _sanitize_stdio() -> None:
    """后台隐藏启动时 stdout/stderr 可能存在但不可写，先替换成安全空流。"""
    if not _stream_is_usable(sys.stdout):
        sys.stdout = _NullStream()
    if not _stream_is_usable(sys.stderr):
        sys.stderr = _NullStream()


def _enable_fault_diagnostics() -> None:
    """Enable on-demand Python thread dumps in systemd logs on Linux."""
    try:
        faulthandler.enable(file=sys.stderr, all_threads=True)
        if hasattr(signal, "SIGUSR1"):
            faulthandler.register(signal.SIGUSR1, file=sys.stderr, all_threads=True)
    except Exception:
        pass


def main() -> None:
    parser = argparse.ArgumentParser(description="A股情绪系统 · Web 看板")
    parser.add_argument("--host", default="127.0.0.1", help="监听地址（默认仅本机）")
    parser.add_argument("--port", type=int, default=8000)
    parser.add_argument("--reload", action="store_true", help="开发热重载")
    parser.add_argument("--workers", type=int, default=1, help="Web Worker 数；大于 1 必须配置 REDIS_URL")
    args = parser.parse_args()

    workers = max(int(args.workers), 1)
    from config.settings import REDIS_URL

    if workers > 1 and not REDIS_URL:
        parser.error("--workers > 1 requires REDIS_URL for shared realtime cache and task state")
    if workers > 1:
        from core.infrastructure.shared_state import get_shared_state_backend

        if not get_shared_state_backend().is_shared:
            parser.error("--workers > 1 requires a reachable Redis service")
    if args.reload and workers > 1:
        parser.error("--reload cannot be combined with --workers > 1")

    _sanitize_stdio()
    _enable_fault_diagnostics()
    uvicorn.run(
        "web.app:app", host=args.host, port=args.port, reload=args.reload, workers=workers
    )


if __name__ == "__main__":
    main()
