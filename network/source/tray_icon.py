# -*- coding: utf-8 -*-
"""A Windows notification-area (system tray) icon, pure ctypes, no dependencies.

Why this exists: the orb is a borderless window with no taskbar button, so once
it is dragged behind other windows there is nothing on screen that says "the
program is still running". The notification area provides a persistent presence without drawing a
decoration on the orb itself.

Design constraints, inherited from the rest of the project:

* Standard library only. No pywin32, no pystray, no Pillow at runtime.
* No worker thread. The icon's callbacks are dispatched by the Tk main loop
  through a `WM_USER` message that the main window's WndProc intercepts; the
  tray owns no loop of its own. That keeps the "one event loop, nothing
  surprising on exit" rule intact and makes shutdown trivially deterministic.
* Nothing on disk. The icon is unpacked from `app.ico` in memory.

How the plumbing fits together:

    Shell_NotifyIcon(NIM_ADD, ...)  registers the icon against an hwnd + uID
    -> Windows posts `callback_message` to that hwnd on every mouse event
    -> the orb's existing WndProc (installed by `_install_wndproc`) sees it,
       converts the lParam into a `(event, x, y)` triple and calls us
    -> we show our own popup menu, or dispatch a double-click

`winfo_id()` on a Tk toplevel returns the *child* HWND, whereas tray and
taskbar messages go to the top-level one. `GetAncestor(hwnd, GA_ROOT)` bridges
that gap; without it the icon registers fine but never receives a click.
"""

import ctypes
import ctypes.wintypes as wt
import os
import struct
import sys
import winreg

# -- Win32 surface -----------------------------------------------------------
#
# 问题：`ctypes.windll` 缓存 DLL 且默认 restype=c_int。两个后果：
#   1. HANDLE/HMENU/HICON 返回值在 64 位进程里被截断成 32 位 —— 现在能用
#      只是侥幸（本机句柄值都小）；一旦带高位，`if not menu` 之类的判断
#      会拿截断值行事。
#   2. `windll` 不保证 GetLastError 语义（调用后错误码可能被覆盖），
#      而 TrackPopupMenuEx 这类"返回 0 后要看 GetLastError"的调用
#      需要一个干净的错误通道。
# 方案：全部改用 `WinDLL(..., use_last_error=True)` 并显式声明
# restype/argtypes（ctypes.windll 只用于不再保留的代码路径——这里没有）。

user32 = ctypes.WinDLL("user32", use_last_error=True)
shell32 = ctypes.WinDLL("shell32", use_last_error=True)
kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)

user32.GetAncestor.restype = wt.HWND
user32.GetAncestor.argtypes = [wt.HWND, wt.UINT]
user32.CreatePopupMenu.restype = wt.HMENU
user32.CreatePopupMenu.argtypes = []
user32.AppendMenuW.restype = wt.BOOL
user32.AppendMenuW.argtypes = [wt.HMENU, wt.UINT, ctypes.c_void_p, wt.LPCWSTR]
user32.DestroyMenu.restype = wt.BOOL
user32.DestroyMenu.argtypes = [wt.HMENU]
user32.GetCursorPos.restype = wt.BOOL
user32.GetCursorPos.argtypes = [ctypes.c_void_p]
user32.SetForegroundWindow.restype = wt.BOOL
user32.SetForegroundWindow.argtypes = [wt.HWND]
user32.TrackPopupMenuEx.restype = wt.UINT
user32.TrackPopupMenuEx.argtypes = [
    wt.HMENU, wt.UINT, ctypes.c_int, ctypes.c_int, wt.HWND, ctypes.c_void_p]
user32.PostMessageW.restype = wt.BOOL
user32.PostMessageW.argtypes = [wt.HWND, wt.UINT, wt.WPARAM, wt.LPARAM]
user32.LoadIconW.restype = wt.HICON
user32.LoadIconW.argtypes = [wt.HINSTANCE, wt.LPCWSTR]
user32.DestroyIcon.restype = wt.BOOL
user32.DestroyIcon.argtypes = [wt.HICON]
user32.CreateIconFromResourceEx.restype = wt.HICON
user32.CreateIconFromResourceEx.argtypes = [
    ctypes.c_void_p, wt.DWORD, wt.BOOL, wt.DWORD, ctypes.c_int,
    ctypes.c_int, wt.UINT]

