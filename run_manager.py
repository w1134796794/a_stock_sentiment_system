"""桌面管理工具启动入口（双击即用，无需开浏览器）。

    python run_manager.py

打包后即为单个可执行的「A股情绪系统 管理工具」exe，见 packaging/manager.spec。
"""
from desktop.manager import main

if __name__ == "__main__":
    main()