# -*- coding: utf-8 -*-
"""Drive the tray icon through its real Win32 path and report what happened.

`Shell_NotifyIcon` is one of those APIs that reports success while doing
nothing, so "install() returned True" proves very little. This walks the same
ground the shell would:

  1. build the window, install the WndProc hook and the icon
  2. confirm the shell accepted the registration
  3. post the *exact* callback message the shell would post, for a right-click
     and for a double-click, and check the routed action
  4. tear down and confirm nothing is left registered

The popup menu itself is stubbed out during the run: `TrackPopupMenuEx` blocks
until the user picks something, which would hang a headless test forever. The
substitution is at `show_popup_menu`, one boundary below the code under test, so
everything between the message and the menu is still the real thing.

    python tools/check_tray_icon.py
"""

import ctypes
import ctypes.wintypes as wt
import os
import sys
import time

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, os.path.join(HERE, "..", "source"))
sys.path.insert(0, HERE)

import memory_widget as mw          # noqa: E402
import tray_icon                    # noqa: E402

user32 = ctypes.windll.user32
shell32 = ctypes.windll.shell32

failures = []


def check(name, ok, detail=""):
    mark = "ok  " if ok else "FAIL"
    print(f"[{mark}] {name}" + (f"  ({detail})" if detail else ""))
    if not ok:
        failures.append(name)


def post_callback(hwnd, message, event, x=0, y=0):
    """Post the packed (event, x, y) lParam exactly as the shell would.

    Low word = mouse message, high word = cursor position in screen pixels.
    """
    lparam = (event & 0xFFFF) | ((x & 0xFFFF) << 16) | ((y & 0xFF) << 24)
    user32.PostMessageW(wt.HWND(hwnd), message, 1, lparam)


