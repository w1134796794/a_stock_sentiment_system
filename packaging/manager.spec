# -*- mode: python ; coding: utf-8 -*-
"""PyInstaller 打包配置：把桌面管理工具打成单目录(onedir)可执行程序。

构建：
    python scripts/build_manager.py
或：
    pyinstaller packaging/manager.spec --noconfirm

产物：dist/A股情绪系统管理工具/  （内含 exe 与 _internal 依赖，整目录分发）

说明：
  - 运行分析(main.py) 在工具进程内惰性导入，故把分析栈(pandas/tushare/akshare)
    一并打入；这会让产物较大，构建机需先 `pip install -r requirements.txt`。
  - 打包后 settings.BASE_DIR 解析为 exe 所在目录，请把工具放在项目数据目录旁
    （webdata / logs / output / dragon_pools.json / .env）。
"""
import os

from PyInstaller.utils.hooks import collect_all, collect_submodules

ROOT = os.path.abspath(os.path.join(os.path.dirname(SPEC), ".."))  # noqa: F821 (SPEC 由 PyInstaller 注入)

datas = []
binaries = []
hiddenimports = ["main"]

# 内嵌页面：模板 + 静态资源
datas += [(os.path.join(ROOT, "web", "templates"), os.path.join("web", "templates"))]
_static = os.path.join(ROOT, "web", "static")
if os.path.isdir(_static):
    datas += [(_static, os.path.join("web", "static"))]

# 配置目录：param_docs 运行时读取 config/*.py、config/**/*.yaml 抽取中文说明
datas += [(os.path.join(ROOT, "config"), "config")]

# 第三方库（含数据文件 / 隐藏子模块）
for pkg in ("webview", "uvicorn", "fastapi", "jinja2", "loguru"):
    try:
        d, b, h = collect_all(pkg)
        datas += d
        binaries += b
        hiddenimports += h
    except Exception:
        pass

# 分析栈（惰性导入，需显式带入）
for pkg in ("akshare", "tushare", "pandas", "numpy", "openpyxl", "xlsxwriter"):
    try:
        hiddenimports += collect_submodules(pkg)
    except Exception:
        pass


a = Analysis(
    [os.path.join(ROOT, "run_manager.py")],
    pathex=[ROOT],
    binaries=binaries,
    datas=datas,
    hiddenimports=hiddenimports,
    hookspath=[],
    hooksconfig={},
    runtime_hooks=[],
    excludes=["tkinter", "matplotlib.tests", "pytest"],
    noarchive=False,
)
pyz = PYZ(a.pure)

exe = EXE(
    pyz,
    a.scripts,
    [],
    exclude_binaries=True,
    name="A股情绪系统管理工具",
    debug=False,
    bootloader_ignore_signals=False,
    strip=False,
    upx=True,
    console=False,
    disable_windowed_traceback=False,
    icon=None,
)

coll = COLLECT(
    exe,
    a.binaries,
    a.datas,
    strip=False,
    upx=True,
    upx_exclude=[],
    name="A股情绪系统管理工具",
)