shell32.Shell_NotifyIconW.restype = wt.BOOL
shell32.Shell_NotifyIconW.argtypes = [wt.DWORD, ctypes.c_void_p]

shell32.ExtractIconW.restype = wt.HICON
shell32.ExtractIconW.argtypes = [wt.HINSTANCE, wt.LPCWSTR, wt.UINT]

kernel32.GetModuleFileNameW.restype = wt.DWORD
kernel32.GetModuleFileNameW.argtypes = [wt.HMODULE, wt.LPWSTR, wt.DWORD]

WM_USER = 0x0400
WM_LBUTTONUP = 0x0202
WM_RBUTTONUP = 0x0205
WM_LBUTTONDBLCLK = 0x0203
WM_CONTEXTMENU = 0x007B

NIM_ADD, NIM_MODIFY, NIM_DELETE, NIM_SETVERSION = 0, 1, 2, 4
NIF_MESSAGE, NIF_ICON, NIF_TIP, NIF_SHOWTIP = 0x01, 0x02, 0x04, 0x80

GA_ROOT = 2

MF_STRING = 0x0000
MF_SEPARATOR = 0x0800
MF_DISABLED = 0x0002
MF_GRAYED = 0x0001
TPM_RIGHTBUTTON = 0x0002
TPM_RETURNCMD = 0x0100
TPM_NONOTIFY = 0x0080

MF_CHECKED = 0x0008
IDI_APPLICATION = 32512


class NOTIFYICONDATAW(ctypes.Structure):
    """Unicode NOTIFYICONDATA, v4 (Vista+).

    The balloon fields (`dwState` … `szInfoTitle`) are vestigial here - this
    app never shows a balloon - but they must be *present*, because the shell
    reads `uVersion` and `guidItem` at fixed offsets. Dropping them silently
    shifts `uVersion` from 816 to 296 and `NIM_SETVERSION` then writes four
    bytes into the middle of `szInfo`; `NIM_ADD` still succeeds, so the bug
    only shows up as clicks never arriving. Offsets verified against the
    published layout: 976 bytes, `uVersion` at 816, `guidItem` at 952.
    """
    class GUID(ctypes.Structure):
        _fields_ = [("Data1", wt.ULONG), ("Data2", wt.WORD),
                    ("Data3", wt.WORD), ("Data4", ctypes.c_byte * 8)]

    _fields_ = [
        ("cbSize", wt.DWORD),
        ("hWnd", wt.HWND),
        ("uID", wt.UINT),
        ("uFlags", wt.UINT),
        ("uCallbackMessage", wt.UINT),
        ("hIcon", wt.HICON),
        ("szTip", wt.WCHAR * 128),
        ("dwState", wt.DWORD),
        ("dwStateMask", wt.DWORD),
        ("szInfo", wt.WCHAR * 256),
        ("uVersion", wt.UINT),
        ("szInfoTitle", wt.WCHAR * 64),
        ("dwInfoFlags", wt.DWORD),
        ("guidItem", GUID),
        ("hBalloonIcon", wt.HICON),
    ]


# The shell validates cbSize, so the struct must be complete before first use.
NID_CBSIZE = ctypes.sizeof(NOTIFYICONDATAW)


class POINT(ctypes.Structure):
    _fields_ = [("x", wt.LONG), ("y", wt.LONG)]


def root_hwnd(window):
    """The top-level HWND for a Tk window.

    `winfo_id()` gives Tk's own child window, but the tray callback, the taskbar
    and the WndProc we hijack all live on the top-level one. Getting this wrong
    fails silently: `NIM_ADD` reports success and clicks simply never arrive.
    """
    hwnd = window.winfo_id()
    parent = user32.GetAncestor(wt.HWND(hwnd), GA_ROOT)
    return int(parent) if parent else int(hwnd)


