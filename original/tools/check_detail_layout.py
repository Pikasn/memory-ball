"""Isolated native Tk regression for the integrated detail card.

Exercises production build, refresh and click handlers using example readings.
Creates no MemoryWidget app, tray, native hook, GPU worker or user-settings file.
"""

import argparse
from itertools import product
import json
import math
from pathlib import Path
import sys
import time
import tkinter as tk
from unittest.mock import patch

from PIL import Image

base = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(base / 'source'))
sys.path.insert(0, str(base / 'tools'))
import memory_widget as mw
import screen_capture as sc

FULL_MODEL = 'NVIDIA Example GPU'
LONG_MODEL = ('NVIDIA GeForce RTX 5090 Laptop GPU Professional Extended Edition '
              'with Dedicated High Performance Graphics for Mobile Workstations')
ROWS = [('Editor.exe', 2150.0, 12), ('node.exe', 1024.0, 22),
        ('Helper.exe', 960.0, 7),
        ('ExceptionallyLongBackgroundHelperProcessForCheckingColumnLayout.exe',
         900.0, 320), ('Chat.exe', 816.0, 1)]


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--no-capture', action='store_true')
    parser.add_argument('--out', type=Path, default=base / 'build' / 'detail-layout')
    args = parser.parse_args()
    args.out.mkdir(parents=True, exist_ok=True)
    failures, evidence, screenshots = [], [], []
    checks = 0
    state = {'gpu': True, 'model': FULL_MODEL}

    def check(condition, message):
        nonlocal checks
        checks += 1
        if not condition:
            failures.append(message)

    def forbidden(*_args, **_kwargs):
        raise AssertionError('The isolated layout check reached an external action')

    mw._enable_high_dpi()
    root = tk.Tk()
    root.withdraw()
    root.report_callback_exception = lambda typ, value, _tb: failures.append(
        'Tk callback error: %s: %s' % (typ.__name__, value))
    mw.resolve_fonts(root)

    def box(canvas, tag, case):
        found = canvas.find_withtag(tag)
        check(bool(found), '%s: missing %s' % (case, tag))
        return canvas.bbox(found[0]) if found else None

    def text(canvas, tag):
        found = canvas.find_withtag(tag)
        return canvas.itemcget(found[0], 'text') if found else ''

    def separated(a, b, label, gap=1):
        if a and b:
            check(a[2] + gap <= b[0] or b[2] + gap <= a[0]
                  or a[3] + gap <= b[1] or b[3] + gap <= a[1],
                  '%s: collision %s / %s' % (label, a, b))

    def click(canvas, tag, case):
        target = box(canvas, tag, case)
        if not target:
            return
        x, y = (target[0] + target[2]) // 2, (target[1] + target[3]) // 2
        canvas.event_generate('<Motion>', x=x, y=y)
        root.update()
        canvas.event_generate('<ButtonPress-1>', x=x, y=y)
        canvas.event_generate('<ButtonRelease-1>', x=x, y=y)
        root.update()

    cases = [(theme, dpi, scale, True, True, True, FULL_MODEL)
             for theme, dpi, scale in product(('light', 'dark'),
                                              (1.0, 1.5, 2.0),
                                              (.7, .731, 1.0, 2.0))]
    cases += [(theme, dpi, scale, True, True, True, LONG_MODEL)
              for theme in ('light', 'dark')
              for dpi, scale in ((1.0, .7), (1.5, .731))]
    cases += [('light', 1.5, .731, gpu, history, top5, FULL_MODEL)
              for gpu, history, top5 in product((False, True), repeat=3)
              if not (gpu and history and top5)]
    try:
        with patch.object(mw.gpu_info, 'available', side_effect=lambda: state['gpu']), \
                patch.object(mw.gpu_info, 'name', side_effect=lambda: state['model']), \
                patch.object(mw.gpu_info, 'reading', return_value=(56, 4, 2090, 8151)), \
                patch.object(mw.gpu_info, 'start', side_effect=forbidden), \
                patch.object(mw.gpu_info, 'stop', side_effect=forbidden), \
                patch.object(mw, 'save_settings', side_effect=forbidden), \
                patch.object(mw, 'read_top5', return_value=ROWS):
            for case_index, (theme, dpi, scale, gpu, history, top5, model) in enumerate(cases, 1):
                name = ('%s/dpi%g/size%g/g%d/h%d/t%d/%s' %
                        (theme, dpi, scale, gpu, history, top5,
                         'long' if model == LONG_MODEL else 'full'))
                print('[%d/%d] %s' % (case_index, len(cases), name), flush=True)
                state.update(gpu=gpu, model=model)
                mw.S.clear()
                mw.S.update(mw.DEFAULTS)
                mw.S.update(theme=theme, history_enabled=history, top5_enabled=top5,
                            detail_scale=scale, pin_top=False, autostart=False)
                mw.UI_SCALE = dpi
                mw.UI_SCALE_OVERRIDE = dpi
                mw._DETAIL_SCALE = 1.0
                mw._BITMAP_CACHE.clear()
                window = tk.Toplevel(root)
                window.overrideredirect(True)
                window.configure(bg=mw.TRANSPARENT)
                try:
                    window.attributes('-transparentcolor', mw.TRANSPARENT)
                except tk.TclError:
                    pass
                app = mw.MemoryWidget.__new__(mw.MemoryWidget)
                app.root, app.detail = root, window
                app.detail_scale = scale
                app.settings_window = app.context_window = None
                app.last_memory = (78, 12.3, 15.7, 3.4)
                app.last_detail_state = None
                app.last_chart_revision = -1
                app.scale_index = 0
                app.top5_cache, app.top5_next_due = [], 0.0
                app.gpu_name = model
                app.gpu_h_built = None
                app.gpu_bar_photo = app.meter_photo = None
                app.chart_box = None
                app._fitting = False
                app._resize_start = app._move_start = app._resize_job = None
                app.settings_error = ''
                app._dbg = lambda _message: None
                process_clicks = []
                app._show_process_menu = lambda _event, index: process_clicks.append(index)
                saved = []
                app._persist_settings = lambda reason: saved.append(reason) or True
                app._clamped_xy = lambda x, y, _w, _h, anchor=None: (x, y)
                app.history = mw.History() if history else None
                if history:
                    now = time.monotonic()
                    app.history.points.extend((now - (1440 - i) * 5,
                                               77 + .8 * math.sin(i * .05))
                                              for i in range(1441))
                    app.history.revision = 1
                width, height = app._detail_width(), app._detail_height()
                window.geometry('%dx%d+60+60' % (width, height))
                canvas = app.detail_canvas = tk.Canvas(window, width=width,
                            height=height, bg=mw.TRANSPARENT, highlightthickness=0, bd=0)
                canvas.pack()
                mw.set_photo_master(canvas)
                app._build_detail()
                canvas.bind('<Button-1>', app._move_press)
                app._refresh_detail(force=True)
                root.update()
                width, height = canvas.winfo_width(), canvas.winfo_height()
                check(width == app._detail_width() and height == app._detail_height(),
                      '%s: physical canvas/geometry mismatch' % name)
                for item in canvas.find_all():
                    if canvas.type(item) == 'text' and canvas.itemcget(item, 'text'):
                        b = canvas.bbox(item)
                        check(b[0] >= 0 and b[1] >= 0 and b[2] <= width and b[3] <= height,
                              '%s: text outside card: %r %s vs %s' %
                              (name, canvas.itemcget(item, 'text'), b, (width, height)))
                title, pin, close = (box(canvas, tag, name) for tag in
                                     ('detail_title', 'pin_bg', 'close_bg'))
                separated(title, pin, name + ' title/pin')
                separated(pin, close, name + ' pin/close')
                if gpu:
                    displayed = text(canvas, 'gpu_name')
                    check(' '.join(model.split()) in ' '.join(displayed.split()),
                          '%s: GPU model lost characters: %r' % (name, displayed))
                    model_box = box(canvas, 'gpu_name', name)
                    separated(model_box, box(canvas, 'gpu_title', name), name + ' model/title')
                    separated(model_box, box(canvas, 'gpu_temp', name), name + ' model/temp')
                    separated(box(canvas, 'gpu_temp_unit', name),
                              box(canvas, 'gpu_bar', name), name + ' temperature/bar')
                    if model == LONG_MODEL and model_box:
                        font = canvas.itemcget('gpu_name', 'font')
                        line_height = int(root.tk.call('font', 'metrics', font, '-linespace'))
                        check(model_box[3] - model_box[1] > line_height,
                              '%s: exceptionally long GPU name did not wrap' % name)
                else:
                    check(not canvas.find_withtag('gpu_name'), name + ': hidden GPU consumes content')
                if history:
                    check(bool(canvas.find_withtag('chart_dynamic')), name + ': trend graph missing')
                    separated(box(canvas, 'trend_title', name),
                              box(canvas, 'scale_0', name), name + ' trend/title controls')
                else:
                    check(not canvas.find_withtag('chart_dynamic'), name + ': disabled graph remains')
                if top5:
                    for i, (_process, amount, count) in enumerate(ROWS):
                        tags = ['proc_name_%d' % i, 'proc_count_%d' % i,
                                'proc_mem_%d' % i, 'proc_unit_%d' % i]
                        boxes = [box(canvas, tag, name) for tag in tags]
                        for j in range(3):
                            separated(boxes[j], boxes[j + 1], name + ' process columns %d' % i)
                        shown_count = text(canvas, tags[1]).replace('×', '').strip()
                        check(shown_count == str(count) or (count == 1 and not shown_count),
                              '%s: incorrect separate process count %d: %r' % (name, i, shown_count))
                        value, unit = mw.memory_text(amount).rsplit(' ', 1)
                        check(text(canvas, tags[2]) == value and text(canvas, tags[3]) == unit,
                              '%s: value/unit columns lost real amount %d' % (name, i))
                else:
                    check(not canvas.find_withtag('proc_name_0'), name + ': disabled process table remains')

                representative = gpu and history and top5 and dpi == 1.5 and scale == .731
                if representative and model == FULL_MODEL:
                    before = set(canvas.find_all())
                    unpinned_image = canvas.itemcget('pin_head', 'image')
                    click(canvas, 'pin', name)
                    check(bool(mw.S['pin_top']) and bool(window.attributes('-topmost')),
                          name + ': pin click did not set topmost')
                    check(canvas.itemcget('pin_head', 'image') != unpinned_image,
                          name + ': pin appearance did not change')
                    click(canvas, 'pin', name)
                    check(not mw.S['pin_top'] and not bool(window.attributes('-topmost')),
                          name + ': second pin click did not clear topmost')
                    check(set(canvas.find_all()) == before and len(saved) == 2,
                          name + ': pin click rebuilt the card or missed persistence')
                    # Each visible piece of a process row remains an action
                    # target while the panel-level press handler declines it.
                    for part in ('name', 'count', 'mem', 'unit'):
                        before_clicks = len(process_clicks)
                        click(canvas, 'proc_%s_0' % part, name)
                        check(process_clicks[before_clicks:] == [0],
                              name + ': process %s did not reach its row action' % part)
                        check(app._move_start is None,
                              name + ': process %s press began a panel drag' % part)
                    for index in (1, 2, 0):
                        click(canvas, 'scale_%d' % index, name)
                        check(app.scale_index == index, name + ': range button failed %d' % index)
                        check(bool(canvas.find_withtag('chart_dynamic')),
                              name + ': clicked range produced empty graph %d' % index)
                if representative and not args.no_capture:
                    suffix = 'long-gpu' if model == LONG_MODEL else 'full'
                    path = (args.out / ('detail-layout-%s-%s.png' % (theme, suffix))).resolve()
                    bmp = path.with_suffix('.bmp')
                    sc.capture_window(canvas.winfo_id(), str(bmp), padding=0, settle=.05)
                    Image.open(bmp).save(path)
                    screenshots.append(str(path))
                evidence.append({'case': name, 'width': width, 'height': height,
                                 'gpu_text': text(canvas, 'gpu_name'),
                                 'gpu_bounds': canvas.bbox('gpu_name') if gpu else None})
                window.destroy()
    finally:
        root.destroy()
    result = {'cases': len(cases), 'checks': checks, 'failures': failures,
              'screenshots': screenshots, 'evidence': evidence}
    result_path = (args.out / 'detail-layout-check.json').resolve()
    result_path.write_text(json.dumps(result, ensure_ascii=False, indent=2), encoding='utf-8')
    for failure in failures[:25]:
        print('FAIL: ' + failure)
    print('DETAIL_LAYOUT_%s: %d cases / %d checks / %d failures; evidence %s' %
          ('OK' if not failures else 'FAIL', len(cases), checks, len(failures), result_path))
    return 1 if failures else 0


if __name__ == '__main__':
    raise SystemExit(main())
