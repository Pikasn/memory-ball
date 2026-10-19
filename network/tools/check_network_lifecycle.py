"""Own-process native/Tk lifecycle stress for the separate network widget.

Only this interpreter's windows are subclassed/messaged. Uses an isolated data
directory, fake tray notifications and deterministic readings. A final phase
uses the actual GPU worker to prove start/stop cleanup on this machine.
"""

import argparse
import ctypes
import gc
import json
import math
import os
from pathlib import Path
import sys
import threading
import time
import weakref
from unittest.mock import patch

BASE = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(BASE / 'source'))


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--cycles', type=int, default=48)
    parser.add_argument('--out', type=Path, default=BASE / 'evidence' / 'network-lifecycle')
    args = parser.parse_args()
    args.out.mkdir(parents=True, exist_ok=True)
    data = args.out.resolve() / ('data-%d-%d' % (os.getpid(), time.time_ns()))
    data.mkdir()
    os.environ['MEMORYNET_DATA_DIR'] = str(data)
    import memory_widget as mw
    import network_info
    import tray_icon

    failures, metrics, io_errors = [], [], []
    checks, restores, samples, fake_starts, fake_stops = 0, 0, 0, 0, 0
    state = {'model': 'NVIDIA GeForce RTX 4070 Laptop GPU', 'sensor': True}
    old_excepthook = sys.excepthook

    def check(value, message):
        nonlocal checks
        checks += 1
        if not value:
            failures.append(message)

    def fake_start():
        nonlocal fake_starts
        fake_starts += 1

    def fake_stop(*_args, **_kwargs):
        nonlocal fake_stops
        fake_stops += 1

    def external_forbidden(*_args, **_kwargs):
        raise AssertionError('Lifecycle test reached an unrelated user action')

    class FakeNetwork:
        def __init__(self):
            self.revision = 1
            self.points = []
            self.reading = network_info.NetworkReading(time.monotonic())

        def sample(self, *_args, **_kwargs):
            nonlocal samples
            samples += 1
            self.revision += 1
            return self.reading

        def history(self, seconds, now):
            return [point for point in self.points if now - seconds <= point[0] <= now]

        def set_state(self, number):
            now = time.monotonic()
            status = ('starting', 'active', 'disconnected', 'unavailable')[number % 4]
            self.reading = network_info.NetworkReading(now, 1_200_000, 170_000,
                                                       status == 'active', status)
            self.points = [] if number % 7 == 0 else [(now - .1, 1_200_000, 170_000)]
            if number % 7 > 1:
                self.points = [(now - 20 + i, 1_200_000 + 100_000 * math.sin(i),
                                None if i == 6 else 170_000) for i in range(20)]
                self.points[8] = (now - 12, None, None)
            self.revision += 1

    class FakeTray:
        def __init__(self, app, **_kwargs):
            self.app, self.hwnd, self.installed = app, app._tray_hwnd, False
            self.handled = []

        def install(self, **_kwargs):
            self.installed = True
            return True

        def remove(self):
            self.installed = False

        def set_tip(self, _value):
            pass

        def handle_message(self, wparam, lparam):
            self.handled.append((wparam, lparam, threading.current_thread().name))
            action = {1: 'detail', 2: 'settings', 3: 'pin', 4: 'toggle', 5: 'hover'}.get(lparam)
            if action:
                self.app.tray_action(action)

    mw._enable_high_dpi()
    mw.S.clear()
    mw.S.update(mw.DEFAULTS)
    mw.S.update(theme='light', pos=[80, 100], detail_scale=.7, top5_enabled=False,
                poll_interval_ms=1000, autostart=False)
    mw._install_native_error_log()
    app = None
    original_make = mw.MemoryWidget._make_transparent_toplevel
    original_replace = os.replace

    def tracked_replace(source, destination):
        try:
            return original_replace(source, destination)
        except OSError as exc:
            io_errors.append({'operation': 'replace', 'errno': exc.errno,
                              'winerror': getattr(exc, 'winerror', None),
                              'message': str(exc), 'source': str(source),
                              'destination': str(destination)})
            raise

    def checked_make(self):
        nonlocal restores
        panel = original_make(self)
        hwnd = tray_icon.root_hwnd(panel)
        proc, previous = self._wndprocs[hwnd]
        reference = weakref.ref(proc)
        destroy = panel.destroy

        def checked_destroy():
            nonlocal restores
            restores += 1
            check(mw._GetWindowLongPtrW(hwnd, mw.GWL_WNDPROC) == previous,
                  'Native hook still installed at panel destruction')
            check(reference() is not None, 'Native callback prematurely released')
            destroy()
            gc.collect()
            check(reference() is not None, 'Callback released inside native panel destruction')
            check(hwnd not in self._wndprocs, 'Destroyed panel retained native mapping')

        panel.destroy = checked_destroy
        return panel

    def run_timer(name, function):
        job = getattr(app, name)
        if job:
            app.root.after_cancel(job)
            setattr(app, name, None)
        function()

    def pending_count():
        return len(app.root.tk.call('after', 'info'))

    def worker_count():
        return sum(thread.name == 'gpu-temp' and thread.is_alive() for thread in threading.enumerate())

    try:
        with patch.object(mw, 'NetworkSampler', FakeNetwork), \
                patch.object(tray_icon, 'TrayIcon', FakeTray), \
                patch.object(mw.MemoryWidget, '_make_transparent_toplevel', checked_make), \
                patch.object(mw.gpu_devices, 'display_name', side_effect=lambda _name='': state['model']), \
                patch.object(mw.gpu_info, 'name', side_effect=lambda: state['model'] if state['sensor'] else ''), \
                patch.object(mw.gpu_info, 'reading', side_effect=lambda: (56, state.get('util', 4), 2090, 8151) if state['sensor'] else None), \
                patch.object(mw, 'set_autostart', side_effect=external_forbidden), \
                patch.object(mw, 'read_top5', return_value=[('Mock.exe', 123.0, 1)]), \
                patch.object(os, 'replace', side_effect=tracked_replace), \
                patch.object(mw, '_exit_now', side_effect=external_forbidden):
            with patch.object(mw.gpu_info, 'start', side_effect=fake_start), \
                    patch.object(mw.gpu_info, 'stop', side_effect=fake_stop):
                app = mw.MemoryWidget()
                app.root.report_callback_exception = lambda typ, value, _tb: failures.append(
                    'Tk callback: %s: %s' % (typ.__name__, value))
                app.root.update()
                send_message = tray_icon.user32.SendMessageW
                send_message.argtypes = (ctypes.c_void_p, ctypes.c_uint,
                                        ctypes.c_size_t, ctypes.c_ssize_t)
                send_message.restype = ctypes.c_ssize_t
                check(Path(mw.SETTINGS_PATH).parent == data, 'Test settings escaped own directory')
                for number in range(args.cycles):
                    if number % 8 == 0:
                        print('CYCLE %d/%d' % (number + 1, args.cycles), flush=True)
                    state['model'] = ('NVIDIA GeForce RTX 4070 Laptop GPU',
                                      'AMD Radeon RX 7900 XTX', 'Intel(R) Arc(TM) Graphics',
                                      'AMD Radeon RX 7900 XTX Professional Extended Edition '
                                      'High Performance Mobile Workstation Graphics')[number % 4]
                    state['sensor'] = number % 4 == 0
                    state['util'] = None if number % 8 == 0 else 4
                    app.network.set_state(number)
                    app._show_detail()
                    app._switch_network_scale(number % 3)
                    if app.history:
                        app._switch_scale((number + 1) % 3)
                    before_name = app.detail_canvas.itemcget('gpu_name', 'text')
                    check(' '.join(before_name.split()) == state['model'], 'GPU model lost during lifecycle')
                    if state['sensor'] and state['util'] is None:
                        check(app.detail_canvas.itemcget('gpu_temp', 'text') == '56' and
                              app.detail_canvas.itemcget('gpu_util', 'text') == '—',
                              'Partial GPU reading lost temperature or invented utilization')
                    app._set_option('theme', 'dark' if number % 2 else 'light')
                    app.detail_scale = (.7, .8, .9)[number % 3]
                    app._reflow_detail()
                    app._set_option('history_enabled', number % 3 != 0)
                    check((app.history is not None) == bool(mw.S['history_enabled']),
                          'History toggle and collector disagree')
                    app._set_option('top5_enabled', number % 4 == 0)
                    app.open_settings()
                    app._set_option('pin_top', bool(number % 2))
                    app._set_option('hover_expand', bool(number % 2))
                    app._close_settings()
                    hwnd = app._tray_hwnd
                    handled_before = len(app.tray.handled)
                    send_message(hwnd, tray_icon.WM_USER + 21, 0, number % 5 + 1)
                    check(len(app.tray.handled) == handled_before and len(app._tray_events) == 1,
                          'Native tray callback executed Tk action synchronously')
                    run_timer('watch_job', app.watch_close_request)
                    check(len(app.tray.handled) == handled_before + 1 and not app._tray_events,
                          'Safe Tk timer did not consume native tray event')
                    check(app.tray.handled[-1][2] == 'MainThread', 'Tray action ran on wrong thread')
                    app.show_orb()
                    app._hide_detail()
                    check(app.detail is None and app.detail_canvas is None,
                          'Hidden panel retained a canvas/window')
                    check(app.network_box is None and app.network_photo is None and
                          app.chart_box is None and app.chart_photo is None and
                          app.last_network_revision == -1,
                          'Hidden panel retained network/chart references')
                    app._refresh_network(force=True)
                    run_timer('network_job', app._network_tick)
                    check(app.network_job is not None, 'Network timer vanished while panel hidden')
                    app._show_detail()
                    check(app.network_box is not None and app.network_photo is not None,
                          'Network panel did not restore after hide')
                    original_photo = app.network_photo
                    app._refresh_network()
                    check(app.network_photo is original_photo, 'Unchanged network revision redrew plot')
                    if number % 8 == 0:
                        dpi = (96, 144, 192)[(number // 8) % 3]
                        send_message(hwnd, mw.WM_DPICHANGED, dpi | (dpi << 16), 0)
                        check(app._wm_dpi_flag[0] == dpi, 'Packed native DPI was not decoded')
                        run_timer('loop_job', app._loop)
                        check(app._wm_dpi_flag[0] == 0, 'Tk poll did not consume DPI request')
                    app.root.update()
                    app._close_settings()
                    run_timer('watch_job', app.watch_close_request)
                    gc.collect()
                    images = len(app.root.tk.call('image', 'names'))
                    metric = {'cycle': number, 'images': images,
                              'bitmap_cache': len(mw._BITMAP_CACHE),
                              'photo_cache': len(mw._PHOTO_CACHE),
                              'native_hooks': len(app._wndprocs),
                              'retired_callbacks': len(app._retired_wndprocs),
                              'pending_timers': pending_count()}
                    metrics.append(metric)
                    check(metric['bitmap_cache'] <= 256, 'Bitmap cache escaped bound')
                    check(metric['photo_cache'] <= 100, 'Photo cache grew without bound')
                    check(metric['native_hooks'] == 2 and metric['retired_callbacks'] == 0,
                          'Destroyed panel leaked native hook/callback')
                    check(metric['pending_timers'] <= 5, 'Timer registrations accumulated')
                    check(images <= 100, 'Tk images accumulated across rebuilds')
                # The native queue is bounded even if an event storm arrives.
                for _ in range(80):
                    send_message(app._tray_hwnd, tray_icon.WM_USER + 21, 0, 99)
                check(len(app._tray_events) == 64, 'Native event queue is unbounded')
                run_timer('watch_job', app.watch_close_request)
                check(not app._tray_events, 'Native event storm did not drain')
                app._set_option('hover_expand', False)
                app._set_option('history_enabled', True)
                app._hide_detail()
            # Actual source GPU worker; own module state, no installed app action.
            real_workers = []
            for number in range(8):
                app._show_detail()
                deadline = time.monotonic() + .06
                while time.monotonic() < deadline:
                    app.root.update()
                check(worker_count() <= 1, 'Concurrent GPU workers appeared')
                app._hide_detail()
                mw.gpu_info.stop()
                check(worker_count() == 0, 'GPU worker survived hidden panel')
                real_workers.append(worker_count())
            app.quit()
            check(app._closing and app.network_job is None and app.loop_job is None and app.watch_job is None,
                  'Quit did not cancel all periodic timers')
            check(not app._wndprocs and app.tray is None, 'Quit retained native hooks or tray')
            check(worker_count() == 0, 'Quit retained GPU sampling thread')
            previous_samples = samples
            app._network_tick()
            check(samples == previous_samples and app.network_job is None,
                  'Closing network timer rearmed itself or sampled')
    except BaseException as exc:
        failures.append('Uncaught %s: %s' % (type(exc).__name__, exc))
    finally:
        sys.excepthook = old_excepthook
        if app is not None and not app._closing:
            app.quit()
        mw.gpu_info.stop()
    error_path = Path(mw.ERROR_LOG_PATH)
    native_path = Path(mw.NATIVE_ERROR_LOG_PATH)
    error_text = error_path.read_text(encoding='utf-8') if error_path.exists() else ''
    check(not error_text, 'Application error log is nonempty: ' + error_text[:1200])
    check(not native_path.exists() or native_path.stat().st_size == 0, 'Native crash log is nonempty')
    result = {'cycles': args.cycles, 'checks': checks, 'failures': failures,
              'panel_destructions_checked': restores, 'network_samples': samples,
              'fake_gpu_start_stop': [fake_starts, fake_stops], 'metrics': metrics,
              'isolated_data_directory': str(data), 'error_log': error_text,
              'io_errors': io_errors,
              'threads_after_cleanup': [thread.name for thread in threading.enumerate()]}
    destination = (args.out / 'network-lifecycle-check.json').resolve()
    destination.write_text(json.dumps(result, ensure_ascii=False, indent=2), encoding='utf-8')
    for failure in failures[:25]:
        print('FAIL: ' + failure)
    print('NETWORK_LIFECYCLE_%s: %d cycles / %d checks / %d failures / %d native panel restores; %s' %
          ('OK' if not failures else 'FAIL', args.cycles, checks, len(failures), restores, destination), flush=True)
    return int(bool(failures))


if __name__ == '__main__':
    raise SystemExit(main())
