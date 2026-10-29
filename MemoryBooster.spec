# -*- mode: python ; coding: utf-8 -*-
"""PyInstaller 打包配置：单文件、无控制台、裁剪 tcl/tk 冗余数据。"""

# 去掉 Tcl/Tk 用不到的数据，显著减小体积
DROP = (
    "_tcl_data\\tzdata",
    "_tcl_data\\msgs",
    "_tk_data\\msgs",
    "_tk_data\\demos",
    "_tk_data\\images",
)

EXCLUDES = [
    "numpy", "pandas", "scipy", "matplotlib", "PIL", "customtkinter", "darkdetect",
    "PyQt5", "PyQt6", "PySide2", "PySide6", "wx",
    "setuptools", "pkg_resources", "pip", "wheel",
    "unittest", "doctest", "pydoc", "lib2to3", "sqlite3", "test", "tkinter.test",
    "email", "http", "xmlrpc", "multiprocessing",
    # 压缩后端（shutil/tempfile 顺带拉进来的），本程序用不到
    "bz2", "lzma", "_bz2", "_lzma",
]

a = Analysis(
    ["memory_booster.py"],
    pathex=[],
    binaries=[],
    # 图标已用 zlib+base64 内嵌在 app_icon.py，不再额外携带资源文件；
    # 这里的 icon= 只用于把图标写进 exe 自己的资源段（资源管理器/任务栏显示用）。
    datas=[],
    hiddenimports=["app_icon"],
    hookspath=[],
    hooksconfig={},
    runtime_hooks=[],
    excludes=EXCLUDES,
    noarchive=False,
    optimize=0,
)

a.datas = [d for d in a.datas if not any(d[0].lower().startswith(p) for p in DROP)]

pyz = PYZ(a.pure)

exe = EXE(
    pyz,
    a.scripts,
    a.binaries,
    a.datas,
    [],
    name="MemoryBooster",
    debug=False,
    bootloader_ignore_signals=False,
    strip=False,
    upx=False,
    console=False,
    disable_windowed_traceback=False,
    argv_emulation=False,
    target_arch=None,
    codesign_identity=None,
    entitlements_file=None,
    icon="assets/icon.ico",
)
