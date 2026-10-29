#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
memory_booster.py  —— 仿微软电脑管家的「内存加速」工具 v2.0（Windows 专用）
=========================================================================
界面：单 Canvas 自绘（零第三方依赖），紧凑 420x580，顶部三段标签：
      加速 / 自动 / 设置，窗口默认出现在屏幕右下角。

功能：
  1. 一键加速：修剪各进程工作集(EmptyWorkingSet) + 清空系统备用/缓存内存(standby list) + 清理老旧临时文件
  2. 阈值自动加速：物理内存占用超过设定阈值（按 GB 或 占比%）时自动加速
  3. 节流保护：自动加速每 5 分钟最多执行一次（min_boost_interval_seconds，默认 300）
  4. 开机自启：通过「计划任务·最高权限」实现，登录后后台运行且不弹 UAC
  5. 系统托盘常驻（最小化到托盘）

原理（与微软电脑管家 Boost 一致，套壳 Windows 自带 API）：
  - SetProcessWorkingSetSize(pid, -1, -1)  -> EmptyWorkingSet，把进程不用内存交还系统
  - NtSetSystemInformation(0x50, MemoryPurgeStandbyList) -> 清系统文件缓存/备用列表（需特权）
  - 临时目录清理：删除 %TEMP% 下超过 N 天的文件

重要局限：
  ★ 不能回收「非分页池(Non-Paged Pool)」内核内存泄漏；NPP 是内核态内存，用户态工具碰不到。
    有 NPP 泄漏请用 RAMMap / poolmon 定位驱动或重启。
  ★ 内存"加速"是短暂回血，释放的内存会被系统和应用很快重新占满。

运行：
  python memory_booster.py           # 图形界面
  python memory_booster.py --tray    # 启动即隐藏到托盘（供开机自启使用）
  python memory_booster.py --once    # 命令行单次加速
  python memory_booster.py --auto    # 命令行后台自动加速
