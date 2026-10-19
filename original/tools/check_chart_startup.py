"""Exercise the real Tk chart renderer without starting the memory widget.

Usage: python tools/check_chart_startup.py [source/memory_widget.py]
No user settings are loaded/saved; no tray, GPU worker or application loop runs.
The only Tk window is withdrawn and is destroyed on the creating thread.
"""

from __future__ import annotations

import argparse
import importlib.util
import math
from pathlib import Path
import sys
import tkinter as tk
from types import SimpleNamespace


def load_source(path: Path):
    spec = importlib.util.spec_from_file_location("memory_widget_chart_test", path)
    module = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    return module


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("source", type=Path, nargs="?",
                        default=Path(__file__).resolve().parents[1] / "source" / "memory_widget.py")
    args = parser.parse_args()
    mw = load_source(args.source.resolve())
    # Keep all settings and state isolated in this test process.
    mw.S = dict(mw.S)
    mw.S["theme"] = "light"
    failures = []
    cases = 0
    root = tk.Tk()
    root.withdraw()
    canvas = tk.Canvas(root, width=1600, height=600)
    canvas.pack()
    widget = mw.MemoryWidget.__new__(mw.MemoryWidget)
    widget.detail_canvas = canvas
    widget.last_chart_revision = -1
    widget.chart_photo = None
    render_calls = []
    original_chart_png = mw.chart_png

    def record_chart_png(*arguments, **keywords):
        render_calls.append((arguments, keywords))
        return original_chart_png(*arguments, **keywords)

    mw.chart_png = record_chart_png

    def check(condition: bool, name: str, detail: str):
        if not condition:
            failures.append(f"{name}: {detail}")

    try:
        # In particular, raw 2 points -> 1 median; raw 2..6 -> 1 mean/band.
        # These combinations were missed by the existing 200-point UI checks.
        sample_counts = (0, 1, 2, 3, 4, 5, 6, 7, 12, 90, 260, 1441)
        value_patterns = {
            "flat": lambda count: [82.0] * count,
            "jitter": lambda count: [82.0 + (i % 2) * 0.02 for i in range(count)],
            "boundary": lambda count: [0.0 if i % 2 == 0 else 100.0 for i in range(count)],
        }
        for dpi in (1.0, 1.5, 2.0):
            mw.UI_SCALE = dpi
            for panel_scale in (0.7, 1.0, 2.0):
                widget.detail_scale = panel_scale
                factor = dpi * panel_scale
                widget.chart_box = (22 * factor, 16 * factor, 318 * factor, 112 * factor)
                for range_index in range(3):
                    widget.scale_index = range_index
                    for count in sample_counts:
                        for pattern, make_values in value_patterns.items():
                            values = make_values(count)
                            line, low, high, bl, bh, _label = mw.trend_series(values, range_index)
                            widget.history = SimpleNamespace(revision=cases,
                                                             get=lambda *a, _values=values: _values)
                            name = f"dpi={dpi} scale={panel_scale} range={range_index} n={count} {pattern}"
                            cases += 1
                            before_scale = mw._DETAIL_SCALE
                            render_calls.clear()
                            try:
                                widget._draw_chart()
                            except Exception as exc:
                                failures.append(f"{name}: {type(exc).__name__}: {exc}")
                                check(mw._DETAIL_SCALE == before_scale, name, "detail scale leaked on error")
                                continue
                            check(mw._DETAIL_SCALE == before_scale, name, "detail scale leaked")
                            check(widget.last_chart_revision == widget.history.revision,
                                  name, "chart revision was not recorded")
                            items = canvas.find_withtag("chart_dynamic")
                            check(bool(items), name, "no waiting message or graph was drawn")
                            drawable = len(line) >= 2
                            image_items = [item for item in items if canvas.type(item) == "image"]
                            text_items = [item for item in items if canvas.type(item) == "text"]
                            for item in items:
                                coords = canvas.coords(item)
                                check(all(math.isfinite(c) for c in coords), name,
                                      f"non-finite coordinates: {coords}")
                            check(all(canvas.type(item) in ("text", "image") for item in items),
                                  name, "unexpected chart canvas representation")
                            if drawable:
                                check(len(image_items) == 1, name, "drawable history needs one native raster")
                                check(len(text_items) == 2, name, "range/algorithm metadata missing")
                                check(len(render_calls) == 1, name, "raster helper not called exactly once")
                                x1, y1, x2, y2 = widget.chart_box
                                top = y1 + round(30 * factor)
                                expected_width = int(x2 - x1)
                                expected_height = int(y2 - top + 1)
                                if len(image_items) == 1:
                                    item = image_items[0]
                                    image_name = canvas.itemcget(item, "image")
                                    check(widget.chart_photo is not None and image_name == str(widget.chart_photo),
                                          name, "canvas raster has no corresponding retained PhotoImage")
                                    actual_width = int(root.tk.call("image", "width", image_name))
                                    actual_height = int(root.tk.call("image", "height", image_name))
                                    check((actual_width, actual_height) == (expected_width, expected_height),
                                          name, "native raster dimensions differ from plot dimensions")
                                    check(canvas.coords(item) == [x1, top], name, "raster origin shifted")
                                    box = canvas.bbox(item)
                                    # Canvas rounds fractional placement to device pixels. The plot's
                                    # bottom coordinate is inclusive, hence the intentional +1 image row.
                                    check(box is not None and box[0] >= math.floor(x1)
                                          and box[1] >= math.floor(top)
                                          and box[2] <= math.ceil(x2)
                                          and box[3] <= math.ceil(y2) + 1,
                                          name, f"native raster escapes plot frame: {box}")
                                if len(render_calls) == 1:
                                    arguments, keywords = render_calls[0]
                                    check(arguments[2] == line and arguments[3:5] == (low, high),
                                          name, "raster received different trend data/view range")
                                    check(keywords.get("band_low") == bl and keywords.get("band_high") == bh,
                                          name, "percentile bands were lost or altered")
                                    check(keywords.get("pixel_scale") == factor,
                                          name, "native stroke scale differs from display/detail scale")
                                    check(keywords.get("threshold") == float(mw.S["warn_red"]),
                                          name, "warning threshold was lost")
                            else:
                                check(len(items) == len(text_items) == 1 and not image_items,
                                      name, "waiting history must contain one message and no raster")
                                check(not render_calls, name, "waiting history invoked raster helper")
                                if len(text_items) == 1:
                                    check("正在积累趋势数据" in canvas.itemcget(text_items[0], "text"),
                                          name, "waiting message changed or missing")
                            check(not line or all(low <= p <= high for p in line), name,
                                  "trend escaped raw sample range")
                            if bl is not None:
                                check(len(bl) == len(bh) == len(line), name, "band lengths differ")
                                check(all(a <= b for a, b in zip(bl, bh)), name, "band inverted")
        history = mw.History()
        check(history.push(82, 100), "history", "first sample rejected")
        check(not history.push(83, 104.9), "history", "sample interval ignored")
        check(history.push(84, 105), "history", "5-second sample rejected")
        check(history.get(3, 106) == [84], "history", "time-window filter broken")
        for i in range(history.MAX_POINTS + 10):
            history.push(float(i % 100), 110 + i * 5)
        check(len(history.points) == history.MAX_POINTS, "history", "history unbounded")
        compact = history.get(7200, history.points[-1][0], limit=260)
        check(len(compact) <= 261, "history", "compaction did not bound retained samples")
        check(compact[-1] == history.points[-1][1], "history", "newest sample lost")
    finally:
        mw.chart_png = original_chart_png
        # No foreign thread ever owns references to this interpreter.
        root.destroy()

    if failures:
        print(f"FAIL: {len(failures)} failures in {cases} renderer cases")
        for failure in failures[:20]:
            print(failure)
        return 1
    print(f"PASS: {cases} real Tk renderer cases; zero/single/bucketed/multiple points, "
          "all 3 ranges, DPI and panel scales, ribbons and bounded history")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