# -- staying out of the overflow ---------------------------------------------
#
# Windows 11 hides a freshly-registered tray icon in the "show hidden icons"
# flyout. The API to change that - `Shell_NotifyIcon(NIM_SETVERSION)` with the
# `NIF_SHOWTIP` preference - does not cover it; the shell keeps the decision in
# the registry, under
#
#     HKCU\Control Panel\NotifyIconSettings\<hash>
#
# one subkey per icon, keyed by a hash of the executable path + uid. `UID` and
# `InitialTooltip` identify our entry; `IsPromoted = 1` is what puts the icon in
# the always-visible strip. Absent (or 0) means "hidden in the flyout".
#
# The user's request was "I need to be able to see that this program is
# running", and an icon behind a chevron does not satisfy that. So the first
# run promotes our own entry. This writes to the current user's own settings,
# never to another application's, and never touches anything but `IsPromoted`
# on the one key that matches this executable.
#
# The write is best-effort: a locked or missing key must not stop the icon from
# working, and a user who deliberately moves it back into the flyout keeps that
# choice (the value is only forced when it is missing or zero, and only on the
# first install of a session).

_ICON_SETTINGS_KEY = r"Control Panel\NotifyIconSettings"


def _executable_path():
    """The path the shell hashed to create this icon's `NotifyIconSettings` key.

    This must be the **image** the process runs from, not the script: the shell
    records `python.exe` when the widget runs from source and
    `MemoryWidget.exe` when frozen, and the key is derived from that string.
    `GetModuleFileNameW` is the only source that is right in both cases -
    `sys.executable` happens to agree today but is about the interpreter Tk was
    imported from, which is not the same thing.
    """
    buffer = ctypes.create_unicode_buffer(32768)
    length = kernel32.GetModuleFileNameW(None, buffer, len(buffer))
    if length:
        return os.path.abspath(buffer.value)
    return os.path.abspath(sys.executable)


def _our_icon_subkey(uid):
    """Find the `NotifyIconSettings` subkey the shell made for this process.

    Matching is on `ExecutablePath` + `UID`, which is exactly what the shell
    hashed to create the key name - so this identifies our entry without
    reimplementing the (undocumented) hash. Returns the subkey name or None.

    ★ **精确匹配失败就返回 None，不做"同 exe 第一个子键"的 fallback。**

    问题：旧实现把"ExecutablePath 相同但 UID 不同"的第一个子键当 fallback
    返回，promote 会往**别的图标**（同 exe 的旧 uid、或同 exe 的另一个
    程序实例的条目）写 `IsPromoted`。往错误的键写值不会报错 —— 它只是
    静默地改了别的条目的可见性，而我们自己的图标仍然藏在溢出区里，
    属于最难发现的那类 bug。
    方案：UID 对不上就跳过这个子键继续找；全都不中就返回 None，
    本次 promote 放弃（见 `promote_tray_icon`，还有重试兜底）。
    """
    wanted = _executable_path().lower()
    try:
        with winreg.OpenKey(winreg.HKEY_CURRENT_USER, _ICON_SETTINGS_KEY) as key:
            count = winreg.QueryInfoKey(key)[0]
            for index in range(count):
                name = winreg.EnumKey(key, index)
                try:
                    with winreg.OpenKey(key, name) as sub:
                        values = {}
                        for j in range(winreg.QueryInfoKey(sub)[1]):
                            field, data, _kind = winreg.EnumValue(sub, j)
                            values[field] = data
                except OSError:
                    continue
                path = str(values.get("ExecutablePath", "")).lower()
                if path != wanted:
                    continue
                if values.get("UID") == uid:
                    return name
                # ExecutablePath 相同但 UID 不同：是同 exe 的另一个图标，
                # 不是我们的条目 —— 跳过，绝不拿它当 fallback。
            return None
    except OSError:
        return None


def _promote_once(uid):
    """Single promote attempt.

    Three outcomes, three values: True once the icon is known visible;
    None when our NotifyIconSettings subkey does not exist yet (the shell
    creates it asynchronously after NIM_ADD - worth retrying); False when
    the key exists but cannot be opened or written (retrying cannot help).
    """
    name = _our_icon_subkey(uid)
    if name is None:
        return None
    try:
        with winreg.OpenKey(
                winreg.HKEY_CURRENT_USER,
                _ICON_SETTINGS_KEY + "\\" + name, 0,
                winreg.KEY_READ | winreg.KEY_SET_VALUE) as sub:
            try:
                current = winreg.QueryValueEx(sub, "IsPromoted")[0]
            except FileNotFoundError:
                current = None
            if current:
                return True
            winreg.SetValueEx(sub, "IsPromoted", 0, winreg.REG_DWORD, 1)
            return True
    except OSError:
        return False


