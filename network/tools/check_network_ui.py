"""Isolated production-canvas validation for the portable network widget.

Creates no app instance, tray, sampler, GPU thread or native callback, and
forbids settings and process actions. Only explicitly supplied evidence is saved.
"""

import argparse
from itertools import product
import json
import math
import os
from pathlib import Path
import sys
import time
import tkinter as tk
from types import SimpleNamespace
from unittest.mock import patch

from PIL import Image

BASE = Path(__file__).resolve().parent.parent
os.environ['MEMORYNET_DATA_DIR'] = str(BASE / 'evidence' / 'ui-isolated-data')
sys.path.insert(0, str(BASE / 'source'))
sys.path.insert(0, str(BASE / 'tools'))
import memory_widget as mw
import screen_capture as sc

MODELS = [
    'NVIDIA GeForce RTX 4070 Laptop GPU',
    'AMD Radeon RX 7800 XT',
    'Intel(R) Arc(TM) Graphics',
    'AMD Radeon RX 7900 XTX Professional Extended Edition Dedicated '
    'High Performance Graphics for Mobile Workstations',
]
ROWS = [('Editor.exe', 2150.0, 12), ('node.exe', 1024.0, 22),
        ('Helper.exe', 960.0, 7), ('Chat.exe', 900.0, 1),
        ('ViewerHost.exe', 816.0, 32)]


