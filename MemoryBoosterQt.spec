# -*- mode: python ; coding: utf-8 -*-
"""PySide6 版打包配置：单文件、无控制台、裁剪 Qt 未用模块。

体积说明：Qt 运行库是硬成本。相比 Tk 版（7MB），PySide6 版即使裁到极限
也在 25~35MB 量级 —— 换来的是矢量渲染、抗锯齿、原生高 DPI 与动画能力。
"""

EXCLUDES = [
    # 不打包 Tk（引擎模块里有 try/except 兜底，导入失败会自动降级）
    "tkinter", "_tkinter",
    # 通用裁剪
    "numpy", "pandas", "scipy", "matplotlib", "PIL", "customtkinter", "darkdetect",
    "PyQt5", "PyQt6", "PySide2", "wx", "IPython", "jinja2",
    "setuptools", "pkg_resources", "pip", "wheel",
    "unittest", "doctest", "pydoc", "lib2to3", "sqlite3", "test",
    "email", "http", "xmlrpc", "multiprocessing",
    "bz2", "lzma", "_bz2", "_lzma",
    # Qt 里本程序完全用不到的模块（保留 Core/Gui/Widgets）
    "PySide6.QtNetwork", "PySide6.QtQml", "PySide6.QtQuick", "PySide6.QtQuickWidgets",
    "PySide6.QtQuickControls2", "PySide6.QtWebEngineCore", "PySide6.QtWebEngineWidgets",
    "PySide6.QtWebChannel", "PySide6.QtWebSockets", "PySide6.QtMultimedia",
    "PySide6.QtMultimediaWidgets", "PySide6.QtCharts", "PySide6.QtDataVisualization",
    "PySide6.QtSql", "PySide6.QtTest", "PySide6.QtOpenGL", "PySide6.QtOpenGLWidgets",
    "PySide6.QtSvg", "PySide6.QtSvgWidgets", "PySide6.QtPrintSupport",
    "PySide6.QtDBus", "PySide6.QtXml", "PySide6.QtConcurrent", "PySide6.QtHelp",
    "PySide6.QtUiTools", "PySide6.QtDesigner", "PySide6.QtPdf", "PySide6.QtPdfWidgets",
    "PySide6.QtSpatialAudio", "PySide6.QtTextToSpeech", "PySide6.QtSerialPort",
    "PySide6.QtBluetooth", "PySide6.QtNfc", "PySide6.QtPositioning", "PySide6.QtLocation",
    "PySide6.QtSensors", "PySide6.Qt3DCore", "PySide6.QtScxml", "PySide6.QtStateMachine",
    "PySide6.QtRemoteObjects", "PySide6.QtVirtualKeyboard",
]

# 二进制层面再兜一层：hook 有时会把上面 exclude 掉的 Qt DLL 仍然带进来
DROP_BIN = (
    "qt6quick", "qt6qml", "qt6webengine", "qt6webchannel", "qt6websockets",
    "qt6multimedia", "qt6charts", "qt6datavisualization", "qt6graphs",
    "qt63d", "qt6opengl", "qt6pdf", "qt6designer", "qt6uitools", "qt6sql",
    "qt6test", "qt6sensors", "qt6serialport", "qt6bluetooth", "qt6nfc",
    "qt6positioning", "qt6location", "qt6remoteobjects", "qt6scxml",
    "qt6statemachine", "qt6texttospeech", "qt6help", "qt6virtualkeyboard",
    "qt6labstexttospeech", "qt6labstemplates", "qt6labssharedimage",
    "qt6labssettings", "qt6labscalendar", "qt6labslocation", "qt6labsscxml",
    "qt6labsplatform", "qt6spatialaudio", "qt6ffmpeg", "qt6vulkan",
    "qt6iconbrowser", "qt6svg", "qt6network", "qt6dbus", "qt6xml",
    "qt6concurrent", "qt6printsupport",
)

# 非 Qt6 前缀、但同样纯属浪费的大块头：
#   opengl32sw 是 Qt 的软件 OpenGL 兜底（~20MB），本程序用 raster 引擎 +
#   QPainter 抗锯齿绘制，不需要 OpenGL；d3dcompiler 是 ANGLE 时代的遗留。
DROP_EXTRA = ("opengl32sw", "d3dcompiler_47")

# 只留需要的 Qt 插件
KEEP_PLUGINS = (
    "platforms/qwindows",              # 必须：Windows 平台插件
    "styles/qmodernwindowsstyle",      # QSS 绘制依赖的控件样式
    "imageformats/qico",               # 托盘/窗口图标
    "imageformats/qjpeg",
    "imageformats/qgif",
)

a = Analysis(
    ["memory_booster_qt.py"],
    pathex=[],
    binaries=[],
    datas=[],
    hiddenimports=["memory_booster", "app_icon"],
    hookspath=[],
    hooksconfig={},
    runtime_hooks=[],
    excludes=EXCLUDES,
    noarchive=False,
    optimize=0,
)

# 索引 0 是目标路径；保留 python 侧必要的 dll 与上面白名单里的插件
_keep = []


def _should_keep(dest):
    low = dest.replace("\\", "/").lower()
    name = low.rsplit("/", 1)[-1]
    if any(name.startswith(d) for d in DROP_EXTRA):
        return False
    if "pyside6/plugins/" in low:
        return any(k in low for k in KEEP_PLUGINS)
    if name.startswith("qt6"):
        return not any(name.startswith(d) for d in DROP_BIN)
    return True


a.binaries = [b for b in a.binaries if _should_keep(b[0])]
a.datas = [d for d in a.datas if _should_keep(d[0])]

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
