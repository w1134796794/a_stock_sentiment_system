"""桌面管理工具支撑包。

把原有 Web 看板包装成一个本机原生窗口（pywebview）+ 后台内嵌的 FastAPI 服务，
对用户表现为一个双击即用、无需开浏览器的 exe 工具。模块划分：

  runner.py   —— 在管理工具进程内跑「收盘分析」(main.py)，并把日志实时喂给界面
  status.py   —— 概览页的健康检查 / 关键产物统计
  manager.py  —— 启动内嵌服务并打开 pywebview 窗口（程序入口）
"""
