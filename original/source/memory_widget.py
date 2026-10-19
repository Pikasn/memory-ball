# -*- coding: utf-8 -*-
"""
Memory Widget · v4

A tiny, dependency-free Windows memory widget with a calm, Apple-flavoured UI.

Rules this file follows:

* One accent colour (orange). Green and red exist only as a small status
  system; there is no blue anywhere, no gradients and no permanent blur.
* Light surfaces express hierarchy through tone and spacing, not borders.
* Layout is authored in 96-dpi design units and resolved to physical pixels for
  the display it runs on, while fonts are requested in pixels too. That keeps
  the panel the same physical size, and the text the same relative size, on a
  100% or a 200% display instead of letting scaled text collide with fixed
  coordinates.
* Every rounded or translucent shape is rasterised once into a cached PNG and
  layered over one opaque card plate, so Tk never strokes a jagged arc and
  never blends against the colour key except at the very outer rim.
* The idle path stays small: one Win32 memory query per interval, one Tk event
  loop, no worker threads, no network, no periodic disk or registry access.

Run:
    pythonw memory_widget.py
    python  memory_widget.py --selftest
    python  memory_widget.py --probe-menu      # 冒烟测试用，见 _start_probe()
"""

from __future__ import annotations

import atexit
import ctypes
import ctypes.wintypes as wt
import faulthandler
import functools
import json
import math
import os
import struct
import sys
# `threading` 只被 `_exit_now()` 用到，而且只在退出那一刻起一个**活着的时间以
# 毫秒计**的 daemon 线程，专门用来执行 `TerminateProcess`。
# 程序的运行期依然是单线程、无后台任务 —— 这一点没有变。
# 之所以非如此不可：在 Tk 回调里直接终止进程会挂住（详见 `_exit_now` 的注释），
# 换一个干净的线程来做就一次不挂。
import threading
import time
import tkinter as tk
import winreg
import zlib
from collections import deque

# 托盘图标是独立模块，只在源码目录里，不进 site-packages。
# PyInstaller 会按 `--onefile` 把它一起打进去（构建脚本显式带了
# `--hidden-import tray_icon` 并加了 `--paths source/`）。
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import gpu_info                                         # noqa: E402
import tray_icon                                        # noqa: E402
from detail_chart import chart_png                      # noqa: E402


APP_DIR = os.path.dirname(
    os.path.abspath(sys.executable if getattr(sys, "frozen", False) else __file__)
)
SETTINGS_PATH = os.path.join(APP_DIR, "settings.json")
# 图标资源要跟着 exe 走：冻结后 `__file__` 指向解包目录 `_MEI…`，
# 而 `app.ico` 是被 `--icon` 嵌进 PE 资源段的，文件系统里没有副本。
# 所以冻结时优先用 exe 旁边的同名 .ico，取不到就退回 exe 自身的图标
# （见 `tray_icon.TrayIcon._acquire_icon` 的兜底）。
ICON_PATH = os.path.join(APP_DIR, "app.ico")
# Crash/error log. Written next to settings.json (same APP_DIR logic) so the
# frozen exe and the source run agree on one location. This file must never
# raise and never pop a dialog: it is the net under every other safety net.
ERROR_LOG_PATH = os.path.join(APP_DIR, "memorywidget-error.log")
NATIVE_ERROR_LOG_PATH = os.path.join(APP_DIR, "memorywidget-native-error.log")
_native_error_file = None
# Single-file cap. Beyond this the file is rewritten with only its tail kept,
# so a runaway error cannot grow the log without bound.
_ERROR_LOG_MAX_BYTES = 256 * 1024
AUTOSTART_NAME = "MemoryRing"
MUTEX_NAME = r"Local\MemoryWidget.v4"
TRANSPARENT = "#010203"

# 圆环边缘抗锯齿过渡带在**两套主题**下各自混合到的底色。
#
# 为什么需要它：窗口靠 `-transparentcolor` 做抠图，画布背景就是那个键色 `TRANSPARENT`。
# 而 Tk 会把 PNG 的 alpha 通道**混到画布背景上** —— 于是半透明像素会被键色（近乎纯黑）
# 拖暗：覆盖率 60% 的边缘像素出来是 `0.6*卡片色 + 0.4*(1,2,3)`，比卡片色暗约 110 级。
# 这条暗边贴着浅色圆盘，就是"毛边"的真实来源。实测见 `tools/_tk_key_tint.py`：
# 白色色块在 alpha 64/128/192 下读回 `(64,65,66)` / `(128,128,129)` / `(192,192,192)`，
# 全都被键色染了。
#
# 所以现在的做法是：**在光栅化器里就把混合算完，像素输出 alpha=255**。
# Tk 没有 alpha 可混，键色就渗不进来，边缘是设计好的那个颜色，且只混一次。
#
# 底色必须跟主题走。浅色主题下取卡片色压暗一档（读作球体边缘自然收暗）；
# 深色主题的卡片色本身已经很暗（#1C1C1E），若还混向浅灰就会在球外圈出一道
# **发亮的光晕**，比浅色主题的暗边更刺眼。深色主题取卡片色提亮一档，
# 读法同样是"边缘自然过渡"。
RIM_BACKDROP = {"light": "#C9C9CE", "dark": "#3A3A3F"}

# 圆盘边缘抗锯齿过渡带的宽度，单位像素。
#
# 为什么不是 1.0：恰好一个像素宽的过渡带（`0.5 + r - d`）只有在某个像素中心
# 恰好落在数学边界半个像素以内时，才会产生中间值。当边界正好卡在两个像素中心
# 之间时，两个像素一个全内一个全外，曲线上就出现一道硬台阶 —— 这正是锯齿的来源。
# 放宽到约 1.6 像素可以保证任何位置都至少有一个像素是部分覆盖的，
# 代价是边缘铺开约两个像素：而曲线本来就应该长这样。
RIM_FEATHER = 1.6

# 右键菜单的行定义：(标签, 行顶设计单位, 显示文字)。
# 绘制、探针点击、命中判定三处都从这里取，避免各写一份而漂移 ——
# 之前探针按坐标点、绘制按 (6,50) 排版，两边一错位就点空了。
MENU_ROWS = (("settings", 6, "设置…"), ("quit", 50, "退出"))

# 右键菜单在 `show` 的瞬间会自己丢一次焦点（见 `_show_context_menu`）。
# 收到 `<FocusOut>` 后等这么久再确认一次真实焦点，避免菜单"弹出来立刻没"。
MENU_FOCUS_GRACE_MS = 300


# ---------------------------------------------------------------------------
# Layout is written in 96-dpi design units. D() converts them to the physical
# pixels of the display this build is running on, and every font is requested
# in pixels for the same reason: Tk scales point sizes itself, which silently
# breaks any fixed coordinate system on a scaled display.
# ---------------------------------------------------------------------------

UI_SCALE = 1.0
# Set by the offscreen UI check to render at a forced scale; None in normal use.
UI_SCALE_OVERRIDE = None
# Extra uniform scale applied only while the detail panel is drawn (see D()).
_DETAIL_SCALE = 1.0
FONTS = {"ui": "Segoe UI", "display": "Segoe UI", "mono": "Consolas"}

DETAIL_W = 340
PAD = 24
# Header row: the pin toggle sits between the title and the close button. Both
# buttons are the same 26-unit square, separated by `HEADER_GAP`; the close one
# is flush with the right padding.
HEADER_H = 26
HEADER_TOP = 19
PIN_SIZE = 26
HEADER_GAP = 8
HERO_BOTTOM = 108
BAR_TOP = 123
BAR_H = 6
CAPACITY_BASELINE = 160
BASE_H = 196
# GPU 区（温度 + 占用率进度条）。只有在**本机真的读得到独显数据**时才占版面，
# 所以高度必须动态算 —— 见 `_gpu_h()`。无 N 卡的机器上这整块不出现，
# 面板不会留一片空白。
GPU_H = 70
HISTORY_H = 170
TOP5_H = 185
ROW_H = 22
SEG_W, SEG_H = 143 / 3, 28
SETTINGS_W, SETTINGS_H = 400, 686
MENU_W, MENU_H = 168, 96

# How far the detail panel may be resized, as a multiple of its design size.
MIN_DETAIL_SCALE = 0.7
MAX_DETAIL_SCALE = 2.0

# -- Win32 window subclassing -------------------------------------------------
#
# Tk owns the window procedure of every window it creates and offers no hook for
# messages it does not model. The tray callback is exactly such a message
# (`WM_USER + 21`), so the only way to receive it is to replace the window
# procedure and forward everything we do not handle to the original.
#
# `GWL_WNDPROC` is the 32-bit selector (-4). `SetWindowLongPtrW` does not exist
# as an export on 32-bit Windows, so it is probed once and falls back to
# `SetWindowLongW`; this keeps the same source working on both builds.
GWL_WNDPROC = -4
WM_CLOSE = 0x0010
WM_QUERYENDSESSION = 0x0011
WM_ENDSESSION = 0x0016
# Sent when the window lands on a display with a different DPI. wParam packs
# X/Y DPI in its low/high words; the wndproc records X DPI and `_loop` applies it (see
# `_apply_dpi_change`) - same ctypes-flag discipline as `WM_CLOSE`.
WM_DPICHANGED = 0x02E0

# 外部关闭请求的轮询间隔。取值理由见 `watch_close_request`：
# 它必须明显短于最小轮询周期（1000 ms），否则"点了关闭窗口立刻消失、
# 进程却要等下一轮轮询才开始退"就会被看成没退出。
CLOSE_WATCH_MS = 120

_SetWindowLongPtrW = getattr(tray_icon.user32, "SetWindowLongPtrW", None)
_GetWindowLongPtrW = getattr(tray_icon.user32, "GetWindowLongPtrW", None)
if _SetWindowLongPtrW is None:
    # 32-bit Windows 没有 `SetWindowLongPtrW` 这个导出，只有 `SetWindowLongW`。
    # 两者的 `nIndex` 语义一致，`GWL_WNDPROC` 这个负值选择子两边都认。
    _SetWindowLongPtrW = tray_icon.user32.SetWindowLongW
if _GetWindowLongPtrW is None:
    _GetWindowLongPtrW = tray_icon.user32.GetWindowLongW

# 必须显式声明签名。ctypes 默认把返回值当 `c_int`，而窗口过程句柄在 64 位下
# 是 8 字节 —— 不声明的话，刚设进去的指针会被截断成 32 位再读回来，
# 还原窗口过程时写回一个错的地址，退出直接崩。
_ULONG_PTR = ctypes.c_ssize_t
_SetWindowLongPtrW.restype = _ULONG_PTR
_SetWindowLongPtrW.argtypes = (wt.HWND, ctypes.c_int, _ULONG_PTR)
_GetWindowLongPtrW.restype = _ULONG_PTR
_GetWindowLongPtrW.argtypes = (wt.HWND, ctypes.c_int)

_WNDPROC = ctypes.WINFUNCTYPE(
    ctypes.c_ssize_t, wt.HWND, wt.UINT, wt.WPARAM, wt.LPARAM)
_CallWindowProcW = tray_icon.user32.CallWindowProcW
_CallWindowProcW.restype = ctypes.c_ssize_t
_CallWindowProcW.argtypes = (ctypes.c_void_p, wt.HWND, wt.UINT, wt.WPARAM,
                             wt.LPARAM)


def D(value: float) -> int:
    """Design units -> physical pixels for this display.

    `_DETAIL_SCALE` is an extra uniform factor applied only while the detail
    panel is being drawn, so its whole UI scales together when the user drags
    the resize corner. It is 1.0 everywhere else.
    """
    return int(round(value * UI_SCALE * _DETAIL_SCALE))


def _at_detail_scale(method):
    """Draw `method` with D()/fonts/plates scaled by the panel's resize factor.

    The detail panel is the only part of the UI that the user can resize. While
    it is being drawn, every design unit is multiplied by `self.detail_scale`
    (on top of the DPI scale), so the whole structure - text, plates, the
    chart, the spacing - changes together and stays proportional.
    """
    @functools.wraps(method)
    def wrapper(self, *args, **kwargs):
        global _DETAIL_SCALE
        previous = _DETAIL_SCALE
        _DETAIL_SCALE = self.detail_scale
        try:
            return method(self, *args, **kwargs)
        finally:
            _DETAIL_SCALE = previous
    return wrapper


def init_ui_scale(root) -> None:
    global UI_SCALE
    if UI_SCALE_OVERRIDE:
        UI_SCALE = float(UI_SCALE_OVERRIDE)
        return
    try:
        dpi = float(root.winfo_fpixels("1i"))
    except (tk.TclError, TypeError, ValueError):
        dpi = 96.0
    if not 60.0 < dpi < 480.0:
        dpi = 96.0
    UI_SCALE = min(3.0, max(1.0, dpi / 96.0))


def ui_font(size: float, weight: str = "normal"):
    return (FONTS["ui"], -max(1, D(size)), weight)


def display_font(size: float, weight: str = "normal"):
    return (FONTS["display"], -max(1, D(size)), weight)


def mono_font(size: float, weight: str = "normal"):
    return (FONTS["mono"], -max(1, D(size)), weight)


DEFAULTS = {
    "ring_size": 72,
    "warn_yellow": 70,
    "warn_red": 85,
    "poll_interval_ms": 2000,
    "theme": "system",
    "autostart": False,
    "hover_expand": False,
    "history_enabled": False,
    "top5_enabled": False,
    "pin_top": False,
    "pos": None,
    "detail_scale": 1.0,
    "panel_offset": None,
}

S = dict(DEFAULTS)
_AUTO_THEME_CACHE = None

THEMES = {
    "light": {
        "card": "#FCFCFD",
        "card_soft": "#F3F3F6",
        "surface": "#EAEAEE",
        "surface_hover": "#E1E1E6",
        "track": "#E6E6EA",
        "text": "#1D1D1F",
        "secondary": "#6E6E73",
        "tertiary": "#A3A3A9",
        "hairline": "#EAEAEE",
        "orange": "#F59D1F",
        "orange_ink": "#8C5504",
        "green": "#2FA36B",
        "green_ink": "#1B7A4B",
        "green_fill": "#E4F4EB",
        "warn_fill": "#FBEFD8",
        "red": "#DE4B3F",
        "red_ink": "#B02A1F",
        "red_fill": "#FBE9E6",
        "chart_area": "#FDF3E3",
        "chart_band": "#F7E8D2",
        "switch_off": "#D6D6DB",
        "on_knob": "#FFFFFF",
    },
    "dark": {
        "card": "#1C1C1E",
        "card_soft": "#28282B",
        "surface": "#333338",
        "surface_hover": "#3E3E44",
        "track": "#3A3A3E",
        "text": "#F2F2F5",
        "secondary": "#A2A2A9",
        "tertiary": "#7B7B84",
        "hairline": "#2E2E33",
        "orange": "#F5A32B",
        "orange_ink": "#FFC978",
        "green": "#32D074",
        "green_ink": "#5FE08F",
        "green_fill": "#17381F",
        "warn_fill": "#3A2E17",
        "red": "#FF6257",
        "red_ink": "#FF9A90",
        "red_fill": "#3C1F1C",
        "chart_area": "#2B2418",
        "chart_band": "#3A2F1D",
        "switch_off": "#48484E",
        "on_knob": "#FFFFFF",
    },
}

# Clear reading hierarchy shared by the actual native UI and its previews.
THEMES["light"].update(
    card="#FFFFFF", card_soft="#F5F5F7", surface="#F0F1F4",
    surface_hover="#E9EAEE", hairline="#ECEEF2", text="#222329",
    secondary="#626874", tertiary="#6F7580", track="#EDEEF2",
    chart_area="#FFF8EB", chart_band="#F7EBD8", selected="#FFFFFF",
    edge="#E4E6EB", grid="#F0F1F4",
)
THEMES["dark"].update(
    card="#202125", card_soft="#2B2C31", surface="#2B2C31",
    surface_hover="#34363C", hairline="#303238", text="#F1F2F5",
    secondary="#B4B9C4", tertiary="#9DA3B0", track="#34363D",
    chart_area="#2C271F", chart_band="#3D3020", selected="#42454D",
    edge="#3A3D45", grid="#2C2E34",
)

# Bytes caches may be flushed freely. The PhotoImage cache may not: live canvas
# items borrow their pixels from it.
_BITMAP_CACHE: dict = {}
_PHOTO_CACHE: dict = {}
# The interpreter the photo cache belongs to, and a widget on it; see
# set_photo_master() for why tk._default_root is not enough.
_PHOTO_INTERP = None
_PHOTO_MASTER = None


def _enable_high_dpi() -> None:
    """Ask Windows for crisp Tk rendering. Safe no-op on older Windows."""
    set_context = ctypes.windll.user32.SetProcessDpiAwarenessContext
    # Returns HANDLE (a fake "success" pointer). Declared explicitly: without
    # `restype` ctypes reads only 32 bits of it and a failed call can look
    # successful, letting the fallback below be skipped wrongly.
    set_context.argtypes = (wt.HANDLE,)
    set_context.restype = wt.HANDLE
    try:
        set_context(ctypes.c_void_p(-4))
    except (AttributeError, OSError):
        try:
            ctypes.windll.shcore.SetProcessDpiAwareness(2)
        except (AttributeError, OSError):
            pass


def resolve_fonts(root) -> None:
    """Pick the best families this machine actually has, once."""
    try:
        from tkinter import font as tkfont
        families = {name.casefold() for name in tkfont.families(root)}
    except tk.TclError:
        return

    def pick(candidates, fallback):
        for candidate in candidates:
            if candidate.casefold() in families:
                return candidate
        return fallback

    FONTS["ui"] = pick(
        ["Segoe UI Variable Text", "Segoe UI", "Microsoft YaHei UI"], "TkDefaultFont"
    )
    FONTS["display"] = pick(
        ["Segoe UI Variable Display", "Segoe UI Semibold", "Segoe UI"], "TkDefaultFont"
    )
    FONTS["mono"] = pick(["Cascadia Mono", "Consolas", "Courier New"], "TkFixedFont")


def _system_theme() -> str:
    """Read the Windows app theme once; this is never polled."""
    try:
        with winreg.OpenKey(
            winreg.HKEY_CURRENT_USER,
            r"Software\Microsoft\Windows\CurrentVersion\Themes\Personalize",
        ) as key:
            value, _ = winreg.QueryValueEx(key, "AppsUseLightTheme")
            return "light" if value else "dark"
    except OSError:
        return "dark"


def theme_name() -> str:
    global _AUTO_THEME_CACHE
    requested = S.get("theme", "system")
    if requested != "system":
        return requested
    if _AUTO_THEME_CACHE is None:
        _AUTO_THEME_CACHE = _system_theme()
    return _AUTO_THEME_CACHE


def palette() -> dict:
    return THEMES.get(theme_name(), THEMES["light"])


def load_settings() -> None:
    try:
        with open(SETTINGS_PATH, "r", encoding="utf-8") as file:
            saved = json.load(file)
    except (OSError, ValueError, TypeError):
        return
    if not isinstance(saved, dict):
        return
    for key in DEFAULTS:
        if key in saved:
            S[key] = saved[key]

    if S["ring_size"] not in (64, 72, 96):
        S["ring_size"] = DEFAULTS["ring_size"]
    if S["poll_interval_ms"] not in (1000, 2000, 4000):
        S["poll_interval_ms"] = DEFAULTS["poll_interval_ms"]
    if S["theme"] not in ("system", "light", "dark"):
        S["theme"] = DEFAULTS["theme"]
    try:
        S["detail_scale"] = min(MAX_DETAIL_SCALE,
                                max(MIN_DETAIL_SCALE, float(S["detail_scale"])))
    except (KeyError, TypeError, ValueError):
        S["detail_scale"] = DEFAULTS["detail_scale"]
    try:
        S["warn_yellow"] = min(95, max(40, int(S["warn_yellow"])))
        S["warn_red"] = min(99, max(S["warn_yellow"] + 1, int(S["warn_red"])))
    except (TypeError, ValueError):
        S["warn_yellow"] = DEFAULTS["warn_yellow"]
        S["warn_red"] = DEFAULTS["warn_red"]
    # `bool` is a subclass of `int`, so a bare `isinstance(v, int)` would let
    # `true/false` in a hand-edited settings.json through as coordinates.
    # Exclude it explicitly.
    if not (
        isinstance(S.get("pos"), list)
        and len(S["pos"]) == 2
        and all(isinstance(v, int) and not isinstance(v, bool)
                for v in S["pos"])
    ):
        S["pos"] = None
    if not (
        isinstance(S.get("panel_offset"), list)
        and len(S["panel_offset"]) == 2
        and all(isinstance(v, int) and not isinstance(v, bool)
                for v in S["panel_offset"])
    ):
        S["panel_offset"] = None


def save_settings() -> bool:
    try:
        temporary_path = SETTINGS_PATH + ".tmp"
        with open(temporary_path, "w", encoding="utf-8") as file:
            json.dump(S, file, ensure_ascii=False, indent=2)
        os.replace(temporary_path, SETTINGS_PATH)
        return True
    except OSError:
        return False


def set_autostart(enable: bool) -> None:
    key = winreg.OpenKey(
        winreg.HKEY_CURRENT_USER,
        r"Software\Microsoft\Windows\CurrentVersion\Run",
        0,
        winreg.KEY_SET_VALUE,
    )
    try:
        if enable:
            if getattr(sys, "frozen", False):
                command = f'"{sys.executable}"'
            else:
                executable = sys.executable
                if executable.lower().endswith("python.exe"):
                    executable = executable[:-10] + "pythonw.exe"
                # Dead-link guard: registering a Run entry that points at a
                # missing interpreter creates a startup error dialog on every
                # logon and the entry can never work. Refuse instead; the
                # caller (`_set_option`) surfaces this as a settings error.
                if not os.path.exists(executable):
                    raise OSError("pythonw.exe not found: %s" % executable)
                command = f'"{executable}" "{os.path.abspath(__file__)}"'
            winreg.SetValueEx(key, AUTOSTART_NAME, 0, winreg.REG_SZ, command)
        else:
            try:
                winreg.DeleteValue(key, AUTOSTART_NAME)
            except FileNotFoundError:
                pass
    finally:
        winreg.CloseKey(key)


def _prune_dead_autostart() -> None:
    """Drop our Run key if its target (exe / script / pythonw) is gone.

    Why: the widget can be moved, renamed or uninstalled by hand, and Windows
    never cleans the Run key up itself - every logon then shows a "cannot find"
    error for a program that no longer exists. Cheapest correct place to fix
    it is here: at startup, one registry read (never polled). Never raises.
    """
    try:
        key = winreg.OpenKey(
            winreg.HKEY_CURRENT_USER,
            r"Software\Microsoft\Windows\CurrentVersion\Run",
            0,
            winreg.KEY_QUERY_VALUE | winreg.KEY_SET_VALUE,
        )
    except OSError:
        return
    try:
        try:
            command, _ = winreg.QueryValueEx(key, AUTOSTART_NAME)
        except FileNotFoundError:
            return
        target = command.strip()
        if target.startswith('"'):
            end = target.find('"', 1)
            target = target[1:end] if end > 0 else target.strip('"')
        else:
            target = target.split()[0] if target.split() else ""
        if target and not os.path.exists(target):
            try:
                winreg.DeleteValue(key, AUTOSTART_NAME)
            except OSError:
                pass
            log_error("autostart: removed dead Run entry -> %s" % target)
    except OSError:
        pass
    finally:
        winreg.CloseKey(key)


