"""桌面管理工具入口：后台内嵌 FastAPI 服务 + 前台 pywebview 原生窗口。

对用户而言就是一个双击即用的工具：无需手动 `python run_web.py`、也无需开浏览器。
内嵌服务只绑定 127.0.0.1 的随机空闲端口，对外不可见，等价于 Tauri/Electron 这类
「壳里跑本地页面」的做法。
"""
from __future__ import annotations

import io
import socket
import sys
import threading
import time
from pathlib import Path

# 允许以 `python desktop/manager.py` 直接运行（把项目根加入 sys.path）
sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

WINDOW_TITLE = "A股情绪系统 · 管理控制台"


class _NullStream(io.TextIOBase):
    """丢弃写入的空流：用于窗口化(console=False) exe 下 stdout/stderr 为 None 的场景。"""

    def write(self, s):  # noqa: D401
        return len(s)

    def flush(self):
        pass


def _harden_streams() -> None:
    """窗口化打包后 sys.stdout/stderr 可能为 None，导致 print / loguru 默认 sink 崩溃。

    - 用空流兜底，避免任何 print 抛异常；
    - 移除 loguru 指向 None stderr 的默认 handler（运行分析时我们自带文件 + 内存 sink）。
    """
    if sys.stdout is None:
        sys.stdout = _NullStream()
    if sys.stderr is None:
        sys.stderr = _NullStream()
    if getattr(sys, "frozen", False):
        try:
            import loguru

            loguru.logger.remove()  # 去掉默认 stderr sink，改由运行控制器挂文件/内存 sink
        except Exception:  # noqa: BLE001
            pass


def _find_free_port() -> int:
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as s:
        s.bind(("127.0.0.1", 0))
        return s.getsockname()[1]


def _start_server(host: str, port: int):
    """在后台线程启动 uvicorn，返回 (server, thread)。"""
    import uvicorn

    from web.app import app

    config = uvicorn.Config(app, host=host, port=port, log_level="warning")
    server = uvicorn.Server(config)
    thread = threading.Thread(target=server.run, daemon=True, name="uvicorn")
    thread.start()
    # 等待服务就绪（最多 ~15s）
    for _ in range(150):
        if getattr(server, "started", False):
            break
        time.sleep(0.1)
    return server, thread


def main() -> None:
    _harden_streams()
    host = "127.0.0.1"
    port = _find_free_port()
    server, _thread = _start_server(host, port)
    url = f"http://{host}:{port}/"

    try:
        import webview  # 惰性导入：缺失时给出友好提示
    except ImportError:
        print("缺少 pywebview 依赖，请先安装：pip install pywebview", file=sys.stderr)
        print(f"（已启动内嵌服务，可临时用浏览器访问 {url}）", file=sys.stderr)
        try:
            while True:
                time.sleep(1)
        except KeyboardInterrupt:
            pass
        return

    webview.create_window(WINDOW_TITLE, url, width=1240, height=840, min_size=(960, 640))
    try:
        webview.start()  # 阻塞，直到窗口关闭
    finally:
        server.should_exit = True


if __name__ == "__main__":
    main()