class FakeNetwork:
    def __init__(self):
        self.revision = 1
        self.queries = []
        self.set_state('active', 601)

    def set_state(self, status, count=601, download=12_800_000, upload=2_400_000):
        now = time.monotonic()
        self.reading = SimpleNamespace(download_bps=download, upload_bps=upload,
                                      status=status, available=status == 'active')
        self.points = [(now - (count - 1 - i),
                        8_000_000 + 4_000_000 * math.sin(i * .13),
                        1_200_000 + 600_000 * math.cos(i * .08))
                       for i in range(count)]
        self.revision += 1

    def history(self, seconds, now):
        self.queries.append(seconds)
        return [point for point in self.points if now - seconds <= point[0] <= now]


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--out', type=Path, default=BASE / 'evidence' / 'network-ui')
    parser.add_argument('--no-capture', action='store_true')
    args = parser.parse_args()
    args.out.mkdir(parents=True, exist_ok=True)
    failures, evidence, screenshots = [], [], []
    checks = 0
    state = {'model': MODELS[0], 'sensor': True}

    def check(condition, message):
        nonlocal checks
        checks += 1
        if not condition:
            failures.append(message)

    def forbidden(*_args, **_kwargs):
        raise AssertionError('The isolated test reached a user setting/process action')

    check(mw.MUTEX_NAME != r'Local\MemoryWidget.v4', 'Mutex aliases installed version')
    check(mw.AUTOSTART_NAME != 'MemoryRing', 'Autostart aliases installed version')
    check(Path(mw.SETTINGS_PATH).parent == Path(os.environ['MEMORYNET_DATA_DIR']),
          'Settings do not respect isolated test data directory')
    check(mw.DEFAULTS['autostart'] is False and mw.DEFAULTS['pos'] is None,
          'Defaults enable startup or copy a foreign screen position')
    check(tuple(seconds for seconds, _ in mw.MemoryWidget.HISTORY_RANGES) == (60, 300, 600),
          'Time windows differ from approved concept')
    mw._enable_high_dpi()
    root = tk.Tk()
    root.withdraw()
    root.report_callback_exception = lambda typ, value, _tb: failures.append(
        'Tk callback error: %s: %s' % (typ.__name__, value))
    mw.resolve_fonts(root)

    def bbox(canvas, tag, label):
        found = canvas.find_withtag(tag)
        check(bool(found), '%s: missing %s' % (label, tag))
        return canvas.bbox(found[0]) if found else None

    def content(canvas, tag):
        found = canvas.find_withtag(tag)
        return canvas.itemcget(found[0], 'text') if found else ''

    def separated(a, b, label, gap=1):
        if a and b:
            check(a[2] + gap <= b[0] or b[2] + gap <= a[0]
                  or a[3] + gap <= b[1] or b[3] + gap <= a[1],
                  '%s: overlapping bounds %s / %s' % (label, a, b))

    def click(canvas, tag, label):
        target = bbox(canvas, tag, label)
        if not target:
            return
        x, y = (target[0] + target[2]) // 2, (target[1] + target[3]) // 2
        canvas.event_generate('<Motion>', x=x, y=y)
        root.update()
        canvas.event_generate('<ButtonPress-1>', x=x, y=y)
        canvas.event_generate('<ButtonRelease-1>', x=x, y=y)
        root.update()

    cases = [(theme, dpi, scale, MODELS[0], True, True, True)
             for theme, dpi, scale in product(('light', 'dark'), (1, 1.5, 2), (.7, 1))]
    cases += [(theme, 1.5, .731, model, False, True, True)
              for theme in ('light', 'dark') for model in MODELS[1:]]
    cases += [(theme, 1.5, .731, MODELS[0], True, True, True)
              for theme in ('light', 'dark')]
    cases += [(theme, 1.5, .55, MODELS[0], True, True, True)
              for theme in ('light', 'dark')]
    cases += [(theme, 1.5, .45, MODELS[0], True, True, True)
              for theme in ('light', 'dark')]
    cases += [(theme, 1, .7, '', False, False, False) for theme in ('light', 'dark')]
    try:
        with patch.object(mw.gpu_devices, 'display_name', side_effect=lambda _sensor='': state['model']), \
                patch.object(mw.gpu_info, 'name', side_effect=lambda: state['model'] if state['sensor'] else ''), \
                patch.object(mw.gpu_info, 'reading', side_effect=lambda: (56, 4, 2090, 8151) if state['sensor'] else None), \
                patch.object(mw.gpu_info, 'start', return_value=None), \
                patch.object(mw.gpu_info, 'stop', side_effect=forbidden), \
                patch.object(mw, 'save_settings', side_effect=forbidden), \
                patch.object(mw, 'read_top5', return_value=ROWS):
            for number, (theme, dpi, scale, model, sensor, history, top5) in enumerate(cases, 1):
                label = '%s/dpi%g/size%g/%s/sensor%d' % (theme, dpi, scale, model or 'no-gpu', sensor)
                print('[%d/%d] %s' % (number, len(cases), label), flush=True)
                state.update(model=model, sensor=sensor)
                mw.S.clear()
                mw.S.update(mw.DEFAULTS)
                mw.S.update(theme=theme, history_enabled=history, top5_enabled=top5,
                            detail_scale=scale, pin_top=False, autostart=False)
                mw.UI_SCALE = mw.UI_SCALE_OVERRIDE = dpi
                mw._DETAIL_SCALE = 1
                mw._BITMAP_CACHE.clear()
                window = tk.Toplevel(root)
                window.overrideredirect(True)
                window.configure(bg=mw.TRANSPARENT)
                window.attributes('-transparentcolor', mw.TRANSPARENT)
                app = mw.MemoryWidget.__new__(mw.MemoryWidget)
                app.root, app.detail, app.detail_scale = root, window, scale
                app.settings_window = app.context_window = None
                app.last_memory = (78, 12.3, 15.7, 3.4)
                app.last_detail_state = None
                app.last_chart_revision = -1
                app.scale_index, app.network_scale_index = 1, 0
                app.top5_cache, app.top5_next_due = [], 0
                app.gpu_name, app.gpu_h_built = model, None
                app.gpu_bar_photo = app.meter_photo = app.network_photo = None
                app.chart_box = app.network_box = None
                app.network, app.last_network_revision = FakeNetwork(), -1
                app._fitting = False
                app._resize_start = app._move_start = app._resize_job = None
                app.settings_error = ''
                app._dbg = lambda _message: None
                process_clicks, saved = [], []
                app._show_process_menu = lambda _event, index: process_clicks.append(index)
                app._persist_settings = lambda reason: saved.append(reason) or True
                app._clamped_xy = lambda x, y, _w, _h, anchor=None: (x, y)
                app.history = mw.History() if history else None
                if history:
                    now = time.monotonic()
                    app.history.points.extend((now - (120 - i) * 5, 77 + .8 * math.sin(i * .05))
                                              for i in range(121))
                    app.history.revision = 1
                width, height = app._detail_width(), app._detail_height()
                window.geometry('%dx%d+60+60' % (width, height))
                canvas = app.detail_canvas = tk.Canvas(window, width=width, height=height,
                            bg=mw.TRANSPARENT, highlightthickness=0, bd=0)
                canvas.pack()
                mw.set_photo_master(canvas)
                app._build_detail()
                canvas.bind('<Button-1>', app._move_press)
                canvas.bind('<B1-Motion>', app._panel_motion)
                canvas.bind('<ButtonRelease-1>', app._panel_release)
                canvas.bind('<Motion>', app._panel_hover)
                app._refresh_detail(force=True)
                root.update()
                # Exercise real Tk events through the production dispatcher.
                # The header moves, body stays inert, bottom adjusts height.
                canvas.event_generate('<Motion>', x=width // 2, y=height // 2)
                canvas.event_generate('<ButtonPress-1>', x=width // 2, y=height // 2)
                root.update()
                check(app._move_start is None and app._resize_start is None,
                      label + ': body press started a gesture')
                check(str(canvas.cget('cursor')) == '', label + ': body advertises movement')
                canvas.event_generate('<ButtonRelease-1>', x=width // 2, y=height // 2)
                header_y = max(1, round(20 * dpi * scale))
                canvas.event_generate('<Motion>', x=width // 2, y=header_y)
                canvas.event_generate('<ButtonPress-1>', x=width // 2, y=header_y)
                root.update()
                check(app._move_start is not None and app._resize_start is None,
                      label + ': header did not start movement')
                canvas.event_generate('<ButtonRelease-1>', x=width // 2, y=header_y)
                root.update()
                for item in canvas.find_all():
                    if (canvas.type(item) == 'text' and canvas.itemcget(item, 'text')
                            and canvas.itemcget(item, 'state') != 'hidden'):
                        b = canvas.bbox(item)
                        check(b[0] >= 0 and b[1] >= 0 and b[2] <= width and b[3] <= height,
                              '%s: text outside card: %r %s vs %s' %
                              (label, canvas.itemcget(item, 'text'), b, (width, height)))
                separated(bbox(canvas, 'network_title', label), bbox(canvas, 'net_scale_0', label),
                          label + ' network title/selector')
                for key in ('download', 'upload'):
                    separated(bbox(canvas, 'net_%s_value' % key, label),
                              bbox(canvas, 'net_%s_unit' % key, label), label + ' speed/value unit')
                    check(content(canvas, 'net_%s_value' % key) == ('12.8' if key == 'download' else '2.4'),
                          label + ' decimal MB/s conversion incorrect')
                separated(bbox(canvas, 'net_download_unit', label), bbox(canvas, 'net_upload_value', label),
                          label + ' download/upload columns')
                check(bool(canvas.find_withtag('network_plot')), label + ' network plot missing')
                app.network.set_state('active', 2, 999_900_000, 999_900)
                app._refresh_network(force=True)
                for key, unit in [('download', 'MB/s'), ('upload', 'KB/s')]:
                    check(content(canvas, 'net_%s_value' % key) == '999.9' and
                          content(canvas, 'net_%s_unit' % key) == unit,
                          label + ' boundary speed/unit formatting incorrect')
                    separated(bbox(canvas, 'net_%s_value' % key, label),
                              bbox(canvas, 'net_%s_unit' % key, label),
                              label + ' large speed/value unit')
                separated(bbox(canvas, 'net_download_unit', label), bbox(canvas, 'net_upload_value', label),
                          label + ' large speed/download upload columns')
                check(canvas.bbox('net_upload_unit')[2] <= width,
                      label + ' large upload unit outside panel')
                app.network.set_state('active')
                app._refresh_network(force=True)
                if model:
                    check(' '.join(content(canvas, 'gpu_name').split()) == ' '.join(model.split()),
                          label + ' full GPU name lost')
                    separated(bbox(canvas, 'gpu_name', label), bbox(canvas, 'gpu_title', label),
                              label + ' GPU model/title')
                    separated(bbox(canvas, 'gpu_name', label), bbox(canvas, 'gpu_temp', label),
                              label + ' GPU model/temp')
                    if not sensor:
                        check(canvas.itemcget('gpu_temp', 'state') == 'hidden'
                              and canvas.itemcget('gpu_temp_unit', 'state') == 'hidden'
                              and content(canvas, 'gpu_util') == '—',
                              label + ' unavailable temperature not hidden')
                else:
                    check(not canvas.find_withtag('gpu_name'), label + ' phantom GPU name remains')

                representative = dpi == 1.5 and scale == .731 and model == MODELS[0]
                if representative:
                    original_width = app._detail_width()
                    original_font = canvas.itemcget('hero_value', 'font')
                    original_xy = (window.winfo_x(), window.winfo_y())
                    before_drag_height = app._detail_height()
                    bottom_y = canvas.winfo_height() - 3
                    canvas.event_generate('<Motion>', x=width // 3, y=bottom_y)
                    canvas.event_generate('<ButtonPress-1>', x=width // 3, y=bottom_y)
                    root.update()
                    check(app._resize_start is not None and app._move_start is None,
                          label + ': bottom edge started a move instead of resizing')
                    check(str(canvas.cget('cursor')) == 'sb_v_double_arrow',
                          label + ': bottom cursor is not vertical resize')
                    canvas.event_generate('<B1-Motion>', x=width // 3 + 30, y=bottom_y - 40)
                    root.update()
                    canvas.event_generate('<ButtonRelease-1>', x=width // 3 + 30, y=bottom_y - 40)
                    root.update()
                    check(abs(app._detail_height() - (before_drag_height - 40)) <= 2,
                          label + ': bottom height did not follow mouse pixels')
                    check(app._detail_width() == original_width and
                          canvas.itemcget('hero_value', 'font') == original_font,
                          label + ': bottom resize changed width or text size')
                    check((window.winfo_x(), window.winfo_y()) == original_xy,
                          label + ': bottom resize moved the panel')
                    check(canvas.grab_current() is None,
                          label + ': gesture left a mouse grab behind')
                    # Corner scaling preserves the shape of a shortened card.
                    app.detail_height_scale = .6
                    app._reflow_detail()
                    root.update()
                    for dx, dy in ((20, 45), (-20, -45)):
                        before_width, before_height = app._detail_width(), app._detail_height()
                        before_scale = app.detail_scale
                        corner_x, corner_y = canvas.winfo_width() - 3, canvas.winfo_height() - 3
                        canvas.event_generate('<Motion>', x=corner_x, y=corner_y)
                        canvas.event_generate('<ButtonPress-1>', x=corner_x, y=corner_y)
                        root.update()
                        check(app._resize_start is not None and app._resize_mode == 'scale'
                              and app._move_start is None,
                              label + ': corner did not take precedence over bottom edge')
                        check(str(canvas.cget('cursor')) == 'size_nw_se',
                              label + ': corner cursor is not diagonal resize')
                        canvas.event_generate('<B1-Motion>', x=corner_x + dx, y=corner_y + dy)
                        root.update()
                        canvas.event_generate('<ButtonRelease-1>', x=corner_x + dx, y=corner_y + dy)
                        root.update()
                        check((app.detail_scale - before_scale) * dx > 0,
                              label + ': corner did not grow/shrink in drag direction')
                        check(abs(app._detail_width() / before_width -
                                  app._detail_height() / before_height) < .015,
                              label + ': corner changed the card aspect ratio')
                        check(app.detail_height_scale == .6 and
                              (window.winfo_x(), window.winfo_y()) == original_xy,
                              label + ': corner changed height compression or position')
                        check(canvas.grab_current() is None,
                              label + ': corner left a mouse grab behind')
                        row_box = bbox(canvas, 'proc_name_4', label)
                        check(row_box and row_box[3] < app._detail_height(),
                              label + ': corner clipped fifth process row')
                    app.detail_scale = scale
                    app.detail_height_scale = mw.MAX_DETAIL_HEIGHT_SCALE
                    app._reflow_detail()
                    root.update()
                    original_height = app._detail_height()
                    app.detail_height_scale = mw.MIN_DETAIL_HEIGHT_SCALE
                    compact_height = app._detail_height()
                    app._reflow_detail()
                    root.update()
                    check(compact_height < original_height * .9,
                          label + ': vertical resize did not shorten the card')
                    for tag in ('proc_name_4', 'proc_mem_4'):
                        box = bbox(canvas, tag, label)
                        check(box and box[3] <= compact_height,
                              label + ': fifth process row clipped in compact height')
                    app.detail_height_scale = mw.MAX_DETAIL_HEIGHT_SCALE
                    app._reflow_detail()
                    root.update()
                    with patch.object(mw.gpu_info, 'reading', return_value=(56, None, 2090, 8151)):
                        app._refresh_detail_gpu(canvas, mw.palette())
                        check(content(canvas, 'gpu_temp') == '56' and content(canvas, 'gpu_util') == '—',
                              label + ' unknown GPU utilization is not preserved as dash')
                        check(bool(canvas.find_withtag('gpu_name')), label + ' partial GPU reading hid model')
                    with patch.object(mw.gpu_info, 'reading', return_value=(None, 37, 0, 0)):
                        temperature_bar_width = canvas.bbox('gpu_bar')[2] - canvas.bbox('gpu_bar')[0]
                        app._refresh_detail_gpu(canvas, mw.palette())
                        check(canvas.itemcget('gpu_temp', 'state') == 'hidden' and content(canvas, 'gpu_util') == '37%',
                              label + ' missing temperature also hid working utilization')
                        check(canvas.itemcget('gpu_temp_unit', 'state') == 'hidden'
                              and canvas.bbox('gpu_temp') is None and canvas.bbox('gpu_temp_unit') is None,
                              label + ' missing temperature still visible')
                        bar_box = bbox(canvas, 'gpu_bar', label)
                        check(bar_box[2] - bar_box[0] > temperature_bar_width,
                              label + ' utilization bar did not use released temperature space')
                        check(bar_box[0] == round(mw.PAD * dpi * app.detail_scale),
                              label + ' utilization bar not flush with content')
                        separated(bar_box, bbox(canvas, 'gpu_util', label), label + ' full-width GPU bar/utilization')
                    app._refresh_detail_gpu(canvas, mw.palette())
                    check(canvas.itemcget('gpu_temp', 'state') == 'normal'
                          and canvas.itemcget('gpu_temp_unit', 'state') == 'normal',
                          label + ' supported temperature did not restore')
                    for count in (0, 1):
                        app.network.set_state('starting', count)
                        app._refresh_network(force=True)
                        check(bool(canvas.find_withtag('network_plot')), label + ' startup %d unsafe' % count)
                    for status, wording in [('starting', '正在采样'), ('disconnected', '等待网络连接'),
                                             ('unavailable', '网速暂不可读')]:
                        app.network.set_state(status, 0, 0, 0)
                        app._refresh_network(force=True)
                        check(content(canvas, 'net_status') == wording, label + ' missing status ' + status)
                        check(content(canvas, 'net_download_value') == '—' and
                              content(canvas, 'net_upload_value') == '—',
                              label + ': unavailable network presented as measured zero ' + status)
                    app.network.set_state('active')
                    app._refresh_network(force=True)
                    for index in (2, 1, 0):
                        before_memory = app.scale_index
                        click(canvas, 'net_scale_%d' % index, label)
                        check(app.network_scale_index == index and app.scale_index == before_memory,
                              label + ' network selection changed memory or failed')
                        check(app.network.queries[-1] == (60, 300, 600)[index],
                              label + ' network selection did not change time query')
                        check(app._move_start is None, label + ' network click started drag')
                    for index in (0, 2, 1):
                        before_network = app.network_scale_index
                        click(canvas, 'scale_%d' % index, label)
                        check(app.scale_index == index and app.network_scale_index == before_network,
                              label + ' memory selection changed network or failed')
                        check(app._move_start is None, label + ' memory click started drag')
                    for count, index in product((0, 1), (0, 1, 2)):
                        app.history.points.clear()
                        if count:
                            app.history.points.append((time.monotonic(), 78))
                        app.scale_index = index
                        app._draw_chart()
                        check(bool(canvas.find_withtag('chart_dynamic')),
                              label + ' memory startup %d/range%d unsafe' % (count, index))
                    now = time.monotonic()
                    app.history.points.clear()
                    app.history.points.extend((now - (120 - i) * 5, 77 + .8 * math.sin(i * .05))
                                              for i in range(121))
                    app.scale_index = 1
                    app._switch_scale(1)
                    if not args.no_capture:
                        path = (args.out / ('memorynet-%s-production.png' % theme)).resolve()
                        bmp = path.with_suffix('.bmp')
                        sc.capture_window(canvas.winfo_id(), str(bmp), padding=0, settle=.05)
                        Image.open(bmp).save(path)
                        screenshots.append(str(path))
                evidence.append({'case': label, 'size': [width, height],
                                 'gpu_name': content(canvas, 'gpu_name'),
                                 'network_queries': app.network.queries})
                window.destroy()
    finally:
        root.destroy()
    result = {'cases': len(cases), 'checks': checks, 'failures': failures,
              'screenshots': screenshots, 'evidence': evidence}
    destination = (args.out / 'network-ui-check.json').resolve()
    destination.write_text(json.dumps(result, ensure_ascii=False, indent=2), encoding='utf-8')
    for failure in failures[:30]:
        print('FAIL: ' + failure)
    print('NETWORK_UI_%s: %d cases / %d checks / %d failures; %s' %
          ('OK' if not failures else 'FAIL', len(cases), checks, len(failures), destination))
    return int(bool(failures))


if __name__ == '__main__':
    raise SystemExit(main())
