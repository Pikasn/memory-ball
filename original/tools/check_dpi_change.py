"""Drive the production WndProc and deferred DPI consumer without creating Tk.

Only Win32 installation and rendering boundaries are stubbed. The actual
ctypes callback, packed-message decoding, _loop and _apply_dpi_change run.
No settings are loaded or saved, and no real window/process is touched.

    python tools/check_dpi_change.py
"""

from collections import Counter, deque
import argparse
import ctypes
import importlib.util
import os
from pathlib import Path
import sys
from types import SimpleNamespace
from unittest.mock import patch


base = Path(__file__).resolve().parent.parent
parser = argparse.ArgumentParser(description=__doc__)
parser.add_argument('--source', type=Path,
                    default=base / 'source' / 'memory_widget.py')
args = parser.parse_args()
sys.path.insert(0, str(base / 'source'))
spec = importlib.util.spec_from_file_location('memory_widget', args.source)
mw = importlib.util.module_from_spec(spec)
sys.modules[spec.name] = mw
spec.loader.exec_module(mw)


def main():
    native_phase = False
    touches, violations, errors = [], [], []
    counts = Counter()

    def touch(name):
        touches.append(name)
        if native_phase:
            violations.append(name)

    class Root:
        def bind(self, *_args, **_kwargs):
            touch('bind')

        def winfo_x(self):
            touch('winfo_x')
            return 40

        def winfo_y(self):
            touch('winfo_y')
            return 60

        def geometry(self, value):
            touch('geometry')
            self.last_geometry = value

        def after(self, _delay, _callback):
            touch('after')
            return 'isolated-timer'

    def record(name):
        def run(*_args, **_kwargs):
            touch(name)
            counts[name] += 1
        return run

    root = Root()
    app = mw.MemoryWidget.__new__(mw.MemoryWidget)
    app.root = root
    app.detail = SimpleNamespace(winfo_exists=lambda: True)
    app.settings_window = SimpleNamespace(winfo_exists=lambda: True)
    app._wndprocs = {}
    app._wm_dpi_flag = (ctypes.c_ssize_t * 1)()
    app._wm_quit_flag = (ctypes.c_int * 1)()
    app._wm_endsession_flag = (ctypes.c_int * 1)()
    app._tray_events = deque()
    app._closing = False
    app._build_ring = record('ring_build')
    app._show_detail = record('detail_build')
    app._draw_settings = record('settings_build')
    app._poll_once = record('poll')
    app.refresh_tray_tip = record('tray_tip')
    app._refresh_detail = record('detail_refresh')
    app._dbg = record('debug')
    app._ring_size = lambda: round(72 * mw.UI_SCALE)
    app._clamped_xy = lambda x, y, _width, _height: (x, y)
    app._draw_ring = lambda force=False: record(
        'ring_force' if force else 'ring_tick')()

    def owner(_hwnd, pointer):
        pointer._obj.value = os.getpid()
        return 1

    def install(_hwnd, _index, callback_pointer):
        assert callback_pointer, 'ctypes callback was not installed'
        return 1234

    def unexpected_forward(*_args):
        raise AssertionError('DPI message was forwarded unexpectedly')

    mw.UI_SCALE = 1.25
    mw.UI_SCALE_OVERRIDE = None
    with patch.object(mw.tray_icon, 'root_hwnd', return_value=12345), \
            patch.object(mw.tray_icon.user32, 'GetWindowThreadProcessId', owner), \
            patch.object(mw.kernel32, 'GetCurrentProcessId', lambda: os.getpid()), \
            patch.object(mw, '_SetWindowLongPtrW', install), \
            patch.object(mw, '_CallWindowProcW', unexpected_forward), \
            patch.object(mw, '_log_exc', side_effect=lambda *a: errors.append(a)):
        app._install_wndproc()
        callback = app._wndproc_ref  # Production WINFUNCTYPE callback, kept alive.
        rebuilds = 0
        for dpi in (96, 144, 192, 192, 96):
            expected = dpi / 96.0
            changed = abs(expected - mw.UI_SCALE) >= 0.01
            packed = dpi | (dpi << 16)
            before_touches = len(touches)
            before_counts = counts.copy()
            mw._BITMAP_CACHE['dpi-sentinel'] = b'cached-at-previous-scale'
            native_phase = True
            result = callback(12345, mw.WM_DPICHANGED, packed, 0)
            native_phase = False
            assert result == 1, 'DPI message was not handled'
            assert not violations, ('Tk/rendering work inside WndProc', violations)
            assert len(touches) == before_touches, 'WndProc did more than record DPI'
            assert counts == before_counts, 'WndProc triggered a redraw'
            assert app._wm_dpi_flag[0] == dpi, ('packed DPI not decoded', dpi,
                                               app._wm_dpi_flag[0])
            if changed:
                assert mw.UI_SCALE != expected, 'WndProc applied DPI synchronously'

            app._loop()  # Real deferred consumer and scale/cache rebuild logic.
            rebuilds += int(changed)
            assert not errors, ('production consumer logged an exception', errors)
            assert app._wm_dpi_flag[0] == 0, 'DPI message was not consumed'
            assert mw.UI_SCALE == expected, ('incorrect physical scale', dpi, mw.UI_SCALE)
            for name in ('ring_build', 'ring_force', 'detail_build', 'settings_build'):
                assert counts[name] == rebuilds, (name, counts[name], rebuilds)
            assert ('dpi-sentinel' not in mw._BITMAP_CACHE) == changed
            if changed:
                assert root.last_geometry == '%dx%d+40+60' % ((round(72 * expected),) * 2)
            assert app.loop_job == 'isolated-timer', 'poll timer was not re-armed'
            print('DPI_DEFERRED_OK: packed %d -> %d DPI -> scale %.2f; rebuilds %d'
                  % (packed, dpi, mw.UI_SCALE, rebuilds))
        assert counts['poll'] == counts['ring_tick'] == counts['detail_refresh'] == 5
    print('DPI_CHANGE_OK: 5 packed messages, 4 deferred rebuilds, duplicate ignored; '
          'no Tk calls from native callback; settings/windows untouched')


if __name__ == '__main__':
    main()