def promote_tray_icon(uid=1, attempts=6, delay=0.25, scheduler=None):
    """Move our icon out of the overflow flyout, if it is not already out.

    Returns True when the icon is known to be in the visible strip. Failures are
    silent by design - this is a nicety on top of a feature that already works,
    and a locked-down machine should still get the icon, just in the flyout.

    The retry loop is not decoration. The shell creates the
    `NotifyIconSettings` subkey *asynchronously* after `NIM_ADD` returns, so a
    single read taken immediately afterwards can find nothing and give up
    silently. `tools/_tray_watch.py` confirmed the key does appear, but its
    arrival is not synchronised with our call - without this loop, whether the
    icon lands in the visible strip would be a race decided by machine speed.

    ★ **重试的等待不能再是 `time.sleep`（问题→实测→方案）：**

    问题：`install()` 是在 Tk 主线程上被调的，而旧实现每次重试之间
    `time.sleep(delay)` —— 6 次尝试 × 0.25 s 等于主线程被冻结约 1.5 s，
    期间悬浮球不刷新、不响应拖动，观感上就是"程序卡了一下"。
    方案：宿主经 `scheduler` 传入一个 `(delay_ms, callback)` 可调用
    （对 Tk 宿主就是 `root.after`），把剩余重试调度进事件循环 —— 每次
    tick 只做一次轻量注册表读，主线程全程可交互。
    `scheduler` 为 None（无事件循环可依赖的环境）时**只做单次尝试、
    立即返回**，绝不 sleep —— 宁可少一次重试，也不冻住调用线程。
    本模块刻意不 import tkinter，scheduler 是多态的注入点。
    """
    if scheduler is None:
        # 没有事件循环可依赖：单次尝试，成败都立即返回。
        return bool(_promote_once(uid))

    def _retry(remaining):
        # None = 注册表键还没出现（值得再等）；True/False 都是终态，停。
        if _promote_once(uid) is not None:
            return
        if remaining > 1:
            scheduler(int(delay * 1000), lambda: _retry(remaining - 1))

    _retry(attempts)
    # 异步调度：此刻只代表"尝试链已启动"，可见性由后续 tick 落定。
    return True


# -- popup menu --------------------------------------------------------------

def _menu_flags(entry):
    if entry.get("separator"):
        return MF_SEPARATOR
    if entry.get("checked"):
        flags = MF_STRING | 0x0008                        # MF_CHECKED
    else:
        flags = MF_STRING
    if not entry.get("enabled", True):
        flags |= MF_DISABLED | MF_GRAYED
    return flags


def show_popup_menu(hwnd, entries):
    """Show a native popup menu at the cursor and return the chosen id.

    Built with `CreatePopupMenu`/`AppendMenuW`/`TrackPopupMenuEx` rather than
    Tk's `tk_popup`, because Tk's menu is a Tk toplevel and would drag the whole
    Tk window-management problem set (focus stealing, `overrideredirect`,
    dismissal timing) into what is otherwise a two-line Win32 call.

    `TPM_RETURNCMD` makes the call *return* the selection instead of posting
    WM_COMMAND, which is far easier to reason about than a message we would have
    to route back into our own WndProc.
    """
    menu = user32.CreatePopupMenu()
    if not menu:
        return 0
    try:
        for entry in entries:
            if entry.get("separator"):
                user32.AppendMenuW(menu, MF_SEPARATOR, 0, None)
            else:
                user32.AppendMenuW(menu, _menu_flags(entry),
                                   entry["id"], entry["label"])
        point = POINT()
        if not user32.GetCursorPos(ctypes.byref(point)):
            return 0
        # `hWnd` here is the window that *owns* the menu; passing our own means
        # dismissal is handled by the shell without us tracking focus.
        user32.SetForegroundWindow(wt.HWND(hwnd))
        choice = user32.TrackPopupMenuEx(
            menu,
            TPM_RIGHTBUTTON | TPM_RETURNCMD | TPM_NONOTIFY,
            point.x, point.y, wt.HWND(hwnd), None)
        # The documented dance: whichever way the menu closed, post a null
        # message so the menu dismisses promptly instead of hanging around
        # painted over other windows.
        user32.PostMessageW(wt.HWND(hwnd), WM_USER + 1, 0, 0)
        return int(choice)
    finally:
        user32.DestroyMenu(menu)


# -- the icon ----------------------------------------------------------------