建议以管理员身份运行（否则清缓存/修剪系统进程会失败）。
"""

import argparse
import ctypes
import json
import math
import os
import queue
import subprocess
import sys
import threading
import time
from ctypes import wintypes, Structure, byref, c_ulong, c_void_p, c_size_t, POINTER

try:
    import winreg
except ImportError:  # 非 Windows 平台
    winreg = None

try:
    import tkinter as tk
    from tkinter import messagebox
    TK_AVAILABLE = True
except Exception:
    TK_AVAILABLE = False
    tk = None
    messagebox = None

# 图标内嵌在 app_icon.py（zlib+base64），不依赖任何外部资源文件
try:
    import app_icon
except Exception:
    app_icon = None

APP_VERSION = "2.2.9"
APP_NAME = "内存加速"

# ---------------------------------------------------------------------------
# 1) Windows API 绑定
# ---------------------------------------------------------------------------
kernel32 = ctypes.windll.kernel32
psapi = ctypes.windll.psapi
advapi32 = ctypes.windll.advapi32
ntdll = ctypes.windll.ntdll
user32 = ctypes.windll.user32
shell32 = ctypes.windll.shell32
try:
    gdi32 = ctypes.windll.gdi32
except Exception:
    gdi32 = None

PROCESS_SET_QUOTA = 0x0100
PROCESS_QUERY_INFORMATION = 0x0400
TOKEN_QUERY = 0x0008
TOKEN_ADJUST_PRIVILEGES = 0x0020
SE_PRIVILEGE_ENABLED = 0x00000002

kernel32.GlobalMemoryStatusEx.argtypes = [wintypes.LPVOID]
kernel32.GlobalMemoryStatusEx.restype = wintypes.BOOL

kernel32.OpenProcess.argtypes = [wintypes.DWORD, wintypes.BOOL, wintypes.DWORD]
kernel32.OpenProcess.restype = wintypes.HANDLE

kernel32.SetProcessWorkingSetSize.argtypes = [wintypes.HANDLE, c_size_t, c_size_t]
kernel32.SetProcessWorkingSetSize.restype = wintypes.BOOL

kernel32.QueryFullProcessImageNameW.argtypes = [
    wintypes.HANDLE, wintypes.DWORD, wintypes.LPWSTR, POINTER(wintypes.DWORD)]
kernel32.QueryFullProcessImageNameW.restype = wintypes.BOOL

user32.GetForegroundWindow.restype = wintypes.HWND
user32.GetWindowThreadProcessId.argtypes = [wintypes.HWND, POINTER(wintypes.DWORD)]
user32.GetWindowThreadProcessId.restype = wintypes.DWORD

kernel32.CloseHandle.argtypes = [wintypes.HANDLE]
kernel32.CloseHandle.restype = wintypes.BOOL

psapi.EnumProcesses.argtypes = [POINTER(wintypes.DWORD), wintypes.DWORD, POINTER(wintypes.DWORD)]
psapi.EnumProcesses.restype = wintypes.BOOL

ntdll.NtSetSystemInformation.argtypes = [wintypes.ULONG, c_void_p, wintypes.ULONG]
ntdll.NtSetSystemInformation.restype = wintypes.LONG

advapi32.OpenProcessToken.argtypes = [wintypes.HANDLE, wintypes.DWORD, POINTER(wintypes.HANDLE)]
advapi32.OpenProcessToken.restype = wintypes.BOOL
advapi32.LookupPrivilegeValueW.argtypes = [wintypes.LPCWSTR, wintypes.LPCWSTR,
                                           POINTER(ctypes.c_ulonglong)]
advapi32.LookupPrivilegeValueW.restype = wintypes.BOOL
shell32.IsUserAnAdmin.argtypes = []
shell32.IsUserAnAdmin.restype = wintypes.BOOL

user32.SystemParametersInfoW.argtypes = [wintypes.UINT, wintypes.UINT, c_void_p, wintypes.UINT]
user32.SystemParametersInfoW.restype = wintypes.BOOL
user32.GetDC.argtypes = [wintypes.HWND]
user32.GetDC.restype = wintypes.HDC
user32.ReleaseDC.argtypes = [wintypes.HWND, wintypes.HDC]
user32.ReleaseDC.restype = ctypes.c_int
shell32.ExtractIconW.argtypes = [wintypes.HINSTANCE, wintypes.LPCWSTR, ctypes.c_int]
shell32.ExtractIconW.restype = wintypes.HANDLE
user32.LoadImageW.argtypes = [wintypes.HINSTANCE, wintypes.LPCWSTR, wintypes.UINT,
                              ctypes.c_int, ctypes.c_int, wintypes.UINT]
user32.LoadImageW.restype = wintypes.HANDLE
user32.DestroyIcon.argtypes = [wintypes.HANDLE]
user32.DestroyIcon.restype = wintypes.BOOL


class RECT(Structure):
    _fields_ = [("left", wintypes.LONG), ("top", wintypes.LONG),
                ("right", wintypes.LONG), ("bottom", wintypes.LONG)]


user32.GetWindowRect.argtypes = [wintypes.HWND, POINTER(RECT)]
user32.GetWindowRect.restype = wintypes.BOOL


# ---------------------------------------------------------------------------
# 托盘的 structures 与 user32 签名（64 位统一调用约定，签名主要保证参数编组正确）
# ---------------------------------------------------------------------------
class POINT(Structure):
    _fields_ = [("x", wintypes.LONG), ("y", wintypes.LONG)]


class MSG(Structure):
    _fields_ = [
        ("hwnd", wintypes.HWND),
        ("message", wintypes.UINT),
        ("wParam", wintypes.WPARAM),
        ("lParam", wintypes.LPARAM),
        ("time", wintypes.DWORD),
        ("pt", POINT),
        ("lPrivate", wintypes.DWORD),
    ]


WNDPROC = ctypes.WINFUNCTYPE(
    ctypes.c_longlong, wintypes.HWND, wintypes.UINT, wintypes.WPARAM, wintypes.LPARAM)


class WNDCLASS(Structure):
    _fields_ = [
        ("style", wintypes.UINT),
        ("lpfnWndProc", WNDPROC),
        ("cbClsExtra", ctypes.c_int),
        ("cbWndExtra", ctypes.c_int),
        ("hInstance", wintypes.HANDLE),
        ("hIcon", wintypes.HANDLE),
        ("hCursor", wintypes.HANDLE),
        ("hbrBackground", wintypes.HANDLE),
        ("lpszMenuName", wintypes.LPCWSTR),
        ("lpszClassName", wintypes.LPCWSTR),
    ]


user32.RegisterClassW.argtypes = [POINTER(WNDCLASS)]
user32.RegisterClassW.restype = wintypes.ATOM
user32.CreateWindowExW.argtypes = [
    wintypes.DWORD, wintypes.LPCWSTR, wintypes.LPCWSTR, wintypes.DWORD,
    ctypes.c_int, ctypes.c_int, ctypes.c_int, ctypes.c_int,
    wintypes.HWND, wintypes.HMENU, wintypes.HINSTANCE, wintypes.LPVOID]
user32.CreateWindowExW.restype = wintypes.HWND
user32.DefWindowProcW.argtypes = [wintypes.HWND, wintypes.UINT, wintypes.WPARAM, wintypes.LPARAM]
user32.DefWindowProcW.restype = ctypes.c_longlong
user32.LoadIconW.argtypes = [wintypes.HINSTANCE, ctypes.c_void_p]
user32.LoadIconW.restype = wintypes.HANDLE
user32.GetMessageW.argtypes = [POINTER(MSG), wintypes.HWND, wintypes.UINT, wintypes.UINT]
user32.GetMessageW.restype = wintypes.LONG
user32.TranslateMessage.argtypes = [POINTER(MSG)]
user32.TranslateMessage.restype = wintypes.BOOL
user32.DispatchMessageW.argtypes = [POINTER(MSG)]
user32.DispatchMessageW.restype = ctypes.c_longlong
user32.CreatePopupMenu.argtypes = []
user32.CreatePopupMenu.restype = wintypes.HMENU
user32.AppendMenuW.argtypes = [wintypes.HMENU, wintypes.UINT, wintypes.UINT, wintypes.LPCWSTR]
user32.AppendMenuW.restype = wintypes.BOOL
user32.GetCursorPos.argtypes = [POINTER(POINT)]
user32.GetCursorPos.restype = wintypes.BOOL
user32.SetForegroundWindow.argtypes = [wintypes.HWND]
user32.SetForegroundWindow.restype = wintypes.BOOL
user32.TrackPopupMenu.argtypes = [wintypes.HMENU, wintypes.UINT, ctypes.c_int,
                                  ctypes.c_int, ctypes.c_int, wintypes.HWND, wintypes.LPVOID]
user32.TrackPopupMenu.restype = wintypes.UINT
user32.DestroyMenu.argtypes = [wintypes.HMENU]
user32.DestroyMenu.restype = wintypes.BOOL
user32.PostMessageW.argtypes = [wintypes.HWND, wintypes.UINT, wintypes.WPARAM, wintypes.LPARAM]
user32.PostMessageW.restype = wintypes.BOOL
user32.DestroyWindow.argtypes = [wintypes.HWND]
user32.DestroyWindow.restype = wintypes.BOOL


class MEMORYSTATUSEX(Structure):
    _fields_ = [
        ("dwLength", wintypes.DWORD),
        ("dwMemoryLoad", wintypes.DWORD),
        ("ullTotalPhys", ctypes.c_ulonglong),
        ("ullAvailPhys", ctypes.c_ulonglong),
        ("ullTotalPageFile", ctypes.c_ulonglong),
        ("ullAvailPageFile", ctypes.c_ulonglong),
        ("ullTotalVirtual", ctypes.c_ulonglong),
        ("ullAvailVirtual", ctypes.c_ulonglong),
        ("ullAvailExtendedVirtual", ctypes.c_ulonglong),
    ]


def is_admin() -> bool:
    try:
        return bool(shell32.IsUserAnAdmin())
    except Exception:
        return False


def enable_dpi_awareness():
    """Per-Monitor V2 DPI 感知。

    这一步是界面清不清晰的分水岭：
      - 不声明（或只声明 System aware）时，缩放不是 100% 的屏幕会被 DWM 把窗口
        位图拉伸，整块界面糊成一层；
      - Per-Monitor V2 下程序自己按真实像素绘制，文字与描边都落在像素网格上。
    """
    ok = False
    # Windows 10 1703+：DPI_AWARENESS_CONTEXT_PER_MONITOR_AWARE_V2 = -4
    try:
        user32.SetProcessDpiAwarenessContext.argtypes = [c_void_p]
        user32.SetProcessDpiAwarenessContext.restype = wintypes.BOOL
        ok = bool(user32.SetProcessDpiAwarenessContext(c_void_p(-4)))
    except Exception:
        ok = False
    if not ok:
        try:
            # PROCESS_PER_MONITOR_DPI_AWARE = 2
            ok = ctypes.windll.shcore.SetProcessDpiAwareness(2) == 0
        except Exception:
            ok = False
    if not ok:
        try:
            ok = bool(user32.SetProcessDPIAware())
        except Exception:
            pass
    return ok


def dpi_scale() -> float:
    """系统 DPI / 96。用于坐标与字号的缩放（字号另走像素单位，见 App.font）。"""
    try:
        if gdi32 is None:
            return 1.0
        hdc = user32.GetDC(0)
        dpi = gdi32.GetDeviceCaps(hdc, 88)  # LOGPIXELSX
        user32.ReleaseDC(0, hdc)
        return max(1.0, min(3.0, dpi / 96.0))
    except Exception:
        return 1.0


# ---------------------------------------------------------------------------
# 2) 内存读取
# ---------------------------------------------------------------------------
def read_memory():
    ms = MEMORYSTATUSEX()
    ms.dwLength = ctypes.sizeof(ms)
    if not kernel32.GlobalMemoryStatusEx(byref(ms)):
        raise ctypes.WinError()
    total = ms.ullTotalPhys
    avail = ms.ullAvailPhys
    in_use = total - avail
    return {
        "total": total,
        "avail": avail,
        "in_use": in_use,
        "in_use_gb": in_use / (1024 ** 3),
        "total_gb": total / (1024 ** 3),
        "avail_gb": avail / (1024 ** 3),
        "avail_mb": avail / (1024 ** 2),
        "percent": (in_use / total * 100.0) if total else 0.0,
        "load": ms.dwMemoryLoad,
    }


# ---------------------------------------------------------------------------
# 3) 加速核心操作
# ---------------------------------------------------------------------------
def enable_privilege(priv_name: str) -> bool:
    try:
        tok = wintypes.HANDLE()
        if not advapi32.OpenProcessToken(kernel32.GetCurrentProcess(),
                                         TOKEN_QUERY | TOKEN_ADJUST_PRIVILEGES, byref(tok)):
            return False
        luid = ctypes.c_ulonglong(0)
        if not advapi32.LookupPrivilegeValueW(None, priv_name, byref(luid)):
            kernel32.CloseHandle(tok)
            return False

        class TOKEN_PRIVILEGES(Structure):
            _fields_ = [
                ("PrivilegeCount", wintypes.DWORD),
                ("LuidLow", wintypes.DWORD),
                ("LuidHigh", wintypes.DWORD),
                ("Attributes", wintypes.DWORD),
            ]

        tp = TOKEN_PRIVILEGES()
        tp.PrivilegeCount = 1
        tp.LuidLow = luid.value & 0xFFFFFFFF
        tp.LuidHigh = (luid.value >> 32) & 0xFFFFFFFF
        tp.Attributes = SE_PRIVILEGE_ENABLED
        # AdjustTokenPrivileges 的坑：它在"部分分配"时也返回非零，
        # 必须看 GetLastError 是否为 ERROR_NOT_ALL_ASSIGNED(1300)。
        # 以前这里直接 return True，导致普通权限下也谎报成功——
        # 实际上一项特权都没拿到，purge 只能靠后续系统调用失败才兜住。
        kernel32.SetLastError(0)
        ok = advapi32.AdjustTokenPrivileges(tok, False, byref(tp), 0, None, None)
        err = kernel32.GetLastError()
        kernel32.CloseHandle(tok)
        return bool(ok) and err == 0
    except Exception:
        return False


# 这些进程的工作集一旦被抽空，用户会立刻感到卡顿或异常：
#   explorer/dwm 一直在重绘桌面与窗口；audiodg 被压会导致爆音；
#   其余是会话与系统服务，抖动会波及整机。按小写镜像名匹配，一律跳过。
PROTECTED_NAMES = frozenset({
    "system", "registry", "memcompression",
    "smss.exe", "csrss.exe", "wininit.exe", "winlogon.exe",
    "services.exe", "lsass.exe", "dwm.exe", "explorer.exe", "audiodg.exe",
    "sihost.exe", "shellexperiencehost.exe", "startmenuexperiencehost.exe",
    "searchhost.exe", "textinputhost.exe", "ctfmon.exe", "fontdrvhost.exe",
    # 安全软件与索引服务：工作集被抽空后要重新加载引擎/重建索引，
    # 期间会持续占用 CPU 和磁盘，比省下的那点内存贵得多。
    "msmpeng.exe", "nissrv.exe", "securityhealthservice.exe",
    "securityhealthsystray.exe",
    "searchindexer.exe", "searchprotocolhost.exe", "searchfilterhost.exe",
    # WMI 宿主与打印后台：被反复重建的过程本身就是卡顿
    "wmiprvse.exe", "spoolsv.exe", "lsaiso.exe",
})

# 修剪工作集与清空 standby 的组合方式，见 boost() 的说明
BOOST_MODES = ("gentle", "balanced", "aggressive")


def _foreground_pid() -> int:
    """当前前台窗口所属进程 —— 用户正在操作它，绝不能修剪。"""
    try:
        hwnd = user32.GetForegroundWindow()
        if not hwnd:
            return 0
        pid = wintypes.DWORD()
        user32.GetWindowThreadProcessId(hwnd, byref(pid))
        return int(pid.value)
    except Exception:
        return 0


def _image_base_name(h) -> str:
    """从句柄取镜像文件名（小写）。取不到返回空串，按“不保护”处理。"""
    try:
        buf = ctypes.create_unicode_buffer(520)
        n = wintypes.DWORD(len(buf))
        if kernel32.QueryFullProcessImageNameW(h, 0, buf, byref(n)):
            return os.path.basename(buf.value).lower()
    except Exception:
        pass
    return ""


def _pid_session(pid: int) -> int:
    """取进程所在会话号。取不到返回 -1（调用方按“不属于当前会话”处理）。"""
    try:
        s = wintypes.DWORD(0xFFFFFFFF)
        if kernel32.ProcessIdToSessionId(wintypes.DWORD(pid), byref(s)):
            return int(s.value)
    except Exception:
        pass
    return -1


def trim_working_sets(skip_self: bool = True, progress=None, protect: bool = True,
                      session_only: bool = True, extra_protect=()):
    """逐个进程修剪工作集。

    progress: 可选回调 progress(done, total)，用于界面显示真实进度。
    先枚举出全部目标 pid 拿到准确的 total，再逐个处理——这样进度条上
    的百分比是真的，不是估出来的。

    protect=True 时跳过 PROTECTED_NAMES 里的关键进程和当前前台进程：
    EmptyWorkingSet 只是把页面从工作集踢到 standby，被踢的是 440 个进程
    的全部热数据，桌面会立刻因此卡顿，所以这一步必须有豁免名单。
    镜像名取不到时**一律不修剪**——Registry、Memory Compression 这类
    内核伪进程正是取不到名字的，fail-open 会把它们当成普通进程处理。

    session_only=True 时只修剪本会话的进程。Session 0 里是系统服务
    （SysMain、搜索索引、杀软、几十个 svchost），它们的工作集大多是大块
    脏页，清空要先写 pagefile。提权后能碰到这批进程、不提权碰不到，
    这就是“只有管理员模式才卡”的来源——限制在当前会话后两种权限一致。

    extra_protect: 额外的镜像名（小写），来自配置项。

    返回 (成功数, 失败数, 被保护跳过数, 因会话被跳过数)。
    """
    self_pid = os.getpid()
    size = 4096
    needed = wintypes.DWORD()
    buf = (wintypes.DWORD * size)()
    while True:
        if not psapi.EnumProcesses(buf, ctypes.sizeof(buf), byref(needed)):
            break
        if needed.value < ctypes.sizeof(buf):
            break
        size *= 2
        buf = (wintypes.DWORD * size)()

    n = needed.value // ctypes.sizeof(wintypes.DWORD)
    pids = []
    for i in range(n):
        pid = buf[i]
        if pid in (0, 4):
            continue
        if skip_self and pid == self_pid:
            continue
        pids.append(pid)

    protected_names = PROTECTED_NAMES
    if extra_protect:
        protected_names = protected_names | {str(x).lower() for x in extra_protect if x}

    my_session = -1
    if session_only:
        my_session = _pid_session(self_pid)
        if my_session < 0:      # 取不到自己的会话号就别过滤，否则会全部跳过
            session_only = False

    total = len(pids)
    count = 0
    failed = 0
    protected = 0
    skipped = 0
    fg_pid = _foreground_pid() if protect else 0
    last_cb = 0.0
    for i, pid in enumerate(pids, 1):
        if session_only and _pid_session(pid) != my_session:
            skipped += 1
        else:
            h = kernel32.OpenProcess(PROCESS_SET_QUOTA | PROCESS_QUERY_INFORMATION, False, pid)
            if not h:
                failed += 1
            else:
                try:
                    base = _image_base_name(h) if protect else ""
                    if protect and (pid == fg_pid or not base or base in protected_names):
                        protected += 1
                    elif kernel32.SetProcessWorkingSetSize(h, c_size_t(-1), c_size_t(-1)):
                        count += 1
                    else:
                        failed += 1
                finally:
                    kernel32.CloseHandle(h)
        if progress is not None:
            now = time.perf_counter()
            # 节流：每 50ms 一次足够顺滑，回调太密反而拖慢处理
            if now - last_cb >= 0.05 or i == total:
                last_cb = now
                try:
                    progress(i, total)
                except Exception:
                    pass
    return count, failed, protected, skipped


def purge_standby_list() -> bool:
    if not enable_privilege("SeProfileSingleProcessPrivilege"):
        return False
    cmd = c_ulong(4)
    status = ntdll.NtSetSystemInformation(0x50, byref(cmd), ctypes.sizeof(cmd))
    return status == 0


def clean_temp_files(max_age_days: int = 7, limit: int = 300) -> int:
    """删除 %TEMP% 下超过 max_age_days 的文件。

    limit 限制单次删除数量：一口气删掉成千上万个文件会触发杀软实时扫描
    和文件系统过滤回调，CPU/磁盘被打满几十秒——这是“清理完就卡”的次要来源。
    每删 20 个让出一次 CPU，把压力摊平，而不是集中爆发。
    """
    temp = os.environ.get("TEMP") or os.environ.get("TMP") or None
    if not temp or not os.path.isdir(temp):
        return 0
    cutoff = time.time() - max_age_days * 86400
    removed = 0
    try:
        for name in os.listdir(temp):
            if removed >= limit:
                break
            path = os.path.join(temp, name)
            try:
                if os.path.isfile(path) and os.path.getmtime(path) < cutoff:
                    os.remove(path)
                    removed += 1
                    if removed % 20 == 0:
                        time.sleep(0.02)
            except Exception:
                continue
    except Exception:
        pass
    return removed


def boost(clean_temp: bool = True, max_age_days: int = 7, progress=None,
          mode: str = "gentle", protect: bool = True,
          temp_limit: int = 300, temp_delay: float = 3.0,
          session_only: bool = True, extra_protect=()) -> dict:
    """执行一次加速。

    progress: 可选回调 progress(stage, done, total)，
      stage 取 "trim" / "standby" / "temp"，界面据此显示真实阶段与进度。

    mode 决定「修剪工作集」和「清空 standby」怎么组合，这是卡顿与否的关键：
      gentle     只修剪工作集，不碰 standby。被踢出的页面留在 standby 里，
                 程序回头访问是软缺页（不用读盘），几乎无感。默认值。
      balanced   先清 standby 再修剪。用系统文件缓存换可用内存，随后修剪
                 出来的页面仍留在 standby 可复用，卡顿轻微。
      aggressive 先修剪再清 standby（旧版行为）。数字最好看，但刚被踢出的
                 热数据会被真正丢出物理内存，之后是一轮硬缺页风暴：
                 桌面、任务栏和所有后台程序都要回磁盘重读。
    """
    def emit(stage, done=0, total=0):
        if progress is None:
            return
        try:
            progress(stage, done, total)
        except Exception:
            pass

    mode = mode if mode in BOOST_MODES else "gentle"
    before = read_memory()
    protected = 0
    skipped = 0
    standby_ok = False

    trim_kw = {
        "protect": protect,
        "session_only": session_only,
        "extra_protect": extra_protect,
        "progress": (lambda d, t: emit("trim", d, t)) if progress else None,
    }
    if mode == "balanced":
        # 先腾系统缓存，再把进程页退到 standby —— 顺序反了就是硬缺页风暴
        emit("standby")
        standby_ok = purge_standby_list()
        trim_count, trim_fail, protected, skipped = trim_working_sets(**trim_kw)
    else:
        trim_count, trim_fail, protected, skipped = trim_working_sets(**trim_kw)
        if mode == "aggressive":
            emit("standby")
            standby_ok = purge_standby_list()

    temp_removed = 0
    if clean_temp:
        emit("temp")
        # 先让系统从刚才的内存压力里喘口气，再去动文件系统
        if temp_delay and temp_delay > 0:
            time.sleep(float(temp_delay))
        temp_removed = clean_temp_files(max_age_days, temp_limit)
    after = read_memory()
    return {
        "before_gb": before["in_use_gb"],
        "after_gb": after["in_use_gb"],
        "freed_gb": max(0.0, before["in_use_gb"] - after["in_use_gb"]),
        "trim_count": trim_count,
        "trim_fail": trim_fail,
        "protected": protected,
        "skipped_session": skipped,
        "standby_ok": standby_ok,
        "temp_removed": temp_removed,
        "mode": mode,
    }


def boost_with_config(cfg, progress=None):
    """按配置执行一次加速。界面、CLI、自动模式共用，避免各处重复拆参数。"""
    return boost(
        clean_temp=bool(cfg.get("clean_temp", True)),
        max_age_days=int(cfg.get("temp_max_age_days", 7)),
        progress=progress,
        mode=cfg.get("boost_mode", "gentle"),
        protect=bool(cfg.get("protect_foreground", True)),
        temp_limit=int(cfg.get("temp_limit_per_run", 300)),
        temp_delay=float(cfg.get("temp_delay_seconds", 3)),
        session_only=bool(cfg.get("session_only", True)),
        extra_protect=tuple(cfg.get("extra_protect") or ()),
    )


# ---------------------------------------------------------------------------
# 4) 配置
# ---------------------------------------------------------------------------
DEFAULT_CONFIG = {
    "auto_enabled": False,
    "threshold_mode": "gb",
    "threshold_gb": 12.0,
    "threshold_percent": 85.0,
    "check_interval_seconds": 15,
    "min_boost_interval_seconds": 300,
    "clean_temp": True,
    "temp_max_age_days": 7,
    "minimize_to_tray": True,
    "appearance": "light",
    # gentle / balanced / aggressive，见 boost() 的说明
    "boost_mode": "gentle",
    # 保护关键进程（桌面/窗口合成/系统服务）与当前前台程序
    "protect_foreground": True,
    # 单次最多清理多少个临时文件，避免杀软扫描把 IO 打满
    "temp_limit_per_run": 300,
    # 加速结束后等几秒再动文件系统，错开内存压力峰值
    "temp_delay_seconds": 3,
    # 只修剪当前登录会话的进程。关掉会连 Session 0 的系统服务一起清，
    # 提权后尤其明显：那批进程的工作集大多是大块脏页，清空要先写 pagefile，
    # 表现为"只有管理员模式才卡"。
    "session_only": True,
    # 额外的保护名单（小写镜像名）。用于安全软件等被清空工作集会重扫的进程。
    "extra_protect": [
        "mspcmanagerservice.exe", "qqpcrtp.exe", "qqpctray.exe",
    ],
}


APP_DIR = "MemoryBooster"


def config_dir() -> str:
    """配置目录：~/MemoryBooster/

    Windows 上展开为 C:\\Users\\<用户名>\\MemoryBooster\\ 。
    放在用户目录而不是 exe 旁边，好处是：程序换位置/换版本不会丢设置，
    也不要求程序所在目录可写（放到 Program Files 时写不进去）。
    """
    d = os.path.join(os.path.expanduser("~"), APP_DIR)
    try:
        os.makedirs(d, exist_ok=True)
    except Exception:
        pass
    return d


def config_path() -> str:
    return os.path.join(config_dir(), "config.json")


def _legacy_config_paths():
    """旧版把配置放在程序目录，列出这些位置用于一次性迁移。"""
    out = []
    here = os.path.dirname(os.path.abspath(__file__))
    out.append(os.path.join(here, "memory_booster_config.json"))
    if getattr(sys, "frozen", False):
        p = os.path.join(os.path.dirname(sys.executable), "memory_booster_config.json")
        if p not in out:
            out.append(p)
    return out


def migrate_config() -> bool:
    """把旧版遗留的配置搬到 ~/MemoryBooster/config.json（只搬一次）。"""
    dst = config_path()
    if os.path.exists(dst):
        return False
    for src in _legacy_config_paths():
        if not os.path.exists(src):
            continue
        try:
            with open(src, "r", encoding="utf-8") as f:
                data = json.load(f)
            with open(dst, "w", encoding="utf-8") as f:
                json.dump(data, f, ensure_ascii=False, indent=2)
            return True
        except Exception:
            continue
    return False


def load_config() -> dict:
    cfg = dict(DEFAULT_CONFIG)
    if not os.path.exists(config_path()):
        migrate_config()
    try:
        with open(config_path(), "r", encoding="utf-8") as f:
            data = json.load(f)
        cfg.update({k: v for k, v in data.items() if k in DEFAULT_CONFIG})
    except Exception:
        pass
    return cfg


def save_config(cfg: dict):
    try:
        with open(config_path(), "w", encoding="utf-8") as f:
            json.dump(cfg, f, ensure_ascii=False, indent=2)
    except Exception:
        pass


def asset_path(name: str) -> str:
    """资源文件路径（PyInstaller onefile 解压目录 / 源码目录）。"""
    base = getattr(sys, "_MEIPASS", None) or os.path.dirname(os.path.abspath(__file__))
    for p in (os.path.join(base, name), os.path.join(base, "assets", name),
              os.path.join(os.path.dirname(os.path.abspath(__file__)), "assets", name)):
        if os.path.exists(p):
            return p
    return ""


# ---------------------------------------------------------------------------
# 5) 开机自启（计划任务优先，HKCU 登录项兜底）
# ---------------------------------------------------------------------------
TASK_NAME = "MemoryBooster_Autostart"
CREATE_NO_WINDOW = 0x08000000

# 自启有两条路，差别只在启动后拿到的权限：
#   ① 计划任务 + RunLevel=Highest —— 需要管理员创建，登录后直接以管理员身份
#      启动且不弹 UAC；任务定义写在 C:\Windows\System32\Tasks\，那个目录
#      ACL 只允许 Administrators/SYSTEM 写。
#   ② HKCU\...\Run 登录项 —— 任何用户都能写，登录后以「普通权限」启动，
#      同样不弹 UAC，只是没有提升后的能力（例如清 standby 需要特权）。
# 所以「开机自启」本身不需要管理员，只有「免 UAC 的提权自启」才需要。
RUN_KEY = r"Software\Microsoft\Windows\CurrentVersion\Run"
RUN_VALUE = "MemoryBooster"
TASK_FILE = os.path.join(os.environ.get("SystemRoot", r"C:\Windows"),
                         "System32", "Tasks", TASK_NAME)


def _skip_task():
    # 仅用于无桌面/CI 环境测试时跳过计划任务探测；正常使用不会设置该变量
    return os.environ.get("MEMORY_BOOSTER_SKIP_TASK") == "1"


def _autostart_cmd() -> str:
    if getattr(sys, "frozen", False):
        return '"%s" --tray' % sys.executable
    script = os.path.abspath(__file__)
    pyw = os.path.join(os.path.dirname(sys.executable), "pythonw.exe")
    interp = pyw if os.path.exists(pyw) else sys.executable
    return '"%s" "%s" --tray' % (interp, script)


def task_exists() -> bool:
    """计划任务是否存在。

    先看任务定义文件（只读、不起子进程），查不到再问 schtasks 兜底：
    标准用户对管理员建的 HIGHEST 任务可能既读不到文件也查不到任务。
    """
    if os.name != "nt":
        return False
    try:
        if os.path.exists(TASK_FILE):
            return True
    except Exception:
        pass
    if _skip_task():
        return False
    try:
        r = subprocess.run(["schtasks", "/Query", "/TN", TASK_NAME],
                           capture_output=True, text=True, creationflags=CREATE_NO_WINDOW)
        return r.returncode == 0
    except Exception:
        return False


def run_value() -> str:
    """HKCU 登录项里我们那条命令，没有就是空串。"""
    if os.name != "nt" or winreg is None:
        return ""
    try:
        with winreg.OpenKey(winreg.HKEY_CURRENT_USER, RUN_KEY) as k:
            return str(winreg.QueryValueEx(k, RUN_VALUE)[0])
    except OSError:
        return ""


def _set_run_value(value):
    """value 为空串表示删除。返回 (ok, err)。"""
    if os.name != "nt" or winreg is None:
        return False, "仅支持 Windows"
    try:
        try:
            k = winreg.OpenKey(winreg.HKEY_CURRENT_USER, RUN_KEY, 0, winreg.KEY_SET_VALUE)
        except FileNotFoundError:
            k = winreg.CreateKeyEx(winreg.HKEY_CURRENT_USER, RUN_KEY, 0, winreg.KEY_SET_VALUE)
        try:
            if value:
                winreg.SetValueEx(k, RUN_VALUE, 0, winreg.REG_SZ, value)
            else:
                try:
                    winreg.DeleteValue(k, RUN_VALUE)
                except FileNotFoundError:
                    pass
        finally:
            winreg.CloseKey(k)
        return True, ""
    except Exception as e:
        return False, str(e)


def _task_cmd(action: str, cmd: str = ""):
    if action == "create":
        args = ["schtasks", "/Create", "/TN", TASK_NAME, "/TR", cmd,
                "/SC", "ONLOGON", "/RL", "HIGHEST", "/F"]
    else:
        args = ["schtasks", "/Delete", "/TN", TASK_NAME, "/F"]
    try:
        r = subprocess.run(args, capture_output=True, text=True, creationflags=CREATE_NO_WINDOW)
        return r.returncode == 0, ((r.stdout or "") + (r.stderr or "")).strip()
    except Exception as e:
        return False, str(e)


def autostart_enabled() -> bool:
    if os.name != "nt":
        return False
    return task_exists() or bool(run_value())


def set_autostart(enable: bool):
    """打开/关闭开机自启。返回 (ok, 说明文字)。

    打开时优先建计划任务（能顺带拿到管理员权限）；不是管理员、或者任务创建
    失败（组策略禁用任务计划等），就退回 HKCU 登录项。那条路不需要任何权限。
    """
    if os.name != "nt":
        return False, "仅支持 Windows"

    cmd = _autostart_cmd()

    if enable:
        if not is_admin() and task_exists():
            # 已经有管理员建好的 HIGHEST 任务，别再叠一条登录项导致启动两份
            _set_run_value("")
            return True, "计划任务（以最高权限启动）"
        if is_admin() and not _skip_task():
            ok, out = _task_cmd("create", cmd)
            if ok:
                _set_run_value("")  # 清掉可能残留的登录项，避免登录时启动两份
                return True, "计划任务（以最高权限启动）"
            ok2, err2 = _set_run_value(cmd)
            if ok2:
                return True, "登录项（普通权限；计划任务创建失败：%s）" % (out or err2)
            return False, out or err2
        ok, err = _set_run_value(cmd)
        return (True, "登录项（普通权限）") if ok else (False, err)

    # 关闭：两条路都要收干净
    ok_run, err_run = _set_run_value("")
    if task_exists():
        if not is_admin():
            return False, ("登录项已关闭，但计划任务「%s」需要管理员权限才能删除" % TASK_NAME)
        ok_t, out_t = _task_cmd("delete")
        if not ok_t:
            return False, out_t
        return True, "已关闭（计划任务已删除）"
    return (True, "已关闭") if ok_run else (False, err_run)


def relaunch_as_admin(extra_args=None) -> bool:
    """以管理员身份重新启动自己。返回 True 表示已拉起新实例，调用方应退出。"""
    if os.name != "nt":
        return False
    try:
        if getattr(sys, "frozen", False):
            exe = sys.executable
            args = list(sys.argv[1:])
        else:
            exe = sys.executable
            args = list(sys.argv)
        if extra_args:
            args.extend(extra_args)
        params = subprocess.list2cmdline(args)
        # SW_SHOWNORMAL=1；返回值 > 32 才算成功（<=32 是各类错误码）
        rc = shell32.ShellExecuteW(None, "runas", exe, params, None, 1)
        return int(rc) > 32
    except Exception:
        return False


# ---------------------------------------------------------------------------
# 6) 系统托盘
# ---------------------------------------------------------------------------
WM_USER = 0x0400
WM_TRAYICON = WM_USER + 1
WM_LBUTTONDBLCLK = 0x0203
WM_RBUTTONUP = 0x0205
NIF_MESSAGE = 0x1
NIF_ICON = 0x2
NIF_TIP = 0x4
NIF_INFO = 0x10
NIM_ADD = 0x0
NIM_MODIFY = 0x1
NIM_DELETE = 0x2
HWND_MESSAGE = -3
IDI_APPLICATION = 32512
IMAGE_ICON = 1
LR_LOADFROMFILE = 0x10


def icon_file_path() -> str:
    """内嵌图标落地后的临时路径（打包/源码运行都可用）。"""
    if app_icon is None:
        return asset_path("icon.ico")
    return app_icon.icon_file()


def app_hicon(size=16):
    """托盘图标：从内嵌 ICO 里按 DPI 取对应尺寸，缺什么退什么。"""
    p = icon_file_path()
    if p:
        try:
            h = user32.LoadImageW(None, p, IMAGE_ICON, size, size, LR_LOADFROMFILE)
            if h:
                return h
        except Exception:
            pass
    try:
        exe = sys.executable if getattr(sys, "frozen", False) else None
        if exe:
            h = shell32.ExtractIconW(None, exe, 0)
            if h and int(h) > 1:
                return h
    except Exception:
        pass
    try:
        return user32.LoadIconW(0, IDI_APPLICATION)
    except Exception:
        return None


class TrayIcon:
    def __init__(self, on_restore, on_exit, tooltip=APP_NAME, icon_size=16):
        self.on_restore = on_restore
        self.on_exit = on_exit
        self.tooltip = tooltip
        self.icon_size = max(16, int(icon_size))
        self.ok = False
        self.hwnd = None
        self.nid = None
        self._hicon = None
        self._wndproc = None
        self._running = True
        self._thread = threading.Thread(target=self._run, daemon=True)
        self._thread.start()

    def _run(self):
        try:
            def wndproc(hwnd, msg, wparam, lparam):
                if msg == WM_TRAYICON:
                    if lparam == WM_LBUTTONDBLCLK:
                        self.on_restore()
                    elif lparam == WM_RBUTTONUP:
                        self._show_menu()
                    return 0
                return user32.DefWindowProcW(hwnd, msg, wparam, lparam)

            self._wndproc = WNDPROC(wndproc)
            wc = WNDCLASS()
            wc.lpfnWndProc = self._wndproc
            wc.hInstance = kernel32.GetModuleHandleW(None)
            wc.lpszClassName = "MemoryBoosterTrayWnd"
            if not user32.RegisterClassW(byref(wc)):
                return
            self.hwnd = user32.CreateWindowExW(
                0, wc.lpszClassName, None, 0, 0, 0, 0, 0,
                HWND_MESSAGE, 0, wc.hInstance, 0)
            if not self.hwnd:
                return

            hicon = app_hicon(self.icon_size)
            self._hicon = hicon

            class NOTIFYICONDATA(Structure):
                _fields_ = [
                    ("cbSize", wintypes.DWORD),
                    ("hWnd", wintypes.HWND),
                    ("uID", wintypes.UINT),
                    ("uFlags", wintypes.UINT),
                    ("uCallbackMessage", wintypes.UINT),
                    ("hIcon", wintypes.HANDLE),
                    ("szTip", wintypes.WCHAR * 128),
                ]

            nid = NOTIFYICONDATA()
            nid.cbSize = ctypes.sizeof(nid)
            nid.hWnd = self.hwnd
            nid.uID = 1
            nid.uFlags = NIF_MESSAGE | NIF_ICON | NIF_TIP
            nid.uCallbackMessage = WM_TRAYICON
            nid.hIcon = hicon
            nid.szTip = self.tooltip[:127]
            if not shell32.Shell_NotifyIconW(NIM_ADD, byref(nid)):
                return
            self.nid = nid
            self.ok = True

            msg = MSG()
            while self._running:
                r = user32.GetMessageW(byref(msg), self.hwnd, 0, 0)
                if r <= 0:
                    break
                user32.TranslateMessage(byref(msg))
                user32.DispatchMessageW(byref(msg))
        except Exception:
            self.ok = False

    def _show_menu(self):
        try:
            hmenu = user32.CreatePopupMenu()
            user32.AppendMenuW(hmenu, 0, 1001, "显示窗口")
            user32.AppendMenuW(hmenu, 0, 1002, "退出")
            pt = POINT()
            user32.GetCursorPos(byref(pt))
            user32.SetForegroundWindow(self.hwnd)
            cmd = user32.TrackPopupMenu(hmenu, 0x0100, pt.x, pt.y, 0, self.hwnd, None)
            user32.DestroyMenu(hmenu)
            if cmd == 1001:
                self.on_restore()
            elif cmd == 1002:
                self.on_exit()
        except Exception:
            pass

    def show_balloon(self, title, text):
        if not self.ok or not self.nid:
            return
        try:
            class NOTIFYICONDATA2(Structure):
                _fields_ = [
                    ("cbSize", wintypes.DWORD),
                    ("hWnd", wintypes.HWND),
                    ("uID", wintypes.UINT),
                    ("uFlags", wintypes.UINT),
                    ("uCallbackMessage", wintypes.UINT),
                    ("hIcon", wintypes.HANDLE),
                    ("szTip", wintypes.WCHAR * 128),
                    ("dwState", wintypes.DWORD),
                    ("dwStateMask", wintypes.DWORD),
                    ("szInfo", wintypes.WCHAR * 256),
                    ("uVersion", wintypes.UINT),
                    ("szInfoTitle", wintypes.WCHAR * 64),
                    ("dwInfoFlags", wintypes.DWORD),
                ]
            nid = NOTIFYICONDATA2()
            nid.cbSize = ctypes.sizeof(nid)
            nid.hWnd = self.hwnd
            nid.uID = 1
            nid.uFlags = NIF_INFO
            nid.szInfoTitle = title[:63]
            nid.szInfo = text[:255]
            nid.dwInfoFlags = 0x1
            shell32.Shell_NotifyIconW(NIM_MODIFY, byref(nid))
        except Exception:
            pass

    def destroy(self):
        self._running = False
        try:
            if self.nid:
                shell32.Shell_NotifyIconW(NIM_DELETE, byref(self.nid))
            if self.hwnd:
                user32.PostMessageW(self.hwnd, 0x0012, 0, 0)  # WM_QUIT
            if self._hicon:
                user32.DestroyIcon(self._hicon)
        except Exception:
            pass


# ---------------------------------------------------------------------------
# 7) 主题
# ---------------------------------------------------------------------------
THEMES = {
    "light": {
        "bg": "#F3F3F3", "card": "#FFFFFF", "border": "#E4E4E4", "seg_bg": "#EBEBEB",
        "track": "#E3E3E3", "text": "#1B1B1B", "dim": "#5E5E5E", "dim2": "#8C8C8C",
        "accent": "#0F6CBD", "accent_hover": "#115EA3", "on_accent": "#FFFFFF",
        "switch_off": "#C6C6C6", "btn_hover": "#3A8BD8",
        "ok_bg": "#DFF6DD", "ok_fg": "#0F7B0F", "warn": "#D97706", "bad": "#C42B1C",
        "ghost_hover": "#EDEDED", "disabled": "#C8C8C8",
        "disabled_bg": "#E3E7EA", "disabled_fg": "#5E5E5E",
    },
    "dark": {
        "bg": "#1E1E1E", "card": "#2B2B2B", "border": "#3B3B3B", "seg_bg": "#252525",
        "track": "#3C3C3C", "text": "#F2F2F2", "dim": "#B0B0B0", "dim2": "#8A8A8A",
        "accent": "#4C9DF3", "accent_hover": "#6DB2FF", "on_accent": "#10243A",
        "switch_off": "#585858", "btn_hover": "#3F8CDA",
        "ok_bg": "#16361B", "ok_fg": "#6FD36F", "warn": "#E8A33D", "bad": "#FF7A6B",
        "ghost_hover": "#333333", "disabled": "#4A4A4A",
        "disabled_bg": "#3A3A3A", "disabled_fg": "#B0B0B0",
    },
}

# 闪电轮廓（归一化坐标，供小 logo 使用）
BOLT = [(0.560, 0.250), (0.365, 0.538), (0.487, 0.538), (0.450, 0.748),
        (0.645, 0.452), (0.517, 0.452), (0.592, 0.250)]


def rounded_pts(x0, y0, x1, y1, r, steps=12):
    """顺时针生成圆角矩形顶点（逻辑坐标）。steps 越大圆角越顺滑。"""
    r = max(0.0, min(r, (x1 - x0) / 2.0, (y1 - y0) / 2.0))
    pts = []
    corners = ((x1 - r, y0 + r, -90), (x1 - r, y1 - r, 0),
               (x0 + r, y1 - r, 90), (x0 + r, y0 + r, 180))
    for cx, cy, a0 in corners:
        for i in range(steps + 1):
            a = math.radians(a0 + 90.0 * i / steps)
            pts.append((cx + r * math.cos(a), cy + r * math.sin(a)))
    return pts


def mix(c1, c2, t):
    """颜色插值：t=0 返回 c1，t=1 返回 c2。用于彗星拖尾的渐隐。"""
    def rgb(c):
        c = c.lstrip("#")
        return int(c[0:2], 16), int(c[2:4], 16), int(c[4:6], 16)
    a, b = rgb(c1), rgb(c2)
    return "#%02X%02X%02X" % tuple(
        max(0, min(255, int(round(a[i] + (b[i] - a[i]) * t)))) for i in range(3))


# ---------------------------------------------------------------------------
# 8) 界面（单 Canvas 自绘）
# ---------------------------------------------------------------------------
WIN_W, WIN_H = 400, 496
FAMILY = "Microsoft YaHei UI"

TAB_BOOST, TAB_AUTO, TAB_SET = "boost", "auto", "set"


class App:
    def __init__(self, start_hidden=False):
        self.cfg = load_config()
        self.T = THEMES.get(self.cfg.get("appearance", "light"), THEMES["light"])
        self.tab = TAB_BOOST
        self.mem = None
        self.boosting = False
        self.tray = None
        self.monitor_running = True
        self.last_auto_boost = 0.0
        self.last_res = None
        self.cfg_lock = threading.Lock()
        self.d = {}            # 当前页动态 item
        self._sliders = {}     # tag -> (x0, x1, y)
        self._anim_id = None
        self._save_job = None
        self._toast_ids = []
        self.q = queue.Queue()
        self._poll_id = None
        # 圆环动画状态（单一动画循环统一驱动，避免两套定时器打架）
        self.ring_cur = 0.0    # 当前显示的占用百分比
        self.spin_a = 0.0      # "加速中"权重 0~1（加速时渐入、完成时渐出）
        self._pulse = 0.0      # 呼吸相位（弧度）
        self._last_anim = time.time()

        enable_dpi_awareness()
        self.S = dpi_scale()

        self.root = tk.Tk()
        self.root.title(APP_NAME)
        self.root.resizable(False, False)
        self.root.configure(bg=self.T["bg"])
        try:
            # 用像素字号后 Tk 的缩放只影响内建控件，统一设成真实 DPI 更一致
            self.root.tk.call("tk", "scaling", (96.0 * self.S) / 72.0)
        except Exception:
            pass
        wpx, hpx = self.px(WIN_W), self.px(WIN_H)
        self._apply_icon()
        self.c = tk.Canvas(self.root, width=wpx, height=hpx, highlightthickness=0,
                           bd=0, bg=self.T["bg"])
        self.c.pack(fill="both", expand=True)
        self._place_bottom_right()

        self.mem = self._safe_mem()
        self.ring_cur = float(self.mem["percent"])
        self.cfg.setdefault("_autostart", False)
        self.render()
        self.root.protocol("WM_DELETE_WINDOW", self._on_close)
        self._tick()
        self.root.after(300, self._start_tray)
        self._start_monitor()
        self._poll()
        self._anim_step()
        threading.Thread(target=self._probe_autostart, daemon=True).start()
        if start_hidden:
            self.root.withdraw()

    def _probe_autostart(self):
        self.q.put(("autostart_state", autostart_enabled()))

    # ---- 跨线程队列（工作线程 -> UI 线程） ----
    def _poll(self):
        try:
            while True:
                kind, payload = self.q.get_nowait()
                self._handle(kind, payload)
        except queue.Empty:
            pass
        if self.monitor_running:
            self._poll_id = self.root.after(60, self._poll)

    def _handle(self, kind, payload):
        if kind == "boost_done":
            self._after_boost(payload)
        elif kind == "boost_err":
            self._after_boost_error(payload)
        elif kind == "auto_trigger":
            self._auto_triggered()
        elif kind == "autostart_state":
            self.cfg["_autostart"] = bool(payload)
            if self.tab == TAB_SET:
                self.render()
        elif kind == "autostart_set":
            self._after_autostart(*payload)
        elif kind == "restore":
            self._restore_now()
        elif kind == "quit":
            self._real_quit()

    # ---- 基础工具 ----
    def px(self, v):
        """逻辑坐标 -> 设备像素，四舍五入到整数。

        必须取整：Tk 的 Canvas 不做抗锯齿，落在半像素上的线和文字会被 GDI
        强行摊到两个像素上，看起来就是“发虚/糊”。
        """
        return int(round(v * self.S))

    def font(self, px, bold=False):
        """字号用「像素」而不是「点」（Tk 里负数表示像素）。

        点制字号会乘上 tk scaling 变成 12.7px 这种非整数值，中文小字尤其糊；
        像素制直接落在整数像素上，笔画才锐利。
        """
        return (FAMILY, -max(11, self.px(px)), "bold" if bold else "normal")

    def _poly(self, x0, y0, x1, y1, r, fill, tags=()):
        flat = []
        for px_, py_ in rounded_pts(x0, y0, x1, y1, r):
            flat.append(self.px(px_))
            flat.append(self.px(py_))
        return self.c.create_polygon(flat, fill=fill, outline="", tags=tags)

    def _rr(self, x0, y0, x1, y1, r=10, fill=None, outline=None, width=1, tags=()):
        """圆角矩形。有描边时用「外层描边色填充 + 内层留白填充」叠出来，
        不再用 polygon 的 1px outline —— 后者的斜边是一串硬锯齿，看着毛糙。"""
        if outline and fill and width > 0:
            w = width
            self._poly(x0, y0, x1, y1, r, outline, tags)
            return self._poly(x0 + w, y0 + w, x1 - w, y1 - w,
                              max(1.0, r - w), fill, tags)
        return self._poly(x0, y0, x1, y1, r, fill or outline or "", tags)

    def _oval(self, x0, y0, x1, y1, fill=None, outline=None, width=1, tags=()):
        return self.c.create_oval(self.px(x0), self.px(y0), self.px(x1), self.px(y1),
                                  fill=fill or "", outline=outline or "",
                                  width=max(1, self.px(width)) if outline else 0,
                                  tags=tags)

    def _text(self, x, y, txt, size=13, bold=False, color=None, anchor="center",
              tags=(), wrap=None):
        kw = {}
        if wrap:
            kw["width"] = self.px(wrap)
            kw["justify"] = "center"
        return self.c.create_text(self.px(x), self.px(y), text=txt,
                                  font=self.font(size, bold), anchor=anchor,
                                  fill=color or self.T["text"], tags=tags, **kw)

    def _arc(self, cx, cy, r, width, color, start=90, extent=-359.9, tags=()):
        w = max(1, self.px(width))
        return self.c.create_arc(self.px(cx - r), self.px(cy - r),
                                 self.px(cx + r), self.px(cy + r),
                                 start=start, extent=extent, style="arc",
                                 outline=color, width=w, tags=tags)

    def _bolt(self, cx, cy, h, color, tags=()):
        flat = []
        for x, y in BOLT:
            flat += [self.px(cx + (x - 0.5) * h), self.px(cy + (y - 0.5) * h)]
        return self.c.create_polygon(flat, fill=color, outline="", tags=tags)

    def _card(self, x0, y0, x1, y1, r=11):
        self._rr(x0, y0, x1, y1, r, fill=self.T["card"],
                 outline=self.T["border"], width=1)

    def _line(self, x0, y0, x1, y1, color):
        self.c.create_line(self.px(x0), self.px(y0), self.px(x1), self.px(y1),
                           fill=color, width=1)

    def _safe_mem(self):
        try:
            return read_memory()
        except Exception:
            return {"total": 0, "avail": 0, "in_use": 0, "in_use_gb": 0.0,
                    "total_gb": 0.0, "avail_gb": 0.0, "avail_mb": 0.0,
                    "percent": 0.0, "load": 0}

    def _apply_icon(self):
        """窗口/任务栏图标来自内嵌 ICO，不依赖外部文件。"""
        path = icon_file_path()
        if not path:
            return
        for call in (lambda: self.root.iconbitmap(path),
                     lambda: self.root.iconbitmap(default=path)):
            try:
                call()
                return
            except Exception:
                continue

    def _place_bottom_right(self):
        """把窗口贴到工作区右下角（避开任务栏）。geometry 的坐标是含标题栏的外框，
        所以要减去外框相对客户区的偏移，保证“内容”真正贴边。"""
        w, h = self.px(WIN_W), self.px(WIN_H)
        m = self.px(14)
        area = None
        try:
            r = RECT()
            if user32.SystemParametersInfoW(0x0030, 0, byref(r), 0):
                area = (r.left, r.top, r.right, r.bottom)
        except Exception:
            pass
        if not area:
            area = (0, 0, self.root.winfo_screenwidth(),
                    self.root.winfo_screenheight() - self.px(40))
        # 先粗排一次让窗口真实存在，再用实测的外框尺寸精排
        self.root.geometry("%dx%d+%d+%d" % (w, h, max(0, area[2] - w - m),
                                            max(0, area[3] - h - m - self.px(32))))
        fw, fh = w, h
        try:
            self.root.update_idletasks()
            hwnd = int(self.root.frame(), 16)
            rc = RECT()
            if user32.GetWindowRect(c_void_p(hwnd), byref(rc)):
                fw, fh = rc.right - rc.left, rc.bottom - rc.top
        except Exception:
            pass
        # 注意 geometry 的 +x+y 是「含标题栏的外框」左上角，
        # 所以要减的是外框宽高，不是客户区宽高，否则会往里缩 8px。
        x = max(0, area[2] - fw - m)
        y = max(0, area[3] - fh - m)
        self.root.geometry("%dx%d+%d+%d" % (w, h, x, y))

    def _ring_extent(self, pct):
        """圆环填充角度。
        注意：Tk 会把 extent 对 360 取模，-360 会变成 0（整圈消失），
        所以 100% 用 -359.9 代替 —— 差 0.1 度，肉眼看不出。"""
        return -min(359.9, 3.6 * max(0.0, min(100.0, pct)))

    def _pct_color(self, pct):
        if pct >= 85:
            return self.T["bad"]
        if pct >= 70:
            return self.T["warn"]
        return self.T["accent"]

    # ---- 渲染 ----
    def render(self):
        self.c.delete("all")
        self.d.clear()
        self._sliders.clear()
        self._toast_ids = []
        self._draw_header()
        self._draw_tabs()
        if self.tab == TAB_BOOST:
            self._draw_boost()
            self._paint_ring()
        elif self.tab == TAB_AUTO:
            self._draw_auto()
        else:
            self._draw_settings()

    def _draw_header(self):
        T = self.T
        self._rr(16, 12, 40, 36, 8, fill=T["accent"])
        self._bolt(28, 24, 16.5, "#FFFFFF")
        self._text(47, 24, APP_NAME, 15, True, T["text"], anchor="w")
        badge = "管理员" if is_admin() else "普通权限"
        bw = len(badge) * 11 + 18
        bx = WIN_W - 16 - bw
        self._rr(bx, 13, WIN_W - 16, 35, 11, fill=T["card"],
                 outline=T["border"], width=1)
        self._text(bx + bw / 2.0, 24, badge, 11, False, T["dim"])
        self._text(bx - 9, 24, "v" + APP_VERSION, 11, False, T["dim2"], anchor="e")

    def _draw_tabs(self):
        T = self.T
        x0, x1, y0, y1 = 16, WIN_W - 16, 48, 80
        self._rr(x0, y0, x1, y1, 16, fill=T["seg_bg"])
        tabs = ((TAB_BOOST, "加速"), (TAB_AUTO, "自动"), (TAB_SET, "设置"))
        seg = (x1 - x0) / 3.0
        for i, (key, label) in enumerate(tabs):
            bx = x0 + i * seg
            sel = self.tab == key
            if sel:
                self._rr(bx + 2.5, y0 + 2.5, bx + seg - 2.5, y1 - 2.5, 13.5,
                         fill=T["card"], outline=T["border"], width=1)
            self._text(bx + seg / 2.0, (y0 + y1) / 2.0, label, 14, sel,
                       T["text"] if sel else T["dim"], tags=("tab_" + key,))
            self.c.tag_bind("tab_" + key, "<Button-1>",
                            lambda e, k=key: self.switch_tab(k))
            self.c.tag_bind("tab_" + key, "<Enter>",
                            lambda e: self.c.configure(cursor="hand2"))
            self.c.tag_bind("tab_" + key, "<Leave>",
                            lambda e: self.c.configure(cursor=""))

    # ---- 加速页 ----
    def _used_text(self):
        m = self.mem
        return "已用 %.2f GB · 可用 %.2f GB" % (m["in_use_gb"], m["avail_gb"])

    def _draw_boost(self):
        T = self.T
        cx, cy, r, ring_w = WIN_W / 2.0, 178, 62, 11.5

        self._arc(cx, cy, r, ring_w, T["track"], extent=-359.9)
        self.d["ring"] = self._arc(cx, cy, r, ring_w, self._pct_color(self.ring_cur),
                                   extent=self._ring_extent(self.ring_cur))
        # 满环另用一个完整椭圆，避免 arc 起终点在 12 点方向留缝
        self.d["ring_full"] = self.c.create_oval(
            self.px(cx - r), self.px(cy - r), self.px(cx + r), self.px(cy + r),
            fill="", outline=self._pct_color(self.ring_cur),
            width=max(1, self.px(ring_w)), state="hidden")

        self.d["pct"] = self._text(cx, 162, "%.0f%%" % self.ring_cur, 33, True,
                                   self._pct_color(self.ring_cur))
        self._text(cx, 194, "内存占用", 12, False, T["dim2"])
        self.d["used"] = self._text(cx, 258, self._used_text(), 13, False, T["dim"])

        # 主按钮
        bx0, bx1, by0, by1 = 44, WIN_W - 44, 282, 332
        on = self.boosting
        self.d["btn"] = self._rr(bx0, by0, bx1, by1, 10,
                                 fill=T["disabled_bg"] if on else T["accent"],
                                 tags=("btn",))
        self.d["btn_txt"] = self._text(
            WIN_W / 2.0, (by0 + by1) / 2.0,
            "正在加速…" if on else "一键加速", 15, True,
            T["disabled_fg"] if on else "#FFFFFF", tags=("btn",))
        self._bind_btn()

        # 三格统计
        res = self.last_res
        labels = ("释放", "修剪进程", "临时文件")
        gap, x0, x1 = 8, 16, WIN_W - 16
        cw = (x1 - x0 - gap * 2) / 3.0
        for i, label in enumerate(labels):
            cx0 = x0 + i * (cw + gap)
            self._rr(cx0, 346, cx0 + cw, 404, 10, fill=T["card"],
                     outline=T["border"], width=1)
            self._text(cx0 + cw / 2.0, 364, label, 12, False, T["dim"])
            if res:
                v, sz, bold = self._cell_value(res, i), 15, True
            else:
                v, sz, bold = "—", 13, False
            self.d["cell%d" % i] = self._text(
                cx0 + cw / 2.0, 386, v, sz, bold,
                (T["accent"] if i == 0 else T["text"]) if res else T["dim2"])

        if res:
            txt = "上次 %s · %s" % (res.get("time", "--:--:--"),
                                    "自动触发" if res.get("auto") else "手动加速")
        else:
            txt = "还没有加速记录"
        self.d["last"] = self._text(WIN_W / 2.0, 424, txt, 12, False, T["dim2"])
        self._text(WIN_W / 2.0, 462,
                   "非分页池(NPP)内核内存无法回收，异常请用 RAMMap 排查",
                   12, False, T["dim2"], wrap=WIN_W - 56)

    def _bind_btn(self):
        if self.boosting:
            for ev in ("<Button-1>", "<Enter>", "<Leave>"):
                self.c.tag_unbind("btn", ev)
            return
        T = self.T
        self.c.tag_bind("btn", "<Button-1>", lambda e: self._on_boost())
        self.c.tag_bind("btn", "<Enter>",
                        lambda e: (self.c.configure(cursor="hand2"),
                                   self.c.itemconfig(self.d["btn"], fill=T["btn_hover"])))
        self.c.tag_bind("btn", "<Leave>",
                        lambda e: (self.c.configure(cursor=""),
                                   self.c.itemconfig(self.d["btn"], fill=T["accent"])))

    def _paint_ring(self):
        """把 ring_cur / spin_a 画到画布上。只做 itemconfig，不重建 item。"""
        if "ring" not in self.d:
            return
        T = self.T
        pct = max(0.0, min(100.0, self.ring_cur))
        a = min(1.0, self.spin_a)
        # 加速中把环底色压淡（改成浅蓝），让亮色拖尾浮在上面看得出来；
        # 颜色随拖尾强度插值，不会从"橙的高占用色"硬跳到"蓝"。
        # 加速中数字涨到 100% 会触发"高占用"红色，像在报警；这里随拖尾强度
        # 平滑过渡到主题蓝，避免这层误导。
        k = min(1.0, a * 1.6)
        # 加速中环固定走主题蓝（数字冲到 100% 不该变红报警）
        ring_col = mix(self._pct_color(pct), T["accent"], k)
        base = ring_col
        try:
            if pct >= 98.0:
                self.c.itemconfig(self.d["ring"], state="hidden")
                self.c.itemconfig(self.d["ring_full"], state="normal",
                                  outline=ring_col)
            else:
                self.c.itemconfig(self.d["ring_full"], state="hidden")
                self.c.itemconfig(self.d["ring"], state="normal",
                                  extent=self._ring_extent(pct), outline=ring_col)
            self.c.itemconfig(self.d["pct"], text="%.0f%%" % pct, fill=base)
        except Exception:
            return
    def _anim_step(self):
        """唯一的动画时钟（16ms 活跃 / 80ms 空闲）。

        数值用「指数逼近」而不是固定时长的补间：无论加速实际耗多久，
        圆环都是连续平滑地涨到 100%、再连续平滑地回落到真实值，不会有跳变。
        """
        self._anim_id = None
        if not self.monitor_running:
            return
        now = time.time()
        dt = min(0.06, max(0.004, now - self._last_anim))
        self._last_anim = now
        busy = False

        # 1) 拖尾强度（与当前标签页无关，保证切页后能收敛）
        tgt_a = 1.0 if (self.boosting and self.tab == TAB_BOOST) else 0.0
        da = tgt_a - self.spin_a
        if abs(da) > 0.004:
            self.spin_a += da * (1.0 - math.exp(-dt / 0.16))
            busy = True
        else:
            self.spin_a = tgt_a

        # 2) 环值
        if self.boosting:
            target, tau = 100.0, 0.30
        else:
            target, tau = float(self.mem["percent"]), 0.42
        diff = target - self.ring_cur
        if abs(diff) > 0.06:
            self.ring_cur += diff * (1.0 - math.exp(-dt / tau))
            busy = True
        elif self.ring_cur != target:
            self.ring_cur = target
            busy = True

        # 3) 呼吸相位（按时间累加，与帧率解耦）
        if self.spin_a > 0.02:
            self._pulse = (self._pulse + dt * 2.0 * math.pi / 1.25) % (2.0 * math.pi)
            busy = True

        if self.tab == TAB_BOOST and "ring" in self.d:
            self._paint_ring()

        self._anim_id = self.root.after(16 if busy else 80, self._anim_step)

    # ---- 自动页 ----
    def _draw_auto(self):
        T = self.T
        gb = self.cfg["threshold_mode"] == "gb"

        # 卡片1：总开关
        self._card(16, 92, WIN_W - 16, 160)
        self._text(32, 114, "自动加速", 13, True, T["text"], anchor="w")
        self._text(32, 138, "内存超过阈值时在后台自动释放", 12, False, T["dim2"], anchor="w")
        self._switch(WIN_W - 32 - 46, 113, bool(self.cfg["auto_enabled"]), "tg_auto")

        # 卡片2：阈值
        self._card(16, 170, WIN_W - 16, 342)
        self._text(32, 194, "触发阈值", 13, True, T["text"], anchor="w")
        if gb:
            vt = "%.1f GB" % self.cfg["threshold_gb"]
        else:
            vt = "%.0f%%" % self.cfg["threshold_percent"]
        self.d["th_val"] = self._text(WIN_W - 32, 194, vt, 13, True, T["accent"], anchor="e")

        self._segment(32, 210, WIN_W - 32, 242,
                      (("gb", "按已用内存 GB"), ("percent", "按占用率 %")),
                      self.cfg["threshold_mode"], "seg_")

        lo, hi, val = self._th_range()
        self._slider(32, WIN_W - 32, 278, (val - lo) / float(hi - lo), "sl_th")
        self._text(32, 296, self._fmt_th(lo), 12, False, T["dim2"], anchor="w")
        self._text(WIN_W - 32, 296, self._fmt_th(hi), 12, False, T["dim2"], anchor="e")
        self.d["th_hint"] = self._text(WIN_W / 2.0, 322, self._th_hint(), 12, False, T["dim2"])

        # 卡片3：最小间隔
        self._card(16, 352, WIN_W - 16, 448)
        self._text(32, 376, "最小加速间隔", 13, True, T["text"], anchor="w")
        self.d["int_val"] = self._text(
            WIN_W - 32, 376, "%d 分钟" % (self.cfg["min_boost_interval_seconds"] // 60),
            13, True, T["accent"], anchor="e")
        mins = self.cfg["min_boost_interval_seconds"] // 60
        self._slider(32, WIN_W - 32, 411, (mins - 1) / 14.0, "sl_int")
        self._text(32, 431, "1 分钟", 12, False, T["dim2"], anchor="w")
        self._text(WIN_W - 32, 431, "15 分钟", 12, False, T["dim2"], anchor="e")

        self._text(WIN_W / 2.0, 468,
                   "自动加速先修剪进程再清缓存；手动点击不受该间隔限制",
                   12, False, T["dim2"], wrap=WIN_W - 56)

    def _th_range(self):
        if self.cfg["threshold_mode"] == "gb":
            total = self.mem["total_gb"] or 16.0
            return 2.0, max(3.0, round(total - 1, 1)), float(self.cfg["threshold_gb"])
        return 50.0, 95.0, float(self.cfg["threshold_percent"])

    def _fmt_th(self, v):
        return ("%.0f GB" % v) if self.cfg["threshold_mode"] == "gb" else ("%.0f%%" % v)

    def _th_hint(self):
        m = self.mem
        if self.cfg["threshold_mode"] == "gb":
            hit = m["in_use_gb"] >= self.cfg["threshold_gb"]
            return "当前已用 %.2f GB，%s" % (m["in_use_gb"], "已超过阈值" if hit else "低于阈值")
        hit = m["percent"] >= self.cfg["threshold_percent"]
        return "当前占用 %.1f%%，%s" % (m["percent"], "已超过阈值" if hit else "低于阈值")

    # ---- 设置页 ----
    def _draw_settings(self):
        T = self.T
        self._card(16, 92, WIN_W - 16, 332)
        rows = (
            ("开机自启", "登录后以最高权限自动启动，免 UAC",
             "sw_start", bool(self.cfg.get("_autostart", False))),
            ("关闭窗口时最小化到托盘", "双击托盘图标可恢复窗口",
             "sw_tray", bool(self.cfg["minimize_to_tray"])),
            ("加速时清理临时文件", "只删除 %TEMP% 里 7 天前的文件",
             "sw_temp", bool(self.cfg["clean_temp"])),
        )
        for i, (title, sub, tag, on) in enumerate(rows):
            top = 92 + i * 60
            self._text(32, top + 24, title, 13, True, T["text"], anchor="w")
            self._text(32, top + 44, sub, 12, False, T["dim2"], anchor="w")
            self._switch(WIN_W - 32 - 46, top + 18, on, tag)
            if i < len(rows) - 1:
                self._line(32, top + 60, WIN_W - 32, top + 60, T["border"])

        # 第 4 行：检测间隔
        top = 92 + 3 * 60
        self._text(32, top + 24, "后台检测间隔", 13, True, T["text"], anchor="w")
        self._text(32, top + 44, "越小越灵敏，会略微增加 CPU 占用", 12, False,
                   T["dim2"], anchor="w")
        self._stepper(top + 24, "chk")

        # 关于卡
        self._card(16, 342, WIN_W - 16, 434)
        self._text(32, 360, "关于", 12, True, T["dim2"], anchor="w")
        admin = "管理员（清缓存全生效）" if is_admin() else "普通权限（清缓存会失败）"
        lines = (
            ("版本", "v%s · 仿微软电脑管家" % APP_VERSION),
            ("当前权限", admin),
            ("配置文件", os.path.basename(config_path())),
        )
        for i, (k, v) in enumerate(lines):
            y = 384 + i * 19
            self._text(32, y, k, 12, False, T["dim2"], anchor="w")
            self._text(WIN_W - 32, y, v, 12, False, T["text"], anchor="e")

        # 底部按钮
        self._ghost_btn(16, 446, WIN_W / 2.0 - 5, 482,
                        "深色外观" if self.cfg["appearance"] == "light" else "浅色外观",
                        "btn_theme")
        self._ghost_btn(WIN_W / 2.0 + 5, 446, WIN_W - 16, 482, "退出程序", "btn_quit")

    def _ghost_btn(self, x0, y0, x1, y1, label, tag):
        T = self.T
        self._rr(x0, y0, x1, y1, 9, fill=T["card"], outline=T["border"], width=1,
                 tags=(tag,))
        self._text((x0 + x1) / 2.0, (y0 + y1) / 2.0, label, 13, False, T["text"],
                   tags=(tag,))
        self.c.tag_bind(tag, "<Button-1>", lambda e: self._on_action(tag))
        self.c.tag_bind(tag, "<Enter>", lambda e: self.c.configure(cursor="hand2"))
        self.c.tag_bind(tag, "<Leave>", lambda e: self.c.configure(cursor=""))

    # ---- 控件：开关 / 分段 / 滑块 / 步进 ----
    def _switch(self, x, y, on, tag):
        T = self.T
        w, h = 46, 26
        self._rr(x, y, x + w, y + h, h / 2.0, fill=T["accent"] if on else T["switch_off"],
                 tags=(tag,))
        kx = x + w - h / 2.0 if on else x + h / 2.0
        self._oval(kx - 8, y + 4, kx + 8, y + h - 4, fill="#FFFFFF", tags=(tag,))
        self.c.tag_bind(tag, "<Button-1>", lambda e: self._on_switch(tag))
        self.c.tag_bind(tag, "<Enter>", lambda e: self.c.configure(cursor="hand2"))
        self.c.tag_bind(tag, "<Leave>", lambda e: self.c.configure(cursor=""))

    def _segment(self, x0, y0, x1, y1, items, current, prefix):
        T = self.T
        seg = (x1 - x0) / float(len(items))
        # 整条作为命中区（先画，后续胶囊与文字覆盖其上）
        hit = "segarea_" + prefix
        self._rr(x0, y0, x1, y1, (y1 - y0) / 2.0, fill=T["seg_bg"], tags=(hit,))
        self.c.tag_bind(hit, "<Button-1>", lambda e: self._on_segment_click(
            items, x0, seg, e))
        self.c.tag_bind(hit, "<Enter>", lambda e: self.c.configure(cursor="hand2"))
        self.c.tag_bind(hit, "<Leave>", lambda e: self.c.configure(cursor=""))
        for i, (key, label) in enumerate(items):
            bx0 = x0 + i * seg
            sel = key == current
            if sel:
                self._rr(bx0 + 2, y0 + 2, bx0 + seg - 2, y1 - 2, (y1 - y0 - 4) / 2.0,
                         fill=T["card"], outline=T["border"], width=1)
            self._text(bx0 + seg / 2.0, (y0 + y1) / 2.0, label, 13, sel,
                       T["text"] if sel else T["dim"])

    def _on_segment_click(self, items, x0, seg, event):
        idx = int((event.x / self.S - x0) // seg)
        idx = max(0, min(len(items) - 1, idx))
        self._on_mode(items[idx][0])

    def _slider(self, x0, x1, y, frac, tag):
        T = self.T
        frac = max(0.0, min(1.0, frac))
        self._sliders[tag] = (x0, x1, y)
        # 大命中区（与卡片同色，视觉不可见）
        self.c.create_rectangle(self.px(x0), self.px(y - 13), self.px(x1), self.px(y + 13),
                                fill=T["card"], outline="", tags=(tag,))
        self._rr(x0, y - 3, x1, y + 3, 3, fill=T["track"])
        kx = x0 + (x1 - x0) * frac
        self.d["fill_" + tag] = self._rr(x0, y - 3, max(x0 + 7, kx), y + 3, 3,
                                         fill=T["accent"])
        self.d["knob_" + tag] = self._oval(
            kx - 8.5, y - 8.5, kx + 8.5, y + 8.5,
            fill=T["card"], outline=T["accent"], width=2)
        self.c.tag_bind(tag, "<Button-1>", lambda e, t=tag: self._on_slider(t, e))
        self.c.tag_bind(tag, "<B1-Motion>", lambda e, t=tag: self._on_slider(t, e))

    def _set_slider(self, tag, frac):
        if tag not in self._sliders:
            return
        x0, x1, y = self._sliders[tag]
        frac = max(0.0, min(1.0, frac))
        kx = x0 + (x1 - x0) * frac
        fill_id = self.d.get("fill_" + tag)
        if fill_id:
            pts = rounded_pts(x0, y - 3, max(x0 + 7, kx), y + 3, 3)
            flat = [self.px(c) for p in pts for c in p]
            self.c.coords(fill_id, *flat)
        knob = self.d.get("knob_" + tag)
        if knob:
            self.c.coords(knob, self.px(kx - 8.5), self.px(y - 8.5),
                          self.px(kx + 8.5), self.px(y + 8.5))

    def _stepper(self, y, tag):
        T = self.T
        val = int(self.cfg["check_interval_seconds"])
        xr = WIN_W - 32
        self._rr(xr - 104, y - 13, xr - 78, y + 13, 7, fill=T["card"],
                 outline=T["border"], width=1, tags=("m_" + tag,))
        self._text(xr - 91, y, "−", 14, False, T["text"], tags=("m_" + tag,))
        self._rr(xr - 26, y - 13, xr, y + 13, 7, fill=T["card"],
                 outline=T["border"], width=1, tags=("p_" + tag,))
        self._text(xr - 13, y, "+", 14, False, T["text"], tags=("p_" + tag,))
        self.d["step_" + tag] = self._text(xr - 52, y, "%d 秒" % val, 12, True, T["text"])
        self.c.tag_bind("m_" + tag, "<Button-1>", lambda e: self._on_step(tag, -5))
        self.c.tag_bind("p_" + tag, "<Button-1>", lambda e: self._on_step(tag, 5))
        for t in ("m_" + tag, "p_" + tag):
            self.c.tag_bind(t, "<Enter>", lambda e: self.c.configure(cursor="hand2"))
            self.c.tag_bind(t, "<Leave>", lambda e: self.c.configure(cursor=""))

    # ---- 交互 ----
    def switch_tab(self, key):
        if self.tab == key:
            return
        self.tab = key
        self.render()

    def _on_action(self, tag):
        if tag == "btn_theme":
            self.cfg["appearance"] = "dark" if self.cfg["appearance"] == "light" else "light"
            self.T = THEMES[self.cfg["appearance"]]
            self.root.configure(bg=self.T["bg"])
            self.c.configure(bg=self.T["bg"])
            self.render()
            self._save_later()
        elif tag == "btn_quit":
            self._real_quit()

    def _on_switch(self, tag):
        if tag == "tg_auto":
            self.cfg["auto_enabled"] = not bool(self.cfg["auto_enabled"])
            self.render()
            self._save_later()
        elif tag == "sw_tray":
            self.cfg["minimize_to_tray"] = not bool(self.cfg["minimize_to_tray"])
            self.render()
            self._save_later()
        elif tag == "sw_temp":
            self.cfg["clean_temp"] = not bool(self.cfg["clean_temp"])
            self.render()
            self._save_later()
        elif tag == "sw_start":
            self._toggle_autostart()

    def _toggle_autostart(self):
        want = not bool(self.cfg.get("_autostart", False))

        def work():
            ok, msg = set_autostart(want)
            self.q.put(("autostart_set", (want, ok, msg)))

        threading.Thread(target=work, daemon=True).start()

    def _after_autostart(self, want, ok, msg):
        if ok:
            self.cfg["_autostart"] = want
            self.render()
        else:
            self.render()
            if not is_admin():
                self._msg("需要管理员权限",
                          "设置开机自启需要管理员权限。\n请右键程序选择「以管理员身份运行」后再试。")
            else:
                self._msg("设置失败", (msg or "未知错误")[:300])

    def _on_mode(self, mode):
        if self.cfg["threshold_mode"] == mode:
            return
        self.cfg["threshold_mode"] = mode
        self.render()
        self._save_later()

    def _on_slider(self, tag, event):
        x0, x1, _ = self._sliders[tag]
        frac = (event.x / self.S - x0) / float(x1 - x0)
        frac = max(0.0, min(1.0, frac))
        self._set_slider(tag, frac)
        if tag == "sl_th":
            lo, hi, _ = self._th_range()
            v = lo + (hi - lo) * frac
            if self.cfg["threshold_mode"] == "gb":
                self.cfg["threshold_gb"] = round(v, 1)
                txt = "%.1f GB" % self.cfg["threshold_gb"]
            else:
                self.cfg["threshold_percent"] = float(int(round(v)))
                txt = "%.0f%%" % self.cfg["threshold_percent"]
            if "th_val" in self.d:
                self.c.itemconfig(self.d["th_val"], text=txt)
            if "th_hint" in self.d:
                self.c.itemconfig(self.d["th_hint"], text=self._th_hint())
        elif tag == "sl_int":
            mins = int(round(1 + 14 * frac))
            self.cfg["min_boost_interval_seconds"] = mins * 60
            if "int_val" in self.d:
                self.c.itemconfig(self.d["int_val"], text="%d 分钟" % mins)
        self._save_later()

    def _on_step(self, tag, delta):
        v = int(self.cfg["check_interval_seconds"]) + delta
        v = max(5, min(120, v))
        self.cfg["check_interval_seconds"] = v
        if "step_" + tag in self.d:
            self.c.itemconfig(self.d["step_" + tag], text="%d 秒" % v)
        self._save_later()

    def _save_later(self):
        if self._save_job:
            try:
                self.root.after_cancel(self._save_job)
            except Exception:
                pass
        self._save_job = self.root.after(500, self._save_now)

    def _save_now(self):
        self._save_job = None
        with self.cfg_lock:
            data = {k: v for k, v in self.cfg.items() if not k.startswith("_")}
        save_config(data)

    def _msg(self, title, text):
        try:
            messagebox.showinfo(title, text, parent=self.root)
        except Exception:
            pass

    # ---- 加速 ----
    def _on_boost(self):
        if self.boosting:
            return
        self.boosting = True
        # 局部更新按钮，不做全量重绘 —— 重绘会重建所有 item，圆环会闪一下，
        # 动画立刻就不连贯了。
        if self.tab == TAB_BOOST and "btn" in self.d:
            self.c.itemconfig(self.d["btn"], fill=self.T["disabled_bg"])
            self.c.itemconfig(self.d["btn_txt"], text="正在加速…",
                              fill=self.T["disabled_fg"])
            self.c.configure(cursor="")
            self._bind_btn()
        threading.Thread(target=self._boost_worker, args=(False,), daemon=True).start()

    def _boost_worker(self, auto):
        try:
            with self.cfg_lock:
                snapshot = dict(self.cfg)
            res = boost_with_config(snapshot)
            res["time"] = time.strftime("%H:%M:%S")
            res["auto"] = auto
            self.q.put(("boost_done", res))
        except Exception as e:
            self.q.put(("boost_err", e))

    def _cell_value(self, res, i):
        return ("%.2f GB" % res["freed_gb"], "%d 个" % res["trim_count"],
                "%d 个" % res["temp_removed"])[i]

    def _update_cells(self):
        res = self.last_res
        if not res:
            return
        for i in range(3):
            cid = self.d.get("cell%d" % i)
            if cid:
                self.c.itemconfig(cid, text=self._cell_value(res, i),
                                  font=self.font(15, True),
                                  fill=self.T["accent"] if i == 0 else self.T["text"])

    def _btn_flash(self, text, fill, fg, hold_ms=2400):
        """按钮临时变成结果色，播完再自动回到「一键加速」。"""
        if self.tab != TAB_BOOST or "btn" not in self.d:
            return
        self.c.itemconfig(self.d["btn"], fill=fill)
        self.c.itemconfig(self.d["btn_txt"], text=text, fill=fg)
        for ev in ("<Button-1>", "<Enter>", "<Leave>"):
            self.c.tag_unbind("btn", ev)
        self.root.after(hold_ms, self._btn_reset)

    def _btn_reset(self):
        if self.tab != TAB_BOOST or "btn" not in self.d or self.boosting:
            return
        self.c.itemconfig(self.d["btn"], fill=self.T["accent"])
        self.c.itemconfig(self.d["btn_txt"], text="一键加速", fill="#FFFFFF")
        self._bind_btn()

    def _after_boost(self, res):
        self.boosting = False
        self.last_res = res
        if res.get("auto"):
            self.last_auto_boost = time.time()
        self.mem = self._safe_mem()
        if self.tab == TAB_BOOST and "btn" in self.d:
            # 只更新文字与数字，圆环交给动画时钟平滑回落
            self._update_cells()
            if "used" in self.d:
                self.c.itemconfig(self.d["used"], text=self._used_text())
            if "last" in self.d:
                self.c.itemconfig(self.d["last"], text="上次 %s · %s" % (
                    res.get("time", "--:--:--"),
                    "自动触发" if res.get("auto") else "手动加速"))
            # 等圆环回落一段再出结果，衔接更自然
            txt = "✓ 已释放 %.2f GB" % res["freed_gb"]
            self.root.after(560, lambda: self._btn_flash(
                txt, self.T["ok_bg"], self.T["ok_fg"]))
        else:
            self.ring_cur = float(self.mem["percent"])

    def _after_boost_error(self, exc):
        self.boosting = False
        if self.tab == TAB_BOOST and "btn" in self.d:
            self._btn_flash("加速失败", self.T["disabled_bg"], self.T["bad"])
            self.root.after(2500, self._btn_reset)
        else:
            self.render()

    # ---- 定时刷新 ----
    def _tick(self):
        if not self.monitor_running:
            return
        self.mem = self._safe_mem()
        if self.tab == TAB_BOOST:
            if "used" in self.d:
                self.c.itemconfig(self.d["used"], text=self._used_text())
        elif self.tab == TAB_AUTO and "th_hint" in self.d:
            self.c.itemconfig(self.d["th_hint"], text=self._th_hint())
        self.root.after(1200, self._tick)

    # ---- 自动监控 ----
    def _start_monitor(self):
        threading.Thread(target=self._monitor, daemon=True).start()

    def _monitor(self):
        while self.monitor_running:
            time.sleep(1.0)
            with self.cfg_lock:
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
                m = read_memory()
            except Exception:
                continue
            triggered = (mode == "gb" and m["in_use_gb"] > th_gb) or \
                        (mode == "percent" and m["percent"] > th_pct)
            now = time.time()
            if triggered and (now - self.last_auto_boost) >= min_int:
                self.last_auto_boost = now
                self.q.put(("auto_trigger", None))

    def _auto_triggered(self):
        if self.boosting:
            return
        self.boosting = True
        if self.tab == TAB_BOOST and "btn" in self.d:
            self.c.itemconfig(self.d["btn"], fill=self.T["disabled_bg"])
            self.c.itemconfig(self.d["btn_txt"], text="正在加速…",
                              fill=self.T["disabled_fg"])
            self._bind_btn()
        threading.Thread(target=self._boost_worker, args=(True,), daemon=True).start()

    # ---- 托盘 / 关闭 ----
    def _start_tray(self):
        self.tray = TrayIcon(self._restore, self._quit, icon_size=self.px(16))

    def _on_close(self):
        if self.cfg["minimize_to_tray"] and self.tray and self.tray.ok:
            self.root.withdraw()
            self.tray.show_balloon(APP_NAME, "已最小化到系统托盘，双击图标恢复。")
        else:
            self._real_quit()

    def _restore(self):
        self.q.put(("restore", None))

    def _restore_now(self):
        self.root.deiconify()
        self._place_bottom_right()
        self.root.lift()
        self.root.focus_force()

    def _quit(self):
        self.q.put(("quit", None))

    def _real_quit(self):
        self.monitor_running = False
        for aid in (self._poll_id, self._anim_id, self._save_job):
            try:
                if aid:
                    self.root.after_cancel(aid)
            except Exception:
                pass
        self._anim_id = None
        self._poll_id = None
        try:
            self._save_now()
        except Exception:
            pass
        try:
            if self.tray:
                self.tray.destroy()
        except Exception:
            pass
        try:
            self.root.destroy()
        except Exception:
            pass

    def run(self):
        self.root.mainloop()


# ---------------------------------------------------------------------------
# 9) CLI
# ---------------------------------------------------------------------------
def run_cli(args):
    cfg = load_config()
    if args.threshold_gb is not None:
        cfg["threshold_mode"] = "gb"
        cfg["threshold_gb"] = args.threshold_gb
    if args.threshold_percent is not None:
        cfg["threshold_mode"] = "percent"
        cfg["threshold_percent"] = args.threshold_percent
    if args.min_interval is not None:
        cfg["min_boost_interval_seconds"] = max(60, int(args.min_interval))
    if args.check_interval is not None:
        cfg["check_interval_seconds"] = max(2, int(args.check_interval))

    m = read_memory()
    print("内存：总量 %.1fGB，已用 %.2fGB (%.1f%%)，可用 %.0fMB" % (
        m["total_gb"], m["in_use_gb"], m["percent"], m["avail_mb"]))
    print("管理员权限：%s" % ("是" if is_admin() else "否（效果打折）"))

    if args.once:
        res = boost_with_config(cfg)
        print("加速完成：已用 %.2f→%.2f GB，释放 %.2f GB；修剪进程 %d（失败 %d，保护 %d，"
              "跨会话跳过 %d），清缓存 %s，清理临时文件 %d 个，模式 %s" % (
                  res["before_gb"], res["after_gb"], res["freed_gb"], res["trim_count"],
                  res["trim_fail"], res["protected"], res["skipped_session"],
                  "成功" if res["standby_ok"] else "未执行", res["temp_removed"], res["mode"]))
        return

    if args.auto:
        mode = cfg["threshold_mode"]
        th = cfg["threshold_gb"] if mode == "gb" else cfg["threshold_percent"]
        th_str = ("%.2f GB" % th) if mode == "gb" else ("%.1f%%" % th)
        print("自动模式：阈值 %s，轮询 %ds，最小间隔 %ds（Ctrl+C 退出）" % (
            th_str, cfg["check_interval_seconds"], cfg["min_boost_interval_seconds"]))
        last = 0.0
        try:
            while True:
                time.sleep(cfg["check_interval_seconds"])
                mm = read_memory()
                triggered = (mode == "gb" and mm["in_use_gb"] > cfg["threshold_gb"]) or \
                            (mode == "percent" and mm["percent"] > cfg["threshold_percent"])
                now = time.time()
                if triggered and (now - last) >= cfg["min_boost_interval_seconds"]:
                    last = now
                    res = boost_with_config(cfg)
                    print("[%s] 自动加速：已用 %.2f→%.2f GB，释放 %.2f GB" % (
                        time.strftime("%H:%M:%S"), res["before_gb"], res["after_gb"],
                        res["freed_gb"]))
        except KeyboardInterrupt:
            print("\n已退出。")
        return

    print("用法：")
    print("  python memory_booster.py            # 图形界面")
    print("  python memory_booster.py --tray     # 启动即隐藏到托盘")
    print("  python memory_booster.py --once     # 立即加速一次")
    print("  python memory_booster.py --auto     # 后台按阈值自动加速")


def main():
    parser = argparse.ArgumentParser(description="仿微软电脑管家内存加速工具")
    parser.add_argument("--once", action="store_true", help="执行一次加速后退出")
    parser.add_argument("--auto", action="store_true", help="后台按阈值自动加速")
    parser.add_argument("--tray", action="store_true", help="启动即隐藏到托盘")
    parser.add_argument("--threshold-gb", type=float, default=None)
    parser.add_argument("--threshold-percent", type=float, default=None)
    parser.add_argument("--min-interval", type=int, default=None)
    parser.add_argument("--check-interval", type=int, default=None)
    args = parser.parse_args()

    if args.once or args.auto:
        run_cli(args)
        return
    if not TK_AVAILABLE:
        print("未检测到 tkinter，切换到命令行模式。")
        run_cli(args)
        return

    app = App(start_hidden=bool(args.tray))
    app.run()


if __name__ == "__main__":
    main()
