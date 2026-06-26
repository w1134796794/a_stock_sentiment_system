"""
启动 Web 看板（P1）。

    python run_web.py            # 默认 127.0.0.1:8000（仅本机）
    python run_web.py --port 9000

仅读取快照产物，无需 tushare 等重依赖。
"""
import argparse
import os
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


def main() -> None:
    parser = argparse.ArgumentParser(description="A股情绪系统 · Web 看板")
    parser.add_argument("--host", default="127.0.0.1", help="监听地址（默认仅本机）")
    parser.add_argument("--port", type=int, default=8000)
    parser.add_argument("--reload", action="store_true", help="开发热重载")
    args = parser.parse_args()

    _sanitize_stdio()
    uvicorn.run("web.app:app", host=args.host, port=args.port, reload=args.reload)


if __name__ == "__main__":
    main()