def load_icon_handle(ico_path, size=32):
    """Extract one frame from a .ico and hand it to the shell.

    `LoadImageW` with `LR_LOADFROMFILE` picks the frame it likes, which on a
    150% display is not necessarily the crisp one. Extracting the exact size we
    want and going through `CreateIconFromResourceEx` keeps the tray icon sharp
    regardless of DPI, and needs no third-party imaging library.

    Returns 0 on any failure - the caller falls back to the executable's own
    icon, which is always available.
    """
    try:
        with open(ico_path, "rb") as handle:
            raw = handle.read()
        if len(raw) < 6:
            return 0
        count = struct.unpack("<H", raw[4:6])[0]
        best = None
        for index in range(count):
            entry = raw[6 + index * 16: 6 + (index + 1) * 16]
            if len(entry) < 16:
                continue
            width, height, _colors, _reserved, _planes, _bpp, length, offset = \
                struct.unpack("<BBBBHHII", entry)
            width = width or 256
            if width > size:
                continue
            if best is None or width > best[0]:
                best = (width, raw[offset:offset + length])
        if best is None:
            return 0
        blob = best[1]
        # PNG-compressed frames (>=128 px in modern .ico files) are not
        # accepted by CreateIconFromResourceEx, so only BMP frames are used;
        # the loop above already prefers the largest frame <= `size`.
        if blob[:8] == b"\x89PNG\r\n\x1a\n":
            return 0
        return int(user32.CreateIconFromResourceEx(
            blob, len(blob), True, 0x00030000, size, size, 0))
    except (OSError, struct.error, ValueError):
        return 0