def acquire_single_instance():
    """Own the single-instance mutex, or None when the widget already runs.

    `use_last_error=True` is not optional here: ctypes itself calls Win32
    routines while marshalling, so reading the error back through
    `kernel32.GetLastError()` can return a value left behind by one of those.
    A valid handle was once mistaken for "already running" that way, and the
    app exited without ever showing a window - which the user experiences as
    "the exe won't open".
    """
    kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
    kernel32.CreateMutexW.argtypes = (ctypes.c_void_p, wt.BOOL, wt.LPCWSTR)
    kernel32.CreateMutexW.restype = wt.HANDLE
    kernel32.CloseHandle.argtypes = (wt.HANDLE,)
    kernel32.CloseHandle.restype = wt.BOOL
    handle = kernel32.CreateMutexW(None, False, MUTEX_NAME)
    error = ctypes.get_last_error()
    if handle:
        if error == 183:                       # ERROR_ALREADY_EXISTS
            kernel32.CloseHandle(handle)
            return None
        return handle
    if error == 5:                             # ERROR_ACCESS_DENIED
        # A copy of the widget started with higher rights owns the mutex.
        # That is still "already running" - just one this process cannot see.
        return None
    return handle


def notify_box(text: str, title: str = "内存悬浮窗") -> None:
    """Say something to the user even though the build has no console."""
    ctypes.windll.user32.MessageBoxW(None, text, title, 0x40)


