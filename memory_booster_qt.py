#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
memory_booster_qt.py —— 仿微软电脑管家「内存加速」· PySide6 重制版
=====================================================================
与 memory_booster.py 的关系：
  · 引擎层（Win32 调用 / 加速逻辑 / 配置 / 开机自启）**直接 import 复用**，
    一行没改；
  · UI 层用 Qt 全部重写：QPainter 矢量绘制 + 抗锯齿 + 圆头弧线 +
    缓动动画，替代 Tk 的手工像素画布。

为什么清晰度会有质变：
  · Tk 的 Canvas 是非抗锯齿的 GDI 位图绘制，1px 描边和中文小字会糊；
  · Qt 走 QPainter 矢量光栅化（默认 Per-Monitor V2 DPI 感知），125%/150%
    缩放下由 GPU/CPU 按真实设备像素重绘，线条和文字始终锐利；
  · 圆弧笔帽是 RoundCap，进度端点自然收圆，不再是 Tk 那种硬平头。

运行：
  python memory_booster_qt.py           # 图形界面
  python memory_booster_qt.py --tray    # 启动即隐藏到托盘（开机自启用）
  python memory_booster_qt.py --once    # 命令行单次加速
  python memory_booster_qt.py --auto    # 命令行后台自动加速
"""

import argparse
import ctypes
import math
import os
import sys
import threading
import time
from functools import lru_cache

# ---------------------------------------------------------------------------
# 引擎复用：同目录下的 memory_booster.py 提供全部底层能力
# ---------------------------------------------------------------------------
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import memory_booster as core  # noqa: E402

from PySide6.QtCore import (  # noqa: E402
    Property, QEasingCurve, QEvent, QPoint, QPointF, QPropertyAnimation, QRectF,
    QSize, Qt, QTimer, Signal, QObject,
)
from PySide6.QtGui import (  # noqa: E402
    QBrush, QColor, QConicalGradient, QFont, QFontMetricsF, QGuiApplication,
    QIcon, QPainter, QPainterPath, QPen, QLinearGradient,
    QRadialGradient,
)
from PySide6.QtWidgets import (  # noqa: E402
    QApplication, QMenu, QMessageBox, QSlider, QStackedWidget, QStyle,
    QStyleOptionSlider, QSystemTrayIcon, QWidget,
)

APP_NAME = core.APP_NAME
APP_VERSION = core.APP_VERSION

# 逻辑尺寸（Qt 会自动按 devicePixelRatio 放大成真实设备像素）
CORE_W, CORE_H = 400, 500
PAD = 16                      # 透明外边距，供手绘阴影
WIN_W, WIN_H = CORE_W + PAD * 2, CORE_H + PAD * 2
RADIUS = 18                   # 主容器圆角

HEADER_H = 44
TAB_H = 34
PAGE_TOP = 84
PAGE_H = CORE_H - PAGE_TOP    # 416

FAMILY = "Microsoft YaHei UI"

# ---------------------------------------------------------------------------
# 主题
#   色板参考现代设计系统的中性色阶（冷灰而非暖灰），accent 用更饱和的
#   皇家蓝并配一个浅一档的 accent2 做渐变，避免大面积纯色带来的塑料感。
# ---------------------------------------------------------------------------
THEMES = {}
for _k, _v in core.THEMES.items():
    _t = dict(_v)
    if _k == "light":
        _t.update({
            "bg": "#F5F6F8", "card": "#FFFFFF", "card_soft": "#FAFBFC",
            "border": "#E9ECF1", "hair": "#EFF2F6", "seg_bg": "#ECF0F5",
            "text": "#0F1521", "dim": "#45505F", "dim2": "#79879A",
            "accent": "#2F6BFF", "accent2": "#6E9BFF", "accent_hover": "#1F58E6",
            "on_accent": "#FFFFFF", "switch_off": "#CDD4DE",
            "ok_bg": "#E3F6E8", "ok_fg": "#0B7A3B",
            "warn": "#D97706", "bad": "#D93A2B",
            "ghost_hover": "#EEF1F5", "disabled": "#C8D0DA",
            "disabled_bg": "#E8ECF1", "disabled_fg": "#8E9AAB",
            "knob": "#FFFFFF", "shadow": "#0B1526", "glow": "#2F6BFF",
            "pressed": "#1A4FCC", "ring_track": "#E7EBF1", "hover_fill": "#F0F3F8",
            "ambient": "#2F6BFF", "hairline": "#DFE5EE", "pill": "#FFFFFF",
        })
    else:
        _t.update({
            "bg": "#14161B", "card": "#1E2129", "card_soft": "#191C23",
            "border": "#2B2F39", "hair": "#242832", "seg_bg": "#1A1D24",
            "text": "#F0F3F8", "dim": "#A6B1C0", "dim2": "#8791A0",
            "accent": "#5B8CFF", "accent2": "#8FB2FF", "accent_hover": "#7BA3FF",
            "on_accent": "#0A1220", "switch_off": "#3D4450",
            "ok_bg": "#13301F", "ok_fg": "#5FD98A",
            "warn": "#E8A33D", "bad": "#FF7A6B",
            "ghost_hover": "#262A33", "disabled": "#3D4450",
            "disabled_bg": "#2A2E37", "disabled_fg": "#7A8494",
            "knob": "#FFFFFF", "shadow": "#000000", "glow": "#5B8CFF",
            "pressed": "#3D6FE0", "ring_track": "#2A2F38", "hover_fill": "#262A33",
            "ambient": "#5B8CFF", "hairline": "#323744", "pill": "#2E3340",
        })
    THEMES[_k] = _t

BOLT = ((0.560, 0.250), (0.365, 0.538), (0.487, 0.538), (0.450, 0.748),
        (0.645, 0.452), (0.517, 0.452), (0.592, 0.250))


def qc(value, alpha=255) -> QColor:
    c = QColor(value)
    if alpha != 255:
        c.setAlpha(alpha)
    return c


def lerp(a: QColor, b: QColor, t: float) -> QColor:
    t = 0.0 if t < 0.0 else (1.0 if t > 1.0 else t)
    return QColor(
        round(a.red() + (b.red() - a.red()) * t),
        round(a.green() + (b.green() - a.green()) * t),
        round(a.blue() + (b.blue() - a.blue()) * t),
        round(a.alpha() + (b.alpha() - a.alpha()) * t),
    )


@lru_cache(maxsize=64)
def font(px: int, bold=False) -> QFont:
    """按字号缓存的 QFont。

    每帧绘制要取十几次字体，QFont 虽然是隐式共享的，但每次构造都要走一遍
    字体数据库查询。缓存之后动画循环里不再产生这些短命对象。
    """
    f = QFont(FAMILY)
    f.setPixelSize(max(10, int(px)))
    f.setWeight(QFont.Weight.DemiBold if bold else QFont.Weight.Normal)
    return f


@lru_cache(maxsize=64)
def fmetrics(px: float, bold=False) -> QFontMetricsF:
    """按字号缓存的文本度量（用字号作键，不依赖 QFont 是否可哈希）。"""
    return QFontMetricsF(font(px, bold))


def is_admin_cached() -> bool:
    """缓存提权判断：它要开一次进程令牌，放进 paintEvent 里就是每帧两次系统调用。"""
    global _ADMIN
    if _ADMIN is None:
        try:
            _ADMIN = bool(core.is_admin())
        except Exception:
            _ADMIN = False
    return _ADMIN


_ADMIN = None


# ---------------------------------------------------------------------------
# 跨线程桥（工作线程 -> UI 线程，Qt 自动排队投递）
# ---------------------------------------------------------------------------
class Bridge(QObject):
    boost_done = Signal(dict)
    boost_err = Signal(str)
    boost_progress = Signal(str, int, int)   # stage, done, total
    auto_trigger = Signal()
    autostart_state = Signal(bool)
    autostart_set = Signal(bool, bool, str)


# ---------------------------------------------------------------------------
# 自绘控件：开关
# ---------------------------------------------------------------------------
class Switch(QWidget):
    toggled = Signal(bool)

    def __init__(self, on: bool, win, parent=None):
        super().__init__(parent)
        self.win = win
        self.setFixedSize(44, 24)
        self.setCursor(Qt.CursorShape.PointingHandCursor)
        self._on = bool(on)
        self._k = 1.0 if on else 0.0
        self._anim = QPropertyAnimation(self, b"knob", self)
        self._anim.setDuration(170)
        self._anim.setEasingCurve(QEasingCurve.Type.OutCubic)

    def _get_k(self):
        return self._k

    def _set_k(self, v):
        self._k = float(v)
        self.update()

    knob = Property(float, _get_k, _set_k)

    def set_on(self, on: bool, animate=True, emit=False):
        on = bool(on)
        if on == self._on and not emit:
            return
        self._on = on
        if animate:
            self._anim.stop()
            self._anim.setStartValue(self._k)
            self._anim.setEndValue(1.0 if on else 0.0)
            self._anim.start()
        else:
            self._set_k(1.0 if on else 0.0)
        if emit:
            self.toggled.emit(on)

    def mouseReleaseEvent(self, e):
        if e.button() == Qt.MouseButton.LeftButton and self.rect().contains(e.position().toPoint()):
            self.set_on(not self._on, emit=True)

    def paintEvent(self, _e):
        T = self.win.T
        p = QPainter(self)
        p.setRenderHint(QPainter.RenderHint.Antialiasing, True)
        r = QRectF(0, 0, self.width(), self.height())
        off = qc(T["switch_off"])
        track = lerp(off, qc(T["accent"]), self._k)
        p.setPen(Qt.PenStyle.NoPen)
        p.setBrush(track)
        p.drawRoundedRect(r, r.height() / 2.0, r.height() / 2.0)

        d = r.height() - 6.0
        x = 3.0 + self._k * (r.width() - d - 6.0)
        knob = QRectF(x, 3.0, d, d)
        p.setBrush(qc(T["knob"]))
        p.drawEllipse(knob)


# ---------------------------------------------------------------------------
# 自绘控件：分段标签（带滑动指示器）
# ---------------------------------------------------------------------------
class Segmented(QWidget):
    changed = Signal(int)

    def __init__(self, items, index, win, parent=None, compact=False):
        super().__init__(parent)
        self.win = win
        self.items = list(items)
        self._idx = int(index)
        self._pos = float(index)
        # compact：设置页行内用的矮版（26px / 11.5px 字号），不撑满 TAB_H
        self._compact = bool(compact)
        self.setFixedHeight(26 if self._compact else TAB_H)
        self.setCursor(Qt.CursorShape.PointingHandCursor)
        self._hover = -1
        self.setMouseTracking(True)
        self._anim = QPropertyAnimation(self, b"indicator", self)
        self._anim.setDuration(220)
        self._anim.setEasingCurve(QEasingCurve.Type.OutCubic)

    def _get_pos(self):
        return self._pos

    def _set_pos(self, v):
        self._pos = float(v)
        self.update()

    indicator = Property(float, _get_pos, _set_pos)

    def set_index(self, i, animate=True):
        i = max(0, min(len(self.items) - 1, int(i)))
        if i == self._idx:
            return
        self._idx = i
        if animate:
            self._anim.stop()
            self._anim.setStartValue(self._pos)
            self._anim.setEndValue(float(i))
            self._anim.start()
        else:
            self._set_pos(float(i))

    def _index_at(self, x):
        seg = self.width() / float(len(self.items))
        i = int(x // seg)
        return max(0, min(len(self.items) - 1, i))

    def mouseMoveEvent(self, e):
        i = self._index_at(e.position().x())
        if i != self._hover:
            self._hover = i
            self.update()

    def leaveEvent(self, _e):
        self._hover = -1
        self.update()

    def mouseReleaseEvent(self, e):
        if e.button() != Qt.MouseButton.LeftButton:
            return
        i = self._index_at(e.position().x())
        if i != self._idx:
            self.set_index(i)
            self.changed.emit(i)

    def paintEvent(self, _e):
        T = self.win.T
        p = QPainter(self)
        p.setRenderHint(QPainter.RenderHint.Antialiasing, True)
        r = QRectF(0, 0, self.width(), self.height())
        rad = r.height() / 2.0
        p.setPen(Qt.PenStyle.NoPen)
        p.setBrush(qc(T["seg_bg"]))
        p.drawRoundedRect(r, rad, rad)

        seg = r.width() / float(len(self.items))
        inset = 2.0 if self._compact else 3.0
        hx = self._pos * seg + inset
        pill = QRectF(hx, inset, seg - inset * 2, r.height() - inset * 2)

        # 胶囊：先一层 1px 淡投影再叠白面，比直接描边更有"浮起"的感觉
        sh = QColor(T["shadow"])
        sh.setAlpha(18)
        p.setBrush(Qt.BrushStyle.NoBrush)
        p.setPen(QPen(sh, 1.2))
        p.drawRoundedRect(pill.adjusted(0, 0.8, 0, 0.8),
                          pill.height() / 2.0, pill.height() / 2.0)
        p.setPen(Qt.PenStyle.NoPen)
        p.setBrush(qc(T["pill"]))
        p.drawRoundedRect(pill, pill.height() / 2.0, pill.height() / 2.0)

        for i, label in enumerate(self.items):
            sel = i == self._idx
            if sel:
                col = qc(T["accent"])
            elif i == self._hover:
                col = qc(T["text"])
            else:
                col = qc(T["dim"])
            p.setPen(col)
            p.setFont(font(11.5 if self._compact else 12.5, bold=sel))
            p.drawText(QRectF(i * seg, 0, seg, r.height()),
                       int(Qt.AlignmentFlag.AlignCenter), label)


# ---------------------------------------------------------------------------
# 自绘控件：主按钮（颜色可缓动，支持忙碌/结果态）
# ---------------------------------------------------------------------------
class FlatButton(QWidget):
    clicked = Signal()

    def __init__(self, text, win, parent=None):
        super().__init__(parent)
        self.win = win
        self.text = text
        self.check = False      # 是否在文字前画一个矢量对勾
        self.enabled = True
        self._hover = False
        self._down = False
        self.setCursor(Qt.CursorShape.PointingHandCursor)
        T = win.T
        self._bg = qc(T["accent"])
        self._fg = qc(T["on_accent"])
        self._anim = QPropertyAnimation(self, b"bg", self)
        self._anim.setDuration(200)
        self._anim.setEasingCurve(QEasingCurve.Type.OutCubic)

    def _get_bg(self):
        return self._bg

    def _set_bg(self, c):
        self._bg = QColor(c)
        self.update()

    bg = Property(QColor, _get_bg, _set_bg)

    def set_colors(self, bg: QColor, fg: QColor, animate=True):
        self._fg = QColor(fg)
        # 必须先停掉上一个动画：否则旧动画仍在把 _bg 往老目标插值，
        # 会把新设的颜色直接覆盖掉（表现为"点了变绿又跳回蓝"）。
        self._anim.stop()
        if animate:
            self._anim.setStartValue(self._bg)
            self._anim.setEndValue(QColor(bg))
            self._anim.start()
        else:
            self._set_bg(QColor(bg))
        self.update()

    def reset_colors(self):
        T = self.win.T
        self.set_colors(qc(T["accent"]), qc(T["on_accent"]))

    def set_enabled_look(self, on: bool):
        self.enabled = bool(on)
        T = self.win.T
        if on:
            self.setCursor(Qt.CursorShape.PointingHandCursor)
            self.reset_colors()
        else:
            self.setCursor(Qt.CursorShape.ArrowCursor)
            self._fg = qc(T["disabled_fg"])
            self.set_colors(qc(T["disabled_bg"]), qc(T["disabled_fg"]))

    def enterEvent(self, _e):
        self._hover = True
        self.update()

    def leaveEvent(self, _e):
        self._hover = False
        self._down = False
        self.update()

    def mousePressEvent(self, e):
        if e.button() == Qt.MouseButton.LeftButton and self.enabled:
            self._down = True
            self.update()

    def mouseReleaseEvent(self, e):
        was = self._down
        self._down = False
        self.update()
        if was and self.enabled and self.rect().contains(e.position().toPoint()):
            self.clicked.emit()

    def set_label(self, text, check=False):
        """改按钮文字；check=True 时在文字前画矢量对勾。

        对勾必须画出来而不是打 "✓" 字符：微软雅黑里没有 U+2713，
        Qt 会为此遍历整个系统字体库（实测打开 222 个字体文件、428 个句柄、
        多吃 17 MB 私有内存），而且要一直挂着不释放。
        """
        self.text = text
        self.check = bool(check)
        self.update()

    def paintEvent(self, _e):
        T = self.win.T
        p = QPainter(self)
        p.setRenderHint(QPainter.RenderHint.Antialiasing, True)
        r = QRectF(0, 0, self.width(), self.height())
        bg = QColor(self._bg)
        if self.enabled:
            if self._down:
                bg = bg.darker(110)
            elif self._hover:
                bg = bg.lighter(106)

        # 竖向渐变 + 顶部内高光：纯色大按钮会显得塑料，一层很轻的明暗就够
        g = QLinearGradient(r.topLeft(), r.bottomLeft())
        g.setColorAt(0.0, lerp(bg, QColor(255, 255, 255), 0.13))
        g.setColorAt(0.58, bg)
        g.setColorAt(1.0, lerp(bg, QColor(0, 0, 0), 0.05))
        p.setPen(Qt.PenStyle.NoPen)
        p.setBrush(QBrush(g))
        p.drawRoundedRect(r, 14.0, 14.0)

        hi = QColor(255, 255, 255)
        hi.setAlpha(48 if self.enabled else 18)
        p.setPen(QPen(hi, 1.0))
        p.setBrush(Qt.BrushStyle.NoBrush)
        p.drawLine(QPointF(15.0, 1.0), QPointF(r.width() - 15.0, 1.0))

        f = font(14.5, bold=True)
        p.setFont(f)
        p.setPen(self._fg)
        if not self.check:
            p.drawText(r, int(Qt.AlignmentFlag.AlignCenter), self.text)
            return

        # 对勾 + 文字作为一个整体居中
        tw = fmetrics(14.5, True).horizontalAdvance(self.text)
        gw, gap = 14.0, 6.0
        x0 = r.center().x() - (gw + gap + tw) / 2.0
        cy = r.center().y()
        p.setPen(QPen(self._fg, 2.0, Qt.PenStyle.SolidLine,
                      Qt.PenCapStyle.RoundCap, Qt.PenJoinStyle.RoundJoin))
        p.setBrush(Qt.BrushStyle.NoBrush)
        tick = QPainterPath()
        tick.moveTo(x0, cy + 0.5)
        tick.lineTo(x0 + gw * 0.36, cy + gw * 0.40)
        tick.lineTo(x0 + gw, cy - gw * 0.42)
        p.drawPath(tick)
        p.drawText(QRectF(x0 + gw + gap, 0.0, tw + 2.0, r.height()),
                   int(Qt.AlignmentFlag.AlignLeft | Qt.AlignmentFlag.AlignVCenter), self.text)


# ---------------------------------------------------------------------------
# 自绘控件：次要按钮（描边）
# ---------------------------------------------------------------------------
class GhostButton(QWidget):
    clicked = Signal()

    def __init__(self, text, win, parent=None):
        super().__init__(parent)
        self.win = win
        self.text = text
        self._hover = False
        self.setCursor(Qt.CursorShape.PointingHandCursor)

    def set_text(self, t):
        self.text = t
        self.update()

    def enterEvent(self, _e):
        self._hover = True
        self.update()

    def leaveEvent(self, _e):
        self._hover = False
        self.update()

    def mouseReleaseEvent(self, e):
        if e.button() == Qt.MouseButton.LeftButton and self.rect().contains(e.position().toPoint()):
            self.clicked.emit()

    def paintEvent(self, _e):
        T = self.win.T
        p = QPainter(self)
        p.setRenderHint(QPainter.RenderHint.Antialiasing, True)
        r = QRectF(0.5, 0.5, self.width() - 1.0, self.height() - 1.0)
        p.setPen(QPen(qc(T["hairline"]), 1.0))
        p.setBrush(qc(T["hover_fill"]) if self._hover else qc(T["card"]))
        p.drawRoundedRect(r, 12.0, 12.0)
        p.setPen(qc(T["text"]))
        p.setFont(font(13))
        p.drawText(self.rect(), int(Qt.AlignmentFlag.AlignCenter), self.text)


# ---------------------------------------------------------------------------
# 自绘控件：数值微调（[-] 值 [+]）
# ---------------------------------------------------------------------------
class Stepper(QWidget):
    def __init__(self, value, lo, hi, win, parent=None):
        super().__init__(parent)
        self.win = win
        self.value = int(value)
        self.lo, self.hi = int(lo), int(hi)
        self.setFixedSize(104, 26)
        self._hover = 0       # -1 减号, 1 加号, 0 无
        self.setMouseTracking(True)
        self.setCursor(Qt.CursorShape.PointingHandCursor)

    def _zone(self, x):
        w = self.width()
        if x < w * 0.32:
            return -1
        if x > w * 0.68:
            return 1
        return 0

    def mouseMoveEvent(self, e):
        z = self._zone(e.position().x())
        if z != self._hover:
            self._hover = z
            self.update()

    def leaveEvent(self, _e):
        self._hover = 0
        self.update()

    def mouseReleaseEvent(self, e):
        if e.button() != Qt.MouseButton.LeftButton:
            return
        z = self._zone(e.position().x())
        if z == 0:
            return
        self.value = max(self.lo, min(self.hi, self.value + z))
        self.update()
        self.win.on_check_interval(self.value)

    def paintEvent(self, _e):
        T = self.win.T
        p = QPainter(self)
        p.setRenderHint(QPainter.RenderHint.Antialiasing, True)
        r = QRectF(0.5, 0.5, self.width() - 1.0, self.height() - 1.0)
        p.setPen(QPen(qc(T["border"]), 1.0))
        p.setBrush(qc(T["card"]))
        p.drawRoundedRect(r, 7.0, 7.0)

        cy = self.height() / 2.0
        for z, x0, x1 in ((-1, 0.0, self.width() * 0.32), (1, self.width() * 0.68, float(self.width()))):
            if self._hover == z:
                p.setPen(Qt.PenStyle.NoPen)
                p.setBrush(qc(T["hover_fill"]))
                p.drawRoundedRect(QRectF(x0 + 1, 1, x1 - x0 - 2, self.height() - 2), 6.0, 6.0)
        # 减号 / 加号
        p.setPen(QPen(qc(T["dim"]), 1.4, Qt.PenStyle.SolidLine, Qt.PenCapStyle.RoundCap))
        cx1 = self.width() * 0.16
        p.drawLine(QPointF(cx1 - 5, cy), QPointF(cx1 + 5, cy))
        cx2 = self.width() * 0.84
        p.drawLine(QPointF(cx2 - 5, cy), QPointF(cx2 + 5, cy))
        p.drawLine(QPointF(cx2, cy - 5), QPointF(cx2, cy + 5))
        # 数值
        p.setPen(qc(T["text"]))
        p.setFont(font(12.5, bold=True))
        p.drawText(QRectF(self.width() * 0.28, 0, self.width() * 0.44, self.height()),
                   int(Qt.AlignmentFlag.AlignCenter), "%d 秒" % self.value)


# ---------------------------------------------------------------------------
# 自绘控件：圆环
# ---------------------------------------------------------------------------
class RingView(QWidget):
    """内存占用圆环。

    语义边界（重要）：这个环永远只表示**真实内存占用**，不承担"处理进度"。
    所以点击加速时它不会冲到 100%——那等于告诉用户"内存占用 100%"，是假数据。

    "正在处理"由另一条独立的旋转弧表达：它绕圈不停转、没有终点，
    是 indeterminate 语义；同时中心副标题显示真实阶段（已处理 N / 共 M 个进程）。
    处理结束后环从旧值平滑落到新值，数字跟着滚，再走一圈涟漪收尾。
    """
    R = 62.0
    LW = 9.5
    CY = 92.0

    def __init__(self, win, parent=None):
        super().__init__(parent)
        self.win = win
        self.pct = 0.0        # 当前显示值（动画逼近的真实占用）
        self.boost_a = 0.0    # 加速中权重 0~1
        self.spin = 0.0       # 扫描弧旋转相位（度）
        self.ripple = 1.0     # 完成涟漪 0~1，1 表示已消失
        self.label = "内存占用"  # 中心副标题
        self.setAttribute(Qt.WidgetAttribute.WA_TranslucentBackground, True)

    def pct_color(self) -> QColor:
        T = self.win.T
        if self.pct >= 85:
            return qc(T["bad"])
        if self.pct >= 70:
            return qc(T["warn"])
        return qc(T["accent"])

    def paintEvent(self, _e):
        T = self.win.T
        p = QPainter(self)
        p.setRenderHint(QPainter.RenderHint.Antialiasing, True)
        cx, cy = self.width() / 2.0, self.CY
        box = QRectF(cx - self.R, cy - self.R, self.R * 2.0, self.R * 2.0)

        # 加速中把颜色压向主题蓝：此时环值在下落，不该因为"曾经很高"而留红
        k = min(1.0, self.boost_a * 1.6)
        col = lerp(self.pct_color(), qc(T["accent"]), k)

        # 轨道
        p.setPen(QPen(qc(T["ring_track"]), self.LW, Qt.PenStyle.SolidLine,
                      Qt.PenCapStyle.RoundCap))
        p.setBrush(Qt.BrushStyle.NoBrush)
        p.drawArc(box, 90 * 16, 360 * 16)

        span = -min(359.99, 3.6 * max(0.0, min(100.0, self.pct)))

        # 柔光层（加速中才出现，随呼吸相位微动）
        if self.boost_a > 0.02:
            breath = 0.55 + 0.45 * (0.5 + 0.5 * math.sin(self.win._pulse))
            glow = QColor(col)
            glow.setAlpha(int(30 * self.boost_a * breath))
            p.setPen(QPen(glow, self.LW + 10.0, Qt.PenStyle.SolidLine,
                          Qt.PenCapStyle.RoundCap))
            p.drawArc(box, 90 * 16, int(span * 16))

        if abs(span) > 0.05:
            if abs(span) >= 359.9:
                # 满环直接用椭圆描边：arc 走满一圈时两个圆头会在 12 点
                # 重叠，叠出一条亮斑，椭圆没有这个接缝问题
                p.setPen(QPen(col, self.LW))
                p.setBrush(Qt.BrushStyle.NoBrush)
                p.drawEllipse(box)
            else:
                # 进度弧：起点稍亮的渐变，制造"流动"感
                grad = QLinearGradient(cx - self.R, cy - self.R, cx + self.R, cy + self.R)
                head = lerp(col, QColor(255, 255, 255), 0.16)
                grad.setColorAt(0.0, head)
                grad.setColorAt(1.0, col)
                p.setPen(QPen(QBrush(grad), self.LW, Qt.PenStyle.SolidLine,
                              Qt.PenCapStyle.RoundCap))
                p.drawArc(box, 90 * 16, int(span * 16))

        # ---- 处理中：环外一圈更细的扫描弧（indeterminate，绕圈不停）----
        # 刻意放在环外侧、比环更细：它表示"正在处理"，和环表示的"占用多少"
        # 是两件事，画在同一圈上会让人误读成"进度快满了"。
        if self.boost_a > 0.02:
            w = self.boost_a
            r2 = self.R + 17.0
            box2 = QRectF(cx - r2, cy - r2, r2 * 2.0, r2 * 2.0)
            span_deg = 76.0
            start = 90.0 - self.spin - span_deg
            cg = QConicalGradient(QPointF(cx, cy), start)
            cg.setColorAt(0.0, qc(T["accent"], 0))
            cg.setColorAt(0.09, qc(T["accent"], int(60 * w)))
            cg.setColorAt(0.185, qc(T["accent"], int(240 * w)))
            cg.setColorAt(0.212, qc(T["accent"], 0))
            cg.setColorAt(1.0, qc(T["accent"], 0))
            p.setPen(QPen(QBrush(cg), 3.0, Qt.PenStyle.SolidLine,
                          Qt.PenCapStyle.RoundCap))
            p.drawArc(box2, int(start * 16), int(span_deg * 16))

        # ---- 完成涟漪：一圈从环外扩散并淡出的描边 ----
        if self.ripple < 1.0:
            e = self.ripple
            rr = self.R + 8.0 + e * 30.0
            rc = QColor(T["accent"])
            rc.setAlpha(int(62 * (1.0 - e) ** 1.5))
            p.setPen(QPen(rc, 1.6))
            p.setBrush(Qt.BrushStyle.NoBrush)
            p.drawEllipse(QRectF(cx - rr, cy - rr, rr * 2.0, rr * 2.0))

        # ---- 中心：大号数字 + 小号 % 单位 ----
        num = "%.0f" % max(0.0, min(100.0, self.pct))
        f_big = font(40, bold=True)
        f_unit = font(15, bold=True)
        w_num = fmetrics(40, True).horizontalAdvance(num)
        w_unit = fmetrics(15, True).horizontalAdvance("%")
        total = w_num + 2.0 + w_unit
        x0 = cx - total / 2.0
        h = 58.0
        top = cy - h / 2.0 - 4.0

        p.setFont(f_big)
        p.setPen(col)
        p.drawText(QRectF(x0, top, w_num + 1.0, h),
                   int(Qt.AlignmentFlag.AlignLeft | Qt.AlignmentFlag.AlignVCenter), num)

        unit_col = QColor(col)
        unit_col.setAlpha(180)
        p.setFont(f_unit)
        p.setPen(unit_col)
        p.drawText(QRectF(x0 + w_num + 2.0, top + 7.0, w_unit + 2.0, h),
                   int(Qt.AlignmentFlag.AlignLeft | Qt.AlignmentFlag.AlignVCenter), "%")

        # 副标题：默认的「内存占用」够短，放环内；加速阶段 / 结果文案更长，
        # 放环内会被圆环压掉两头，所以移到环下方
        outside = self.label != "内存占用"
        ly = (cy + self.R + 13.0) if outside else (cy + 26.0)
        p.setPen(qc(T["accent"]) if self.boost_a > 0.4 else qc(T["dim2"]))
        p.setFont(font(11.5))
        p.drawText(QRectF(cx - 130, ly, 260, 20),
                   int(Qt.AlignmentFlag.AlignCenter), self.label)


# ---------------------------------------------------------------------------
# 圆形关闭按钮
# ---------------------------------------------------------------------------
class CloseButton(QWidget):
    def __init__(self, win, parent=None):
        super().__init__(parent)
        self.win = win
        self._hover = False
        self.setFixedSize(30, 30)
        self.setCursor(Qt.CursorShape.PointingHandCursor)
        self.setToolTip("隐藏到托盘")

    def enterEvent(self, _e):
        self._hover = True
        self.update()

    def leaveEvent(self, _e):
        self._hover = False
        self.update()

    def mouseReleaseEvent(self, e):
        if e.button() == Qt.MouseButton.LeftButton:
            self.win.close_or_hide()

    def paintEvent(self, _e):
        T = self.win.T
        p = QPainter(self)
        p.setRenderHint(QPainter.RenderHint.Antialiasing, True)
        if self._hover:
            p.setPen(Qt.PenStyle.NoPen)
            p.setBrush(qc(T["hover_fill"]))
            p.drawEllipse(QRectF(0, 0, self.width(), self.height()))
        col = qc(T["text"]) if self._hover else qc(T["dim"])
        p.setPen(QPen(col, 1.4, Qt.PenStyle.SolidLine, Qt.PenCapStyle.RoundCap))
        c = self.width() / 2.0
        d = 5.0
        p.drawLine(QPointF(c - d, c - d), QPointF(c + d, c + d))
        p.drawLine(QPointF(c + d, c - d), QPointF(c - d, c + d))


# ---------------------------------------------------------------------------
# 页面：加速
# ---------------------------------------------------------------------------
class BoostPage(QWidget):
    PAD_X = 32          # 内容区左右边距（与其它页卡片内文字对齐）
    BAR_Y = 217         # 内存构成条

    def __init__(self, win, parent=None):
        super().__init__(parent)
        self.win = win
        self.setAttribute(Qt.WidgetAttribute.WA_TranslucentBackground, True)

        self.ring = RingView(win, self)
        self.ring.setGeometry(int(CORE_W / 2 - 110), 0, 220, 200)

        self.btn = FlatButton("一键加速", win, self)
        self.btn.setGeometry(44, 236, CORE_W - 88, 50)
        self.btn.clicked.connect(win.on_boost)

    def paintEvent(self, _e):
        T = self.win.T
        p = QPainter(self)
        p.setRenderHint(QPainter.RenderHint.Antialiasing, True)

        x0, x1 = self.PAD_X, CORE_W - self.PAD_X
        m = self.win.mem

        # ---- 已用 / 总量（左） + 可用（右） ----
        p.setPen(qc(T["dim"]))
        p.setFont(font(11.5))
        p.drawText(QRectF(x0, 194, 220, 18),
                   int(Qt.AlignmentFlag.AlignLeft | Qt.AlignmentFlag.AlignVCenter),
                   "已用 %.2f GB / %.1f GB" % (m["in_use_gb"], m["total_gb"]))
        p.setPen(qc(T["dim2"]))
        p.drawText(QRectF(x1 - 160, 194, 160, 18),
                   int(Qt.AlignmentFlag.AlignRight | Qt.AlignmentFlag.AlignVCenter),
                   "可用 %.2f GB" % m["avail_gb"])

        # ---- 内存构成条：比一行文字直观，一眼看出"满到什么程度" ----
        bar = QRectF(x0, self.BAR_Y, x1 - x0, 6.0)
        p.setPen(Qt.PenStyle.NoPen)
        p.setBrush(qc(T["ring_track"]))
        p.drawRoundedRect(bar, 3.0, 3.0)

        ratio = max(0.0, min(1.0, (m["percent"] or 0.0) / 100.0))
        fw = bar.width() * ratio
        if fw > 1.0:
            clip = QPainterPath()
            clip.addRoundedRect(QRectF(bar.x(), bar.y(), fw, bar.height()), 3.0, 3.0)
            p.save()
            p.setClipPath(clip)
            g = QLinearGradient(bar.x(), 0, bar.right(), 0)
            g.setColorAt(0.0, qc(T["accent2"]))
            g.setColorAt(1.0, qc(T["accent"]))
            p.fillRect(bar, QBrush(g))
            p.restore()

        # ---- 三格统计 ----
        res = self.win.last_res
        labels = ("释放", "修剪进程", "临时文件")
        gap = 10.0
        cw = (x1 - x0 - gap * 2) / 3.0
        top, ch = 298.0, 60.0
        for i, label in enumerate(labels):
            cx0 = x0 + i * (cw + gap)
            p.setPen(Qt.PenStyle.NoPen)
            p.setBrush(qc(T["card"]))
            p.drawRoundedRect(QRectF(cx0, top, cw, ch), 13.0, 13.0)
            p.setPen(QPen(qc(T["hair"]), 1.0))
            p.setBrush(Qt.BrushStyle.NoBrush)
            p.drawRoundedRect(QRectF(cx0 + 0.5, top + 0.5, cw - 1.0, ch - 1.0), 13.0, 13.0)

            if res:
                val = ("%.2f GB" % res["freed_gb"], "%d" % res["trim_count"],
                       "%d" % res["temp_removed"])[i]
                p.setPen(qc(T["accent"]) if i == 0 else qc(T["text"]))
                p.setFont(font(16, bold=True))
            else:
                val = "—"
                p.setPen(qc(T["dim2"]))
                p.setFont(font(16, bold=True))
            p.drawText(QRectF(cx0, top + 11, cw, 24),
                       int(Qt.AlignmentFlag.AlignCenter), val)

            p.setPen(qc(T["dim2"]))
            p.setFont(font(10.5))
            p.drawText(QRectF(cx0, top + 37, cw, 16),
                       int(Qt.AlignmentFlag.AlignCenter), label)

        # ---- 上次记录 ----
        if res:
            txt = "上次 %s · %s" % (res.get("time", "--:--:--"),
                                    "自动触发" if res.get("auto") else "手动加速")
        else:
            txt = "还没有加速记录"
        p.setPen(qc(T["dim2"]))
        p.setFont(font(11))
        p.drawText(QRectF(0, 366, CORE_W, 18),
                   int(Qt.AlignmentFlag.AlignCenter), txt)

        # ---- 底部说明 ----
        p.setPen(qc(T["dim2"]))
        p.setFont(font(10.5))
        p.drawText(QRectF(x0, 390, x1 - x0, 18),
                   int(Qt.AlignmentFlag.AlignHCenter) | int(Qt.AlignmentFlag.AlignVCenter),
                   "非分页池(NPP)内核内存无法回收，异常请用 RAMMap 排查")


# ---------------------------------------------------------------------------
# 页面：自动
# ---------------------------------------------------------------------------
class HSlider(QSlider):
    """整条高度都能点、能拖的滑块。

    原生 QSlider 的 mousePressEvent 有个 `groove.contains(pos)` 判断：只有按在
    轨道细条上才认。QSS 把轨道做成 4px、控件却有 26px，于是只有中间几像素能点，
    其余全是死区 —— 表现就是"点不中、拖动却正常"。
    这里自己算数值，视觉仍交给 QSS，轨道粗细不再影响可点范围。
    """

    _grab_dx = 0.0      # 按在手柄上时的抓取偏移，保证拖起来不跳

    def _handle_rect(self):
        opt = QStyleOptionSlider()
        self.initStyleOption(opt)
        return self.style().subControlRect(
            QStyle.ComplexControl.CC_Slider, opt,
            QStyle.SubControl.SC_SliderHandle, self)

    def _groove_rect(self):
        opt = QStyleOptionSlider()
        self.initStyleOption(opt)
        return self.style().subControlRect(
            QStyle.ComplexControl.CC_Slider, opt,
            QStyle.SubControl.SC_SliderGroove, self), opt

    def _value_at(self, x):
        groove, opt = self._groove_rect()
        hw = self._handle_rect().width()
        span = max(1, groove.width() - hw)
        # 减掉抓取偏移，让手柄中心跟住光标
        pos = int(round(x - self._grab_dx - groove.x() - hw / 2.0))
        pos = max(0, min(span, pos))
        return QStyle.sliderValueFromPosition(
            self.minimum(), self.maximum(), pos, span, opt.upsideDown)

    def mousePressEvent(self, e):
        if e.button() != Qt.MouseButton.LeftButton:
            super().mousePressEvent(e)
            return
        h = self._handle_rect()
        x = e.position().x()
        # 按在手柄上就记住偏移，拖起来不会瞬移；按在别处则让它直接跟手
        self._grab_dx = (x - h.center().x()) if h.contains(int(x), int(e.position().y())) else 0.0
        self.setSliderDown(True)
        self.setValue(self._value_at(x))
        e.accept()

    def mouseMoveEvent(self, e):
        if self.isSliderDown() and (e.buttons() & Qt.MouseButton.LeftButton):
            self.setValue(self._value_at(e.position().x()))
            e.accept()
            return
        super().mouseMoveEvent(e)

    def mouseReleaseEvent(self, e):
        if e.button() == Qt.MouseButton.LeftButton and self.isSliderDown():
            self.setSliderDown(False)
            self._grab_dx = 0.0
            e.accept()
            return
        super().mouseReleaseEvent(e)


class AutoPage(QWidget):
    def __init__(self, win, parent=None):
        super().__init__(parent)
        self.win = win
        self.setAttribute(Qt.WidgetAttribute.WA_TranslucentBackground, True)

        cfg = win.cfg
        self.sw = Switch(bool(cfg["auto_enabled"]), win, self)
        self.sw.setGeometry(CORE_W - 32 - 44, 20, 44, 24)
        self.sw.toggled.connect(win.on_auto_toggle)

        idx = 0 if cfg["threshold_mode"] == "gb" else 1
        self.seg = Segmented(("按已用内存 GB", "按占用率 %"), idx, win, self)
        self.seg.setGeometry(32, 122, CORE_W - 64, TAB_H)
        self.seg.changed.connect(win.on_mode_change)

        # 控件高度 26px 全都能点（HSlider 会把 y 折到轨道中心）；
        # y 比原来上移 2px，保证轨道中心线位置不变
        self.sl_th = HSlider(Qt.Orientation.Horizontal, self)
        self.sl_th.setGeometry(32, 170, CORE_W - 64, 26)
        self.sl_th.valueChanged.connect(win.on_threshold_slider)

        self.sl_int = HSlider(Qt.Orientation.Horizontal, self)
        self.sl_int.setGeometry(32, 294, CORE_W - 64, 26)
        self.sl_int.valueChanged.connect(win.on_interval_slider)


    def paintEvent(self, _e):
        T = self.win.T
        cfg = self.win.cfg
        p = QPainter(self)
        p.setRenderHint(QPainter.RenderHint.Antialiasing, True)

        def card(top, bot):
            p.setPen(Qt.PenStyle.NoPen)
            p.setBrush(qc(T["card"]))
            p.drawRoundedRect(QRectF(16, top, CORE_W - 32.0, bot - top), 14.0, 14.0)

        # 卡片1：总开关
        card(8, 76)
        p.setPen(qc(T["text"]))
        p.setFont(font(13, bold=True))
        p.drawText(QRectF(32, 20, 260, 20), int(Qt.AlignmentFlag.AlignLeft | Qt.AlignmentFlag.AlignVCenter),
                   "自动加速")
        p.setPen(qc(T["dim2"]))
        p.setFont(font(11.5))
        p.drawText(QRectF(32, 42, 280, 18), int(Qt.AlignmentFlag.AlignLeft | Qt.AlignmentFlag.AlignVCenter),
                   "内存超过阈值时在后台自动释放")

        # 卡片2：阈值
        card(88, 252)
        p.setPen(qc(T["text"]))
        p.setFont(font(13, bold=True))
        p.drawText(QRectF(32, 96, 160, 20), int(Qt.AlignmentFlag.AlignLeft | Qt.AlignmentFlag.AlignVCenter),
                   "触发阈值")
        vt = ("%.1f GB" % cfg["threshold_gb"]) if cfg["threshold_mode"] == "gb" \
            else ("%.0f%%" % cfg["threshold_percent"])
        p.setPen(qc(T["accent"]))
        p.setFont(font(13, bold=True))
        p.drawText(QRectF(CORE_W - 32 - 120, 96, 120, 20),
                   int(Qt.AlignmentFlag.AlignRight | Qt.AlignmentFlag.AlignVCenter), vt)

        lo, hi, _ = self.win.th_range()
        p.setPen(qc(T["dim2"]))
        p.setFont(font(11.5))
        p.drawText(QRectF(32, 196, 120, 18), int(Qt.AlignmentFlag.AlignLeft | Qt.AlignmentFlag.AlignVCenter),
                   self.win.fmt_th(lo))
        p.drawText(QRectF(CORE_W - 32 - 120, 196, 120, 18),
                   int(Qt.AlignmentFlag.AlignRight | Qt.AlignmentFlag.AlignVCenter),
                   self.win.fmt_th(hi))
        p.drawText(QRectF(32, 218, CORE_W - 64, 20), int(Qt.AlignmentFlag.AlignCenter),
                   self.win.th_hint())

        # 卡片3：最小间隔
        card(264, 356)
        p.setPen(qc(T["text"]))
        p.setFont(font(13, bold=True))
        p.drawText(QRectF(32, 276, 220, 20), int(Qt.AlignmentFlag.AlignLeft | Qt.AlignmentFlag.AlignVCenter),
                   "最小加速间隔")
        p.setPen(qc(T["accent"]))
        p.setFont(font(13, bold=True))
        p.drawText(QRectF(CORE_W - 32 - 120, 276, 120, 20),
                   int(Qt.AlignmentFlag.AlignRight | Qt.AlignmentFlag.AlignVCenter),
                   "%d 分钟" % (cfg["min_boost_interval_seconds"] // 60))
        p.setPen(qc(T["dim2"]))
        p.setFont(font(11.5))
        p.drawText(QRectF(32, 318, 120, 18), int(Qt.AlignmentFlag.AlignLeft | Qt.AlignmentFlag.AlignVCenter),
                   "1 分钟")
        p.drawText(QRectF(CORE_W - 32 - 120, 318, 120, 18),
                   int(Qt.AlignmentFlag.AlignRight | Qt.AlignmentFlag.AlignVCenter), "15 分钟")

        p.setPen(qc(T["dim2"]))
        p.setFont(font(11))
        p.drawText(QRectF(28, 372, CORE_W - 56, 36),
                   int(Qt.AlignmentFlag.AlignHCenter) | int(Qt.AlignmentFlag.AlignTop)
                   | int(Qt.TextFlag.TextWordWrap),
                   "自动加速先修剪进程再清缓存；手动点击不受该间隔限制")


# ---------------------------------------------------------------------------
# 页面：设置
# ---------------------------------------------------------------------------
class SettingsPage(QWidget):
    # 6 行 × 46 = 276，卡片 8..284；下面依次是关于卡 290..358、按钮 364..402（页高 416）
    ROW_H = 46
    TOP = 8
    ABOUT_TOP = 290
    ABOUT_BOT = 358
    BTN_Y = 364
    BTN_H = 38

    ROWS = (
        ("加速模式", "越激进释放越多，也可能越卡", "mode"),
        ("保护前台程序", "正在使用的窗口跳过修剪", "protect"),
        ("开机自启", "登录后自动启动（以托盘运行）", "start"),
        ("关闭窗口时最小化到托盘", "双击托盘图标可恢复窗口", "tray"),
        ("加速时清理临时文件", "只删除 %TEMP% 里 7 天前的文件", "temp"),
        ("后台检测间隔", "越小越灵敏，会略微增加 CPU 占用", None),
    )
    MODES = ("gentle", "balanced", "aggressive")
    MODE_LABELS = ("温和", "均衡", "激进")
    def __init__(self, win, parent=None):
        super().__init__(parent)
        self.win = win
        self.setAttribute(Qt.WidgetAttribute.WA_TranslucentBackground, True)

        self.switches = {}
        for i, (_t, _s, key) in enumerate(self.ROWS):
            if key in (None, "mode"):
                continue
            sw = Switch(self._value(key), win, self)
            sw.setGeometry(CORE_W - 32 - 44, self.TOP + i * self.ROW_H + 11, 44, 24)
            sw.toggled.connect(lambda v, k=key: win.on_setting_switch(k, v))
            self.switches[key] = sw

        self.seg_mode = Segmented(self.MODE_LABELS, self._mode_index(), win, self, compact=True)
        self.seg_mode.setGeometry(CORE_W - 32 - 156, self.TOP + 10, 156, 26)
        self.seg_mode.changed.connect(win.on_boost_mode)

        self.stepper = Stepper(int(win.cfg["check_interval_seconds"]), 2, 60, win, self)
        self.stepper.setGeometry(CORE_W - 32 - 104, self.TOP + 5 * self.ROW_H + 10, 104, 26)

        self.btn_theme = GhostButton(self._theme_label(), win, self)
        self.btn_theme.setGeometry(16, self.BTN_Y, CORE_W // 2 - 21, self.BTN_H)
        self.btn_theme.clicked.connect(win.toggle_theme)

        self.btn_quit = GhostButton("退出程序", win, self)
        self.btn_quit.setGeometry(CORE_W // 2 + 5, self.BTN_Y, CORE_W // 2 - 21, self.BTN_H)
        self.btn_quit.clicked.connect(win.quit_app)

    def _mode_index(self):
        mode = str(self.win.cfg.get("boost_mode", "gentle"))
        try:
            return self.MODES.index(mode)
        except ValueError:
            return 0

    def _mode_hint(self):
        """模式行的副标题随模式和权限变化：清缓存只有提权后才真的执行。"""
        T = self.win.T
        mode = str(self.win.cfg.get("boost_mode", "gentle"))
        admin = is_admin_cached()
        if mode == "aggressive":
            if admin:
                return "会清空缓存，提权后明显卡顿", T["bad"]
            return "会清空缓存，普通权限下不生效", T["dim2"]
        if mode == "balanced":
            return "先清缓存再修剪，轻微卡顿", T["warn"] if admin else T["dim2"]
        return "只修剪工作集，页面留在内存可复用", T["dim2"]

    def _value(self, key):
        cfg = self.win.cfg
        if key == "start":
            return bool(cfg.get("_autostart", False))
        if key == "tray":
            return bool(cfg["minimize_to_tray"])
        if key == "protect":
            return bool(cfg.get("protect_foreground", True))
        return bool(cfg["clean_temp"])

    def _theme_label(self):
        return "深色外观" if self.win.cfg["appearance"] == "light" else "浅色外观"

    def refresh(self):
        for key, sw in self.switches.items():
            sw.set_on(self._value(key), animate=True)
        idx = self._mode_index()
        if idx != self.seg_mode._idx:
            self.seg_mode.set_index(idx)
        self.btn_theme.set_text(self._theme_label())

    def paintEvent(self, _e):
        T = self.win.T
        p = QPainter(self)
        p.setRenderHint(QPainter.RenderHint.Antialiasing, True)

        # 主卡片
        p.setPen(Qt.PenStyle.NoPen)
        p.setBrush(qc(T["card"]))
        p.drawRoundedRect(QRectF(16, self.TOP, CORE_W - 32.0, len(self.ROWS) * self.ROW_H), 14.0, 14.0)

        for i, (title, sub, _k) in enumerate(self.ROWS):
            top = self.TOP + i * self.ROW_H
            sub_col = T["dim2"]
            if _k == "mode":
                sub, sub_col = self._mode_hint()
            p.setPen(qc(T["text"]))
            p.setFont(font(12.5, bold=True))
            p.drawText(QRectF(32, top + 5, 240, 18),
                       int(Qt.AlignmentFlag.AlignLeft | Qt.AlignmentFlag.AlignVCenter), title)
            p.setPen(qc(sub_col))
            p.setFont(font(11))
            p.drawText(QRectF(32, top + 24, 226, 17),
                       int(Qt.AlignmentFlag.AlignLeft | Qt.AlignmentFlag.AlignVCenter), sub)
            if i < len(self.ROWS) - 1:
                p.setPen(QPen(qc(T["hair"]), 1.0))
                p.drawLine(QPointF(32, top + self.ROW_H), QPointF(CORE_W - 32, top + self.ROW_H))

        # 关于卡（去掉了独立标题行，省下的高度留给上面的模式行）
        p.setPen(Qt.PenStyle.NoPen)
        p.setBrush(qc(T["card"]))
        p.drawRoundedRect(QRectF(16, self.ABOUT_TOP, CORE_W - 32.0, self.ABOUT_BOT - self.ABOUT_TOP),
                          14.0, 14.0)
        admin = "管理员（清缓存全生效）" if is_admin_cached() else "普通权限（清缓存会失败）"
        lines = (
            ("版本", "v%s · 仿微软电脑管家" % APP_VERSION),
            ("当前权限", admin),
            ("配置目录", "~/MemoryBooster/"),
        )
        for i, (k, v) in enumerate(lines):
            y = self.ABOUT_TOP + 8 + i * 19
            p.setPen(qc(T["dim2"]))
            p.setFont(font(11.5))
            p.drawText(QRectF(32, y, 80, 18), int(Qt.AlignmentFlag.AlignLeft | Qt.AlignmentFlag.AlignVCenter), k)
            p.setPen(qc(T["text"]))
            p.drawText(QRectF(80, y, CORE_W - 112, 18),
                       int(Qt.AlignmentFlag.AlignRight | Qt.AlignmentFlag.AlignVCenter), v)


# ---------------------------------------------------------------------------
# 主窗口
# ---------------------------------------------------------------------------
class MainWindow(QWidget):
    def __init__(self, start_hidden=False):
        super().__init__()
        self.cfg = core.load_config()
        self.T = THEMES.get(self.cfg.get("appearance", "light"), THEMES["light"])
        self.mem = self._safe_mem()
        self.last_res = None
        self.boosting = False
        self.last_auto_boost = 0.0
        self.start_hidden = start_hidden
        self._cfg_lock = threading.Lock()
        self._running = True
        self._save_timer = QTimer(self)
        self._save_timer.setSingleShot(True)
        self._save_timer.timeout.connect(self._save_now)

        self.setWindowFlags(Qt.WindowType.Window | Qt.WindowType.FramelessWindowHint)
        self.setAttribute(Qt.WidgetAttribute.WA_TranslucentBackground, True)
        self.setWindowTitle(APP_NAME)
        self.setFixedSize(WIN_W, WIN_H)
        self.setMouseTracking(True)

        icon_path = core.icon_file_path()
        if icon_path:
            self._icon = QIcon(icon_path)
            self.setWindowIcon(self._icon)
        else:
            self._icon = QIcon()

        # 子控件（坐标都加 PAD 偏移，让出阴影留白）
        self.btn_close = CloseButton(self, self)
        self.btn_close.move(PAD + CORE_W - 42, PAD + 7)

        self.seg = Segmented(("加速", "自动", "设置"), 0, self, self)
        self.seg.setGeometry(PAD + 16, PAD + 46, CORE_W - 32, TAB_H)
        self.seg.changed.connect(self._on_tab)

        self.stack = QStackedWidget(self)
        self.stack.setGeometry(PAD, PAD + PAGE_TOP, CORE_W, PAGE_H)
        self.stack.setAttribute(Qt.WidgetAttribute.WA_TranslucentBackground, True)

        self.page_boost = BoostPage(self, self.stack)
        self.page_auto = AutoPage(self, self.stack)
        self.page_set = SettingsPage(self, self.stack)
        for w in (self.page_boost, self.page_auto, self.page_set):
            w.setFixedSize(CORE_W, PAGE_H)
            self.stack.addWidget(w)
        self.sync_auto_controls()

        self._drag = None
        self._drag_lock_until = 0.0   # 托盘恢复后的拖拽冷却截止时刻
        self._ring_val = float(self.mem["percent"])
        self._boost_a = 0.0
        self._pulse = 0.0
        self._spin = 0.0          # 扫描弧相位（度）
        self._ripple = 1.0        # 完成涟漪进度，1 = 已消失
        self._stage_text = ""     # 加速中的真实阶段文字
        self._result_until = 0.0  # 结果态副标题显示到什么时候
        self._last_tick = time.perf_counter()

        # 桥接
        self.bridge = Bridge()
        self.bridge.boost_done.connect(self.on_boost_done)
        self.bridge.boost_err.connect(self.on_boost_error)
        self.bridge.boost_progress.connect(self.on_boost_progress)
        self.bridge.auto_trigger.connect(self.on_auto_trigger)
        self.bridge.autostart_state.connect(self.on_autostart_state)
        self.bridge.autostart_set.connect(self.on_autostart_set)

        # 定时器：先不启动，等窗口真正显示时由 showEvent 拉起。
        # --tray 启动的进程从不显示窗口，没必要空转一个渲染时钟。
        self._anim = QTimer(self)
        self._anim.setInterval(16)
        self._anim.timeout.connect(self._anim_step)

        self._tick_timer = QTimer(self)
        self._tick_timer.setInterval(1200)
        self._tick_timer.timeout.connect(self._refresh_mem)

        self._tray = None
        self._tray_menu = None
        # 每次启动只提示一次"已最小化到托盘"，否则每次点关闭都弹一条系统通知很吵。
        # 只存在内存里，不写配置 —— 下次启动会重新允许提示一次。
        self._tray_tip_shown = False
        QTimer.singleShot(200, self._init_tray)
        QTimer.singleShot(400, self._probe_autostart)
        self._monitor_thread = threading.Thread(target=self._monitor, daemon=True)
        self._monitor_thread.start()

    # ---- 基础 ----
    def _safe_mem(self):
        try:
            return core.read_memory()
        except Exception:
            return {"total": 0, "avail": 0, "in_use": 0, "in_use_gb": 0.0,
                    "total_gb": 0.0, "avail_gb": 0.0, "avail_mb": 0.0,
                    "percent": 0.0, "load": 0}

    def _place_bottom_right(self):
        scr = self.screen() or QGuiApplication.primaryScreen()
        area = scr.availableGeometry()
        m = 14
        x = area.right() + 1 - CORE_W - m - PAD
        y = area.bottom() + 1 - CORE_H - m - PAD
        self.move(max(area.left(), x), max(area.top(), y))

    def showEvent(self, e):
        super().showEvent(e)
        if not getattr(self, "_placed", False):
            self._placed = True
            self._place_bottom_right()
            self._round_corners_off()
        # 从托盘恢复时把渲染时钟接回去（隐藏时停掉了），并重置计时基准，
        # 否则第一帧的 dt 会是一个很大的值，动画会跳一下
        self._last_tick = time.perf_counter()
        for t in (getattr(self, "_anim", None), getattr(self, "_tick_timer", None)):
            if t is not None and not t.isActive():
                t.start()

    def _round_corners_off(self):
        """告诉 Win11 不要给窗口加系统圆角，避免和自绘 14px 圆角打架。"""
        try:
            hwnd = int(self.winId())
            val = ctypes.c_int(1)  # DWMWCP_DONOTROUND
            ctypes.windll.dwmapi.DwmSetWindowAttribute(
                ctypes.c_void_p(hwnd), ctypes.c_uint(33),
                ctypes.byref(val), ctypes.sizeof(val))
        except Exception:
            pass

    # ---- 绘制（阴影 + 圆角背景 + 标题栏） ----
    def paintEvent(self, _e):
        T = self.T
        p = QPainter(self)
        p.setRenderHint(QPainter.RenderHint.Antialiasing, True)
        body = QRectF(PAD, PAD, CORE_W, CORE_H)
        dark = self.cfg.get("appearance", "light") == "dark"

        # 手绘柔和阴影：层数更多、单层更淡，过渡比 QGraphicsDropShadowEffect 干净，
        # 而且动画时不用重算整个窗口
        p.setBrush(Qt.BrushStyle.NoBrush)
        steps = 18
        for i in range(steps, 0, -1):
            k = i / float(steps)
            a = int(30 * (1.0 - k) ** 2.0) + 1
            p.setPen(QPen(qc(T["shadow"], a), 2.0))
            p.drawRoundedRect(body.adjusted(-i, -i + 4, i, i + 4), RADIUS + i, RADIUS + i)

        # 圆角主体
        shell = QPainterPath()
        shell.addRoundedRect(body, RADIUS, RADIUS)
        p.setPen(Qt.PenStyle.NoPen)
        p.setBrush(qc(T["bg"]))
        p.drawPath(shell)

        # 顶部环境光：一层极淡的 accent 径向渐变，避免大面积纯色显得"平"
        p.save()
        p.setClipPath(shell)
        amb = QRadialGradient(QPointF(PAD + CORE_W * 0.5, PAD - 30), CORE_W * 1.0)
        amb.setColorAt(0.0, qc(T["ambient"], 34 if dark else 24))
        amb.setColorAt(1.0, qc(T["ambient"], 0))
        p.fillRect(body, QBrush(amb))
        p.restore()

        # 1px 边界：深色下窗口容易和桌面糊在一起，给一圈极淡的描边界定轮廓
        p.setPen(QPen(qc("#FFFFFF", 28) if dark else qc("#0B1526", 20), 1.0))
        p.setBrush(Qt.BrushStyle.NoBrush)
        p.drawRoundedRect(body.adjusted(0.5, 0.5, -0.5, -0.5), RADIUS, RADIUS)

        # 标题栏 logo：渐变圆角块
        logo = QRectF(PAD + 16, PAD + 12, 26, 26)
        lg = QLinearGradient(logo.topLeft(), logo.bottomRight())
        lg.setColorAt(0.0, qc(T["accent2"]))
        lg.setColorAt(1.0, qc(T["accent"]))
        p.setPen(Qt.PenStyle.NoPen)
        p.setBrush(QBrush(lg))
        p.drawRoundedRect(logo, 8.5, 8.5)

        path = QPainterPath()
        for i, (x, y) in enumerate(BOLT):
            pt = QPointF(PAD + 16 + 13 + (x - 0.5) * 15.0, PAD + 12 + 13 + (y - 0.5) * 15.0)
            if i == 0:
                path.moveTo(pt)
            else:
                path.lineTo(pt)
        path.closeSubpath()
        p.setBrush(QColor("#FFFFFF"))
        p.drawPath(path)

        p.setPen(qc(T["text"]))
        p.setFont(font(14.5, bold=True))
        p.drawText(QRectF(PAD + 52, PAD + 13, 160, 24),
                   int(Qt.AlignmentFlag.AlignLeft | Qt.AlignmentFlag.AlignVCenter), APP_NAME)

        # 权限徽章
        badge = "管理员" if is_admin_cached() else "普通权限"
        bw = len(badge) * 12 + 20
        bx = PAD + CORE_W - 48 - bw
        p.setPen(Qt.PenStyle.NoPen)
        p.setBrush(qc(T["card"]))
        p.drawRoundedRect(QRectF(bx, PAD + 14, bw, 22), 11.0, 11.0)
        p.setPen(qc(T["dim2"]))
        p.setFont(font(10.5))
        p.drawText(QRectF(bx, PAD + 14, bw, 22), int(Qt.AlignmentFlag.AlignCenter), badge)

    # ---- 动画时钟 ----
    def _anim_step(self):
        now = time.perf_counter()
        dt = min(0.06, max(0.004, now - self._last_tick))
        self._last_tick = now
        busy = False

        # 加速中权重
        tgt_a = 1.0 if (self.boosting and self.stack.currentIndex() == 0) else 0.0
        da = tgt_a - self._boost_a
        if abs(da) > 0.004:
            self._boost_a += da * (1.0 - math.exp(-dt / 0.16))
            busy = True
        else:
            self._boost_a = tgt_a

        # 环值只跟真实内存占用走。
        # 旧版在加速时把目标设成 100%，等于让"内存占用"显示一个假数字，
        # 用户会看到 60% → 100% → 55% 的过山车。这里彻底不再这么做。
        # 加速刚结束的那阵用更短的 tau，下落更果断，像"被真的放掉了"。
        target = float(self.mem["percent"])
        tau = 0.26 if time.time() < self._result_until else 0.42
        diff = target - self._ring_val
        if abs(diff) > 0.06:
            self._ring_val += diff * (1.0 - math.exp(-dt / tau))
            busy = True
        elif self._ring_val != target:
            self._ring_val = target
            busy = True

        # 扫描弧相位：只在加速中旋转（indeterminate）
        if self._boost_a > 0.02:
            self._spin = (self._spin + dt * 330.0) % 360.0
            self._pulse = (self._pulse + dt * 2.0 * math.pi / 1.25) % (2.0 * math.pi)
            busy = True

        # 完成涟漪：0 → 1 约 0.85s
        if self._ripple < 1.0:
            self._ripple = min(1.0, self._ripple + dt / 0.85)
            busy = True

        ring = self.page_boost.ring
        label = self._ring_label()
        if (abs(ring.pct - self._ring_val) > 0.01 or ring.boost_a != self._boost_a
                or ring.spin != self._spin or ring.ripple != self._ripple
                or ring.label != label):
            ring.pct = self._ring_val
            ring.boost_a = self._boost_a
            ring.spin = self._spin
            ring.ripple = self._ripple
            ring.label = label
            ring.update()

        if self._anim.interval() != (16 if busy else 80):
            self._anim.setInterval(16 if busy else 80)

    def _ring_label(self):
        """环中心副标题：加速阶段 > 结果态 > 默认。"""
        if self.boosting:
            return self._stage_text or "正在加速…"
        if time.time() < self._result_until and self.last_res:
            # 保护了几个进程是要如实说出来的：这直接决定了加速后会不会卡
            p = int(self.last_res.get("protected") or 0)
            if p:
                return "已释放 %.2f GB · 保护 %d" % (self.last_res["freed_gb"], p)
            return "已释放 %.2f GB" % self.last_res["freed_gb"]
        return "内存占用"

    # ---- 内存刷新 ----
    def _refresh_mem(self):
        self.mem = self._safe_mem()
        self.page_boost.update()
        if self.stack.currentIndex() == 1:
            self.page_auto.update()

    # ---- 标签切换 ----
    def _on_tab(self, i):
        self.stack.setCurrentIndex(i)
        self._last_tick = time.perf_counter()

    # ---- 阈值工具 ----
    def th_range(self):
        cfg = self.cfg
        if cfg["threshold_mode"] == "gb":
            total = self.mem["total_gb"] or 16.0
            return 2.0, max(3.0, round(total - 1, 1)), float(cfg["threshold_gb"])
        return 50.0, 95.0, float(cfg["threshold_percent"])

    def fmt_th(self, v):
        return ("%.0f GB" % v) if self.cfg["threshold_mode"] == "gb" else ("%.0f%%" % v)

    def th_hint(self):
        m = self.mem
        if self.cfg["threshold_mode"] == "gb":
            hit = m["in_use_gb"] >= self.cfg["threshold_gb"]
            return "当前已用 %.2f GB，%s" % (m["in_use_gb"], "已超过阈值" if hit else "低于阈值")
        hit = m["percent"] >= self.cfg["threshold_percent"]
        return "当前占用 %.1f%%，%s" % (m["percent"], "已超过阈值" if hit else "低于阈值")

    def sync_auto_controls(self):
        """把 cfg 同步到自动页的两个滑块（程序化设置，不触发回调）。"""
        page = self.page_auto
        page.sl_th.blockSignals(True)
        page.sl_int.blockSignals(True)
        lo, hi, val = self.th_range()
        if self.cfg["threshold_mode"] == "gb":
            page.sl_th.setRange(0, max(1, int(round((hi - lo) * 2))))
            page.sl_th.setValue(int(round((val - lo) * 2)))
        else:
            page.sl_th.setRange(0, int(hi - lo))
            page.sl_th.setValue(int(round(val - lo)))
        mins = self.cfg["min_boost_interval_seconds"] // 60
        page.sl_int.setRange(0, 14)
        page.sl_int.setValue(max(0, min(14, mins - 1)))
        page.sl_th.blockSignals(False)
        page.sl_int.blockSignals(False)
        self._style_sliders()

    def _style_sliders(self):
        T = self.T
        qss = """
        QSlider { min-height: 26px; background: transparent; }
        QSlider::groove:horizontal {
            height: 4px; background: %(track)s; border-radius: 2px;
        }
        QSlider::sub-page:horizontal {
            background: %(accent)s; border-radius: 2px;
        }
        QSlider::handle:horizontal {
            width: 13px; height: 13px; margin: -5px 0;
            border-radius: 7px; background: %(card)s;
            border: 1px solid %(accent)s;
        }
        QSlider::handle:horizontal:hover {
            border: 2px solid %(accent)s;
        }
        """ % {"track": T["track"], "accent": T["accent"], "card": T["card"]}
        self.page_auto.sl_th.setStyleSheet(qss)
        self.page_auto.sl_int.setStyleSheet(qss)

    # ---- 交互回调 ----
    def on_threshold_slider(self, v):
        lo, hi, _ = self.th_range()
        if self.cfg["threshold_mode"] == "gb":
            val = lo + v / 2.0
            self.cfg["threshold_gb"] = round(val * 2) / 2.0
        else:
            self.cfg["threshold_percent"] = float(lo + v)
        self.page_auto.update()
        self.save_later()

    def on_interval_slider(self, v):
        self.cfg["min_boost_interval_seconds"] = (v + 1) * 60
        self.page_auto.update()
        self.save_later()

    def on_mode_change(self, idx):
        self.cfg["threshold_mode"] = "gb" if idx == 0 else "percent"
        self.sync_auto_controls()
        self.page_auto.update()
        self.save_later()

    def on_auto_toggle(self, on):
        self.cfg["auto_enabled"] = bool(on)
        self.save_later()

    def on_check_interval(self, v):
        self.cfg["check_interval_seconds"] = max(2, int(v))
        self.save_later()

    def on_setting_switch(self, key, on):
        if key == "start":
            self._set_autostart_async(bool(on))
            return
        field = {"tray": "minimize_to_tray",
                 "protect": "protect_foreground",
                 "temp": "clean_temp"}.get(key)
        if not field:
            return
        self.cfg[field] = bool(on)
        self.save_later()

    def on_boost_mode(self, idx):
        modes = SettingsPage.MODES
        self.cfg["boost_mode"] = modes[max(0, min(len(modes) - 1, int(idx)))]
        self.page_set.update()
        self.save_later()

    # ---- 开机自启 ----
    def _probe_autostart(self):
        def run():
            try:
                self.bridge.autostart_state.emit(bool(core.autostart_enabled()))
            except Exception:
                pass
        threading.Thread(target=run, daemon=True).start()

    def on_autostart_state(self, on):
        self.cfg["_autostart"] = bool(on)
        if self.cfg.get("_autostart_probed") != on:
            self.cfg["_autostart_probed"] = on
            self.page_set.refresh()

    def _set_autostart_async(self, want):
        # 不再要求提权：管理员走计划任务（最高权限），普通用户自动退回 HKCU
        # 登录项。两条路都不弹 UAC，所以开关在任何权限下都应该能打开。
        def run():
            ok, msg = core.set_autostart(want)
            self.bridge.autostart_set.emit(want, ok, msg or "")
        threading.Thread(target=run, daemon=True).start()

    def _ask_elevate_for_autostart(self):
        # 显式 new 出来的 QMessageBox 以窗口为父对象，exec() 返回后只是隐藏，
        # C++ 实例还挂在父对象下面 —— 每弹一次就多留一个，攒着不释放。
        # 用完必须自己回收；静默模式的 QMessageBox.warning() 是栈对象，不需要管。
        box = QMessageBox(self)
        box.setWindowTitle(APP_NAME)
        box.setIcon(QMessageBox.Icon.Question)
        box.setText("删除自启计划任务需要管理员权限")
        box.setInformativeText(
            "之前以管理员身份创建的计划任务（%s）只能由管理员删除。\n\n"
            "要以管理员身份重新启动程序吗？" % core.TASK_NAME)
        yes = box.addButton("以管理员身份重启", QMessageBox.ButtonRole.AcceptRole)
        box.addButton("取消", QMessageBox.ButtonRole.RejectRole)
        box.exec()
        hit_yes = box.clickedButton() is yes
        box.deleteLater()
        box = None
        if hit_yes:
            if core.relaunch_as_admin():
                self.quit_app()
            else:
                QMessageBox.warning(self, APP_NAME,
                                    "提权启动失败（可能取消了 UAC 提示）。\n"
                                    "请右键程序选择「以管理员身份运行」后再试一次。")

    def on_autostart_set(self, want, ok, msg):
        if ok:
            self.cfg["_autostart"] = want
            self.page_set.refresh()
            return
        self.page_set.refresh()
        if "需要管理员权限" in (msg or ""):
            self._ask_elevate_for_autostart()
            return
        QMessageBox.warning(self, APP_NAME,
                            "开机自启设置失败：\n%s\n\n"
                            "可以手动把程序快捷方式放进「启动」文件夹（Win+R 输入 shell:startup）。"
                            % (msg or "未知错误"))

    # ---- 加速 ----
    def on_boost(self):
        if self.boosting:
            return
        self.boosting = True
        self._result_until = 0.0
        self._stage_text = "准备中…"
        self.page_boost.btn.set_enabled_look(False)
        self.page_boost.btn.set_label("正在加速…")
        self.page_boost.btn.update()
        threading.Thread(target=self._boost_worker, args=(False,), daemon=True).start()

    def _boost_worker(self, auto):
        try:
            snapshot = dict(self.cfg)

            def prog(stage, done=0, total=0):
                # 每 50ms 一次，次数很少；emit 会自动排队到 UI 线程
                self.bridge.boost_progress.emit(stage, int(done), int(total))

            res = core.boost_with_config(snapshot, progress=prog)
            res["time"] = time.strftime("%H:%M:%S")
            res["auto"] = auto
            self.bridge.boost_done.emit(res)
        except Exception as e:  # noqa: BLE001
            self.bridge.boost_err.emit(str(e))

    def on_boost_progress(self, stage, done, total):
        """真实阶段文字。这些数字都来自引擎实际处理的进程数，不是估的。"""
        if not self.boosting:
            return
        if stage == "trim":
            self._stage_text = ("清理进程 %d/%d" % (done, total)) if total else "清理进程…"
        elif stage == "standby":
            self._stage_text = "释放备用内存"
        elif stage == "temp":
            self._stage_text = "清理临时文件"
        self.page_boost.ring.update()

    def on_boost_done(self, res):
        self.boosting = False
        self.last_res = res
        if res.get("auto"):
            self.last_auto_boost = time.time()
        self.mem = self._safe_mem()
        self.page_boost.update()

        # 收尾反馈：环下落 + 一圈涟漪
        self._ripple = 0.0
        self._result_until = time.time() + 3.4

        btn = self.page_boost.btn
        btn.set_label("一键加速")
        btn.set_enabled_look(True)
        QTimer.singleShot(560, lambda: self._flash_btn(
            "已释放 %.2f GB" % res["freed_gb"], "ok"))
        # 界面刷完再把自己也修剪一遍：一个内存工具不该自己抱着几十 MB 工作集
        QTimer.singleShot(2000, self._trim_self)

    def _trim_self(self):
        """把本进程多余的工作集交还系统（Qt 常驻 ~74MB，修剪后可降到 ~20MB）。"""
        try:
            k32 = ctypes.windll.kernel32
            h = k32.OpenProcess(0x0100 | 0x0400, False, os.getpid())
            if h:
                k32.SetProcessWorkingSetSize(h, ctypes.c_size_t(-1), ctypes.c_size_t(-1))
                k32.CloseHandle(h)
        except Exception:
            pass

    def _flash_btn(self, text, kind):
        T = self.T
        btn = self.page_boost.btn
        if self.boosting:
            return
        btn.set_label(text, check=(kind == "ok"))
        if kind == "ok":
            btn.set_colors(qc(T["ok_bg"]), qc(T["ok_fg"]))
        else:
            btn.set_colors(qc(T["disabled_bg"]), qc(T["bad"]))
        QTimer.singleShot(2400, self._reset_btn)

    def _reset_btn(self):
        if self.boosting:
            return
        btn = self.page_boost.btn
        btn.set_label("一键加速")
        btn.reset_colors()
        btn.update()

    def on_boost_error(self, msg):
        self.boosting = False
        self.last_error = msg
        btn = self.page_boost.btn
        btn.set_enabled_look(True)
        self._flash_btn("加速失败", "err")

    # ---- 自动监控 ----
    def _monitor(self):
        while self._running:
            time.sleep(1.0)
            with self._cfg_lock:
                enabled = bool(self.cfg["auto_enabled"])
                mode = self.cfg["threshold_mode"]
                th_gb = float(self.cfg["threshold_gb"])
                th_pct = float(self.cfg["threshold_percent"])
                check = max(2, int(self.cfg["check_interval_seconds"]))
                min_int = max(60, int(self.cfg["min_boost_interval_seconds"]))
            if not enabled:
                continue
            time.sleep(max(0, check - 1))
            if self.boosting:
                continue
            try:
                m = core.read_memory()
            except Exception:
                continue
            triggered = (mode == "gb" and m["in_use_gb"] > th_gb) or \
                        (mode == "percent" and m["percent"] > th_pct)
            now = time.time()
            if triggered and (now - self.last_auto_boost) >= min_int:
                self.last_auto_boost = now
                self.bridge.auto_trigger.emit()

    def on_auto_trigger(self):
        if self.boosting:
            return
        self.boosting = True
        self.page_boost.btn.set_enabled_look(False)
        self.page_boost.btn.set_label("正在加速…")
        self.page_boost.btn.update()
        threading.Thread(target=self._boost_worker, args=(True,), daemon=True).start()

    # ---- 主题 ----
    def toggle_theme(self):
        key = "dark" if self.cfg.get("appearance", "light") == "light" else "light"
        self.cfg["appearance"] = key
        self.T = THEMES[key]
        self._style_sliders()
        self.page_set.btn_theme.set_text(self.page_set._theme_label())
        # 主按钮的前景色是缓动出来的，主题切换要重新取色
        self.page_boost.btn.reset_colors()
        for w in (self, self.seg, self.page_boost, self.page_auto, self.page_set,
                  self.btn_close, self.page_boost.ring, self.page_boost.btn,
                  self.page_auto.sw, self.page_auto.seg, self.page_auto.sl_th,
                  self.page_auto.sl_int, self.page_set.stepper,
                  self.page_set.seg_mode,
                  self.page_set.btn_theme, self.page_set.btn_quit):
            w.update()
        for sw in self.page_set.switches.values():
            sw.update()
        self.save_later()

    # ---- 托盘 ----
    def _init_tray(self):
        if not QSystemTrayIcon.isSystemTrayAvailable():
            return
        self._tray = QSystemTrayIcon(self._icon, self)
        self._tray.setToolTip("%s · 内存加速" % APP_NAME)
        menu = QMenu(self)
        act_show = menu.addAction("显示窗口")
        act_boost = menu.addAction("立即加速")
        menu.addSeparator()
        act_quit = menu.addAction("退出")
        act_show.triggered.connect(self.restore)
        act_boost.triggered.connect(self.on_boost)
        act_quit.triggered.connect(self.quit_app)
        self._tray_menu = menu
        self._tray.setContextMenu(menu)
        self._tray.activated.connect(self._on_tray_activated)
        self._tray.show()

    def _on_tray_activated(self, reason):
        if reason in (QSystemTrayIcon.ActivationReason.DoubleClick,
                      QSystemTrayIcon.ActivationReason.Trigger):
            self.restore()

    def restore(self):
        # 从托盘恢复的瞬间，窗口会正好出现在光标下方，系统还可能把"点托盘那一下"
        # 派发成一次按下 —— 不清状态、不设冷却，窗口就会随着鼠标一起跑。
        self._drag = None
        self._drag_lock_until = time.monotonic() + 0.30
        self.showNormal()
        self._place_bottom_right()
        self.raise_()
        self.activateWindow()

    def close_or_hide(self):
        if self.cfg.get("minimize_to_tray", True) and self._tray is not None:
            self.hide()
            # 只在本次启动的第一次关闭时提示一次，之后静默隐藏
            if not self._tray_tip_shown and self._tray.supportsMessages():
                self._tray_tip_shown = True
                self._tray.showMessage(APP_NAME, "已最小化到系统托盘，双击图标恢复。",
                                       QSystemTrayIcon.MessageIcon.Information, 2200)
        else:
            self.quit_app()

    def closeEvent(self, e):
        # 点窗口关闭按钮时走「隐藏到托盘」，真正退出用托盘菜单 / 设置页按钮
        if self.cfg.get("minimize_to_tray", True) and self._tray is not None:
            e.ignore()
            self.close_or_hide()
        else:
            e.accept()

    def quit_app(self):
        self._running = False
        try:
            self._save_now()
        except Exception:
            pass
        if self._tray is not None:
            self._tray.hide()
        QApplication.quit()

    # ---- 配置保存（防抖） ----
    def save_later(self):
        self._save_timer.start(500)

    def _save_now(self):
        data = {k: v for k, v in self.cfg.items()
                if not k.startswith("_") and k in core.DEFAULT_CONFIG}
        core.save_config(data)

    # ---- 窗口拖动（无边框） ----
    def mousePressEvent(self, e):
        if e.button() != Qt.MouseButton.LeftButton:
            return
        # 刚用过托盘就别信这次按下：那是点托盘图标的那一下，不是想拖窗口
        if time.monotonic() < self._drag_lock_until:
            return
        if e.position().y() < PAD + HEADER_H:
            self._drag = e.globalPosition().toPoint() - self.frameGeometry().topLeft()

    def mouseMoveEvent(self, e):
        if self._drag is None:
            return
        # 必须确认左键还按着。托盘恢复时可能只收到 move（按键状态由系统给出），
        # 一旦某次 press 没配上 release，不校验就会变成"空手移动也能拖窗口"
        if not (e.buttons() & Qt.MouseButton.LeftButton):
            self._drag = None
            return
        self.move(e.globalPosition().toPoint() - self._drag)

    def mouseReleaseEvent(self, _e):
        self._drag = None

    def hideEvent(self, e):
        # 藏起来时把拖拽状态清干净，否则下次显示出来还带着老的锚点
        self._drag = None
        # 同时停掉渲染时钟：窗口都看不见了，60/12.5fps 的空转纯属浪费 CPU
        for t in (getattr(self, "_anim", None), getattr(self, "_tick_timer", None)):
            if t is not None:
                t.stop()
        super().hideEvent(e)

    def changeEvent(self, e):
        # 最小化 / 从任务栏还原时同样作废拖拽状态
        if e.type() == QEvent.Type.WindowStateChange:
            self._drag = None
        super().changeEvent(e)


# ---------------------------------------------------------------------------
def main():
    parser = argparse.ArgumentParser(description="仿微软电脑管家内存加速工具（PySide6 版）")
    parser.add_argument("--once", action="store_true", help="执行一次加速后退出")
    parser.add_argument("--auto", action="store_true", help="后台按阈值自动加速")
    parser.add_argument("--tray", action="store_true", help="启动即隐藏到托盘")
    parser.add_argument("--threshold-gb", type=float, default=None)
    parser.add_argument("--threshold-percent", type=float, default=None)
    parser.add_argument("--min-interval", type=int, default=None)
    parser.add_argument("--check-interval", type=int, default=None)
    args = parser.parse_args()

    if args.once or args.auto:
        core.run_cli(args)
        return

    QGuiApplication.setHighDpiScaleFactorRoundingPolicy(
        Qt.HighDpiScaleFactorRoundingPolicy.PassThrough)
    app = QApplication([sys.argv[0]])
    app.setApplicationName(APP_NAME)
    app.setQuitOnLastWindowClosed(False)
    app.setFont(font(12))

    win = MainWindow(start_hidden=bool(args.tray))
    if not args.tray:
        win.show()
    sys.exit(app.exec())


if __name__ == "__main__":
    main()