class TrayIcon:
    """Owns one notification-area icon and its click routing.

    Lifecycle is explicit and idempotent: `install()` / `remove()`. `install()`
    is a no-op if the icon is already up, `remove()` a no-op if it is down, so
    neither the quit path nor a settings toggle can double-free the handle.
    """

    def __init__(self, app, ico_path=None, tip="内存悬浮窗", menu_builder=None):
        self.app = app
        self.ico_path = ico_path
        self.tip = tip
        self.menu_builder = menu_builder
        self.hwnd = 0
        self.hicon = 0
        self.owned_icon = False
        self.callback_message = WM_USER + 21
        self.installed = False
        self.uid = 1

    # -- lifecycle ---------------------------------------------------------

    def install(self, scheduler=None):
        if self.installed:
            return True
        window = self.app.root
        self.hwnd = root_hwnd(window)
        if not self.hwnd:
            return False
        if not self.hicon:
            self._acquire_icon()
        data = self._notify_data(NIF_MESSAGE | NIF_ICON | NIF_TIP |
                                 NIF_SHOWTIP)
        if not shell32.Shell_NotifyIconW(NIM_ADD, ctypes.byref(data)):
            return False
        self.installed = True
        # NIM_SETVERSION is what makes the shell send the modern `lParam`
        # encoding instead of the legacy one. Without it, right-clicks can
        # arrive as WM_CONTEXTMENU and the menu anchor is wrong.
        #
        # ★ 这里用文档定义的 NIM_SETVERSION (0x4)，不再复用 NIM_MODIFY。
        # MODIFY 管的是图标/提示/回调消息，语义上不管 uVersion；多数 shell
        # 版本会顺带接受它，但文档只承诺 SETVERSION 设置版本，个别路径下
        # MODIFY 会忽略 uVersion 字段，右击坐标就永远走旧编码。
        # WM_CONTEXTMENU 兼容层不动 —— handle_message 仍把它当右键处理。
        data.uVersion = 4
        shell32.Shell_NotifyIconW(NIM_SETVERSION, ctypes.byref(data))
        # scheduler：宿主注入的 (delay_ms, callback)（对 Tk 宿主即
        # root.after）。有它就把 promote 的重试调度进事件循环；
        # 为 None 时 promote 只做单次尝试（见 promote_tray_icon，
        # 绝不 sleep 主线程）。
        promote_tray_icon(scheduler=scheduler)
        return True

    def remove(self):
        if not self.installed:
            self._release_icon()
            return
        data = self._notify_data(0)
        try:
            shell32.Shell_NotifyIconW(NIM_DELETE, ctypes.byref(data))
        except OSError:
            pass
        self.installed = False
        self._release_icon()

    def set_tip(self, text):
        self.tip = text
        if not self.installed:
            return
        data = self._notify_data(NIF_TIP | NIF_SHOWTIP)
        shell32.Shell_NotifyIconW(NIM_MODIFY, ctypes.byref(data))

    # -- internals ---------------------------------------------------------

    def _acquire_icon(self):
        if self.ico_path and os.path.exists(self.ico_path):
            self.hicon = load_icon_handle(self.ico_path)
            self.owned_icon = bool(self.hicon)
        if not self.hicon:
            # 冻结交付时 app.ico 不在 EXE 旁边（onefile 单文件），但 PyInstaller
            # --icon 已经把同一个橙色图标嵌进了 EXE 的 PE 资源。用 ExtractIconW
            # 从进程自己的映像里取出来，而不是退回系统通用图标。
            # 旧兜底是 LoadIconW(None, IDI_APPLICATION)：那是一个白色窗口的
            # 通用程序图标，托盘里看起来像"没有图标"（2026-09-20 用户实测）。
            # （LoadImageW + LR_LOADFROMFILE 在这里不可用：它把文件当 .ico
            # 解析，而 EXE 是 PE 格式，实测返回 NULL。）
            # ExtractIconW 失败返回 NULL、文件里没有图标返回 1，两种都不算数。
            exe = _executable_path()
            extracted = int(shell32.ExtractIconW(None, exe, 0) or 0)
            if extracted > 1:
                self.hicon = extracted
                self.owned_icon = True        # ExtractIconW 的句柄归我们销毁
        if not self.hicon:
            # 最后的兜底：系统通用图标（理论上到不了这里）。
            self.hicon = int(user32.LoadIconW(None, wt.LPCWSTR(IDI_APPLICATION)))
            self.owned_icon = False

    def _release_icon(self):
        if self.owned_icon and self.hicon:
            user32.DestroyIcon(wt.HICON(self.hicon))
        self.hicon = 0
        self.owned_icon = False

    def _notify_data(self, flags):
        data = NOTIFYICONDATAW()
        data.cbSize = NID_CBSIZE
        data.hWnd = self.hwnd
        data.uID = self.uid
        data.uFlags = flags
        data.uCallbackMessage = self.callback_message
        data.hIcon = self.hicon
        data.szTip = self.tip[:127]
        return data

    # -- click routing -----------------------------------------------------

    def handle_message(self, wparam, lparam):
        """Called by the host's Tk timer after WndProc enqueues the message.

        Never call this directly from a ctypes WndProc: its dispatch and
        popup actions call Tk and would re-enter _tkinter with invalid saved
        thread state.

        `lParam` carries the mouse event in its low word (legacy encoding put
        the icon id there instead, which is why `NIM_SETVERSION` matters).
        The cursor position is *not* in this message in any encoding - with
        NOTIFYICON_VERSION_4 the shell puts packed x/y in `wParam`, but this
        app never needed it: the popup menu anchors at the cursor via
        `GetCursorPos` at show time, which is strictly more correct than any
        position stashed in the callback. (An earlier revision decoded x/y
        from the wrong lParam fields here; that code had no consumer and was
        removed rather than corrected, so no stale misreading survives.)
        """
        event = lparam & 0xFFFF
        if event in (WM_RBUTTONUP, WM_CONTEXTMENU):
            self._popup()
        elif event == WM_LBUTTONDBLCLK:
            self._dispatch("toggle")
        elif event == WM_LBUTTONUP:
            self._dispatch("primary")
        return 0

    def _popup(self):
        entries = (self.menu_builder or self._default_menu)()
        choice = show_popup_menu(self.hwnd, entries)
        if not choice:
            return
        for entry in entries:
            if entry.get("id") == choice and entry.get("action"):
                entry["action"]()
                return

    def _default_menu(self):
        return [
            {"id": 1, "label": "显示 / 隐藏悬浮球", "action": lambda: self._dispatch("toggle")},
            {"id": 2, "label": "内存详情…", "action": lambda: self._dispatch("detail")},
            {"id": 3, "label": "设置…", "action": lambda: self._dispatch("settings")},
            {"separator": True},
            {"id": 9, "label": "退出", "action": lambda: self._dispatch("quit")},
        ]

    def _dispatch(self, name):
        handler = getattr(self.app, "tray_action", None)
        if handler is not None:
            handler(name)
