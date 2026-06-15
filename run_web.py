"""
启动 Web 看板（P1）。

    python run_web.py            # 默认 127.0.0.1:8000（仅本机）
    python run_web.py --port 9000

仅读取快照产物，无需 tushare 等重依赖。
"""
import argparse

import uvicorn


def main() -> None:
    parser = argparse.ArgumentParser(description="A股情绪系统 · Web 看板")
    parser.add_argument("--host", default="127.0.0.1", help="监听地址（默认仅本机）")
    parser.add_argument("--port", type=int, default=8000)
    parser.add_argument("--reload", action="store_true", help="开发热重载")
    args = parser.parse_args()

    uvicorn.run("web.app:app", host=args.host, port=args.port, reload=args.reload)


if __name__ == "__main__":
    main()