def main():
    mw.SETTINGS_PATH = os.path.join(HERE, "_capture_settings.json")
    mw._enable_high_dpi()
    mw.load_settings()
    mw.save_settings = lambda: True
    mw.S.update(theme="light", ring_size=72, history_enabled=False,
                top5_enabled=False, poll_interval_ms=4000, pos=[1500, 400])

    # Stub the blocking menu call; record what it was asked to show instead.
    shown = []
    tray_icon.show_popup_menu = lambda hwnd, entries: (
        shown.append(entries) or 0)

    app = mw.MemoryWidget()
    try:
        check("tray object exists", app.tray is not None)
        if app.tray is None:
            print("  托盘没起来，后面没法测")
            return 1
        tray = app.tray

        check("Shell_NotifyIcon accepted the icon", tray.installed)
        check("callback routed to the top-level hwnd",
              tray.hwnd == tray_icon.root_hwnd(app.root),
              f"tray={tray.hwnd} root={tray_icon.root_hwnd(app.root)}")
        check("NOTIFYICONDATA cbSize is the v4 size",
              tray_icon.NID_CBSIZE == 976, str(tray_icon.NID_CBSIZE))
        check("icon handle acquired", bool(tray.hicon), hex(tray.hicon))
        # Frozen builds ship without app.ico next to the EXE; the tray icon
        # must then come out of the executable's own PE resources, never the
        # stock IDI_APPLICATION white-window glyph (that read as "no icon").
        probe = tray_icon.TrayIcon(app, ico_path="Z:\\no\\such\\file.ico")
        probe._acquire_icon()
        stock = int(tray_icon.user32.LoadIconW(
            None, tray_icon.wt.LPCWSTR(tray_icon.IDI_APPLICATION)))
        check("fallback icon comes from the exe, not the stock generic one",
              bool(probe.hicon) and probe.hicon != stock,
              hex(probe.hicon))
        if probe.owned_icon and probe.hicon:
            tray_icon.user32.DestroyIcon(tray_icon.wt.HICON(probe.hicon))
        check("WndProc hook installed", app._old_wndproc is not None)
        check("tooltip carries a live reading",
              "内存悬浮窗" in tray.tip and "%" in tray.tip, tray.tip)

        # -- right click shows the menu -----------------------------------
        shown.clear()
        post_callback(tray.hwnd, tray.callback_message,
                      tray_icon.WM_RBUTTONUP, 100, 100)
        deadline = time.time() + 3
        while not shown and time.time() < deadline:
            app.root.update()
            time.sleep(0.02)
        check("right click reaches the popup builder", bool(shown))
        if shown:
            labels = [e.get("label") for e in shown[0] if e.get("label")]
            check("menu carries the four actions",
                  {"显示 / 隐藏悬浮球", "内存详情…", "设置…", "退出"} <= set(labels),
                  " / ".join(labels))

        # -- double click toggles ----------------------------------------
        check("orb starts visible", not app.hidden)
        post_callback(tray.hwnd, tray.callback_message,
                      tray_icon.WM_LBUTTONDBLCLK)
        deadline = time.time() + 3
        while not app.hidden and time.time() < deadline:
            app.root.update()
            time.sleep(0.02)
        check("double click hides the orb", app.hidden)
        check("tray icon survives the hide",
              tray.installed and bool(user32.IsWindow(wt.HWND(tray.hwnd))))

        post_callback(tray.hwnd, tray.callback_message,
                      tray_icon.WM_LBUTTONDBLCLK)
        deadline = time.time() + 3
        while app.hidden and time.time() < deadline:
            app.root.update()
            time.sleep(0.02)
        check("double click brings it back", not app.hidden)

        # -- menu actions -------------------------------------------------
        for action, probe in (
            ("toggle", lambda: app.hidden),
            ("pin", lambda: bool(mw.S["pin_top"])),
            ("hover", lambda: bool(mw.S["hover_expand"])),
        ):
            before = probe()
            app.tray_action(action)
            app.root.update()
            check(f"action {action!r} flips its setting", probe() != before,
                  f"{before} -> {probe()}")

        app.tray_action("detail")
        app.root.update()
        check("action 'detail' opens the panel", app.detail is not None)
        app.tray_action("settings")
        app.root.update()
        check("action 'settings' opens the pane",
              app.settings_canvas is not None)
        app.tray_action("toggle")
        app.root.update()
        check("hiding the orb closes the panels",
              app.detail is None and app.settings_canvas is None)

        # -- teardown -----------------------------------------------------
        app._teardown_tray()
        check("tray removed on teardown", app.tray is None)
        app._restore_wndproc()
        check("WndProc restored", app._old_wndproc is None)
    finally:
        app.quit()

    # -- promote_tray_icon retry ------------------------------------------
    #
    # The shell creates the NotifyIconSettings subkey asynchronously after
    # NIM_ADD, so a single read can find nothing and give up silently. These
    # checks pin the retry down: it must give up (not hang) when the key never
    # appears, and it must succeed once the key does appear - even if that is
    # only on a later attempt.
    #
    # The retry loop no longer sleeps: waiting moved into the host's event
    # loop via the `scheduler` parameter (root.after). The checks below drive
    # it with a synchronous fake scheduler that runs each scheduled callback
    # immediately, so the whole chain is exercised without real waiting.
    import winreg

    def sync_scheduler(_delay_ms, callback):
        callback()

    real_lookup = tray_icon._our_icon_subkey
    try:
        tray_icon._our_icon_subkey = lambda uid=1: None
        started = time.perf_counter()
        result = tray_icon.promote_tray_icon(attempts=3, delay=0.05)
        elapsed = time.perf_counter() - started
        check("promote gives up when the shell never registers us",
              result is False)
        check("promote does not hang while retrying", elapsed < 2.0)

        # Without a scheduler there is exactly one attempt - never a sleep.
        calls_no_sched = []
        tray_icon._our_icon_subkey = lambda uid=1: calls_no_sched.append(1) or None
        tray_icon.promote_tray_icon(attempts=6, delay=0.05)
        check("promote without scheduler tries exactly once",
              len(calls_no_sched) == 1)

        # With a scheduler the chain keeps re-reading until the key appears.
        calls = []

        def appears_late(uid=1):
            calls.append(1)
            return "deadbeef" if len(calls) >= 3 else None

        tray_icon._our_icon_subkey = appears_late
        tray_icon.promote_tray_icon(attempts=6, delay=0.05,
                                    scheduler=sync_scheduler)
        check("promote re-reads the key until it appears",
              len(calls) >= 3)
        check("promote stops retrying once the key is found",
              len(calls) == 3)
        # A bogus subkey name means OpenKey fails, which a single attempt
        # reports as False.
        check("promote reports failure when the key exists but is unreadable",
              tray_icon._promote_once(1) is False)
    finally:
        tray_icon._our_icon_subkey = real_lookup

    if failures:
        print("\nTRAY_CHECK_FAILED: %d" % len(failures))
        for name in failures:
            print("  -", name)
        return 1
    print("\nTRAY_CHECK_OK")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