def log_error(text: str) -> None:
    """Append one timestamped line to the error log. Never raises, never shows
    a dialog.

    Why this exists: every safety net in this file (Tk callback guard,
    sys.excepthook, report_callback_exception, save_settings failures) funnels
    into here, and the funnel itself must be bulletproof - an exception while
    reporting an exception would take the whole process down silently.

    When the file grows past `_ERROR_LOG_MAX_BYTES` it is rewritten keeping
    only its tail, so the newest evidence survives and the file stays small.
    """
    try:
        stamp = time.strftime("%Y-%m-%d %H:%M:%S")
        with open(ERROR_LOG_PATH, "a", encoding="utf-8") as fh:
            fh.write("%s %s\n" % (stamp, text))
        if os.path.getsize(ERROR_LOG_PATH) <= _ERROR_LOG_MAX_BYTES:
            return
        with open(ERROR_LOG_PATH, "rb") as fh:
            data = fh.read()
        tail = data[-(_ERROR_LOG_MAX_BYTES // 2):]
        newline = tail.find(b"\n")
        if newline >= 0:
            tail = tail[newline + 1:]
        with open(ERROR_LOG_PATH, "wb") as fh:
            fh.write(tail)
    except Exception:                                    # noqa: BLE001
        pass


def _log_exc(context: str, exc_type, exc_value, exc_tb) -> None:
    """Format one exception into `log_error`. Never raises."""
    try:
        import traceback
        detail = "".join(
            traceback.format_exception(exc_type, exc_value, exc_tb)).strip()
        log_error("%s: %s" % (context, detail))
    except Exception:                                    # noqa: BLE001
        pass


def _hooked_excepthook(exc_type, exc_value, exc_tb) -> None:
    """sys.excepthook replacement: route unhandled exceptions to the log.

    Without this, an exception on a non-Tk thread (or after mainloop ends)
    prints to stderr - which a windowed PyInstaller build does not have - and
    the process just vanishes with no evidence left anywhere.
    """
    _log_exc("unhandled exception", exc_type, exc_value, exc_tb)


def _report_callback_exception(exc_type, exc_value, exc_tb) -> None:
    """Tk callback exception handler. Replaces Tk's default, which only prints
    to stderr (invisible in a windowed build) and otherwise lets the callback
    chain die - one bad tick then silently freezes the whole widget.
    """
    _log_exc("tk callback exception", exc_type, exc_value, exc_tb)


def _install_exception_hooks(root=None) -> None:
    """Install the process-wide and per-root exception nets. Never raises."""
    try:
        sys.excepthook = _hooked_excepthook
    except Exception:                                    # noqa: BLE001
        pass
    if root is not None:
        try:
            root.report_callback_exception = _report_callback_exception
        except Exception:                                # noqa: BLE001
            pass


def _install_native_error_log() -> None:
    """Retain fatal Python/Tcl thread stacks in a windowed build."""
    global _native_error_file
    if _native_error_file is not None:
        return
    try:
        _native_error_file = open(NATIVE_ERROR_LOG_PATH, "a", encoding="utf-8")
        faulthandler.enable(file=_native_error_file, all_threads=True)
    except (OSError, RuntimeError):
        if _native_error_file is not None:
            _native_error_file.close()
        _native_error_file = None


# ---------------------------------------------------------------------------
# Win32 readings
# ---------------------------------------------------------------------------

class _MemStatusEx(ctypes.Structure):
    _fields_ = [
        ("dwLength", wt.DWORD),
        ("dwMemoryLoad", wt.DWORD),
        ("ullTotalPhys", ctypes.c_ulonglong),
        ("ullAvailPhys", ctypes.c_ulonglong),
        ("ullTotalPageFile", ctypes.c_ulonglong),
        ("ullAvailPageFile", ctypes.c_ulonglong),
        ("ullTotalVirtual", ctypes.c_ulonglong),
        ("ullAvailVirtual", ctypes.c_ulonglong),
        ("ullAvailExtendedVirtual", ctypes.c_ulonglong),
    ]


class _MonitorInfo(ctypes.Structure):
    _fields_ = [
        ("cbSize", wt.DWORD),
        ("rcMonitor", wt.RECT),
        ("rcWork", wt.RECT),
        ("dwFlags", wt.DWORD),
    ]


def _is_descendant_of(hwnd, ancestors) -> bool:
    """`hwnd` 是否是 `ancestors` 里某个窗口的后代（含自身）。

    `GetForegroundWindow()` 返回的常常是子窗口（本程序的 Tk canvas 就是
    `TkTopLevel` 的子窗口），所以判断"前台是不是自己"必须往上走一遍父链，
    只比顶层句柄会漏掉。
    """
    if not hwnd:
        return False
    wanted = set(int(item) for item in ancestors)
    current = int(hwnd)
    # HWND is pointer-sized on 64-bit; undeclared, ctypes would truncate it to
    # c_int and the parent walk could compare garbage.
    get_parent = ctypes.windll.user32.GetParent
    get_parent.argtypes = (wt.HWND,)
    get_parent.restype = wt.HWND
    for _ in range(32):                      # 父链不可能有这么深，纯保险
        if current in wanted:
            return True
        current = int(get_parent(ctypes.c_void_p(current)) or 0)
        if not current:
            return False
    return False


def monitor_work_area(x: int, y: int):
    """Return the nearest monitor's usable desktop rectangle."""
    try:
        user32 = ctypes.windll.user32
        user32.MonitorFromPoint.argtypes = (wt.POINT, wt.DWORD)
        user32.MonitorFromPoint.restype = wt.HANDLE
        user32.GetMonitorInfoW.argtypes = (wt.HANDLE, ctypes.POINTER(_MonitorInfo))
        user32.GetMonitorInfoW.restype = wt.BOOL
        monitor = user32.MonitorFromPoint(wt.POINT(int(x), int(y)), 2)
        info = _MonitorInfo()
        info.cbSize = ctypes.sizeof(info)
        if monitor and user32.GetMonitorInfoW(monitor, ctypes.byref(info)):
            rect = info.rcWork
            return (rect.left, rect.top, rect.right, rect.bottom)
    except (AttributeError, OSError, TypeError):
        pass
    return (
        0, 0,
        ctypes.windll.user32.GetSystemMetrics(0),
        ctypes.windll.user32.GetSystemMetrics(1),
    )


def read_memory():
    """Return (load %, used GiB, total GiB, available GiB) or None."""
    status = _MemStatusEx()
    status.dwLength = ctypes.sizeof(_MemStatusEx)
    # Declared explicitly: `restype` defaults to c_int which happens to work
    # for BOOL, but an undeclared byref argument can be marshalled wrongly on
    # some ctypes builds - the signature is the contract.
    gms = ctypes.windll.kernel32.GlobalMemoryStatusEx
    gms.argtypes = (ctypes.POINTER(_MemStatusEx),)
    gms.restype = wt.BOOL
    if not gms(ctypes.byref(status)):
        return None
    gib = 1024 ** 3
    total = status.ullTotalPhys
    available = status.ullAvailPhys
    return (
        int(status.dwMemoryLoad),
        (total - available) / gib,
        total / gib,
        available / gib,
    )


class _ProcMemCounters(ctypes.Structure):
    _fields_ = [
        ("cb", wt.DWORD),
        ("PageFaultCount", wt.DWORD),
        ("PeakWorkingSetSize", ctypes.c_size_t),
        ("WorkingSetSize", ctypes.c_size_t),
        ("QuotaPeakPagedPoolUsage", ctypes.c_size_t),
        ("QuotaPagedPoolUsage", ctypes.c_size_t),
        ("QuotaPeakNonPagedPoolUsage", ctypes.c_size_t),
        ("QuotaNonPagedPoolUsage", ctypes.c_size_t),
        ("PagefileUsage", ctypes.c_size_t),
        ("PeakPagefileUsage", ctypes.c_size_t),
    ]


def _process_name(handle) -> str | None:
    size = wt.DWORD(1024)
    buffer = ctypes.create_unicode_buffer(1024)
    kernel32 = ctypes.windll.kernel32
    query_name = kernel32.QueryFullProcessImageNameW
    query_name.argtypes = (wt.HANDLE, wt.DWORD, wt.LPWSTR, ctypes.POINTER(wt.DWORD))
    query_name.restype = wt.BOOL
    if query_name(handle, 0, buffer, ctypes.byref(size)):
        name = os.path.basename(buffer.value)
        if name:
            return name

    # Some protected processes reject the query above but still expose a device
    # path through PSAPI. A name we cannot read is skipped entirely: the UI
    # never falls back to a "PID xxxx" placeholder.
    fallback = ctypes.windll.psapi.GetProcessImageFileNameW
    fallback.argtypes = (wt.HANDLE, wt.LPWSTR, wt.DWORD)
    fallback.restype = wt.DWORD
    if fallback(handle, buffer, 1024):
        name = os.path.basename(buffer.value.replace("\\", "/"))
        if name:
            return name
    return None


def group_processes(entries, limit: int = 5):
    """Merge same-named processes and report the real count for each group.

    ``entries`` is [(name, working_set_mb)]. Names arrive already resolved, so
    an unreadable process is simply absent from the list rather than being
    shown as a "PID 1234" placeholder, and a group of three processes reports
    ×3 because it really is three, never a hard-coded number.
    """
    grouped: dict = {}
    for name, megabytes in entries:
        if not name:
            continue
        key = name.casefold()
        entry = grouped.get(key)
        if entry is None:
            grouped[key] = [name, float(megabytes), 1]
        else:
            entry[1] += float(megabytes)
            entry[2] += 1
    merged = list(grouped.values())
    merged.sort(key=lambda item: (-item[1], item[0].casefold()))
    return merged[:limit]


def read_top5():
    """Return [(name, working_set_mb, process_count)] for the five largest
    groups, or [] when nothing could be read. Only called while due."""
    psapi = ctypes.windll.psapi
    kernel32 = ctypes.windll.kernel32
    kernel32.OpenProcess.argtypes = (wt.DWORD, wt.BOOL, wt.DWORD)
    kernel32.OpenProcess.restype = wt.HANDLE
    kernel32.CloseHandle.argtypes = (wt.HANDLE,)
    kernel32.CloseHandle.restype = wt.BOOL
    kernel32.GetCurrentProcessId.argtypes = ()
    kernel32.GetCurrentProcessId.restype = wt.DWORD
    psapi.GetProcessMemoryInfo.argtypes = (
        wt.HANDLE, ctypes.POINTER(_ProcMemCounters), wt.DWORD
    )
    psapi.GetProcessMemoryInfo.restype = wt.BOOL

    process_ids = (wt.DWORD * 4096)()
    returned_bytes = wt.DWORD(ctypes.sizeof(process_ids))
    # Explicit signature: EnumProcesses returns BOOL and takes two pointers -
    # the defaults happen to work here, but the declaration is the contract
    # (see the audit note on TerminateProcess for what undeclared means cost).
    enum = psapi.EnumProcesses
    enum.argtypes = (ctypes.POINTER(wt.DWORD), wt.DWORD,
                     ctypes.POINTER(wt.DWORD))
    enum.restype = wt.BOOL
    if not enum(
        process_ids,
        ctypes.sizeof(process_ids),
        ctypes.byref(returned_bytes),
    ):
        return []

    entries = []
    count = returned_bytes.value // ctypes.sizeof(wt.DWORD)
    for index in range(count):
        pid = int(process_ids[index])
        if not pid:
            continue
        handle = kernel32.OpenProcess(0x1000, False, pid)
        if not handle:
            continue
        try:
            counters = _ProcMemCounters()
            counters.cb = ctypes.sizeof(_ProcMemCounters)
            if not psapi.GetProcessMemoryInfo(
                handle, ctypes.byref(counters), counters.cb
            ):
                continue
            name = _process_name(handle)
            if name:
                entries.append((name, counters.WorkingSetSize / 1048576))
        finally:
            kernel32.CloseHandle(handle)
    return group_processes(entries)


# Processes the widget refuses to end no matter what: killing any of them is
# either impossible to survive (blue screen) or breaks the machine for the
# rest of the session. Everything else is the user's call.
#
# `explorer.exe` is the shell (taskbar, desktop, file shell) - terminating it
# tears the session's UI down and only auto-restarts sometimes, so a "killed"
# explorer reads to the user as "Windows broke". `dllhost.exe` hosts COM
# surrogates (thumbnails, shell extensions, DCOM services): killing one can
# take the service or the hosting application with it, so it belongs with the
# system set rather than with the ordinary killable list.
_UNTOUCHABLE = frozenset((
    "system", "registry", "memory compression", "idle", "secure system",
    "smss.exe", "csrss.exe", "wininit.exe", "winlogon.exe", "services.exe",
    "lsass.exe", "svchost.exe", "dwm.exe", "fontdrvhost.exe", "sihost.exe",
    "taskhostw.exe", "explorer.exe", "dllhost.exe", "memorywidget.exe",
))


def _own_image_name() -> str:
    """The exe this widget is really running as, frozen or from source."""
    buffer = ctypes.create_unicode_buffer(1024)
    get_name = ctypes.windll.kernel32.GetModuleFileNameW
    get_name.argtypes = (wt.HMODULE, wt.LPWSTR, wt.DWORD)
    get_name.restype = wt.DWORD
    get_name(None, buffer, 1024)
    return os.path.basename(buffer.value)


def _block_reason(name: str):
    """Why a process group must not be ended, or None when it may be."""
    label = (name or "").strip().casefold()
    if not label or label in _UNTOUCHABLE:
        return "系统关键进程，不能结束"
    if label == _own_image_name().casefold():
        return "不能结束悬浮窗自己"
    return None


def terminate_process_group(name: str):
    """End every process called `name` (case-insensitive).

    Returns ``(killed, denied, blocked)``: how many ended, how many refused
    (almost always because they run with higher rights than this widget),
    and a reason string when the whole group is off limits.
    """
    blocked = _block_reason(name)
    if blocked:
        return 0, 0, blocked
    label = name.strip().casefold()

    psapi = ctypes.windll.psapi
    kernel32 = ctypes.windll.kernel32
    kernel32.OpenProcess.argtypes = (wt.DWORD, wt.BOOL, wt.DWORD)
    kernel32.OpenProcess.restype = wt.HANDLE
    kernel32.CloseHandle.argtypes = (wt.HANDLE,)
    kernel32.CloseHandle.restype = wt.BOOL
    kernel32.GetCurrentProcessId.argtypes = ()
    kernel32.GetCurrentProcessId.restype = wt.DWORD
    kernel32.TerminateProcess.argtypes = (wt.HANDLE, wt.UINT)
    kernel32.TerminateProcess.restype = wt.BOOL

    process_ids = (wt.DWORD * 4096)()
    returned_bytes = wt.DWORD(ctypes.sizeof(process_ids))
    enum = psapi.EnumProcesses
    enum.argtypes = (ctypes.POINTER(wt.DWORD), wt.DWORD,
                     ctypes.POINTER(wt.DWORD))
    enum.restype = wt.BOOL
    if not enum(
        process_ids,
        ctypes.sizeof(process_ids),
        ctypes.byref(returned_bytes),
    ):
        return 0, 0, "读不到进程列表，稍后再试"

    killed = denied = 0
    own_pid = kernel32.GetCurrentProcessId()
    # One handle per PID, opened ONCE with both rights needed. The previous
    # version opened a QUERY handle, checked the name, closed it, then opened a
    # TERMINATE handle - a classic TOCTOU window: after the close, Windows may
    # recycle the PID for an unrelated process between the two OpenProcess
    # calls, and the terminate then lands on the wrong program. Holding a
    # single handle pins the process object so the name check and the kill
    # refer to the same task, guaranteed.
    PROCESS_TERMINATE = 0x0001
    PROCESS_QUERY_LIMITED_INFORMATION = 0x1000
    access = PROCESS_TERMINATE | PROCESS_QUERY_LIMITED_INFORMATION
    for index in range(returned_bytes.value // ctypes.sizeof(wt.DWORD)):
        pid = int(process_ids[index])
        if not pid or pid == own_pid:
            continue
        handle = kernel32.OpenProcess(access, False, pid)
        if not handle:
            continue                                       # gone or protected
        try:
            if (_process_name(handle) or "").casefold() != label:
                continue
            if kernel32.TerminateProcess(handle, 1):
                killed += 1
            else:
                denied += 1
        finally:
            kernel32.CloseHandle(handle)
    return killed, denied, None


# ---------------------------------------------------------------------------
# Anti-aliased rasteriser
# ---------------------------------------------------------------------------

def _rgb(color):
    if isinstance(color, str):
        return (
            int(color[1:3], 16),
            int(color[3:5], 16),
            int(color[5:7], 16),
        )
    return tuple(color)


def _png_bytes(width: int, height: int, pixels) -> bytes:
    rows = []
    offset = 0
    for _ in range(height):
        rows.append(b"\x00" + bytes(pixels[offset:offset + width * 4]))
        offset += width * 4
    raw = b"".join(rows)

    def chunk(kind, data):
        return (struct.pack(">I", len(data)) + kind + data
                + struct.pack(">I", zlib.crc32(kind + data) & 0xFFFFFFFF))

    return (b"\x89PNG\r\n\x1a\n"
            + chunk(b"IHDR", struct.pack(">IIBBBBB", width, height, 8, 6, 0, 0, 0))
            + chunk(b"IDAT", zlib.compress(raw, 6))
            + chunk(b"IEND", b""))


def _cache(key, data):
    if len(_BITMAP_CACHE) >= 256:
        _BITMAP_CACHE.clear()
    _BITMAP_CACHE[key] = data
    return data


def pin_png(size: int, color, pinned: bool) -> bytes:
    """Rasterise a recognisable tack with a cap, shoulders and needle."""
    size = max(1, int(size))
    rgb = _rgb(color)
    key = ("pin", size, rgb, bool(pinned))
    if key in _BITMAP_CACHE:
        return _BITMAP_CACHE[key]
    body = [(8, 4), (16, 4), (15, 10), (18, 14),
            (6, 14), (9, 10), (8, 4)]
    segments = list(zip(body, body[1:])) + [((12, 14), (12, 21))]
    angle = 0.0 if pinned else math.pi / 6
    cosine, sine = math.cos(angle), math.sin(angle)
    scale = size / 24.0
    pixels = bytearray()
    for py in range(size):
        for px in range(size):
            dx, dy = (px + 0.5) / scale - 12, (py + 0.5) / scale - 12
            x, y = cosine * dx + sine * dy + 12, -sine * dx + cosine * dy + 12
            distance = 100.0
            inside = False
            for (ax, ay), (bx, by) in segments:
                vx, vy = bx - ax, by - ay
                t = max(0.0, min(1.0, ((x - ax) * vx + (y - ay) * vy)
                                     / (vx * vx + vy * vy)))
                distance = min(distance, math.hypot(x - ax - t * vx, y - ay - t * vy))
            if pinned:
                for (ax, ay), (bx, by) in zip(body, body[1:]):
                    if (ay > y) != (by > y) and x < (bx - ax) * (y - ay) / (by - ay) + ax:
                        inside = not inside
            coverage = 1.0 if inside else max(0.0, min(1.0, (0.9 - distance) * scale + 0.5))
            pixels.extend((*rgb, round(255 * coverage)))
    return _cache(key, _png_bytes(size, size, pixels))


def pin_plate_color(colors, hover=False):
    amount = 0.22 if hover else 0.13
    return tuple(round(background * (1 - amount) + accent * amount)
                 for background, accent in zip(_rgb(colors["card_soft"]),
                                               _rgb(colors["orange"])))


def plate_png(width: int, height: int, radius: float, color, alpha: int = 255):
    """An anti-aliased rounded rectangle whose shape fills the whole image.

    The straight edges sit exactly on the image border, so they need no
    anti-aliasing; only the corner arcs are softened. Interior spans are emitted
    in bulk, which keeps even a full-panel plate cheap to build.
    """
    width = max(1, int(width))
    height = max(1, int(height))
    radius = max(0.0, min(float(radius), width / 2.0, height / 2.0))
    red, green, blue = _rgb(color)
    key = ("plate", width, height, round(radius, 2), (red, green, blue), alpha)
    cached = _BITMAP_CACHE.get(key)
    if cached is not None:
        return cached

    solid = bytes((red, green, blue, alpha))
    clear = bytes((0, 0, 0, 0))
    inner_y = height - radius
    edge = 0 if radius <= 0.0 else min(
        width // 2, max(0, int(math.ceil(radius - 0.5)))
    )
    middle = max(0, width - edge * 2)

    output = bytearray()
    for y in range(height):
        yc = y + 0.5
        if radius <= 0.0 or radius <= yc <= inner_y:
            output.extend(solid * width)
            continue
        dy = (radius - yc) if yc < radius else (yc - inner_y)
        corner = bytearray()
        for x in range(edge):
            coverage = 0.5 - (math.hypot(radius - (x + 0.5), dy) - radius)
            if coverage <= 0.0:
                corner.extend(clear)
            elif coverage >= 1.0:
                corner.extend(solid)
            else:
                corner.extend((red, green, blue, round(coverage * alpha)))
        # The row is symmetric about the vertical centre, so the right corner is
        # the same coverage profile read backwards, pixel by pixel.
        output.extend(corner)
        output.extend(solid * middle)
        for x in range(len(corner) - 4, -1, -4):
            output.extend(corner[x:x + 4])
    return _cache(key, _png_bytes(width, height, output))


def bar_png(width: int, height: int, fill_fraction: float,
            warn_fraction: float, colors):
    """The whole meter: track, warning zone and the rounded fill.

    Rasterised rather than stroked, so the two end caps stay smooth and the
    warning zone keeps a square left edge.
    """
    width = max(4, int(width))
    height = max(4, int(height))
    radius = height / 2.0
    fill_x = width * max(0.0, min(1.0, fill_fraction))
    warn_x = width * max(0.0, min(1.0, warn_fraction))
    key = ("bar", width, height, round(fill_fraction, 4), round(warn_fraction, 4),
           theme_name())
    cached = _BITMAP_CACHE.get(key)
    if cached is not None:
        return cached

    track = _rgb(colors["track"])
    warn = _rgb(colors["warn_fill"])
    accent = _rgb(colors["orange"])
    inner_x = width - radius
    fill_inner = fill_x - radius
    output = bytearray()
    for y in range(height):
        distance = abs(y + 0.5 - radius)
        for x in range(width):
            xc = x + 0.5
            dx = 0.0
            if xc < radius:
                dx = radius - xc
            elif xc > inner_x:
                dx = xc - inner_x
            silhouette = (0.5 + math.sqrt(max(0.0, radius * radius - dx * dx))
                          - distance) if dx else (0.5 + radius - distance)
            if silhouette <= 0.0:
                output.extend((0, 0, 0, 0))
                continue
            silhouette = min(1.0, silhouette)
            red, green, blue = warn if xc >= warn_x else track
            if xc < fill_x:
                fdx = 0.0
                if xc < radius:
                    fdx = radius - xc
                elif xc > fill_inner:
                    fdx = xc - fill_inner
                fhalf = (math.sqrt(max(0.0, radius * radius - fdx * fdx))
                         if fdx else radius)
                coverage = min(1.0, max(0.0, 0.5 + fhalf - distance))
                if coverage > 0.0:
                    red = round(accent[0] * coverage + red * (1.0 - coverage))
                    green = round(accent[1] * coverage + green * (1.0 - coverage))
                    blue = round(accent[2] * coverage + blue * (1.0 - coverage))
            output.extend((red, green, blue, round(silhouette * 255)))
    return _cache(key, _png_bytes(width, height, output))


def _layer(accumulator, color, alpha):
    """Composite one layer into a premultiplied (r, g, b, a) accumulator."""
    if alpha <= 0.0:
        return accumulator
    red, green, blue, base = accumulator
    keep = 1.0 - alpha
    return (
        color[0] * alpha + red * keep,
        color[1] * alpha + green * keep,
        color[2] * alpha + blue * keep,
        alpha + base * keep,
    )


def ring_png(size: int, colors, percent: float, progress_color: str,
             backdrop="theme") -> bytes:
    """The floating orb: an opaque disc, a track arc and a rounded progress arc.

    `backdrop` exists because of a Tk limitation, and it is what separates a
    smooth rim from a dirty one.

    The orb window uses `-transparentcolor` colour-keying, and the canvas
    background *is* that key colour. Tk composites a PNG's alpha channel against
    the canvas background - so a semi-transparent rim pixel gets darkened by the
    key colour, which is nearly black. Measured: a white patch at 60% alpha
    comes back as `(151,152,153)` instead of staying near white. That dark
    fringe hugging a light disc is what reads as "毛边".

    So the rim is not left semi-transparent at all. It is blended to `backdrop`
    here and emitted at alpha 255, which means Tk has nothing left to blend and
    the key colour cannot bleed into the edge.

    `backdrop="theme"` picks the entry for the active theme from `RIM_BACKDROP`;
    pass an explicit colour (or `None` to disable rim treatment entirely) to
    override. The two themes need different colours: light blends towards a
    slightly darker grey so the rim reads as natural shading, while dark blends
    towards a slightly lighter grey - mixing a dark card towards light would
    otherwise leave a bright halo around the orb.
    """
    if backdrop == "theme":
        backdrop = RIM_BACKDROP.get(theme_name(), RIM_BACKDROP["light"])
    key = ("ring", size, theme_name(), round(percent, 2), progress_color,
           backdrop)
    cached = _BITMAP_CACHE.get(key)
    if cached is not None:
        return cached

    card = _rgb(colors["card"])
    track = _rgb(colors["track"])
    accent = _rgb(progress_color)
    center = size / 2.0
    disc_radius = size / 2.0 - 2.0
    band_radius = disc_radius - max(4.5, size * 0.082)
    half_band = max(4.5, size * 0.075) / 2.0
    percent = max(0.0, min(100.0, percent))
    extent = math.tau * percent / 100.0
    start_x, start_y = center, center - band_radius
    end_x = center + band_radius * math.sin(extent)
    end_y = center - band_radius * math.cos(extent)
    reach = band_radius + half_band + 1.0
    lower = max(0, int(math.floor(center - reach)))
    upper = min(size, int(math.ceil(center + reach + 1.0)))
    tau = math.tau
    rim = _rgb(backdrop) if backdrop is not None else None

    output = bytearray()
    for y in range(size):
        for x in range(size):
            px, py = x + 0.5, y + 0.5
            distance = math.hypot(px - center, py - center)
            # Coverage of the disc at this pixel.
            #
            # The ramp is deliberately a little over one pixel wide. A ramp of
            # exactly 1.0 px (`0.5 + r - d`) only produces an intermediate value
            # when a pixel centre happens to land within half a pixel of the
            # mathematical edge; when the edge falls midway between two centres,
            # both pixels come out fully on or fully off and the curve shows a
            # hard step. Widening to ~1.6 px guarantees at least one pixel is
            # always partially covered, at the cost of spreading the edge over
            # two pixels - which is what a curve should look like anyway.
            disc = min(1.0, max(0.0, (RIM_FEATHER + disc_radius - distance)
                                / RIM_FEATHER))
            in_band = rim is not None and 0.0 < disc < 1.0
            if in_band:
                # Anti-aliasing band, and the reason this branch exists.
                #
                # The obvious implementation - store the pixel at `alpha = disc`
                # and let Tk blend it - does not work here, because the canvas
                # background is `TRANSPARENT`, the very colour the window
                # punches out. Tk blends against that background, so a rim pixel
                # at 60% coverage comes out as `0.6*card + 0.4*(1,2,3)`: the key
                # colour bleeds in and darkens the edge by ~110 levels. Measured
                # in `tools/_tk_key_tint.py`; the live rim came out at
                # `(190,190,194)` and `(182,182,184)` against a card of
                # `(252,252,253)`, far too dark to read as a soft edge - it reads
                # as a dirty fringe, i.e. the "毛边" this fixes.
                #
                # So the blend is baked into the RGB here and the pixel is
                # emitted fully opaque. Tk then has no alpha left to blend and
                # the key colour cannot bleed: the rim is exactly the colour
                # computed below and nothing else.
                #
                # The coverage is bent towards opaque with a square root. A
                # linear ramp spends most of its width nearly transparent, which
                # against a backdrop darker than the card reads as a thin dark
                # outline rather than a fade. The curve makes it read as a soft
                # edge instead of a stroke.
                blend = disc ** 0.5
                pixel = _layer((0.0, 0.0, 0.0, 0.0), card, 1.0)
                pixel = _layer(pixel, rim, 1.0 - blend)
                # `_layer` already leaves alpha at 1.0 here; pin it explicitly so
                # a later edit to the composite cannot silently reintroduce the
                # key-colour bleed.
                red, green, blue, _ = pixel
                pixel = (red, green, blue, 1.0)
            else:
                pixel = _layer((0.0, 0.0, 0.0, 0.0), card, disc)
            if lower <= x < upper and lower <= y < upper:
                radial = min(
                    1.0, max(0.0, half_band + 0.5 - abs(distance - band_radius))
                )
                if radial > 0.0:
                    pixel = _layer(pixel, track, radial)
                    if percent > 0.0:
                        if percent >= 100.0:
                            angular = 1.0
                        else:
                            theta = math.atan2(px - center, -(py - center)) % tau
                            angular = min(
                                1.0, max(0.0, 0.5 + (extent - theta) * band_radius)
                            )
                        cap_start = min(1.0, max(
                            0.0, half_band + 0.5
                            - math.hypot(px - start_x, py - start_y)))
                        cap_end = min(1.0, max(
                            0.0, half_band + 0.5
                            - math.hypot(px - end_x, py - end_y)))
                        pixel = _layer(
                            pixel, accent,
                            max(radial * angular, cap_start, cap_end),
                        )
            red, green, blue, alpha = pixel
            if alpha <= 0.0:
                output.extend((0, 0, 0, 0))
            else:
                output.extend((
                    max(0, min(255, round(red / alpha))),
                    max(0, min(255, round(green / alpha))),
                    max(0, min(255, round(blue / alpha))),
                    max(0, min(255, round(alpha * 255))),
                ))
    return _cache(key, _png_bytes(size, size, output))


def set_photo_master(widget) -> None:
    """Start a build for `widget`: name its interpreter and reset its images.

    Two things are tracked here:

    * `_PHOTO_INTERP` - a Tk image lives on exactly one interpreter, and
      `tk._default_root` is only the *first* root a process ever created. A
      widget that is not that root would otherwise build its images on a
      foreign interpreter and fail at `create_image` with "image pyimage1
      doesn't exist".
    * `widget._photo_keepalive` - the images this build put on screen. A canvas
      item only *names* its image: as soon as Python collects the PhotoImage,
      tkinter deletes the Tcl image and the item renders as nothing, which is
      how a panel turns transparent. So every displayed image must stay
      referenced for as long as it is on screen, and a rebuild is exactly the
      moment the previous build's images stop being needed. Storing the list on
      the widget itself means it is dropped together with the canvas.

    `_PHOTO_CACHE` is a pure lookup memo. Clearing it can never unset an image
    that is still on screen, because the keep-alive list owns that, which is
    what keeps it bounded while the panel is resized through many sizes.
    """
    global _PHOTO_INTERP, _PHOTO_MASTER
    interpreter = widget.tk
    if interpreter is not _PHOTO_INTERP:
        _PHOTO_INTERP = interpreter
    _PHOTO_MASTER = widget
    widget._photo_keepalive = []
    _PHOTO_CACHE.clear()


def photo_for(key, data: bytes) -> tk.PhotoImage:
    """Build (and remember) a PhotoImage. Tk drops the pixels otherwise."""
    existing = _PHOTO_CACHE.get(key)
    if existing is not None:
        return existing
    master = _PHOTO_MASTER or tk._default_root
    image = tk.PhotoImage(data=data, format="png", master=master)
    # Both references matter: the memo avoids rebuilding an identical plate,
    # the keep-alive list keeps every on-screen image from being collected.
    _PHOTO_CACHE[key] = image
    keep = getattr(master, "_photo_keepalive", None)
    if keep is not None:
        keep.append(image)
    return image


def plate_photo(name, width, height, radius, color, alpha=255):
    """A cached, scaled plate image, ready for `create_image`."""
    width = max(1, D(width))
    height = max(1, D(height))
    return photo_for(
        ("plate", name, width, height, round(float(radius), 2),
         _rgb(color), alpha),
        plate_png(width, height, D(radius), color, alpha),
    )


# ---------------------------------------------------------------------------
# History and trend algorithms
# ---------------------------------------------------------------------------

class History:
    """A lazy, bounded two-hour buffer; it does not exist while disabled."""

    SAMPLE_SECONDS = 5.0
    MAX_POINTS = 1441

    def __init__(self) -> None:
        self.points = deque(maxlen=self.MAX_POINTS)
        self.next_sample = 0.0
        self.revision = 0

    def push(self, percent: float, now: float) -> bool:
        if now < self.next_sample:
            return False
        self.points.append((now, percent))
        self.next_sample = now + self.SAMPLE_SECONDS
        self.revision += 1
        return True

    def get(self, seconds: int, now: float, limit: int = 260):
        cutoff = now - seconds
        values = [value for stamp, value in self.points if stamp >= cutoff]
        if len(values) <= limit:
            return values
        stride = math.ceil(len(values) / limit)
        compact = values[::stride]
        if (len(values) - 1) % stride:
            compact.append(values[-1])
        return compact


def _binomial_smooth(values, weights=(1, 2, 3, 2, 1)):
    """Small symmetric kernel: removes single-sample jitter, keeps shape."""
    span = len(weights) // 2
    total_weight = sum(weights)
    last = len(values) - 1
    result = []
    for index in range(len(values)):
        total = 0.0
        for offset, weight in zip(range(-span, span + 1), weights):
            total += values[min(last, max(0, index + offset))] * weight
        result.append(total / total_weight)
    return result


def _ema(values, alpha: float):
    if not values:
        return []
    result = [float(values[0])]
    for value in values[1:]:
        result.append(result[-1] + alpha * (float(value) - result[-1]))
    return result


def _buckets(values, bucket: int):
    return [values[start:start + bucket]
            for start in range(0, len(values), bucket)]


def _median(group):
    ordered = sorted(group)
    return ordered[len(ordered) // 2]


def _percentile(group, fraction: float):
    ordered = sorted(group)
    index = min(len(ordered) - 1, max(0, int(round(fraction * (len(ordered) - 1)))))
    return ordered[index]


def _view_range(values, minimum_span: float = 0.3):
    """Pick a padded range from the real samples, never from a smoothed line.

    ``minimum_span`` is deliberately small: the axis is allowed to zoom in on
    genuinely small movement, and the printed range always states the truth, so
    a calm machine still reads as calm instead of as an artificially flat line.
    """
    low, high = min(values), max(values)
    span = max(float(minimum_span), (high - low) * 1.15)
    center = (low + high) / 2.0
    lower = max(0.0, center - span / 2.0)
    upper = min(100.0, center + span / 2.0)
    if upper - lower < span:
        if lower <= 0.0:
            upper = min(100.0, span)
        elif upper >= 100.0:
            lower = max(0.0, 100.0 - span)
    return lower, upper


def algorithm_label(index: int) -> str:
    return ("局部细节", "稳健中位", "长期总览")[max(0, min(2, index))]


# The smallest vertical span each view may zoom down to, in points of memory
# percentage. Small enough that real movement always fills the plot.
RANGE_MIN_SPAN = (0.3, 0.6, 0.8)


def trend_series(values, range_index: int):
    """Return (line, view_low, view_high, band_low, band_high, label).

    Three honest views of the same real samples:

    ``0``  five minutes -- keeps local movement and lifts fine detail back out
           of the smoothing so genuinely small changes stay visible.
    ``1``  thirty minutes -- robust per-bucket medians plus a light EMA, so one
           spike cannot bend the line.
    ``2``  two hours -- per-bucket means with a slow EMA, plus a 20-80%
           percentile band showing how volatile each bucket really was.

    The vertical range always comes from the raw samples, so the curve can never
    look calmer or livelier than the machine actually was.
    """
    source = [float(value) for value in values]
    if not source:
        return [], 0.0, 100.0, None, None, ""
    index = max(0, min(2, range_index))
    minimum_span = RANGE_MIN_SPAN[index]
    if len(source) == 1:
        low, high = _view_range(source, minimum_span)
        return source, low, high, None, None, algorithm_label(index)

    band_low = band_high = None
    if index == 0:
        baseline = _binomial_smooth(source)
        # A restrained residual boost: real detail, lifted 25%, nothing added.
        line = [base + (raw - base) * 1.25 for raw, base in zip(source, baseline)]
    elif index == 1:
        bucket = max(2, math.ceil(len(source) / 90))
        line = _ema([_median(group) for group in _buckets(source, bucket)], 0.35)
    else:
        bucket = max(6, math.ceil(len(source) / 80))
        groups = _buckets(source, bucket)
        line = _ema([sum(group) / len(group) for group in groups], 0.2)
        band_low = _ema([_percentile(group, 0.2) for group in groups], 0.35)
        band_high = _ema([_percentile(group, 0.8) for group in groups], 0.35)

    low, high = _view_range(source, minimum_span)
    line = [max(low, min(high, value)) for value in line]
    if band_low is not None:
        band_low = [max(low, min(high, value)) for value in band_low]
        band_high = [max(low, min(high, value)) for value in band_high]
    return line, low, high, band_low, band_high, algorithm_label(index)


def memory_text(megabytes: float) -> str:
    if megabytes >= 1024:
        return f"{megabytes / 1024:.1f} GB"
    return f"{megabytes:.0f} MB"


def action_hint(top5) -> str:
    """One short, real suggestion derived from the live process ranking."""
    if not top5:
        return "暂时读不到进程占用"
    name, megabytes, count = top5[0]
    label = name[:-4] if name.lower().endswith(".exe") else name
    if count > 1:
        label = f"{label} ×{count}"
    return f"关闭 {label} 可释放约 {memory_text(megabytes)}"


class MemoryWidget:
    HISTORY_RANGES = ((300, "5 分"), (1800, "30 分"), (7200, "2 小时"))
    TOP5_CACHE_SECONDS = 12.0

    def __init__(self) -> None:
        self.root = tk.Tk()
        # Exception nets go on as early as possible: any failure between here
        # and the end of __init__ used to leave only stderr - invisible in a
        # windowed build - and "double-click does nothing".
        _install_exception_hooks(self.root)
        self.root.withdraw()
        self.root.overrideredirect(True)
        self.root.attributes("-topmost", bool(S["pin_top"]))
        self.root.configure(bg=TRANSPARENT)
        try:
            self.root.attributes("-transparentcolor", TRANSPARENT)
        except tk.TclError:
            pass
        self.root.protocol("WM_DELETE_WINDOW", self.quit)
        # 兜底：**任何人**把 root 拆掉，都要先停掉 GPU 取数线程。
        #
        # `quit()` 里已经停过一次，但那个前提是调用方肯走 `quit()`。测试脚手架、
        # 抓屏工具、以及将来任何一个只想要"窗口关掉"的调用方，都可能直接
        # `root.destroy()`，于是一个 daemon 版 `nvidia-smi` 轮询线程就被剩下来。
        # 累积够多之后，随便哪个在解释器 GC 时被拆掉就会打出
        # `Tcl_AsyncDelete: async handler deleted by the wrong thread`
        # 并把进程在打印结论之前中止 —— 详见 `quit()` 里那段注释。
        #
        # `<Destroy>` 在窗口真正消失前触发，此时线程还在，`gpu_info.stop()`
        # 的有界 join 能正常收工。`add="+"` 保留 Tk 自己的处理函数。
        self.root.bind("<Destroy>", self._on_root_destroy, add="+")
        init_ui_scale(self.root)
        resolve_fonts(self.root)

        self.detail = None
        self.detail_canvas = None
        self.settings_window = None
        self.settings_canvas = None
        self.context_window = None
        self.context_canvas = None
        self.context_focus_job = None
        self.probe_script = ()
        self.probe_pressed = False
        self.probe_poll_job = None
        self.history = History() if S["history_enabled"] else None
        self.scale_index = 0
        self.detail_scale = float(S["detail_scale"])
        self._resize_start = None
        self._resize_job = None
        self._move_start = None
        self.press_offset = None
        self.press_root = None
        self.dragged = False
        self.hover_job = None
        self.close_job = None
        self.loop_job = None
        self.watch_job = None
        self.last_memory = None
        self.last_ring_state = None
        self.last_detail_state = None
        self.last_chart_revision = -1
        self.top5_cache = []
        self.top5_next_due = 0.0
        self.settings_error = ""
        self.chart_box = None
        self.ring_photo = None
        self.meter_photo = None
        self.hidden = False
        self.tray = None
        # {hwnd: (回调对象, 原窗口过程)}。**每个窗口都要挂钩**（圆环、详情面板、
        # 设置面板都是各自独立的窗口过程），所以这里按 hwnd 存一份映射，
        # 退出时逐个还原。只存一个 `_old_wndproc` 的写法会让面板收不到
        # `WM_CLOSE`，见 `_install_wndproc`。
        self._wndprocs = {}
        # A native callback can still be on the stack when a Tk Destroy
        # binding runs. Retain removed callbacks until the next safe timer.
        self._retired_wndprocs = []
        # Window procedures only enqueue data. Tk calls from a ctypes
        # WndProc corrupt _tkinter's saved thread state on nested dispatch.
        self._tray_events = deque(maxlen=64)
        self._wndproc_ref = None                        # root 的回调（防 GC）
        self._old_wndproc = None                        # root 的原过程（见 _install_wndproc）
        self._tray_hwnd = 0
        # Native callbacks record close/session/DPI requests; they never
        # call Tk. The existing timer processes them in the Tk event loop.
        self._wm_quit_flag = (ctypes.c_int * 1)()
        # Session-end notification, same discipline as `_wm_quit_flag`: the
        # window procedure only writes a ctypes flag; `watch_close_request`
        # (120 ms poll, safe Python context) does the actual tray teardown.
        # 1 = WM_QUERYENDSESSION seen (tray must come out before we answer),
        # 2 = WM_ENDSESSION wParam=TRUE (session really ending - also exit).
        self._wm_endsession_flag = (ctypes.c_int * 1)()
        # DPI-change notification (WM_DPICHANGED). The decoded low word of
        # wParam (X DPI) is written here; `_loop` applies it on the Tk side.
        self._wm_dpi_flag = (ctypes.c_ssize_t * 1)()
        # Set by `quit()`: periodic callbacks check it so a callback already in
        # flight does not re-arm itself after the teardown has started.
        self._closing = False
        # One-shot guard for the visible "settings could not be saved" hint -
        # a drag generates many save attempts, but the user only needs to be
        # told once per session.
        self._save_failure_notified = False
        # 退出时要打印的最后一句话（`--selftest` 的判词）。见 `run()`。
        self.exit_announce = ""
        # GPU 区：取数线程只在详情面板打开期间跑（`_show_detail` / `_hide_detail`），
        # `gpu_bar_photo` 持有进度条贴图的引用，否则 Tk 会把像素丢掉。
        self.gpu_name = ""
        self.gpu_bar_photo = None
        # 上一次建面板时 GPU 区占了多少高度。变了就重建版式 ——
        # 探测是异步的，打开面板那一刻常常还不知道有没有独显。
        self.gpu_h_built = None
        self._fitting = False

        self._build_ring()
        self.root.update_idletasks()
        self._place_initially()
        self._poll_once()
        self._draw_ring(force=True)
        self.root.deiconify()
        set_window_rounding(self.root)
        self._install_wndproc()
        self._install_tray()
        # `--detail` 让详情面板直接展开。冒烟测试用它顶替"点一下圆环"这一步：
        # 点击落在无边框窗口的 canvas 绑定上，外部进程驱动不了（见 `_start_probe`），
        # 而 `_show_detail()` 是同一个方法，验到的窗口行为完全一致。
        if "--detail" in sys.argv:
            self._show_detail()
        self._start_probe()
        self._schedule_next()
        self.watch_close_request()

    # -- scripted probe (smoke test) ---------------------------------------

    def _start_probe(self) -> None:
        """按启动参数自动走一遍交互，供打包后的冒烟测试从外部验证。

        为什么需要它：后台进程既造不出"真的被派发、且窗口处于激活态"的鼠标
        事件，也没法把窗口提到前台 —— 实测 `WindowFromPoint` 在「退出」那一行
        返回的确实是本程序的 canvas，但 `tag_bind` 始终不触发。

        那么退一步问：这些点击**真的需要模拟鼠标吗**？不需要。程序里有现成的
        真实入口可以直接调 —— 右键菜单就是 `_show_context_menu`，菜单行的动作
        由 `current` 标签分派（见 `_on_menu_click`），都不依赖"窗口处于激活态"。
        于是让程序自己在正确的时机走这些入口，冒烟测试只负责从外面观察窗口
        有没有出现在该出现的位置 —— 交互语义本身不靠"假装点过了"。

        这是一条只由启动参数触发的旁路：不带参数时完全不执行，
        不增加常驻开销，也不破坏"常驻循环不做多余的事"这条约束。
        """
        script = os.environ.get("MEMORY_WIDGET_PROBE", "")
        if "--probe-menu" not in sys.argv and not script:
            return
        # 脚本内容是**菜单项的显示名**（用 `/` 分隔），例如 "设置…/退出"。
        # 旧版用设计坐标（"1,50"）—— 那东西会随 DPI 与字号漂移，
        # 实测点到了行缝隙上只关掉菜单。名字是稳定的。
        parts = [p.strip() for p in script.replace(",", "/").split("/") if p.strip()]
        # "0" 是特例：只写一份窗口自述日志，完全不碰界面。
        if parts == ["0"]:
            self.root.after(1200, self._probe_report_only)
            return
        self.probe_script = parts
        # 看门狗：自动点菜单的场合，最迟 12 秒后强制退出。
        # 没有它，一次没点中就会留一个常驻实例在全局跑（用户会看到残留进程）。
        self.root.after(12000, self._probe_watchdog)
        self.root.after(1200, self._probe_open_menu)
        # 轮询"点击指令"文件。为什么要用文件而不是再起一个带 `--probe-press`
        # 的进程：程序是单实例的（互斥量），第二个进程根本进不来，只会弹一个
        # 模态框等人点确定。而外部又驱动不了 `tag_bind` 的点击 ——
        # 于是留一个文件信箱：冒烟测试观察到菜单之后写进去，本进程读到就点，
        # 点的仍然是真实命中判定 `_menu_action_for`。
        self.probe_poll_job = None
        self._probe_poll_command()

    def _probe_poll_command(self) -> None:
        path = os.environ.get("MEMORY_WIDGET_PRESS_FILE", "")
        if path and not self.probe_pressed:
            try:
                with open(path, encoding="utf-8") as fh:
                    labels = [p.strip() for p in fh.read().split(",") if p.strip()]
            except OSError:
                labels = []
            if labels:
                self.probe_pressed = True
                self._dbg("probe: 读到点击指令 %s" % (labels,))
                self._probe_press_rows(labels)
        self.probe_poll_job = self.root.after(250, self._probe_poll_command)

    def _probe_watchdog(self) -> None:
        """到点还没退就自己退 —— 宁可留下"退出失败"的记录，也不留残留进程。"""
        self._dbg("probe: 看门狗触发，强制退出")
        self.quit(terminate=True)

    def _probe_report_only(self) -> None:
        """只把当前窗口状态写进日志，不改变界面。

        给"外部到底看得见看不见这个窗口"这类问题留一份程序自己的说法：
        程序知道自己窗口的 `winfo_id()` 和屏幕矩形，那两个数是权威事实，
        外部枚举对不上就是外部检测逻辑的问题。
        """
        parts = ["report root=%s" % self.root.winfo_id()]
        for name, win in (("detail", getattr(self, "detail_window", None)),
                          ("context", getattr(self, "context_window", None)),
                          ("settings", getattr(self, "settings_window", None))):
            if win is not None and win.winfo_exists():
                parts.append("%s=%s@%s,%s+%sx%s" % (
                    name, win.winfo_id(), win.winfo_rootx(), win.winfo_rooty(),
                    win.winfo_width(), win.winfo_height()))
        self._dbg(" ".join(parts))

    def _probe_open_menu(self) -> None:
        """打开右键菜单并**让它一直开着**，等 `--probe-press` 或看门狗。

        为什么不在同一个回调里顺势点下去：菜单显示后 Windows 会立刻收回
        强制焦点，`<FocusOut>` 300 ms 宽限期一过就判定"用户走了"把菜单收掉，
        而这正是我们计划点击的时刻 —— 实测菜单在 1.9 s 就没了，
        点击回调 2.4 s 才到，永远点空。所以这里不关菜单，
        冒烟测试看到它之后再从外部发 `--probe-press=<行名>` 让它点。
        """
        self._dbg("probe: 打开右键菜单 script=%s" % (self.probe_script,))
        if getattr(self, "ring_canvas", None) is None:
            self._dbg("probe: 失败 —— 没有 ring_canvas")
            return
        # 真实路径：右键事件 → `_show_context_menu`。`x_root/y_root` 指到圆环中心，
        # 菜单会自己贴边收进来。这里必须构造事件对象，因为位置取自指针，
        # 而程序化路径下指针并不在圆环上。
        size = self._ring_size()
        event = type("E", (), {
            "x_root": self.root.winfo_rootx() + size // 2,
            "y_root": self.root.winfo_rooty() + size // 2,
        })()
        self._show_context_menu(event)
        # 记下显示之后的样子，1 秒后再记一次 —— 菜单若"闪一下就没了"，
        # 两次记录会不一样，比事后猜快得多。
        self.root.after(1000, self._probe_verify_menu)

    def _probe_verify_menu(self) -> None:
        """记录菜单显示 1 秒后还在不在、可不可见。"""
        window = self.context_window
        if window is None:
            self._dbg("probe: 菜单 1 秒后已被收起（context_window=None）")
            return
        try:
            state = window.state()
            width = window.winfo_width()
            height = window.winfo_height()
        except tk.TclError:
            self._dbg("probe: 菜单 1 秒后已销毁")
            return
        self._dbg("probe: 菜单 1 秒后 state=%s %sx%s views=%s"
                  % (state, width, height, window.winfo_viewable()))

    def _probe_press_rows(self, presses=None) -> None:
        """在菜单的指定行上走一次真实命中判定。

        由外部写 `MEMORY_WIDGET_PRESS_FILE` 指定文件触发（见 `_probe_poll_command`），
        外部可在观察完菜单之后再决定点哪一行 —— 顺序对了，菜单也还在。
        """
        if presses is None:
            presses = getattr(self, "probe_script", None) or []
        canvas = getattr(self, "context_canvas", None)
        if canvas is None:                       # 菜单没开出来，说不了更多了
            self._dbg("probe: 菜单没开出来，跳过点击")
            return
        self._dbg("probe: 菜单在 %.1f,%.1f %sx%s" % (
            self.context_window.winfo_rootx(), self.context_window.winfo_rooty(),
            self.context_window.winfo_width(), self.context_window.winfo_height()))
        # `presses` 里给的是**菜单项的显示名**（"设置…" / "退出"），不是设计坐标：
        # 坐标在 DPI 与字号变化后会漂，用坐标点很容易落到行与行之间的缝隙上
        # （实测点到了 `menu_bg`，于是"点了退出"却只把菜单关掉、进程没退）。
        # 改成按文本项自身的包围盒取中心点，命中就与坐标无关了。
        for tag, _top, label in MENU_ROWS:
            if label not in presses:
                continue
            self._dbg("probe: 点击行 %s" % label)
            self._probe_press_one(tag)

    def _probe_press_one(self, tag) -> None:
        """在指定菜单行的**实际**绘制位置上点一下。

        坐标取自该项自己的包围盒中心（`bbox`），不是估出来的设计值 ——
        这样 DPI、字体、行高怎么变都不会点空。
        """
        canvas = getattr(self, "context_canvas", None)
        if canvas is None:
            self._dbg("probe: 点击 %s 时菜单已不在" % tag)
            return
        try:
            box = canvas.bbox(tag)
        except tk.TclError:
            self._dbg("probe: 取 %s 包围盒失败（标签是组合标签，改用文字项）" % tag)
            box = None
        if not box:
            box = canvas.bbox("%s_text" % tag)
        if not box:
            self._dbg("probe: %s 没有包围盒，放弃点击" % tag)
            return
        x = (box[0] + box[2]) // 2
        y = (box[1] + box[3]) // 2
        self._dbg("probe: 点击 %s 于 bbox=%s -> (%d,%d)" % (tag, box, x, y))
        self._menu_action_for(canvas, x, y)

    def _menu_row_at(self, canvas, x, y):
        """坐标 (x, y) 落在哪一行，返回 (tag, label) 或 None。

        **不要用 `find_closest`**：它只按"到 item 锚点的距离"排序，
        菜单底图 `menu_bg` 是一整块矩形，锚点在左上角，但它的包围盒覆盖
        整个菜单 —— 实测点 (126,105)（正是「退出」那一行）时 `find_closest`
        返回的是 `menu_bg`，于是"点了退出"变成了"关掉菜单"。这是真实点击
        也会踩到的坑，不只是探针的问题。

        正确做法是直接对每一行的包围盒做点包含判定。
        """
        for tag, _top, _label in MENU_ROWS:
            for target in (tag, f"{tag}_text"):
                try:
                    box = canvas.bbox(target)
                except tk.TclError:
                    box = None
                if box and box[0] <= x <= box[2] and box[1] <= y <= box[3]:
                    return tag
        return None

    def _menu_action_for(self, canvas, x, y) -> None:
        """菜单里坐标 (x, y) 对应哪个动作，并执行它。

        真实点击走的是同一个方法（传入 `event.widget` 与 `event.x/y`），
        所以探针验过的分支就是用户会走到的分支。
        命中判定见 `_menu_row_at` —— 那里解释了为什么不能用 `find_closest`。
        """
        tag = self._menu_row_at(canvas, x, y)
        # 打点：命中判定必须可观测。之前出现过"探针日志说点了退出、
        # 进程却没退"的情况，光看结果分不清是没命中还是 quit() 失败。
        self._dbg("_menu_action_for xy=%s -> %s" % ((x, y), tag))
        if tag == "quit":
            self.quit(terminate=True)
        elif tag == "settings":
            self.open_settings()
        else:
            self._close_context_menu()

    # -- geometry helpers --------------------------------------------------

    def _ring_size(self) -> int:
        return D(S["ring_size"])

    def _detail_width(self) -> int:
        return int(round(DETAIL_W * UI_SCALE * self.detail_scale))

    def _gpu_model_layout(self):
        """Fit the complete detected model using the same type as the GPU label."""
        name = gpu_info.name() or self.gpu_name or ""
        scale = UI_SCALE * self.detail_scale
        font = (FONTS["ui"], -max(1, int(round(11.5 * scale))), "normal")
        measure = lambda value: int(self.root.tk.call("font", "measure", font, value))
        label_width = measure("显卡")
        available = max(1, self._detail_width() - int(round(PAD * 2 * scale))
                        - label_width - int(round(12 * scale)))
        lines, current = [], ""
        for word in name.split():
            candidate = (current + " " + word).strip()
            if current and measure(candidate) > available:
                lines.append(current)
                current = ""
            while measure(word) > available and len(word) > 1:
                cut = 1
                while cut < len(word) and measure(word[:cut + 1]) <= available:
                    cut += 1
                lines.append(word[:cut])
                word = word[cut:]
            current = (current + " " + word).strip()
        if current:
            lines.append(current)
        line_height = int(self.root.tk.call("font", "metrics", font, "-linespace"))
        extra = max(0, len(lines) - 1) * max(14, math.ceil(line_height / scale))
        return "\n".join(lines), extra

    def _gpu_h(self) -> int:
        """Show a GPU section only when available; allow the full model to wrap."""
        return GPU_H + self._gpu_model_layout()[1] if gpu_info.available() else 0


    def _detail_height(self) -> int:
        height = BASE_H
        height += self._gpu_h()
        if S["history_enabled"]:
            height += HISTORY_H
        if S["top5_enabled"]:
            height += TOP5_H
        return int(round(height * UI_SCALE * self.detail_scale))

    def _settings_size(self):
        return D(SETTINGS_W), D(SETTINGS_H)

    # -- window plumbing ---------------------------------------------------

    def _make_transparent_toplevel(self) -> tk.Toplevel:
        window = tk.Toplevel(self.root)
        window.withdraw()
        window.overrideredirect(True)
        # 面板/菜单默认跟随 `pin_top`：勾选置顶时浮在其它窗口之上，
        # 不勾选时就和普通窗口一样会被别的程序盖住。圆环窗口在
        # `_apply_topmost` 里一起切。
        window.attributes("-topmost", bool(S["pin_top"]))
        window.configure(bg=TRANSPARENT)
        try:
            window.attributes("-transparentcolor", TRANSPARENT)
        except tk.TclError:
            pass
        # **每个新面板都要单独挂钩**：`Toplevel` 有自己独立的窗口过程，
        # 只挂 root 的话发给面板的 `WM_CLOSE` 会被 Tk 丢掉，
        # 表现是"面板收不到关闭请求、进程一直不退"。见 `_install_wndproc`。
        # 必须等窗口真的建出来（`winfo_id` 有效）才能挂 —— 这里已经
        # `withdraw()` 过了，窗口存在但没有映射，句柄是有效的。
        try:
            window.update_idletasks()
        except tk.TclError:
            pass
        self._install_wndproc(window)
        return window

    def _apply_topmost(self) -> None:
        """把 `pin_top` 应用到本程序的每一个窗口。

        置顶是窗口属性，不是画布内容，所以改完之后不需要重画任何东西 ——
        但**已经存在的**窗口不会自己跟着变，必须逐个设置。菜单是临时窗口，
        每次弹出时由 `_make_transparent_toplevel` 现取当时的设置，不用管。
        """
        wanted = bool(S["pin_top"])
        for window in (self.root, self.detail, self.settings_window):
            if window is None:
                continue
            try:
                window.attributes("-topmost", wanted)
            except tk.TclError:
                pass

    def _dbg(self, text) -> None:
        """调试打点：只在设了 `MEMORY_WIDGET_DEBUG` 时写一行到该文件。

        用来查"窗口建好又被谁关掉"这类时序问题 —— 事后从外部只能看到
        最终状态，看不到中间哪一步动的手。
        """
        path = os.environ.get("MEMORY_WIDGET_DEBUG", "")
        if not path:
            return
        try:
            with open(path, "a", encoding="utf-8") as fh:
                fh.write("%s\n" % (text,))
        except OSError:
            pass

    def _clamped_xy(self, x: int, y: int, width: int, height: int, anchor=None):
        point_x, point_y = anchor if anchor else (x + width // 2, y + height // 2)
        left, top, right, bottom = monitor_work_area(point_x, point_y)
        margin = D(8)
        max_x = max(left + margin, right - width - margin)
        max_y = max(top + margin, bottom - height - margin)
        return (max(left + margin, min(x, max_x)),
                max(top + margin, min(y, max_y)))

    def _place_initially(self) -> None:
        size = self._ring_size()
        if S["pos"]:
            x, y = self._clamped_xy(S["pos"][0], S["pos"][1], size, size)
        else:
            _left, _top, right, bottom = monitor_work_area(0, 0)
            x = right - size - D(28)
            y = bottom - size - D(28)
        self.root.geometry(f"{size}x{size}+{x}+{y}")

    # -- the orb -----------------------------------------------------------

    def _build_ring(self) -> None:
        if getattr(self, "ring_canvas", None):
            self.ring_canvas.destroy()
        size = self._ring_size()
        design = int(S["ring_size"])
        colors = palette()
        self.root.geometry(f"{size}x{size}")
        canvas = self.ring_canvas = tk.Canvas(
            self.root, width=size, height=size, bg=TRANSPARENT,
            highlightthickness=0, bd=0,
        )
        set_photo_master(canvas)
        canvas.pack()

        self.ring_photo = photo_for(
            ("ring", size, theme_name(), 0.0, colors["orange"]),
            ring_png(size, colors, 0.0, colors["orange"],
                     backdrop="theme"),
        )
        self.ring_image_item = canvas.create_image(
            0, 0, image=self.ring_photo, anchor="nw"
        )
        center = size / 2.0
        self.ring_text_y = (
            center + D(design * 0.02) if design >= 72
            else center + D(design * 0.095)
        )
        canvas.create_text(
            center, self.ring_text_y, text="--", fill=colors["text"],
            font=display_font(max(14, design * 0.235), "bold"),
            anchor="sw", tags="ring_number",
        )
        canvas.create_text(
            center, self.ring_text_y, text="%", fill=colors["orange"],
            font=display_font(max(10, design * 0.115), "bold"),
            anchor="sw", tags="ring_unit",
        )
        if design >= 72:
            canvas.create_text(
                center, center + D(design * 0.215), text="",
                fill=colors["tertiary"],
                font=mono_font(max(9, design * 0.115)), anchor="center",
                tags="ring_foot",
            )
        self._center_ring_text()

        canvas.bind("<Button-1>", self._on_press)
        canvas.bind("<B1-Motion>", self._on_drag)
        canvas.bind("<ButtonRelease-1>", self._on_release)
        canvas.bind("<Enter>", self._on_enter)
        canvas.bind("<Leave>", self._on_leave)
        canvas.bind("<Button-3>", self._show_context_menu)
        self.last_ring_state = None

    def _center_ring_text(self) -> None:
        """Centre the number and its unit sign as one optical group.

        Both items are anchored south-west, so the anchor is the *left* edge of
        the text; the group is placed by measuring the painted widths.
        """
        canvas = self.ring_canvas
        size = self._ring_size()
        canvas.update_idletasks()
        number_box = canvas.bbox("ring_number")
        unit_box = canvas.bbox("ring_unit")
        if not number_box or not unit_box:
            return
        number_width = number_box[2] - number_box[0]
        unit_width = unit_box[2] - unit_box[0]
        gap = max(1, D(S["ring_size"] * 0.025))
        left = size / 2.0 - (number_width + gap + unit_width) / 2.0
        canvas.coords("ring_number", left, self.ring_text_y)
        canvas.coords("ring_unit", left + number_width + gap, self.ring_text_y)

    def _status(self, percent: float):
        """Return (ink colour, pill fill, wording) for the pressure level."""
        colors = palette()
        if percent >= int(S["warn_red"]):
            return colors["red_ink"], colors["red_fill"], "压力较高"
        if percent >= int(S["warn_yellow"]):
            return colors["orange_ink"], colors["warn_fill"], "需要留意"
        return colors["green_ink"], colors["green_fill"], "运行顺畅"

    def _draw_ring(self, force: bool = False) -> None:
        # No reading yet (first poll failed): show a neutral state, never a
        # fake "0%". Drawing percent 0 would paint a full empty track with a
        # green reading underneath it - indistinguishable from "the machine
        # is idle", which is exactly the wrong message when we simply do not
        # know. The arc is drawn with percent 0 (track only, no accent) and
        # the number is replaced by "--" with no unit.
        has_data = self.last_memory is not None
        percent = self.last_memory[0] if has_data else 0
        used = f"{self.last_memory[1]:.1f} GB" if has_data else ""
        colors = palette()
        state = (percent, theme_name(), S["ring_size"], used, has_data)
        if not force and state == self.last_ring_state:
            return
        self.last_ring_state = state

        size = self._ring_size()
        # The progress arc changes often, so it is rebuilt rather than cached;
        # it still has to be built on the interpreter that owns the canvas.
        self.ring_photo = tk.PhotoImage(
            data=ring_png(size, colors, percent, colors["orange"],
                          backdrop="theme"), format="png",
            master=self.ring_canvas,
        )
        canvas = self.ring_canvas
        canvas.itemconfigure(self.ring_image_item, image=self.ring_photo)
        canvas.itemconfigure(
            "ring_number", text=f"{percent}" if has_data else "--",
            fill=colors["text"])
        # Hide the "%" unit while there is nothing to be a percent of.
        canvas.itemconfigure("ring_unit", fill=colors["orange"],
                             text="%" if has_data else "")
        if int(S["ring_size"]) >= 72:
            canvas.itemconfigure("ring_foot", text=used)
        self._center_ring_text()

    def _apply_dpi_change(self, dpi: int) -> None:
        """Re-render the UI for a new DPI (polled from `_loop`, never from the
        wndproc itself - see the WM_CLOSE note in `_install_wndproc` for why
        Python objects are off-limits inside the window procedure).

        Deliberately minimal: only `UI_SCALE`, the raster caches and the
        window sizes are rebuilt. The layout is authored in design units, so
        structure and proportions cannot misplace - what changes is the
        resolution the plates are rasterised at. `_BITMAP_CACHE` is dropped so
        no stale-scale PNG is reused; on-screen images are replaced wholesale
        by each rebuild's `set_photo_master`.
        """
        global UI_SCALE
        try:
            if UI_SCALE_OVERRIDE:
                return                     # a forced scale ignores the OS
            dpi = float(dpi)
            if not 60.0 < dpi < 480.0:
                return
            new_scale = min(3.0, max(1.0, dpi / 96.0))
            if abs(new_scale - UI_SCALE) < 0.01:
                return
            UI_SCALE = new_scale
            _BITMAP_CACHE.clear()
            x, y = self.root.winfo_x(), self.root.winfo_y()
            self._build_ring()
            size = self._ring_size()
            x, y = self._clamped_xy(x, y, size, size)
            self.root.geometry(f"{size}x{size}+{x}+{y}")
            self._draw_ring(force=True)
            if self.detail:
                self._show_detail()
            if (self.settings_window is not None
                    and self.settings_window.winfo_exists()):
                self._draw_settings()
            self._dbg("DPI 变化已应用 scale=%.3f" % UI_SCALE)
        except Exception:                                # noqa: BLE001
            _log_exc("_apply_dpi_change", *sys.exc_info())

    # -- polling -----------------------------------------------------------

    def _schedule_next(self) -> None:
        self.loop_job = self.root.after(int(S["poll_interval_ms"]), self._loop)

    def watch_close_request(self) -> None:
        """Light timer that notices the `WM_CLOSE` flag between polls.

        Why this exists: the window procedure cannot quit by itself (see
        `_install_wndproc`), it can only raise a flag, and the poll loop's
        interval is up to 2 s. Waiting for the next poll tick made an external
        close take a second or more to even *begin* leaving — measured as
        "the window is gone at 0.2 s but the process lives past 8 s" whenever
        the poll loop had other work queued. 120 ms is below the threshold
        where anyone notices, and it is a single Tk timer registration, so the
        "no disk, no registry, no process enumeration" rule still holds.

        This is **not** a second event loop: it re-arms itself with
        `after()`, same as the poll loop, and runs entirely inside Tk's
        existing mainloop.

        The whole body is wrapped in try/finally: whatever happens, the watch
        re-arms itself unless the teardown has started. Without that, one
        unlucky exception here used to kill the 120 ms heartbeat and every
        exit path that depends on it (WM_CLOSE, session end) went silent.
        """
        self.watch_job = None
        try:
            self._retired_wndprocs.clear()
            if self._wm_endsession_flag[0]:
                session_ending = self._wm_endsession_flag[0] == 2
                self._wm_endsession_flag[0] = 0
                self._dbg("watch_close_request 收到会话结束标志")
                # Tray out first - a ghost icon survives the session. This is
                # a Tk timer callback (GIL held), so tray.remove() is safe
                # here even though the wndproc could not call it directly.
                self._teardown_tray()
                if session_ending:
                    self.quit(terminate=True)
                    return
            if self._wm_quit_flag[0]:
                self._dbg("watch_close_request 收到 WM_CLOSE 标志，执行退出")
                self._wm_quit_flag[0] = 0
                self.quit(terminate=True)
                return
            while self._tray_events and not self._closing:
                wparam, lparam = self._tray_events.popleft()
                if self.tray is not None:
                    try:
                        self.tray.handle_message(wparam, lparam)
                    except Exception:
                        _log_exc("tray action", *sys.exc_info())
        except Exception:                                # noqa: BLE001
            _log_exc("watch_close_request", *sys.exc_info())
        finally:
            if not self._closing:
                self.watch_job = self.root.after(
                    CLOSE_WATCH_MS, self.watch_close_request)

    def _loop(self) -> None:
        self.loop_job = None
        # try/finally is the contract here: the next poll MUST be scheduled
        # even when this tick throws. Previously one exception (a bad plate
        # build, a Tcl error from a half-destroyed panel) propagated out of
        # the callback, Tk logged it and moved on, and the reschedule line
        # never ran - the widget froze on its last good frame with no trace
        # in a windowed build. The finally below keeps the heartbeat alive;
        # `self._closing` stops it only when quit() is tearing down.
        try:
            # 兜底。正常路径由 `watch_close_request` 处理（120 ms 一次，比这轮询快得多）；
            # 这里再查一遍是为了防"监听器恰好被撤掉/异常"这种边角情况，代价是一次
            # ctypes 数组读。窗口过程不能直接退出 —— 详见 `_install_wndproc`。
            if self._wm_quit_flag[0]:
                self._dbg("_loop 收到 WM_CLOSE 标志，执行退出")
                self._wm_quit_flag[0] = 0
                self.quit(terminate=True)
                return
            dpi = self._wm_dpi_flag[0]
            if dpi:
                self._wm_dpi_flag[0] = 0
                self._apply_dpi_change(int(dpi))
            self._poll_once()
            self._draw_ring()
            self.refresh_tray_tip()
            if self.detail and self.detail.winfo_exists():
                self._refresh_detail()
            else:
                self.detail = None
                self.detail_canvas = None
        except Exception:                                # noqa: BLE001
            _log_exc("_loop", *sys.exc_info())
        finally:
            if not self._closing:
                self._schedule_next()

    def _poll_once(self):
        reading = read_memory()
        if reading is not None:
            self.last_memory = reading
            if self.history is not None and reading[2]:
                # Track the memory itself rather than the rounded whole percent,
                # so a genuinely small change still shows in the 5 minute view.
                self.history.push(reading[1] * 100.0 / reading[2], time.monotonic())
        return reading

    # -- ring interaction --------------------------------------------------

    def _on_press(self, event) -> None:
        self._close_context_menu()
        self.press_offset = (
            event.x_root - self.root.winfo_x(),
            event.y_root - self.root.winfo_y(),
        )
        self.press_root = (event.x_root, event.y_root)
        self.dragged = False

    def _on_drag(self, event) -> None:
        if self.press_offset is None:
            return
        if self.press_root:
            distance = abs(event.x_root - self.press_root[0]) + abs(
                event.y_root - self.press_root[1]
            )
            self.dragged = self.dragged or distance >= D(4)
        if self.dragged:
            size = self._ring_size()
            x, y = self._clamped_xy(
                event.x_root - self.press_offset[0],
                event.y_root - self.press_offset[1], size, size,
            )
            self.root.geometry(f"+{x}+{y}")
            if self.detail:
                self._place_detail()

    def _on_release(self, _event) -> None:
        if not self.dragged:
            self._toggle_detail()
        else:
            S["pos"] = [self.root.winfo_x(), self.root.winfo_y()]
            self._persist_settings("ring position after drag")
        self.press_offset = None
        self.press_root = None
        self.dragged = False

    def _on_enter(self, _event) -> None:
        if self.close_job:
            self.root.after_cancel(self.close_job)
            self.close_job = None
        if S["hover_expand"] and not self.detail and not self.hover_job:
            self.hover_job = self.root.after(220, self._hover_open)

    def _hover_open(self) -> None:
        self.hover_job = None
        if not self.detail:
            self._show_detail()

    def _on_leave(self, _event) -> None:
        if self.hover_job:
            self.root.after_cancel(self.hover_job)
            self.hover_job = None
        if S["hover_expand"] and self.detail:
            self._schedule_hover_close()

    def _schedule_hover_close(self) -> None:
        if self.close_job:
            self.root.after_cancel(self.close_job)
        self.close_job = self.root.after(520, self._maybe_hover_close)

    def _maybe_hover_close(self) -> None:
        self.close_job = None
        if S["hover_expand"] and not getattr(self.detail, "pointer_inside", False):
            self._hide_detail()

    # -- detail panel ------------------------------------------------------

    def _toggle_detail(self) -> None:
        if self.detail:
            self._hide_detail()
        else:
            self._show_detail()

    def _show_detail(self) -> None:
        self._hide_detail()
        self._close_context_menu()
        width = self._detail_width()
        height = self._detail_height()
        window = self.detail = self._make_transparent_toplevel()
        window.pointer_inside = False
        canvas = self.detail_canvas = tk.Canvas(
            window, width=width, height=height, bg=TRANSPARENT,
            highlightthickness=0, bd=0,
        )
        set_photo_master(canvas)
        canvas.pack()
        self._place_detail()
        # 取数线程只在面板存在期间跑 —— 面板关着的时候不该有任何子进程开销。
        # 启动放在 `_build_detail` 之前：探测哪怕还没出结果，下一轮刷新也会
        # 发现 `_gpu_h()` 变了而重建面板（见 `_refresh_detail`）。
        gpu_info.start()
        self._build_detail()
        window.bind("<Enter>", self._detail_enter)
        window.bind("<Leave>", self._detail_leave)
        # One dispatcher per gesture stage: a press either starts a move (the
        # panel background) or a resize (the corner grip), and the motion and
        # release handlers route to whichever one started. Binding at the
        # canvas - not on the items - is what keeps a drag alive when the
        # pointer leaves the grip or the panel is resized under it.
        canvas.bind("<Button-1>", self._move_press)
        canvas.bind("<B1-Motion>", self._panel_motion)
        canvas.bind("<ButtonRelease-1>", self._panel_release)
        canvas.bind("<Motion>", self._panel_hover)
        window.deiconify()
        set_window_rounding(window)
        self.last_detail_state = None
        self.last_chart_revision = -1
        self._refresh_detail(force=True)
        # The panel is a fresh window, so it needs the pin setting applied to it
        # as well; `_make_transparent_toplevel` already did it at creation, but
        # this also covers a widget whose root was toggled while the panel was
        # closed via a stale settings file.
        self._apply_topmost()

    def _place_detail(self) -> None:
        if not self.detail:
            return
        width = self._detail_width()
        height = self._detail_height()
        root_x, root_y = self.root.winfo_x(), self.root.winfo_y()
        ring = self._ring_size()
        offset = S.get("panel_offset")
        if isinstance(offset, list) and len(offset) == 2:
            # The user parked the panel somewhere; it keeps that spot relative
            # to the ring, so moving the ring moves the panel along with it.
            x, y = root_x + offset[0], root_y + offset[1]
        else:
            left, _top, _right, _bottom = monitor_work_area(
                root_x + ring // 2, root_y + ring // 2
            )
            gap = D(14)
            x = (root_x - width - gap) if root_x - width - gap >= left + D(8) \
                else root_x + ring + gap
            y = root_y - D(20)
        x, y = self._clamped_xy(
            x, y, width, height, anchor=(x + width // 2, y + height // 2)
        )
        self.detail.geometry(f"{width}x{height}+{x}+{y}")

    def _detail_enter(self, _event) -> None:
        if self.detail:
            self.detail.pointer_inside = True
        if self.close_job:
            self.root.after_cancel(self.close_job)
            self.close_job = None

    def _detail_leave(self, _event) -> None:
        if self.detail:
            self.detail.pointer_inside = False
        if S["hover_expand"]:
            self._schedule_hover_close()

    def _hairline(self, canvas, x, y, width, color):
        height = D(1)
        canvas.create_image(
            x, y, anchor="nw",
            image=photo_for(("hairline", width, height, color),
                            plate_png(width, height, 0, color)),
        )

    @_at_detail_scale
    def _draw_detail_gpu(self, canvas, colors, left, right, content, y):
        caption, extra = self._gpu_model_layout()
        self._hairline(canvas, left, y, content, colors["hairline"])
        canvas.create_text(left, y + D(21), text="显卡", fill=colors["secondary"],
                           font=ui_font(11.5), anchor="w", tags="gpu_title")
        model_font = ui_font(11.5)
        ascent = int(self.root.tk.call("font", "metrics", model_font, "-ascent"))
        descent = int(self.root.tk.call("font", "metrics", model_font, "-descent"))
        canvas.create_text(right, y + D(21) - (ascent + descent) / 2,
                           text=caption, fill=colors["secondary"], font=model_font,
                           anchor="ne", justify="right", tags="gpu_name")
        row_y = y + D(49 + extra)
        canvas.create_text(left, row_y, text="--", fill=colors["text"],
                           font=display_font(16, "bold"), anchor="w", tags="gpu_temp")
        canvas.create_text(left, row_y, text="°C", fill=colors["tertiary"],
                           font=ui_font(11), anchor="w", tags="gpu_temp_unit")
        self.gpu_bar_photo = tk.PhotoImage(
            data=bar_png(self._gpu_bar_width(), D(4), 0.0, 1.01, colors),
            format="png", master=canvas)
        canvas.create_image(left + D(70), row_y - D(2), anchor="nw",
                            image=self.gpu_bar_photo, tags="gpu_bar")
        canvas.create_text(right, row_y, text="--%", fill=colors["secondary"],
                           font=ui_font(11.5), anchor="e", tags="gpu_util")

    @_at_detail_scale
    def _align_capacity(self):
        canvas = self.detail_canvas
        for index, tag in enumerate(("used_value", "available_value")):
            tags = (f"stat_label_{index}", tag, tag + "_unit")
            widths = [int(self.root.tk.call("font", "measure",
                        canvas.itemcget(t, "font"), canvas.itemcget(t, "text")))
                      for t in tags]
            gaps = (D(6), D(4))
            x = D(PAD) if index == 0 else self._detail_width() - D(PAD) - sum(widths) - sum(gaps)
            for i, item in enumerate(tags):
                descent = int(self.root.tk.call("font", "metrics",
                              canvas.itemcget(item, "font"), "-descent"))
                canvas.coords(item, x, D(CAPACITY_BASELINE) + descent)
                x += widths[i] + (gaps[i] if i < 2 else 0)

    def _detail_segment_photo(self, colors, active):
        return plate_photo("detail-seg" + str(active), SEG_W - 4, SEG_H - 5, 8,
                           colors["selected"] if active else colors["surface"])

    @_at_detail_scale
    def _build_detail(self) -> None:
        canvas, colors = self.detail_canvas, palette()
        width, height = self._detail_width(), self._detail_height()
        self.gpu_h_built = self._gpu_h()
        left, right = D(PAD), width - D(PAD)
        content = right - left
        canvas.create_image(0, 0, anchor="nw", tags="detail_bg",
            image=photo_for(("card-edge", width, height, theme_name()),
                            plate_png(width, height, D(28), colors["edge"])))
        rim = max(1, D(.8))
        canvas.create_image(rim, rim, anchor="nw",
            image=photo_for(("card-body", width, height, rim, theme_name()),
                            plate_png(width - 2 * rim, height - 2 * rim,
                                      D(27.2), colors["card"])))
        canvas.create_text(left, D(32), text="内存", fill=colors["text"],
                           font=ui_font(13, "bold"), anchor="w", tags="detail_title")
        self._draw_detail_header_buttons(canvas, colors, right)
        canvas.create_text(left, D(HERO_BOTTOM), text="--", fill=colors["text"],
                           font=display_font(44, "bold"), anchor="sw", tags="hero_value")
        canvas.create_text(left, D(HERO_BOTTOM - 6), text="%", fill=colors["secondary"],
                           font=display_font(16), anchor="sw", tags="hero_unit")
        canvas.create_image(right - D(78), D(72), anchor="nw", tags="status_pill",
                            image=plate_photo("pill-healthy", 78, 25, 12.5, colors["green_fill"]))
        canvas.create_text(right - D(39), D(84.5), text="读取中", fill=colors["secondary"],
                           font=ui_font(10, "bold"), tags="status_text")
        self.meter_photo = tk.PhotoImage(data=bar_png(content, D(BAR_H), 0.0,
                            float(S["warn_red"]) / 100.0, colors), format="png", master=canvas)
        canvas.create_image(left, D(BAR_TOP), anchor="nw", image=self.meter_photo, tags="meter")
        canvas.create_image(left + content * float(S["warn_red"]) / 100.0, D(BAR_TOP - 1),
                            anchor="nw", tags="warning_tick",
                            image=plate_photo("tick", 1, BAR_H + 2, .5, colors["orange_ink"]))
        for index, (label, tag) in enumerate((("已用", "used_value"), ("可用", "available_value"))):
            for item_tag, value, font, ink in (
                (f"stat_label_{index}", label, ui_font(11), colors["secondary"]),
                (tag, "--", display_font(14), colors["text"]),
                (tag + "_unit", "GB", ui_font(11), colors["secondary"])):
                canvas.create_text(0, 0, text=value, font=font, fill=ink, anchor="sw", tags=item_tag)
        self._align_capacity()
        y = D(BASE_H)
        if self.gpu_h_built:
            self._draw_detail_gpu(canvas, colors, left, right, content, y)
            y += D(self.gpu_h_built)
        self.chart_box = None
        if S["history_enabled"]:
            self._hairline(canvas, left, y, content, colors["hairline"])
            canvas.create_text(left, y + D(24), text="趋势", fill=colors["text"],
                               font=ui_font(12, "bold"), anchor="w", tags="trend_title")
            track_w = D(SEG_W * 3)
            track_x, track_y = right - track_w, y + D(10)
            canvas.create_image(track_x, track_y, anchor="nw", tags="scale_track",
                image=photo_for(("detail-track", track_w, D(SEG_H), theme_name()),
                                plate_png(track_w, D(SEG_H), D(10), colors["surface"])))
            for index, (_, label) in enumerate(self.HISTORY_RANGES):
                tag, active = f"scale_{index}", index == self.scale_index
                canvas.create_image(track_x + D(2 + index * SEG_W), track_y + D(2), anchor="nw",
                                    tags=(tag, f"scale_bg_{index}"),
                                    image=self._detail_segment_photo(colors, active))
                canvas.create_text(track_x + D(index * SEG_W + SEG_W / 2), track_y + D(SEG_H / 2),
                    text=label, fill=colors["orange_ink"] if active else colors["secondary"],
                    font=ui_font(10.5, "bold" if active else "normal"), tags=(tag, f"scale_text_{index}"))
                canvas.tag_bind(tag, "<Button-1>", lambda _e, i=index: self._switch_scale(i))
            canvas.create_text(left, y + D(51), text="", fill=colors["tertiary"],
                               font=ui_font(10.5), anchor="w", tags="range_label")
            self.chart_box = (left, y + D(42), right, y + D(149))
            y += D(HISTORY_H)
        if S["top5_enabled"]:
            self._hairline(canvas, left, y, content, colors["hairline"])
            canvas.create_text(left, y + D(22), text="占用最多", fill=colors["text"],
                               font=ui_font(12, "bold"), anchor="w", tags="top5_title")
            canvas.create_text(right, y + D(22), text="工作集 · 12 秒更新", fill=colors["tertiary"],
                               font=ui_font(10.5), anchor="e", tags="top5_meta")
            for index in range(5):
                row_y = y + D(48 + index * ROW_H)
                for tag, x, value, font, ink, anchor in (
                    (f"proc_rank_{index}", left + D(6), str(index + 1), ui_font(10), colors["tertiary"], "e"),
                    (f"proc_name_{index}", left + D(19), "", ui_font(11.5), colors["text"], "w"),
                    (f"proc_count_{index}", left + D(19), "", ui_font(10), colors["tertiary"], "w"),
                    (f"proc_mem_{index}", right - D(24), "", mono_font(11.5), colors["text"], "e"),
                    (f"proc_unit_{index}", right, "", ui_font(10.5), colors["tertiary"], "e")):
                    canvas.create_text(x, row_y, text=value, font=font, fill=ink, anchor=anchor, tags=tag)
                    canvas.tag_bind(tag, "<Button-1>", lambda _e, i=index: self._show_process_menu(_e, i))
            canvas.create_text(left, y + D(158), text="", fill=colors["tertiary"],
                               font=ui_font(10.5), anchor="w", tags="action_hint")
        # The native drag target stays large while the visual grip stays quiet.
        for index in range(2):
            canvas.create_line(right - D(11 - index * 4), height - D(21),
                               right - D(4 - index * 4), height - D(28),
                               fill=colors["tertiary"], width=max(1, D(.6)), tags="resize_grip")
        canvas.create_rectangle(right - D(24), height - D(24), width, height,
                                fill="", outline="", tags="resize_grip_hit")
        canvas.tag_bind("resize_grip", "<Button-1>", self._resize_press)
        canvas.tag_bind("resize_grip_hit", "<Button-1>", self._resize_press)


    # -- header buttons ----------------------------------------------------

    def _draw_pin(self, canvas, x, y, colors) -> None:
        """Outline and tilt when free; orange and upright when pinned."""
        pinned = bool(S["pin_top"])
        ink = colors["orange"] if pinned else colors["secondary"]
        size = D(20)
        glyph = photo_for(("pin", size, ink, pinned), pin_png(size, ink, pinned))
        canvas.coords("pin_head", x + D(PIN_SIZE) / 2, y + D(PIN_SIZE) / 2)
        canvas.itemconfigure("pin_head", image=glyph)

    @_at_detail_scale
    def _draw_detail_header_buttons(self, canvas, colors, right) -> None:
        """The pin toggle and the close button, side by side in the title row.

        Both are built at the correct detail scale here, and the hover handlers
        only *swap references*: they run later, when `_DETAIL_SCALE` is back to
        1.0, so anything they compute on the spot would be drawn at the wrong
        size. That is the reason the two close plates are pre-built below.
        """
        size = D(PIN_SIZE)
        close = D(HEADER_H)
        close_x = right - close
        pin_x = close_x - D(HEADER_GAP) - size
        row_y = D(HEADER_TOP)
        plate = plate_photo("headerbtn", PIN_SIZE, PIN_SIZE,
                            PIN_SIZE / 2, colors["card_soft"])
        plate_hover = plate_photo("headerbtn-hover", PIN_SIZE, PIN_SIZE,
                                  PIN_SIZE / 2, colors["surface_hover"])

        # Build both hover states now (at the correct scale); the callbacks only
        # swap references, because they run later, when _DETAIL_SCALE is reset.
        close_plate = plate_photo("close", 26, 26, 13, colors["card_soft"])
        close_hover_plate = plate_photo("close-hover", 26, 26, 13,
                                        colors["surface_hover"])

        # Both pin states are pre-rendered at this panel's actual scale.
        active_plate = plate_photo("pin-active", PIN_SIZE, PIN_SIZE,
                                   PIN_SIZE / 2, pin_plate_color(colors))
        active_hover = plate_photo("pin-active-hover", PIN_SIZE, PIN_SIZE,
                                   PIN_SIZE / 2, pin_plate_color(colors, True))
        self._pin_hovered = False
        canvas.create_image(pin_x, row_y, anchor="nw", tags="pin_bg",
                            image=active_plate if S["pin_top"] else plate)
        canvas.create_image(0, 0, tags=("pin", "pin_head"))
        self._draw_pin(canvas, pin_x, row_y, colors)
        for tag in ("pin", "pin_bg"):
            canvas.tag_bind(tag, "<Button-1>", lambda _e: self._toggle_pin())
        def pin_hover(event, hovered):
            self._pin_hovered = hovered
            image = ((active_hover if hovered else active_plate) if S["pin_top"]
                     else (plate_hover if hovered else plate))
            canvas.itemconfigure("pin_bg", image=image)
        for tag in ("pin", "pin_bg"):
            canvas.tag_bind(tag, "<Enter>", lambda e: pin_hover(e, True))
            canvas.tag_bind(tag, "<Leave>", lambda e: pin_hover(e, False))

        # --- close ---
        canvas.create_image(close_x, row_y, anchor="nw", tags="close_bg",
                            image=close_plate)
        canvas.create_text(close_x + close / 2, D(32), text="×",
                           fill=colors["secondary"], font=ui_font(15),
                           tags="close")
        for tag in ("close", "close_bg"):
            canvas.tag_bind(tag, "<Button-1>", lambda _e: self._hide_detail())
        canvas.tag_bind(
            "close", "<Enter>",
            lambda _e: canvas.itemconfigure("close_bg", image=close_hover_plate))
        canvas.tag_bind(
            "close", "<Leave>",
            lambda _e: canvas.itemconfigure("close_bg", image=close_plate))

    # -- pin ---------------------------------------------------------------

    def _toggle_pin(self) -> None:
        """Flip the pin button. Checked keeps the widget above other windows;
        unchecked lets it fall behind them like any normal window. The choice is
        written to settings.json, so the next launch starts the same way.
        """
        self._set_pin(not bool(S["pin_top"]))

    def _set_pin(self, value) -> None:
        S["pin_top"] = bool(value)
        self._persist_settings("pin toggle")
        self._apply_topmost()
        self._repaint_pin()

    @_at_detail_scale
    def _repaint_pin(self) -> None:
        """Refresh the pin's own appearance in place.

        Deliberately not `self._show_detail()`: that would rebuild the whole
        panel just to flip two colours, and the rebuild also drops the pointer's
        hover state for the button the user is standing on.
        """
        canvas = self.detail_canvas
        if canvas is None:
            return
        colors = palette()
        inked = bool(S["pin_top"])
        canvas.itemconfigure(
            "pin_bg",
            image=plate_photo("pin-active", PIN_SIZE, PIN_SIZE, PIN_SIZE / 2,
                              pin_plate_color(colors, getattr(self, "_pin_hovered", False))) if inked
            else plate_photo("headerbtn", PIN_SIZE, PIN_SIZE, PIN_SIZE / 2,
                             colors["surface_hover"] if getattr(self, "_pin_hovered", False)
                             else colors["card_soft"]))
        box = canvas.bbox("pin_bg")
        if box:
            self._draw_pin(canvas, box[0], box[1], colors)
        canvas.tag_raise("pin")

    @_at_detail_scale
    def _switch_scale(self, index: int) -> None:
        self.scale_index = index
        colors = palette()
        for i in range(3):
            active = i == index
            self.detail_canvas.itemconfigure(f"scale_bg_{i}", image=self._detail_segment_photo(colors, active))
            self.detail_canvas.itemconfigure(f"scale_text_{i}",
                fill=colors["orange_ink"] if active else colors["secondary"],
                font=ui_font(10.5, "bold" if active else "normal"))
        self.last_chart_revision = -1
        self._draw_chart()


    @_at_detail_scale
    def _refresh_detail(self, force: bool = False) -> None:
        if not self.detail or not self.detail.winfo_exists() or not self.last_memory:
            return
        # GPU 数据是后台线程异步来的，所以"面板里该不该有 GPU 区"这件事本身
        # 会变：打开面板时探测可能还没出结果（按 0 高度画的），几十毫秒后
        # 取到了就得重建版式。这里靠比对期望高度来发现，重建而不是硬塞 ——
        # 面板高度、分隔线位置、下面几块的整体下移都得跟着走。
        if self._gpu_h() != self.gpu_h_built:
            self.gpu_h_built = self._gpu_h()
            self._resize_detail_to_fit()
            return
        self.gpu_name = gpu_info.name()
        percent, used, _total, available = self.last_memory
        colors = palette()
        ink, fill, wording = self._status(percent)
        canvas = self.detail_canvas
        state = (percent, round(used, 2), round(available, 2), ink, theme_name())
        if force or state != self.last_detail_state:
            self.last_detail_state = state
            canvas.itemconfigure("hero_value", text=f"{percent}",
                                 fill=colors["text"])
            hero_box = canvas.bbox("hero_value")
            if hero_box:
                canvas.coords("hero_unit", hero_box[2] + D(2), D(HERO_BOTTOM - 6))
            canvas.itemconfigure("status_pill",
                                 image=plate_photo("pill", 78, 25, 12.5, fill))
            canvas.itemconfigure("status_text", text=wording, fill=ink)
            for tag, value in (("used_value", used), ("available_value", available)):
                canvas.itemconfigure(tag, text=f"{value:.1f}")
            self._align_capacity()
            self.meter_photo = tk.PhotoImage(
                data=bar_png(self._detail_width() - D(PAD * 2), D(BAR_H),
                             percent / 100.0, float(S["warn_red"]) / 100.0,
                             colors),
                format="png", master=canvas,
            )
            canvas.itemconfigure("meter", image=self.meter_photo)
            canvas.tag_raise("warning_tick")

        if self._gpu_h():
            self._refresh_detail_gpu(canvas, colors)
        if S["history_enabled"]:
            revision = self.history.revision if self.history else -1
            if force or revision != self.last_chart_revision:
                self._draw_chart()
        if S["top5_enabled"]:
            self._refresh_top5(force=force)

    def _gpu_bar_photo(self, canvas, width, percent, colors):
        """GPU 占用条的光栅化贴图，并持有引用。

        **最小可见宽度**：占用率很低时（1~2%，显卡闲着的时候就是这样），
        纯按比例算出来的填充连两个圆角端头都填不满，整条看起来是空的 ——
        用户会以为这个功能坏了。所以只要占用率不为 0，就至少画出一个圆点的
        宽度。这是仪表盘的通行做法，**不是美化数据**：右边的百分比数字始终是
        真值，被保底的只有那一点像素。

        `tk.PhotoImage` 必须保持引用，否则 Tk 会把像素丢掉、条子变成透明 ——
        和 `_photo_keepalive` 是同一个道理。这里单独用一个属性而不是走
        `photo_for`，因为占用率每几秒就变一次，缓存会一直堆。
        """
        fraction = max(0.0, min(1.0, percent / 100.0))
        if fraction > 0.0:
            dot = D(4) / float(width)                # 一个圆点占条长的比例
            fraction = max(fraction, dot)
        self.gpu_bar_photo = tk.PhotoImage(
            data=bar_png(width, D(4), fraction, 1.01, colors),
            format="png", master=canvas,
        )
        return self.gpu_bar_photo

    @_at_detail_scale
    def _refresh_detail_gpu(self, canvas, colors) -> None:
        """把 GPU 读数写进面板。没数据就保持占位，不动。

        GPU 数据是后台线程异步取的，所以这里有两种情况要分开：
        数字变了（换文本 / 换进度条贴图）、以及面板还没画过 GPU 区
        （`_build_detail` 时探测还没出结果，那时按 0 高度画的）。
        后者交给调用方重建 —— 见 `_refresh_detail` 开头的 `state` 比较。
        """
        value = gpu_info.reading()
        if value is None:
            return
        temperature, utilization, used_mb, total_mb = value
        if not canvas.find_withtag("gpu_temp"):
            return                                   # 这一版面板没含 GPU 区
        # 先换图、再改文本，最后统一对齐 —— 顺序反了的话，对齐用的是旧文本的
        # 包围盒（"53" 和 "100" 宽度不同；"5%" 和 "100%" 同理）。
        canvas.itemconfigure(
            "gpu_bar",
            image=self._gpu_bar_photo(canvas, self._gpu_bar_width(),
                                      utilization, colors),
        )
        canvas.itemconfigure("gpu_temp", text=str(temperature))
        canvas.itemconfigure("gpu_util", text="%d%%" % utilization)
        canvas.itemconfigure("gpu_name",
                             text=self._gpu_caption(used_mb, total_mb))
        self._align_detail_gpu(canvas, colors)

    @_at_detail_scale
    def _align_detail_gpu(self, canvas, colors) -> None:
        """把 GPU 区那几个元素对到同一条水平中线上。

        温度是这一行的锚：字号最大（16 号），单位、进度条、占用率都对到它的
        **垂直中线**上。用固定设计坐标去猜是没用的 —— 它们的字号各不相同
        （16 / 11 / 一条 6px 高的条子），bbox 高度差好几像素，只有实测才能
        对齐。第一版靠手填 y 偏移，结果 °C 挂到了数字的左上角、
        占用率也高出半行。
        """
        box = canvas.bbox("gpu_temp")
        if not box:
            return
        middle = (box[1] + box[3]) // 2
        canvas.coords("gpu_temp_unit", box[2] + D(3), middle)
        canvas.coords("gpu_util", self._detail_width() - D(PAD), middle)
        bar_box = canvas.bbox("gpu_bar")
        if bar_box:
            canvas.coords("gpu_bar", bar_box[0],
                          middle - (bar_box[3] - bar_box[1]) // 2)

    def _gpu_caption(self, used_mb, total_mb) -> str:
        """Keep the detected full model, using line wrapping instead of truncation."""
        return self._gpu_model_layout()[0]

    def _gpu_bar_width(self) -> int:
        return max(D(24), self._detail_width() - D(PAD * 2 + 70 + 39))


    @_at_detail_scale
    def _draw_chart(self) -> None:
        canvas = self.detail_canvas
        if not canvas or not self.history or not self.chart_box:
            return
        canvas.delete("chart_dynamic")
        self.last_chart_revision = self.history.revision
        values = self.history.get(self.HISTORY_RANGES[self.scale_index][0], time.monotonic())
        x1, y1, x2, y2 = self.chart_box
        colors = palette()
        line, low, high, band_low, band_high, algorithm = trend_series(values, self.scale_index)
        if len(line) < 2:
            canvas.create_text((x1 + x2) / 2, (y1 + y2) / 2,
                text="正在积累趋势数据 · 每 5 秒一个采样点", fill=colors["tertiary"],
                font=ui_font(11), tags="chart_dynamic")
            return
        canvas.create_text(x1, y1 + D(9), text=f"{min(values):.1f} – {max(values):.1f}%",
                           fill=colors["tertiary"], font=ui_font(10.5), anchor="w", tags="chart_dynamic")
        meta = "每 5 秒采样" if self.scale_index == 0 else algorithm
        canvas.create_text(x2, y1 + D(9), text=meta, fill=colors["tertiary"],
                           font=ui_font(10.5), anchor="e", tags="chart_dynamic")
        top = y1 + D(30)
        self.chart_photo = tk.PhotoImage(data=chart_png(
            x2 - x1, y2 - top + 1, line, low, high, colors,
            pixel_scale=UI_SCALE * self.detail_scale,
            band_low=band_low, band_high=band_high, threshold=float(S["warn_red"])),
            format="png", master=canvas)
        canvas.create_image(x1, top, image=self.chart_photo, anchor="nw", tags="chart_dynamic")

    @_at_detail_scale
    def _refresh_top5(self, force: bool = False) -> None:
        now = time.monotonic()
        refreshed = False
        if now >= self.top5_next_due:
            self.top5_cache = read_top5()
            self.top5_next_due = now + self.TOP5_CACHE_SECONDS
            refreshed = True
        if not (force or refreshed):
            return
        canvas, colors = self.detail_canvas, palette()
        if not self.top5_cache:
            for i in range(5):
                for part in ("name", "count", "mem", "unit"):
                    canvas.itemconfigure(f"proc_{part}_{i}", text="")
            canvas.itemconfigure("action_hint", text="未能读取到进程占用，稍后再试", fill=colors["tertiary"])
            return
        for i in range(5):
            tags = [f"proc_{part}_{i}" for part in ("name", "count", "mem", "unit")]
            name_tag, count_tag, mem_tag, unit_tag = tags
            if i >= len(self.top5_cache):
                for tag in tags:
                    canvas.itemconfigure(tag, text="")
                continue
            name, megabytes, count = self.top5_cache[i]
            amount, unit = memory_text(megabytes).split()
            canvas.itemconfigure(mem_tag, text=amount)
            canvas.itemconfigure(unit_tag, text=unit)
            display = name[:-4] if name.lower().endswith(".exe") else name
            canvas.itemconfigure(name_tag, text=display)
            canvas.itemconfigure(count_tag, text=f"×{count}" if count > 1 else "")
            memory_box = canvas.bbox(mem_tag)
            count_box = canvas.bbox(count_tag)
            count_width = count_box[2] - count_box[0] if count > 1 and count_box else 0
            available_right = memory_box[0] - D(12) - count_width - (D(5) if count > 1 else 0)
            name_box = canvas.bbox(name_tag)
            if name_box and name_box[2] > available_right:
                for length in range(len(display) - 1, -1, -1):
                    canvas.itemconfigure(name_tag, text=display[:length] + "…")
                    name_box = canvas.bbox(name_tag)
                    if name_box and name_box[2] <= available_right:
                        break
            if name_box:
                row_y = canvas.coords(name_tag)[1]
                canvas.coords(count_tag, name_box[2] + D(5), row_y)
        hint = action_hint(self.top5_cache)
        canvas.itemconfigure("action_hint", text=hint, fill=colors["tertiary"])
        # A long process group name may need a compact hint on this narrow card.
        hint_box = canvas.bbox("action_hint")
        available = self._detail_width() - D(PAD + 32)
        if hint_box and hint_box[2] > available:
            amount = memory_text(self.top5_cache[0][1])
            suffix = " 可释放约 " + amount
            prefix = "关闭 " + self.top5_cache[0][0].removesuffix(".exe")
            for length in range(len(prefix), 2, -1):
                canvas.itemconfigure("action_hint", text=prefix[:length] + "…" + suffix)
                if canvas.bbox("action_hint")[2] <= available:
                    break


    def _hide_detail(self) -> None:
        if self.close_job:
            self.root.after_cancel(self.close_job)
            self.close_job = None
        self._resize_start = None
        self._move_start = None
        if self._resize_job:
            self.root.after_cancel(self._resize_job)
            self._resize_job = None
        if self.detail:
            try:
                self._unhook_window(self.detail)
                self.detail.destroy()
            except tk.TclError:
                pass
        self.detail = None
        self.detail_canvas = None
        # 面板没了就别再起 `nvidia-smi` 子进程了。**要在丢引用之前停** ——
        # 取数线程还活着的时候，`gpu_bar_photo` 一旦被 GC，`PhotoImage.__del__`
        # 就会在**那个线程**里调 Tk，抛 `RuntimeError: main thread is not in
        # main loop`（UI 检查脚本里实测到过）。这里在 Tk 主线程里主动解引用，
        # 析构就不会跑到别的线程上。
        gpu_info.stop()
        self.gpu_bar_photo = None

    # -- detail move and resize --------------------------------------------

    # Anything tagged with one of these prefixes handles its own click, so a
    # press there must not turn into "grab the panel and move it".
    _MOVE_BLOCKED = ("close", "resize_grip", "scale_", "pin", "proc_")

    def _press_is_interactive(self, canvas) -> bool:
        current = canvas.find_withtag("current")
        if not current:
            return True
        return self._tags_block_move(canvas.gettags(current[0]))

    @classmethod
    def _tags_block_move(cls, tags) -> bool:
        return any(tag.startswith(cls._MOVE_BLOCKED) for tag in tags)

    def _move_press(self, event) -> None:
        """Press on the panel background: grab it and drag it somewhere else.

        This widget-level binding fires for every press, including on the close
        button and the corner grip; those presses are recognised and left to
        their own item bindings.
        """
        if self._resize_start is not None or not self.detail:
            return
        canvas = self.detail_canvas
        if canvas is None or self._press_is_interactive(canvas):
            return
        self._move_start = (event.x_root, event.y_root,
                            self.detail.winfo_x(), self.detail.winfo_y())
        canvas.configure(cursor="fleur")

    def _move_motion(self, event) -> None:
        start_x, start_y, panel_x, panel_y = self._move_start
        x, y = self._clamped_xy(
            panel_x + (event.x_root - start_x),
            panel_y + (event.y_root - start_y),
            self._detail_width(), self._detail_height(),
            anchor=(event.x_root, event.y_root),
        )
        self.detail.geometry(f"+{x}+{y}")

    def _move_release(self, _event) -> None:
        start_x, start_y, panel_x, panel_y = self._move_start
        self._move_start = None
        if self.detail_canvas is not None:
            self.detail_canvas.configure(cursor="")
        dx = self.detail.winfo_x() - panel_x
        dy = self.detail.winfo_y() - panel_y
        if not dx and not dy:
            return
        # Remember the position relative to the ring, so the pair keeps its
        # arrangement when the ring itself is dragged to another spot.
        S["panel_offset"] = [
            self.detail.winfo_x() - self.root.winfo_x(),
            self.detail.winfo_y() - self.root.winfo_y(),
        ]
        self._persist_settings("detail panel position")

    def _panel_hover(self, _event) -> None:
        """Cursor as an affordance: hand on the buttons, move elsewhere."""
        if self._move_start is not None or self._resize_start is not None:
            return
        canvas = self.detail_canvas
        if canvas is None:
            return
        current = canvas.find_withtag("current")
        tags = canvas.gettags(current[0]) if current else ()
        if self._tags_block_move(tags):
            clickable = ("proc_", "pin", "close")
            want = "hand2" if any(t.startswith(clickable) for t in tags) else ""
        else:
            want = "fleur"
        if str(canvas.cget("cursor")) != want:
            canvas.configure(cursor=want)

    def _panel_motion(self, event) -> None:
        if self._resize_start is not None:
            self._resize_drag(event)
        elif self._move_start is not None:
            self._move_motion(event)

    def _panel_release(self, event) -> None:
        if self._resize_start is not None:
            self._resize_release(event)
        elif self._move_start is not None:
            self._move_release(event)

    def _resize_press(self, event) -> None:
        self._resize_start = (
            event.x_root, event.y_root, self.detail_scale,
            self.detail.winfo_x(), self.detail.winfo_y(),
        )

    def _resize_drag(self, event) -> None:
        if self._resize_start is None:
            return
        start_x, _start_y, start_scale, _wx, _wy = self._resize_start
        base_width = DETAIL_W * UI_SCALE
        # The corner drag maps directly onto a scale factor; width and height
        # always move together, so the aspect ratio never changes.
        scale = (base_width * start_scale + (event.x_root - start_x)) / base_width
        scale = min(MAX_DETAIL_SCALE, max(MIN_DETAIL_SCALE, scale))
        if abs(scale - 1.0) < 0.04:
            scale = 1.0
        if abs(scale - self.detail_scale) < 0.004:
            return
        self.detail_scale = scale
        if self._resize_job is None:
            self._resize_job = self.root.after(16, self._resize_apply)

    def _resize_apply(self) -> None:
        self._resize_job = None
        self._reflow_detail()

    def _resize_release(self, _event) -> None:
        if self._resize_start is None:
            return
        self._resize_start = None
        if self._resize_job is not None:
            self.root.after_cancel(self._resize_job)
            self._resize_job = None
        self._reflow_detail()
        S["detail_scale"] = round(self.detail_scale, 3)
        self._persist_settings("detail scale")

    def _resize_detail_to_fit(self) -> None:
        """面板内容变了（GPU 区出现/消失）→ 按新高度重画一遍。

        加一个重入闩：`_reflow_detail` 结尾会调 `_refresh_detail(force=True)`，
        而那里又能触发高度比对。闩住之后第二次进来直接返回，重画交给外层
        那一次完成。
        """
        if self._fitting:
            return
        self._fitting = True
        try:
            self._reflow_detail()
        finally:
            self._fitting = False

    def _reflow_detail(self) -> None:
        """Redraw the detail panel at the current detail_scale, in place."""
        if not self.detail or not self.detail.winfo_exists():
            return
        canvas = self.detail_canvas
        width = self._detail_width()
        height = self._detail_height()
        x, y = self.detail.winfo_x(), self.detail.winfo_y()
        x, y = self._clamped_xy(x, y, width, height,
                                anchor=(x + width // 2, y + height // 2))
        self.detail.geometry(f"{width}x{height}+{x}+{y}")
        canvas.config(width=width, height=height)
        canvas.delete("all")
        set_photo_master(canvas)
        self._build_detail()
        self.last_detail_state = None
        self.last_chart_revision = -1
        self._refresh_detail(force=True)

    # -- context menu ------------------------------------------------------

    def _show_process_menu(self, event, index: int) -> None:
        """Actions for one Top 5 row: end the group, or copy its name."""
        if not self.top5_cache or index >= len(self.top5_cache):
            return
        name, _megabytes, count = self.top5_cache[index]
        display = f"{name} ×{count}" if count > 1 else name
        self._close_context_menu()
        colors = palette()
        blocked = _block_reason(name)
        row_h = D(40)
        width = D(230)
        items = []
        if blocked:
            items.append((None, blocked))
        else:
            items.append(("kill", f"结束 {display}"))
        items.append(("copy", "复制进程名"))
        height = D(12) + row_h * len(items)
        x, y = self._clamped_xy(
            event.x_root - width // 2, event.y_root + D(6), width, height,
            anchor=(event.x_root, event.y_root),
        )
        window = self.context_window = self._make_transparent_toplevel()
        window.geometry(f"{width}x{height}+{x}+{y}")
        canvas = tk.Canvas(window, width=width, height=height, bg=TRANSPARENT,
                           highlightthickness=0, bd=0)
        set_photo_master(canvas)
        canvas.pack()
        canvas.create_image(
            0, 0, anchor="nw", tags="menu_bg",
            image=photo_for(("menu", width, height, theme_name()),
                            plate_png(width, height, D(18), colors["card"])),
        )
        top = D(6)
        for tag, label in items:
            ink = colors["text"]
            if tag == "kill":
                ink = colors["red_ink"]
            elif tag is None:
                ink = colors["tertiary"]
            item_tag = tag or f"static_{int(top)}"
            canvas.create_image(
                D(6), top, anchor="nw",
                tags=(item_tag, f"{item_tag}_bg"), state="hidden",
                image=plate_photo("menu-hover", width - 12, 40, 10,
                                  colors["surface_hover"]),
            )
            canvas.create_text(D(24), top + row_h / 2, text=label,
                               fill=ink, font=ui_font(12),
                               anchor="w", tags=item_tag)
            if tag:
                canvas.tag_bind(
                    item_tag, "<Enter>",
                    lambda _e, t=item_tag: canvas.itemconfigure(
                        f"{t}_bg", state="normal"))
                canvas.tag_bind(
                    item_tag, "<Leave>",
                    lambda _e, t=item_tag: canvas.itemconfigure(
                        f"{t}_bg", state="hidden"))
                if tag == "kill":
                    canvas.tag_bind(item_tag, "<Button-1>",
                                    lambda _e, n=name: self._kill_process(n))
                else:
                    canvas.tag_bind(item_tag, "<Button-1>",
                                    lambda _e, n=name: self._copy_process_name(n))
            top += row_h
        # 这两行原本是 `window.bind("<FocusOut>", lambda _e: self._close_context_menu())`
        # —— 无条件、立刻销毁。它和 `_show_context_menu` 末尾那套"宽限 300 ms
        # 再确认真实焦点"是同一个坑，只是这边没修：`overrideredirect` 窗口在
        # 显示瞬间就会被 Windows 收回强制焦点，于是 `<FocusOut>` 立刻到达，
        # 菜单刚 `deiconify()` 就被销毁。实测表现就是"窗口建成 252x144，
        # 但 IsWindowVisible 一直是 False"。
        # 统一走带确认的收起逻辑。
        self._bind_menu_focus_dismiss(window)
        window.deiconify()
        set_window_rounding(window)

    def _kill_process(self, name: str) -> None:
        self._close_context_menu()
        killed, denied, blocked = terminate_process_group(name)
        label = name[:-4] if name.lower().endswith(".exe") else name
        if blocked:
            text, fill = blocked, palette()["tertiary"]
        elif killed and not denied:
            text = f"已结束 {label} ×{killed}"
            fill = palette()["secondary"]
        elif killed:
            text = f"已结束 {label} ×{killed}，{denied} 个拒绝访问"
            fill = palette()["secondary"]
        else:
            text = f"{denied} 个进程拒绝访问，可尝试以管理员身份运行"
            fill = palette()["tertiary"]
        if not blocked:
            # The list is stale the moment a process dies; read it again so
            # the row disappears right away instead of up to 12 s later.
            self.top5_next_due = 0.0
            self._refresh_top5(force=True)
        if self.detail_canvas is not None:
            self.detail_canvas.itemconfigure("action_hint", text=text, fill=fill)

    def _copy_process_name(self, name: str) -> None:
        self._close_context_menu()
        widget = self.detail or self.root
        try:
            widget.clipboard_clear()
            widget.clipboard_append(name)
        except tk.TclError:
            return
        if self.detail_canvas is not None:
            self.detail_canvas.itemconfigure(
                "action_hint", text=f"已复制 {name}",
                fill=palette()["tertiary"])

    def _show_context_menu(self, event) -> None:
        self._close_context_menu()
        colors = palette()
        width, height = D(MENU_W), D(MENU_H)
        row_h = D(40)
        x, y = self._clamped_xy(
            event.x_root - width + D(10), event.y_root + D(4),
            width, height, anchor=(event.x_root, event.y_root),
        )
        window = self.context_window = self._make_transparent_toplevel()
        window.geometry(f"{width}x{height}+{x}+{y}")
        canvas = tk.Canvas(window, width=width, height=height, bg=TRANSPARENT,
                           highlightthickness=0, bd=0)
        self.context_canvas = canvas
        set_photo_master(canvas)
        canvas.pack()
        canvas.create_image(
            0, 0, anchor="nw", tags="menu_bg",
            image=photo_for(("menu", width, height, theme_name()),
                            plate_png(width, height, D(18), colors["card"])),
        )
        for tag, top, label in MENU_ROWS:
            canvas.create_image(
                D(6), D(top), anchor="nw", tags=(tag, f"{tag}_bg"), state="hidden",
                image=plate_photo("menu-hover", MENU_W - 12, 40, 10,
                                  colors["surface_hover"]),
            )
            canvas.create_text(D(24), D(top) + row_h / 2, text=label,
                               fill=colors["text"], font=ui_font(12),
                               anchor="w", tags=(tag, f"{tag}_text"))
            canvas.tag_bind(
                tag, "<Enter>",
                lambda _e, t=tag: canvas.itemconfigure(f"{t}_bg", state="normal"))
            canvas.tag_bind(
                tag, "<Leave>",
                lambda _e, t=tag: canvas.itemconfigure(f"{t}_bg", state="hidden"))
        canvas.tag_bind("settings", "<Button-1>", lambda _e: self.open_settings())
        canvas.tag_bind("quit", "<Button-1>",
                        lambda _e: self.quit(terminate=True))
        # 行点击另绑一层 `<ButtonRelease-1>`：`overrideredirect` 窗口即使
        # `focus_force()` 也不保证后续真实点击按"激活窗口"派发，
        # 而 `<ButtonRelease-1>` 是必然到达的。命中判定见 `_on_menu_click`。
        canvas.bind("<ButtonRelease-1>", self._on_menu_click, add="+")
        window.deiconify()
        window.focus_force()
        set_window_rounding(window)
        # 菜单会自己丢掉刚拿到的焦点 —— 这一条是本项目里最贵的一课。
        #
        # 实测（用 `MEMORY_WIDGET_DEBUG` 打点）：`deiconify()` + `focus_force()`
        # 之后紧跟着就冒出一条 `<FocusOut>`，顺序是 `menu shown` →
        # `close_context_menu`。也就是说，只要把 `<FocusOut>` 直接接上
        # `_close_context_menu`，菜单就是"弹出来立刻没"。
        # 对 `overrideredirect` 窗口，Windows 的焦点语义和普通窗口不同：
        # 系统会立刻收回那次强制焦点，于是这条 `<FocusOut>` 与
        # "用户点了别处"在事件层面根本区分不了。
        #
        # 结论：不能一收到 `<FocusOut>` 就关。宽限 300 ms 再确认一次真实焦点
        # （`_on_menu_focus_out` / `_maybe_close_context_menu`），
        # 只有焦点确实离开本菜单才收起。宽限期内不销毁任何东西，
        # 用户想点的那一行就一定还在。
        self._bind_menu_focus_dismiss(window)

    def _bind_menu_focus_dismiss(self, window) -> None:
        """给菜单类窗口装上"焦点真的走了才收起"的关闭逻辑。

        为什么要绕这一圈：`overrideredirect` 窗口在 `deiconify()` 的瞬间就被
        Windows 收回强制焦点，`<FocusOut>` 立刻到达 —— 直接绑 `_close_context_menu`
        等于"弹出来立刻没"。必须延后 `MENU_FOCUS_GRACE_MS` 再问一次
        `GetForegroundWindow()`，只有前台确实不是本应用才收起。
        """
        self.context_focus_job = None
        window.bind("<FocusOut>", self._on_menu_focus_out)

    def _on_menu_focus_out(self, _event=None) -> None:
        """焦点离开菜单时，延后确认一次再收起。

        为什么不直接关：见 `_bind_menu_focus_dismiss` ——
        `overrideredirect` 窗口在显示瞬间就会丢一次焦点，
        直接关等于菜单永远弹不出来。
        """
        if self.context_focus_job:
            try:
                self.root.after_cancel(self.context_focus_job)
            except tk.TclError:
                pass
        self.context_focus_job = self.root.after(
            MENU_FOCUS_GRACE_MS, self._maybe_close_context_menu)

    def _maybe_close_context_menu(self) -> None:
        self.context_focus_job = None
        window = self.context_window
        if window is None:
            return
        try:
            if not window.winfo_exists():
                self._close_context_menu()
                return
        except tk.TclError:
            self.context_window = None
            self.context_canvas = None
            return
        # 判断"焦点是不是还在本应用"不能用 `focus_displayof()`：本程序的窗口全是
        # `overrideredirect`，Windows 不给它们焦点，`focus_displayof()` 在这里
        # 恒返回 None（实测），拿它当判据会把每次 `<FocusOut>` 都读成"用户走了"。
        # 直接问系统哪个窗口在前才准。HWND is pointer-sized on 64-bit -
        # declared, not defaulted, so the comparison below is not truncated.
        user32 = ctypes.windll.user32
        user32.GetForegroundWindow.argtypes = ()
        user32.GetForegroundWindow.restype = wt.HWND
        foreground = user32.GetForegroundWindow()
        mine = {self.root.winfo_id(), window.winfo_id()}
        if foreground in mine or _is_descendant_of(foreground, mine):
            return
        # 前台不是本应用 = 用户真的去看别的窗口了
        self._close_context_menu()

    def _on_menu_click(self, event) -> None:
        """真实点击落在菜单上：分派到具体那一行，或者收起菜单。

        为什么不能只靠 `canvas.tag_bind(tag, "<Button-1>")`：菜单是
        `overrideredirect` 窗口，`focus_force()` 逼一次焦点并不保证后续真实
        点击被当成"激活窗口的点击"派发。而 `<ButtonRelease-1>` 是必然到达的，
        于是自己算坐标落在哪一行 —— 与 `_probe_press_rows` 共用
        `_menu_action_for`，两条路径不会各说各话。
        """
        canvas = getattr(self, "context_canvas", None)
        if canvas is None or event.widget is not canvas:
            return
        self._menu_action_for(canvas, event.x, event.y)


    def _close_context_menu(self) -> None:
        if self.context_focus_job:
            try:
                self.root.after_cancel(self.context_focus_job)
            except tk.TclError:
                pass
            self.context_focus_job = None
        if self.context_window:
            try:
                self._unhook_window(self.context_window)
                self.context_window.destroy()
            except tk.TclError:
                pass
        self.context_window = None
        self.context_canvas = None

    # -- settings ----------------------------------------------------------

    def _persist_settings(self, context: str) -> None:
        """`save_settings()` with the failure made visible.

        Every failure goes to the error log. The in-UI hint is one-shot per
        session on purpose: dragging fires a save per release, and a
        permanent failure (read-only folder) must not nag on every gesture -
        the footer line of the settings pane is enough, set once.
        """
        if save_settings():
            return
        log_error("save_settings failed (%s)" % context)
        if self._save_failure_notified:
            return
        self._save_failure_notified = True
        self.settings_error = "设置无法保存，请检查文件夹权限"
        if (self.settings_window is not None
                and self.settings_window.winfo_exists()):
            self._draw_settings()

    def open_settings(self) -> None:
        self._close_context_menu()
        if self.settings_window and self.settings_window.winfo_exists():
            self.settings_window.lift()
            self.settings_window.focus_force()
            return
        width, height = self._settings_size()
        window = self.settings_window = self._make_transparent_toplevel()
        x, y = self._clamped_xy(
            self.root.winfo_x() - width + self._ring_size(),
            self.root.winfo_y() - D(150), width, height,
            anchor=(self.root.winfo_x() + self._ring_size() // 2,
                    self.root.winfo_y() + self._ring_size() // 2),
        )
        window.geometry(f"{width}x{height}+{x}+{y}")
        canvas = self.settings_canvas = tk.Canvas(
            window, width=width, height=height, bg=TRANSPARENT,
            highlightthickness=0, bd=0,
        )
        set_photo_master(canvas)
        canvas.pack()
        self.settings_error = ""
        self._draw_settings()
        canvas.bind("<Button-1>", self._settings_press, add="+")
        canvas.bind("<B1-Motion>", self._settings_drag, add="+")
        window.deiconify()
        window.focus_force()
        set_window_rounding(window)
        self._apply_topmost()

    def _draw_settings(self) -> None:
        canvas = self.settings_canvas
        if not canvas:
            return
        canvas.delete("all")
        # The previous draw's images are gone from the canvas now, so this
        # rebuild can drop them and start a fresh keep-alive list.
        set_photo_master(canvas)
        colors = palette()
        width, height = self._settings_size()
        box_left, box_right = D(16), D(380)
        row_left, row_right = D(32), D(364)
        canvas.create_image(
            0, 0, anchor="nw", tags="settings_bg",
            image=photo_for(("cardplate", width, height, theme_name()),
                            plate_png(width, height, D(26), colors["card"])),
        )

        canvas.create_text(D(28), D(36), text="内存 · 设置", fill=colors["text"],
                           font=display_font(16, "bold"), anchor="w",
                           tags="settings_title")
        canvas.create_text(D(28), D(60), text="轻巧、清晰，随时掌握内存状态",
                           fill=colors["tertiary"], font=ui_font(11),
                           anchor="w", tags="settings_subtitle")
        done_w, done_h = D(72), D(32)
        done_x, done_y = box_right - done_w, D(26)
        canvas.create_image(
            done_x, done_y, anchor="nw", tags="settings_close",
            image=plate_photo("done", 72, 32, 16, colors["orange"]),
        )
        canvas.create_text(done_x + done_w / 2, done_y + done_h / 2, text="完成",
                           fill="#FFFFFF", font=ui_font(11, "bold"),
                           tags="settings_close")
        canvas.tag_bind("settings_close", "<Button-1>",
                        lambda _e: self._close_settings())

        canvas.create_text(row_left, D(94), text="显示与信息",
                           fill=colors["secondary"], font=ui_font(11, "bold"),
                           anchor="w", tags="group_label_0")
        toggles = (
            ("pin_top", "窗口置顶", "始终浮在其它窗口之上"),
            ("hover_expand", "悬停展开", "停留时显示完整详情"),
            ("history_enabled", "内存趋势", "保留最近两小时的走势"),
            ("top5_enabled", "占用最多", "低频读取进程用量"),
            ("autostart", "开机启动", "登录 Windows 后自动运行"),
        )
        group_top, group_bottom = D(110), D(110 + 5 * 54 + 10)
        canvas.create_image(
            box_left, group_top, anchor="nw", tags="group_0",
            image=photo_for(
                ("group", box_right - box_left, group_bottom - group_top,
                 theme_name()),
                plate_png(box_right - box_left, group_bottom - group_top,
                          D(18), colors["card_soft"])),
        )
        for index, (key, title, subtitle) in enumerate(toggles):
            top = group_top + D(index * 54)
            tag = f"toggle_{key}"
            if index:
                self._hairline(canvas, row_left - D(10), top,
                               row_right - row_left + D(20), colors["hairline"])
            canvas.create_text(row_left, top + D(19), text=title,
                               fill=colors["text"], font=ui_font(12, "bold"),
                               anchor="w", tags=(tag, f"toggle_title_{key}"))
            canvas.create_text(row_left, top + D(39), text=subtitle,
                               fill=colors["tertiary"], font=ui_font(9),
                               anchor="w", tags=(tag, f"toggle_subtitle_{key}"))
            self._draw_switch(canvas, row_right - D(44), top + D(14),
                              bool(S[key]), tag, colors)
            canvas.tag_bind(tag, "<Button-1>",
                            lambda _e, option=key: self._toggle_setting(option))

        second_y = group_bottom + D(30)
        canvas.create_text(row_left, second_y, text="外观与节奏",
                           fill=colors["secondary"], font=ui_font(11, "bold"),
                           anchor="w", tags="group_label_1")
        options = (
            ("theme", "主题", "自动或固定外观",
             (("system", "自动"), ("light", "浅色"), ("dark", "深色"))),
            ("ring_size", "尺寸", "桌面圆环的大小",
             ((64, "小"), (72, "标准"), (96, "大"))),
            ("poll_interval_ms", "刷新", "采样速度与能耗",
             ((4000, "省电"), (2000, "平衡"), (1000, "实时"))),
        )
        block_top = second_y + D(16)
        block_bottom = block_top + D(3 * 54 + 10)
        canvas.create_image(
            box_left, block_top, anchor="nw", tags="group_1",
            image=photo_for(
                ("group", box_right - box_left, block_bottom - block_top,
                 theme_name()),
                plate_png(box_right - box_left, block_bottom - block_top,
                          D(18), colors["card_soft"])),
        )
        for index, (key, title, subtitle, items) in enumerate(options):
            top = block_top + D(2 + index * 54)
            if index:
                self._hairline(canvas, row_left - D(10), top,
                               row_right - row_left + D(20), colors["hairline"])
            canvas.create_text(row_left, top + D(19), text=title,
                               fill=colors["text"], font=ui_font(12, "bold"),
                               anchor="w", tags=f"option_title_{key}")
            canvas.create_text(row_left, top + D(39), text=subtitle,
                               fill=colors["tertiary"], font=ui_font(9),
                               anchor="w", tags=f"option_subtitle_{key}")
            self._draw_segmented(canvas, row_right - D(192), top + D(13),
                                 D(192), D(28), items, S[key], key, colors)

        footer = self.settings_error or "趋势与进程读取默认关闭 · 修改后即时生效"
        canvas.create_text(width / 2, block_bottom + D(30), text=footer,
                           fill=colors["red_ink"] if self.settings_error
                           else colors["tertiary"], font=ui_font(9))
        canvas.create_text(width / 2, block_bottom + D(52),
                           text="Memory Widget · 轻量桌面工具",
                           fill=colors["tertiary"], font=ui_font(9),
                           tags="settings_footer")

    def _draw_switch(self, canvas, x, y, enabled: bool, tag: str, colors) -> None:
        track = colors["orange"] if enabled else colors["switch_off"]
        canvas.create_image(
            x, y, anchor="nw", tags=(tag, f"switch_{tag}"),
            image=plate_photo("switch", 44, 26, 13, track))
        canvas.create_image(
            x + D(22 if enabled else 2), y + D(2), anchor="nw",
            tags=(tag, f"switch_{tag}"),
            image=plate_photo("knob", 22, 22, 11, colors["on_knob"]),
        )

    def _draw_segmented(self, canvas, x, y, width, height, items, selected,
                        key, colors) -> None:
        canvas.create_image(
            x, y, anchor="nw", tags=f"segment_track_{key}",
            image=photo_for(("track", width, theme_name()),
                            plate_png(width, height, height / 2,
                                      colors["surface"])),
        )
        segment = width / len(items)
        for index, (value, label) in enumerate(items):
            tag = f"segment_{key}_{index}"
            left = x + index * segment + D(2)
            active = value == selected
            canvas.create_image(
                left, y + D(2), anchor="nw", tags=tag,
                image=plate_photo("seg" + str(active), 192 / 3 - 4, 24, 12,
                                  colors["orange"] if active
                                  else colors["surface"]),
            )
            canvas.create_text(
                left + (segment - D(4)) / 2, y + height / 2, text=label,
                fill="#FFFFFF" if active else colors["secondary"],
                font=ui_font(9, "bold" if active else "normal"), tags=tag,
            )
            canvas.tag_bind(
                tag, "<Button-1>",
                lambda _e, option=key, new_value=value:
                self._set_option(option, new_value),
            )

    def _settings_press(self, event) -> None:
        if event.y <= D(76) and self.settings_window:
            self.settings_press_offset = (event.x_root, event.y_root)
            self.settings_window_start = (
                self.settings_window.winfo_x(), self.settings_window.winfo_y()
            )
        else:
            self.settings_press_offset = None

    def _settings_drag(self, event) -> None:
        if not getattr(self, "settings_press_offset", None):
            return
        dx = event.x_root - self.settings_press_offset[0]
        dy = event.y_root - self.settings_press_offset[1]
        width, height = self._settings_size()
        x, y = self._clamped_xy(
            self.settings_window_start[0] + dx,
            self.settings_window_start[1] + dy, width, height,
        )
        self.settings_window.geometry(f"+{x}+{y}")

    def _toggle_setting(self, key: str) -> None:
        self._set_option(key, not bool(S[key]))

    def _set_option(self, key: str, value) -> None:
        global _AUTO_THEME_CACHE
        old_value = S.get(key)
        if old_value == value:
            if key == "theme" and value == "system":
                _AUTO_THEME_CACHE = _system_theme()
                self._build_ring()
                self._draw_ring(force=True)
                if self.detail:
                    self._show_detail()
                if self.settings_canvas:
                    self._draw_settings()
            return
        self.settings_error = ""
        if key == "autostart":
            try:
                set_autostart(bool(value))
            except OSError:
                self.settings_error = "无法更改开机启动，请检查系统权限"
                value = old_value
        S[key] = value
        if not save_settings():
            log_error("save_settings failed (option %s)" % key)
            self.settings_error = "设置无法保存，请检查文件夹权限"

        if key == "history_enabled":
            self.history = History() if value else None
        elif key == "top5_enabled":
            self.top5_cache = []
            self.top5_next_due = 0.0
        elif key == "pin_top":
            # 置顶是窗口属性，改完立刻生效，界面不用重画；
            # 详情面板上那个小按钮由下面那轮重绘同步过去。
            self._apply_topmost()
        elif key == "ring_size":
            old_x, old_y = self.root.winfo_x(), self.root.winfo_y()
            self._build_ring()
            size = self._ring_size()
            x, y = self._clamped_xy(old_x, old_y, size, size)
            self.root.geometry(f"{size}x{size}+{x}+{y}")
            S["pos"] = [x, y]
            self._persist_settings("ring size change")
        elif key == "poll_interval_ms":
            if self.loop_job:
                self.root.after_cancel(self.loop_job)
            self._schedule_next()
        elif key == "theme":
            if value == "system":
                _AUTO_THEME_CACHE = _system_theme()
            self._build_ring()
            self._draw_ring(force=True)

        if self.detail:
            self._show_detail()
        if self.settings_canvas:
            self._draw_settings()

    def _close_settings(self) -> None:
        if self.settings_window:
            try:
                self._unhook_window(self.settings_window)
                self.settings_window.destroy()
            except tk.TclError:
                pass
        self.settings_window = None
        self.settings_canvas = None

    # -- tray icon ---------------------------------------------------------

    def _install_wndproc(self, window=None) -> None:
        """Hook a window's window procedure so tray messages and `WM_CLOSE` reach us.

        Why this is needed at all: Tk dispatches the messages it knows about and
        silently drops the rest, and `Shell_NotifyIcon` delivers clicks as a
        private `WM_USER` message addressed to the window we registered. There
        is no Tk-level hook for that, so the window procedure is replaced and
        anything we do not recognise is forwarded to Tk's original one.

        ★ **每一个窗口都要单独挂钩，不能只挂圆环那一个。** 详情面板、设置面板
        都是 `tk.Toplevel`，各自有**独立**的窗口过程；只挂 root 的话，
        发给面板的 `WM_CLOSE` 会被 Tk 丢掉 —— 表现为"面板收不到关闭请求、
        进程一直不退"。实测（2026-09-18）：

            给圆环发 WM_CLOSE -> 1.38 s 干净退出，内部日志走到 `quit()` ✓
            给详情面板发      -> 6 s 无反应，内部日志只有 `进入 mainloop` ✗

        这条曾经被误判成"外部 WM_CLOSE 要等 12~20 秒"，见 `BUILD_INFO.md`。
        调用点：`__init__` 挂 root，`_make_transparent_toplevel` 挂每个新面板。

        The callback object is kept alive on `self` on purpose: ctypes wrappers
        are not owned by Windows, and letting the reference drop would leave the
        window pointing at freed memory the first time the tray is clicked.
        每个窗口的旧过程与回调对象都按 hwnd 存下来 —— 面板会被反复重建，
        必须一一对应，否则还原时会串。
        """
        target = window if window is not None else self.root
        hwnd = tray_icon.root_hwnd(target)
        if not hwnd:
            return
        if hwnd in self._wndprocs:
            return                                      # 已经挂过了
        # Ownership check: an hwnd can be recycled by the system for a
        # completely different process's window between the moment Tk created
        # the widget and this call (or a stale hwnd can survive in a reused
        # slot). Subclassing a foreign window with SetWindowLongPtrW succeeds
        # silently - and then writes our trampoline into someone else's
        # window. Verify the hwnd still belongs to this process before
        # touching it.
        get_tid = tray_icon.user32.GetWindowThreadProcessId
        get_tid.argtypes = (wt.HWND, ctypes.POINTER(wt.DWORD))
        get_tid.restype = wt.DWORD
        # GetCurrentProcessId lives in kernel32, not user32.
        kernel32.GetCurrentProcessId.argtypes = ()
        kernel32.GetCurrentProcessId.restype = wt.DWORD
        owner = wt.DWORD(0)
        get_tid(ctypes.c_void_p(int(hwnd)), ctypes.byref(owner))
        if int(owner.value) != int(kernel32.GetCurrentProcessId()):
            return
        is_root = target is self.root
        if is_root:
            self._tray_hwnd = hwnd
        # 面板被销毁时同步清掉映射项：不然 hwnd 会被系统回收重用，
        # 下一次挂钩判断"已经挂过了"会误判，退出还原时也会写错窗口。
        # `<Destroy>` 对后代也触发，所以只对窗口自身生效的那次处理。
        def _drop_mapping(event, _hwnd=int(hwnd)):
            if event.widget is target:
                self._restore_one_wndproc(_hwnd)
        target.bind("<Destroy>", _drop_mapping, add="+")

        def handler(hwnd_, msg, wparam, lparam):
            if is_root and msg == tray_icon.WM_USER + 21:
                self._tray_events.append((int(wparam), int(lparam)))
                return 0
            if msg == WM_CLOSE:
                # No Tk calls here, including root.after or logging through
                # UI callbacks. Calling Tk while ctypes is forwarding a native
                # message overwrites _tkinter's saved Tcl thread state and can
                # abort with PyEval_RestoreThread(NULL). Flags and queued data
                # are consumed by watch_close_request in a normal Tk timer.
                self._wm_quit_flag[0] = 1
                return 0
            if msg == WM_QUERYENDSESSION:
                # Defer tray teardown and session exit to the safe Tk timer.
                self._wm_endsession_flag[0] = 1
                return 1                             # TRUE: 同意结束会话
            if msg == WM_ENDSESSION:
                # wParam 为 TRUE 时会话真的在结束。同样只写标志（值为 2），
                # 轮询侧会摘掉托盘并执行退出；这里必须尽快返回，
                # 系统只给几秒钟。
                if wparam:
                    self._wm_endsession_flag[0] = 2
                return 0
            if msg == WM_DPICHANGED:
                # wParam 的低/高 16 位分别是横/纵 DPI；此布局统一使用横向 DPI。
                # 只记录低位，由 `_loop` 轮询侧重算 UI_SCALE、清位图缓存、
                # 重设窗口尺寸并重绘 —— 见 `_apply_dpi_change`。
                self._wm_dpi_flag[0] = int(wparam) & 0xFFFF
                return 1                             # TRUE: 我们自己缩放
            return _CallWindowProcW(previous, hwnd_, msg, wparam, lparam)

        proc = _WNDPROC(handler)
        # `previous` 必须是**本窗口**原来的过程，而且要在换掉之前就取到。
        # 放在闭包里而不是退出时查字典：字典在 `_restore_wndproc` 里会被清空，
        # 而窗口过程随时可能还被调用一次；闭包里的值跟着回调对象一起活，
        # 生命周期天然对齐。
        previous = _SetWindowLongPtrW(
            hwnd, GWL_WNDPROC, ctypes.cast(proc, ctypes.c_void_p).value)
        # 回调对象必须一直活着（见 docstring），旧过程要能按 hwnd 找回来。
        self._wndprocs[hwnd] = (proc, previous)
        if is_root:
            self._wndproc_ref = proc
            # 圆环的原过程单独留一份。`_wndprocs` 才是权威（退出时按它逐个还原），
            # 这个属性只是给外部检查/调试看的（`tools/check_tray_icon.py` 断言
            # "WndProc hook installed" 读的就是它）。挂到面板上的那几次不覆盖它，
            # 否则最后挂钩的那个面板会顶掉 root 的记录。
            self._old_wndproc = previous

    def _install_tray(self) -> None:
        """Put the ring in the notification area.

        The orb is a `overrideredirect` window, so it never gets a taskbar
        button - drag it behind something and there is no on-screen evidence the
        program is running. This is that evidence, and it doubles as the way
        back to a window that has been dragged off-screen.
        """
        if "--no-tray" in sys.argv:
            return
        tray = self.tray = tray_icon.TrayIcon(
            self, ico_path=ICON_PATH, tip=self._tray_tip(),
            menu_builder=self._tray_menu)
        try:
            # New interface: the scheduler (root.after) moves tray promotion
            # retries out of the install call so install() never sleeps on the
            # Tk thread; scheduler=None means a single attempt, no sleep.
            installed = tray.install(scheduler=self.root.after)
        except TypeError:
            # Older tray_icon without the scheduler parameter.
            installed = tray.install()
        if not installed:
            self._dbg("托盘图标注册失败（不影响悬浮球）")
            self.tray = None
            return
        # Crash-residue net: every normal exit path calls tray.remove(), but
        # a hard crash (or os._exit from an unexpected path) skips them all
        # and leaves a dead icon in the notification area until hover. atexit
        # runs on every interpreter exit that still gets to run Python code,
        # including unhandled exceptions - remove() is idempotent, so double
        # removal via quit() is harmless.
        atexit.register(self._atexit_tray_remove)
        self._dbg("托盘图标已就位 hwnd=%s" % tray.hwnd)

    def _atexit_tray_remove(self) -> None:
        """atexit hook: last-chance tray cleanup. Never raises."""
        try:
            if self.tray is not None:
                self.tray.remove()
        except Exception:                                # noqa: BLE001
            pass

    def _tray_tip(self) -> str:
        """Tooltip text; shows the live reading when one is available."""
        if self.last_memory:
            percent, used, total, _free = self.last_memory
            return f"内存悬浮窗 · {percent}%  {used:.1f}/{total:.1f} GB"
        return "内存悬浮窗"

    def _tray_menu(self):
        """Right-click menu, kept in step with the settings pane.

        `checks` mirrors `S` so the two surfaces cannot disagree; there is one
        source of truth and both read it.
        """
        return [
            {"id": 1, "label": "显示 / 隐藏悬浮球",
             "action": lambda: self.tray_action("toggle")},
            {"id": 2, "label": "内存详情…",
             "action": lambda: self.tray_action("detail")},
            {"id": 3, "label": "设置…",
             "action": lambda: self.tray_action("settings")},
            {"separator": True},
            {"id": 4, "label": "窗口置顶", "checked": bool(S["pin_top"]),
             "action": lambda: self.tray_action("pin")},
            {"id": 5, "label": "悬停展开", "checked": bool(S["hover_expand"]),
             "action": lambda: self.tray_action("hover")},
            {"separator": True},
            {"id": 9, "label": "退出",
             "action": lambda: self.tray_action("quit")},
        ]

    def tray_action(self, name: str) -> None:
        """Every tray interaction funnels through here.

        Single entry point on purpose: the same action is reachable from the
        tray menu, a double-click and the settings pane, and routing them all
        through one method means there is exactly one place where "what does
        this action do" is decided.
        """
        self._dbg("托盘点到 %s" % name)
        if name == "quit":
            self.quit(terminate=True)
        elif name == "toggle":
            self.toggle_visible()
        elif name == "detail":
            self.show_orb()
            if self.detail:
                self._hide_detail()
            else:
                self._show_detail()
        elif name == "settings":
            self.show_orb()
            self.open_settings()
        elif name == "pin":
            # `_set_pin` 自己会把两个面板重画一遍（见它的实现），
            # 这里不要再补一次 `_draw_settings`。
            self._set_pin(not bool(S["pin_top"]))
        elif name == "hover":
            self._set_option("hover_expand", not bool(S["hover_expand"]))

    def toggle_visible(self) -> None:
        """Show or hide the orb, without disturbing the detail/settings panes."""
        if self.hidden:
            self.show_orb()
        else:
            self.hide_orb()

    def hide_orb(self) -> None:
        """Hide just the orb. The tray icon stays, which is the whole point."""
        if self.hidden:
            return
        self._close_context_menu()
        self._hide_detail()
        self._close_settings()
        self.root.withdraw()
        self.hidden = True

    def show_orb(self) -> None:
        if not self.hidden:
            return
        self.root.deiconify()
        self._apply_topmost()
        self.hidden = False

    def refresh_tray_tip(self) -> None:
        """Push the current reading into the tooltip. Called from the poll loop."""
        if self.tray is not None and self.tray.installed:
            self.tray.set_tip(self._tray_tip())

    # -- lifecycle ---------------------------------------------------------

    def _on_root_destroy(self, event) -> None:
        """Last-chance teardown when the root window goes away.

        Bound to `<Destroy>` on the root. `<Destroy>` fires for **every**
        descendant destroyed too, and `event.widget` is whichever one that is,
        so the identity check is what makes this fire once, for the root only.

        Doing it here rather than only in `quit()` is what makes the GPU
        sampler leak-proof: `quit()` covers the real app, this covers every
        other caller that just calls `root.destroy()`. See `quit()` for what
        an orphaned `gpu-temp` thread costs.
        """
        if event.widget is not self.root:
            return
        gpu_info.stop()
        self._dbg("root <Destroy> 已停取数线程")

    def quit(self, terminate: bool = False) -> None:
        """Tear the widget down; optionally end the process as well.

        `terminate` defaults to **False** on purpose. `quit()` means "close this
        widget", and callers that only want a window closed must not have their
        process killed underneath them — the UI regression check builds and
        closes widgets repeatedly in one interpreter, and an unconditional kill
        there would take the whole test run down with it (it did, until this
        parameter existed).

        The three places that really mean "the user is quitting the app" —
        the context menu, the `WM_CLOSE` flag in `_loop`, and `--selftest` —
        pass `terminate=True` explicitly. They are also the paths where
        `os._exit`-style exits used to hang; see `_exit_now`.
        """
        self._dbg("quit() 进入 terminate=%s" % terminate)
        # Re-entrancy guard: quit() can be reached from several callbacks in
        # the same tick (menu click + WM_CLOSE flag + watchdog). The second
        # entry would run the teardown twice against half-destroyed windows.
        if self._closing:
            return
        self._closing = True
        if self.root.winfo_exists():
            S["pos"] = [self.root.winfo_x(), self.root.winfo_y()]
            save_settings()
        # 先撤掉轮询任务再拆窗口。`mainloop()` 退出后还会处理一次到期回调，
        # 那时 `after` 注册的脚本已经被销毁，Tk 会往 stderr 打
        # `invalid command name "…_loop"`。功能上无害，但这是个真实的应用
        # （`--selftest` 里就是 quit() 收尾），留着脏输出会掩盖真正的报错。
        if self.loop_job:
            try:
                self.root.after_cancel(self.loop_job)
            except tk.TclError:
                pass
            self.loop_job = None
        # 关闭请求监听器同理。它是 120 ms 一次的高频定时器，必须在拆窗口前撤掉，
        # 否则 destroy 之后它还会被调度，在已销毁的解释器上注册 `after`。
        if self.watch_job:
            try:
                self.root.after_cancel(self.watch_job)
            except tk.TclError:
                pass
            self.watch_job = None
        # 探针轮询同理：不撤掉的话，destroy 之后还会被调度一次，
        # 在已销毁的解释器上读文件、碰控件。
        if self.probe_poll_job:
            try:
                self.root.after_cancel(self.probe_poll_job)
            except tk.TclError:
                pass
            self.probe_poll_job = None
        # Order matters here: window procedures are restored **before** any
        # window is destroyed. The old order (destroy panels, then restore)
        # raced hwnd recycling - a destroyed panel's hwnd can be handed to an
        # unrelated window by the time SetWindowLongPtrW runs; the call
        # succeeds but writes our old wndproc into a window we do not own.
        # Restoring while every hooked window is still alive removes the race.
        #
        # 托盘图标同样要在窗口销毁**之前**摘掉：`NIM_DELETE` 要拿 hwnd 去定位
        # 那条通知，窗口没了之后 shell 就只能靠超时回收，右下角会留下一个
        # 点不动的幽灵图标。还原窗口过程也避免 Tk 在销毁自己的窗口时还经过
        # 我们的回调。
        self._teardown_tray()
        self._dbg("quit() 托盘已摘")
        self._restore_wndproc()
        self._dbg("quit() 窗口过程已还原")
        self._close_context_menu()
        self._hide_detail()
        self._close_settings()
        # `_hide_detail()` 上面已经停过取数线程，但那一路只在"面板还开着"时生效。
        # 这里再无条件停一次，兜住两种漏掉的情况：
        #
        #   1. `_show_detail()` 里先 `gpu_info.start()` 再建面板，而窗口可能
        #      在取数线程起来之后、面板登记之前就被拆掉；
        #   2. 调用方直接 `root.destroy()`，根本没走 `quit()`。
        #
        # 漏掉的代价很大：`gpu_info._worker` 是 daemon 线程，每漏一个就留下
        # 一个 `gpu-temp` 加两个 `nvidia-smi` 的 `_readerthread`。UI 回归检查
        # 一轮建销 50 个实例，于是攒下近百个游离线程，最后随便哪个在解释器
        # GC 时被拆掉，就打印
        #
        #     Tcl_AsyncDelete: async handler deleted by the wrong thread
        #
        # 并把进程**在打印结论之前**中止（退出码 0x80000003）。现象是
        # "50 项组合全过、没有一条 FAIL、结论行整行不见"，`run_gates.py`
        # 只看退出码于是报 GATES_FAILED，实测 10 次里错 6 次、完全随机。
        gpu_info.stop()
        self._dbg("quit() 取数线程已停")
        self._dbg("quit() 准备 destroy root")
        self.root.destroy()
        self._dbg("quit() root 已 destroy")
        # **在这里就结束进程，不回去等 `mainloop()` 返回。**
        #
        # 曾经的写法是让 `run()` 在 `mainloop()` 返回之后调 `os._exit(0)`，
        # 那条路在有些调用栈下走不通：外部 `WM_CLOSE` 触发退出时，日志能一路
        # 打到 "mainloop 已返回，强制结束进程"，进程却再也不动 ——
        # `_exit` 要拿 CRT/堆的进程级锁，而此刻线程可能还压在 Windows 消息
        # 分发的路径上（外部 WM_CLOSE 下稳定复现）。
        #
        # 把收尾放在 `quit()` 里、且是在 `destroy()` **之后**，就不再有这个问题：
        # 窗口已经拆干净、托盘和窗口过程都已还原，剩下的只是结束进程。
        # 位置先后很重要 —— 这三件事都依赖窗口还活着，不能挪到 `_exit` 之后。
        self._dbg("quit() 收尾完成")
        if terminate:
            self._dbg("quit() 结束进程")
            _exit_now(self.exit_announce)

    def _teardown_tray(self) -> None:
        if self.tray is not None:
            try:
                self.tray.remove()
            except Exception:                            # noqa: BLE001
                pass
            self.tray = None

    def _restore_wndproc(self) -> None:
        """Put Tk's own window procedure back on every hooked window.

        Skipping this leaks the ctypes trampoline: Windows would keep calling
        into it after the Python objects behind it are gone, which turns an
        ordinary exit into an access violation.

        逐 hwnd 还原。退出时只有圆环还活着（面板早已 destroy），但残留的映射
        项也要照删 —— 那些 hwnd 已经失效，`SetWindowLongPtrW` 会失败并被吞掉，
        而把回调对象一起释放才是重点。
        """
        if not self._wndprocs:
            return
        for hwnd in list(self._wndprocs):
            self._restore_one_wndproc(hwnd)
        self._old_wndproc = None
        self._wndproc_ref = None

    def _unhook_window(self, window) -> None:
        """Restore a panel before destroy starts dispatching native messages."""
        self._restore_one_wndproc(tray_icon.root_hwnd(window))

    def _restore_one_wndproc(self, hwnd) -> None:
        entry = self._wndprocs.get(hwnd)
        if entry is None:
            return
        proc, old = entry
        try:
            if tray_icon.user32.IsWindow(wt.HWND(hwnd)):
                # On failure keep ownership: releasing a still-installed
                # trampoline would turn a recoverable error into a crash.
                if not _SetWindowLongPtrW(hwnd, GWL_WNDPROC, old):
                    return
        except Exception:
            _log_exc("restore window procedure", *sys.exc_info())
            return
        self._retired_wndprocs.append(proc)
        self._wndprocs.pop(hwnd, None)

    def run(self, announce: str = "") -> None:
        """Enter the Tk event loop, then end the process for real.

        `announce` is printed (and flushed) on the way out, just before the
        forced exit. It exists for `--selftest`: the forced exit never returns,
        so a caller cannot print a verdict after this method, and printing one
        *before* it would not prove the event loop ever ran.

        注意 `announce` 挂在实例上而不是在这里打印：正常退出是 `quit()` 自己
        结束进程的，根本不会回到这个方法。谁负责结束进程，就得由谁负责把这句话
        说出来（见 `quit()`）。
        """
        self.exit_announce = announce
        self._dbg("进入 mainloop")
        self.root.mainloop()
        # 走到这里只说明 Tk 事件循环结束了（root 被 destroy）。进程能不能真的
        # 结束是另一回事 —— 实测在 `mainloop()` 返回之后进程会挂住，
        # 而解释器收尾时会调 `Py_FinalizeEx()`，那里会 join 掉所有
        # 非 daemon 线程。Tcl 自己的 notifier 线程就属于这一类。
        # 这是个常驻小工具，"点了退出还赖在任务管理器里"是不可接受的，
        # 所以这里不再指望解释器优雅收尾，直接结束进程。
        self._dbg("mainloop 已返回（未走 quit()），兜底结束进程")
        _exit_now(self.exit_announce)


def _exit_now(announce: str = "") -> None:
    """End the process immediately, without returning.

    `announce` is printed and flushed right before the kill, for `--selftest`.
    It has to happen here rather than in the caller: this is the only place that
    runs on every exit path, and the kill discards all buffers, so a verdict
    printed any earlier can still be lost.

    Two separate problems had to be solved here, both measured (2026-09-18 晚,
    external `WM_CLOSE`, reproducible every time):

    1. **`os._exit` hangs.** It is `ExitProcess`: every other thread is
       suspended and its DLL detach routines run. Tk keeps a notifier thread
       alive, and when the quit originates from a window message the process
       ends up waiting on a lock that thread holds — the last log line gets
       written and then nothing, forever. `TerminateProcess` skips all of that;
       the kernel tears the process down and no user-mode code runs after.

    2. **`TerminateProcess` must be called from a thread that is not inside
       Tk.** Called directly from the Tk callback it hangs too (it also takes
       the target process's lock, and the Tcl notifier thread is parked on it).
       Calling it from a fresh, short-lived thread works every time — that
       thread carries none of Tk's state into the call.

    Flushing first is mandatory: nothing runs after the kill, so anything still
    in a stream buffer is lost. That is how the `--selftest` verdict was dropped
    once (see `run`).
    """
    try:
        if announce:
            print(announce)
        sys.stdout.flush()
        sys.stderr.flush()
    except Exception:                                    # noqa: BLE001
        pass

    def _terminate():
        kernel32.TerminateProcess(kernel32.GetCurrentProcess(), 0)
        # 走到这里说明内核级终止没有生效。留一条痕迹，
        # 别让"点了退出没反应"变成无解的悬案。
        _diag_exit("_exit_now: TerminateProcess 返回了（进程没被结束）")

    thread = threading.Thread(target=_terminate, name="exit", daemon=True)
    thread.start()
    # 主线程在这里退场，把收尾完全交给上面那个线程。
    # 用 join 而不是直接往下走：调用方（`quit()`）的语义是"不会返回"，
    # 而它一旦返回，Tk 那边还有一堆在途回调会继续跑，可能又碰上死锁。
    #
    # The join must be bounded, though: if TerminateProcess itself fails or
    # the kernel call never returns (measured on one path: the announce line
    # printed and then nothing, forever), an unbounded join lets `quit()`
    # return into a UI that has already been destroyed - and then Tk callbacks
    # run against dead widgets. Five seconds is far beyond any observed
    # successful kill; falling through to os._exit(1) guarantees this
    # function never returns either way.
    thread.join(timeout=5.0)
    if thread.is_alive():
        _diag_exit("_exit_now: terminate thread hung; falling back to "
                   "os._exit(1)")
        os._exit(1)


kernel32 = ctypes.windll.kernel32
# `GetCurrentProcess` 返回的是伪句柄 `(HANDLE)-1`。**必须声明类型**，
# 否则 ctypes 按 `c_int` 传参会把它截成 32 位，`TerminateProcess` 直接失败并
# 返回 FALSE，而 ctypes 默认又不为此抛异常 —— 表现就是"调用了，什么也没发生"。
kernel32.GetCurrentProcess.restype = ctypes.c_void_p
kernel32.TerminateProcess.argtypes = (ctypes.c_void_p, ctypes.c_uint)
kernel32.TerminateProcess.restype = ctypes.c_int


def _diag_exit(text):
    """退出路径的旁路打点。直接写文件，绕过任何缓冲与日志开关。

    `os._exit` / `TerminateProcess` 之前的最后一步如果出事，正常的日志代码
    也可能一起没了 —— 这里不依赖 `self`、不依赖环境变量、出错就静默。
    """
    path = os.environ.get("MEMORY_WIDGET_EXIT_TRACE", "")
    if not path:
        return
    try:
        with open(path, "a", encoding="utf-8") as fh:
            fh.write("%s\n" % (text,))
    except Exception:                                    # noqa: BLE001
        pass


def set_window_rounding(window) -> None:
    """Use Windows 11's native corner preference where available."""
    try:
        window.update_idletasks()
        preference = ctypes.c_int(2)
        ctypes.windll.dwmapi.DwmSetWindowAttribute(
            window.winfo_id(), 33, ctypes.byref(preference), ctypes.sizeof(preference)
        )
    except (AttributeError, OSError, tk.TclError):
        pass


def _utf8_console() -> None:
    """Make the --selftest log readable when the console code page is legacy.

    A Windows console defaults to a local code page, which turns the '×' in the
    process list into mojibake. This only affects the diagnostic log; the
    widget's own text is drawn on a Tk canvas and never touches the console.
    """
    for stream in (sys.stdout, sys.stderr):
        try:
            stream.reconfigure(encoding="utf-8", errors="replace")
        except (AttributeError, ValueError, OSError):
            pass


def selftest() -> int:
    _utf8_console()
    reading = read_memory()
    assert reading is not None, "GlobalMemoryStatusEx failed"
    assert 0 <= reading[0] <= 100
    started = time.perf_counter()
    for _ in range(1000):
        assert read_memory() is not None
    elapsed_ms = (time.perf_counter() - started) * 1000
    print(
        f"Memory: {reading[0]}% | {reading[1]:.1f}/{reading[2]:.1f} GiB | "
        f"1000 reads {elapsed_ms:.1f} ms"
    )
    top = read_top5()
    print("Top groups:",
          ", ".join(f"{name}×{count} {megabytes:.0f}MB"
                    for name, megabytes, count in top) or "(none readable)")
    app = MemoryWidget()
    print(f"UI scale: {UI_SCALE:.3f}  detail: "
          f"{app._detail_width()}x{app._detail_height()}  "
          f"orb: {app._ring_size()}px  settings: {app._settings_size()}")
    app._show_detail()
    app.open_settings()
    # `run()` 正常情况下**不会返回** —— 它结尾是 `os._exit(0)`（见那里的注释），
    # 用于保证"点了退出进程就真的没了"。所以 `print("SELFTEST OK")` 不能写在
    # `run()` 之后：那行永远执行不到，交付门槛实际上只验了一个"退出码是 0"，
    # 等于没验（这是 2026-09-18 发现并修掉的一个假绿）。
    #
    # 改成让 `run()` 自己认领这件事：只有主循环正常结束、走到收尾那一步，
    # 才打印这一行。它出现在 stdout 上，就同时证明了窗口建起来过、
    # 详情与设置面板都开过、`quit()` 链路完整。
    app.root.after(1500, lambda: app.quit(terminate=True))
    app.run(announce="SELFTEST OK")
    return 0


def _report_startup_failure(detail: str) -> None:
    """A windowed build has no stderr, so a crash would stay invisible.

    Anything that goes wrong before the window comes up used to look exactly
    like "double-click does nothing". Say it out loud instead.
    """
    short = detail.strip().splitlines()[-6:]
    notify_box(
        "悬浮窗启动失败了：\n\n" + "\n".join(short) +
        "\n\n（完整记录在上面几行里；最常见的两种原因是设置文件损坏"
        "或被安全软件拦截。）",
        "内存悬浮窗 · 启动失败",
    )


if __name__ == "__main__":
    try:
        # Exception nets first: anything raised before the window exists used
        # to be invisible in a windowed build. The per-root Tk net is added in
        # MemoryWidget.__init__; this covers everything earlier and the
        # non-Tk threads.
        _install_exception_hooks()
        _install_native_error_log()
        _enable_high_dpi()
        load_settings()
        # One registry read per start, never polled: a stale Run entry whose
        # target has vanished produces a logon error dialog forever if no one
        # removes it - so we remove it ourselves.
        _prune_dead_autostart()
        if "--selftest" in sys.argv:
            raise SystemExit(selftest())
        _mutex_handle = acquire_single_instance()
        if _mutex_handle is not None:
            MemoryWidget().run()
        else:
            notify_box(
                "内存悬浮窗已经在运行了 —— 桌面上现在应该有一个圆环。\n\n"
                "如果找不到它，可以在任务管理器里结束 MemoryWidget.exe "
                "后再重新打开。",
                "内存悬浮窗",
            )
    except SystemExit:
        raise
    except Exception:
        import traceback
        _log_exc("startup failure", *sys.exc_info())
        _report_startup_failure(traceback.format_exc())
        raise SystemExit(1)
