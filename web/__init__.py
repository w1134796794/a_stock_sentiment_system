"""
Web 层（P1）：FastAPI + Jinja2/HTMX 只读看板。

仅依赖快照产物（``snapshot.SnapshotReader``），不触碰分析流水线，
因此 Web 进程无需 tushare/akshare 等重依赖即可启动。
"""