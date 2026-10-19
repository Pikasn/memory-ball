# -*- coding: utf-8 -*-
"""Non-invasive canvas and interaction check, run before packaging the EXE.

    python tools/check_memory_widget_ui.py

It builds every theme / orb size / feature combination at several display
scales, then verifies that no text escapes its canvas or collides with a
neighbouring element, that charts stay inside their plot box, that same-named
processes merge with their real count, and that the three trend views only ever
report what the samples actually did.
"""

import os
import sys
import time
import tkinter as tk

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, os.path.join(HERE, "..", "source"))

import memory_widget as mw                                   # noqa: E402


mw._enable_high_dpi()
# Read and write a scratch file so the check never depends on, or disturbs,
# whatever the shipped settings.json happens to contain.
mw.SETTINGS_PATH = os.path.join(HERE, "_check_settings.json")
mw.load_settings()
mw.save_settings = lambda: True      # the check must never touch the user file
# The layout matrix needs five rows independently of the host process list.
# Use synthetic groups so local permissions and running apps cannot affect it.
mw.read_top5 = lambda: [
    ("Worker.exe", 1536.0, 2),
    ("Editor.exe", 586.0, 1),
    ("Viewer.exe", 464.0, 1),
    ("Helper.exe", 300.0, 4),
    ("Service.exe", 210.0, 3),
]

failures = []


def check(condition, message):
    if not condition:
        failures.append(message)


def item_box(canvas, tag):
    items = canvas.find_withtag(tag)
    check(bool(items), f"missing canvas element: {tag}")
    if not items:
        return None
    return canvas.bbox(items[0])


def separated(first, second, name, gap=2):
    """True when the two boxes share no area, allowing a gap of `gap` px."""
    if not first or not second:
        return
    check(first[2] + gap <= second[0] or second[2] + gap <= first[0]
          or first[3] + gap <= second[1] or second[3] + gap <= first[1],
          f"{name}: elements overlap {first} {second}")


def stacked(upper, lower, name, gap=1):
    if not upper or not lower:
        return
    check(upper[3] + gap <= lower[1],
          f"{name}: vertical collision {upper} {lower}")


def all_text_inside(canvas, name):
    canvas.update_idletasks()
    for item in canvas.find_all():
        if canvas.type(item) != "text":
            continue
        box = canvas.bbox(item)
        if box is None:
            continue
        width, height = canvas.winfo_width(), canvas.winfo_height()
        text = canvas.itemcget(item, "text")
        check(box[0] >= 0 and box[2] <= width,
              f"{name}: '{text}' escapes horizontally {box} vs {width}")
        check(box[1] >= 0 and box[3] <= height,
              f"{name}: '{text}' escapes vertically {box} vs {height}")


# ---------------------------------------------------------------------------
# Trend honesty
# ---------------------------------------------------------------------------

flat = [82.0] * 200
for index in range(3):
    line, low, high, band_low, band_high, label = mw.trend_series(flat, index)
    check(max(line) - min(line) < 0.01,
          f"range {index}: flat input produced fake movement")
    check(high - low >= mw.RANGE_MIN_SPAN[index] - 1e-9,
          f"range {index}: view span below the declared floor")
    check(all(low - 1e-9 <= value <= high + 1e-9 for value in line),
          f"range {index}: line leaves the honest view range")

tiny = [82.0 + 0.02 * (i % 2) for i in range(200)]
line, low, high, _bl, _bh, _label = mw.trend_series(tiny, 0)
check(max(line) - min(line) > 0.0, "5 minute view lost a real 0.02 pt change")
check(high - low <= mw.RANGE_MIN_SPAN[0] + 1e-9,
      "5 minute view refused to zoom in on a small real change")

drift = [74.0 + index * 0.01 for index in range(1440)]
long_line, _ll, _lh, band_low, band_high, long_label = mw.trend_series(drift, 2)
check(long_label == "长期总览", "2 hour label changed")
check(max(long_line) - min(long_line) > 5.0,
      "2 hour view flattened a real 14 pt drift")
check(band_low is not None and len(band_low) == len(long_line),
      "2 hour percentile band missing or misaligned")
check(band_low and all(band_low[i] <= band_high[i] for i in range(len(band_low))),
      "2 hour percentile band is inverted")

medians = mw.trend_series(drift, 1)[0]
check(len(medians) < len(drift), "30 minute view did not bucket its samples")
check(max(medians) - min(medians) > 5.0,
      "30 minute view flattened a real 14 pt drift")

check(mw.trend_series([], 0)[0] == [], "empty samples must yield an empty line")
check(mw.trend_series([81.0], 0)[0] == [81.0], "single sample must pass through")

spiked = [80.0] * 360
spiked[180] = 96.0
mean_line = mw.trend_series(spiked, 2)[0]
median_line = mw.trend_series(spiked, 1)[0]
check(max(median_line) - min(median_line)
      < max(mean_line) - min(mean_line),
      "30 minute medians did not resist a single spike")

# ---------------------------------------------------------------------------
# Process grouping
# ---------------------------------------------------------------------------

merged = mw.group_processes([
    ("Worker.exe", 300.0), ("worker.EXE", 220.0),
    ("Worker.exe", 160.0), ("Editor.exe", 586.0), ("Viewer.exe", 464.0),
])
check(merged[0][0] == "Worker.exe" and merged[0][2] == 3,
      f"same-named processes were not merged with a real count: {merged[0]}")
check(abs(merged[0][1] - 680.0) < 1e-6, "merged working set was not summed")
check(merged[1][0] == "Editor.exe" and merged[1][2] == 1,
      "a single process must not be given a count")
check(len(mw.group_processes([("a.exe", 1.0)] * 7, limit=5)) == 1, "limit broken")
check(mw.group_processes([("", 10.0)]) == [], "empty names must be dropped")
check(len(mw.group_processes([("a.exe", 1.0)] * 9, limit=5)) == 1,
      "one group must never be split to fill five rows")

hint = mw.action_hint([("Editor.exe", 586.0, 1)])
check(hint == "关闭 Editor 可释放约 586 MB", f"unexpected hint wording: {hint}")
hint = mw.action_hint([("Worker.exe", 3584.0, 3)])
check(hint == "关闭 Worker ×3 可释放约 3.5 GB", f"unexpected hint: {hint}")
check("PID" not in mw.action_hint([("node.exe", 411.0, 19)]),
      "action hint must never mention a PID")
check(mw.memory_text(1023.6) == "1024 MB", "memory text rounding")
check(mw.memory_text(2048.0) == "2.0 GB", "memory text gigabytes")
check(mw.memory_text(1020.0) == "1020 MB", "values under 1024 MB must stay in MB")

# ---------------------------------------------------------------------------
# Live widgets, across display scales
# ---------------------------------------------------------------------------

SCALES = [(1.0, False), (1.25, False), (1.5, False), (2.0, False)]
main_scales = (1.0, 1.5)
combinations = []

for scale, _unused in SCALES:
    mw.UI_SCALE_OVERRIDE = scale
    themes = ("light", "dark") if scale in main_scales else ("light",)
    sizes = (64, 72, 96) if scale in main_scales else (72,)
    features = ((False, False), (True, False), (False, True),
                (True, True)) if scale in main_scales else ((True, True),)
    for theme in themes:
        for size in sizes:
            for history, top5 in features:
                mw.S.update(theme=theme, ring_size=size, history_enabled=history,
                            top5_enabled=top5, hover_expand=False,
                            autostart=False, poll_interval_ms=2000)
                name = f"s{scale:g}/{theme}/{size}/h{int(history)}/t{int(top5)}"
                # 进度打点：这项检查要建 50 个真实 Tk 窗口，跑几分钟。
                # 没有回显时"卡住了"和"还在跑"从外面分不出来，
                # 而这个工具恰恰是用来判断"界面到底对不对"的，不能靠猜。
                print(f"  [{len(combinations) + 1}/50] {name}", flush=True)
                widget = mw.MemoryWidget()
                try:
                    widget.last_memory = (82, 13.1, 15.7, 2.6)
                    widget._draw_ring(force=True)
                    if widget.history is not None:
                        now = time.monotonic()
                        widget.history.points.extend(
                            (now - (60 - index) * 5.0,
                             80.0 + index * 0.05 + (index % 7) * 0.03)
                            for index in range(60))
                        widget.history.revision += 1
                    widget._show_detail()
                    widget.open_settings()
                    widget.root.update()

                    device = scale
                    ring = widget.ring_canvas
                    expected = int(round(size * scale))
                    check(ring.winfo_width() == expected
                          and ring.winfo_height() == expected,
                          f"{name}: orb canvas is {ring.winfo_width()}px, "
                          f"expected {expected}")
                    all_text_inside(ring, f"{name} orb")
                    number = item_box(ring, "ring_number")
                    unit = item_box(ring, "ring_unit")
                    separated(number, unit, f"{name} orb number/unit", gap=0)
                    if size >= 72:
                        foot = item_box(ring, "ring_foot")
                        check(foot is not None, f"{name}: orb footer missing")
                        stacked(number, foot, f"{name} orb number/footer")
                        inner = (expected / 2.0 - 2.0 * scale) \
                            - max(4.5, expected * 0.082) \
                            - max(4.5, expected * 0.075) / 2.0
                        check(foot[3] <= expected / 2.0 + inner,
                              f"{name}: orb footer crosses the track {foot}")

                    detail = widget.detail_canvas
                    all_text_inside(detail, f"{name} detail")
                    for tag in ("detail_title", "hero_value", "hero_unit",
                                "status_pill", "status_text", "used_value",
                                "available_value", "close", "pin", "pin_bg"):
                        item_box(detail, tag)
                    # The pin and the close button share the title row: both are
                    # the same size, separated by a gap, clear of the title and
                    # inside the card.
                    title = item_box(detail, "detail_title")
                    pin = item_box(detail, "pin_bg")
                    close = item_box(detail, "close_bg")
                    card = item_box(detail, "detail_bg")
                    separated(title, pin, f"{name} title/pin", gap=8)
                    separated(pin, close, f"{name} pin/close", gap=4)
                    check(abs((pin[2] - pin[0]) - (close[2] - close[0])) <= 1,
                          f"{name}: pin {pin} and close {close} are not the "
                          f"same size")
                    check(close[2] <= card[2] - mw.D(mw.PAD - 4),
                          f"{name}: close button escapes the card {close}")
                    check(pin[0] > title[2],
                          f"{name}: the pin overlaps the title {pin} {title}")
                    # The pin glyph itself must sit inside its own button.
                    glyph = item_box(detail, "pin_head")
                    if glyph:
                        check(glyph[0] >= pin[0] and glyph[2] <= pin[2]
                              and glyph[1] >= pin[1] and glyph[3] <= pin[3],
                              f"{name}: pin glyph escapes its button "
                              f"{glyph} vs {pin}")
                    hero = item_box(detail, "hero_value")
                    pill = item_box(detail, "status_pill")
                    unit = item_box(detail, "hero_unit")
                    separated(hero, pill, f"{name} hero/status pill", gap=8)
                    separated(unit, pill, f"{name} hero unit/status pill", gap=8)
                    for index in range(2):
                        label = item_box(detail, f"stat_label_{index}")
                        value_tag = "used_value" if index == 0 else "available_value"
                        value = item_box(detail, value_tag)
                        unit = item_box(detail, value_tag + "_unit")
                        separated(label, value, f"{name} inline stat label/value")
                        separated(value, unit, f"{name} inline stat value/unit")
                        baselines = []
                        for tag in (f"stat_label_{index}", value_tag, value_tag + "_unit"):
                            descent = int(detail.tk.call(
                                "font", "metrics", detail.itemcget(tag, "font"), "-descent"))
                            baselines.append(detail.coords(tag)[1] - descent)
                        check(max(baselines) - min(baselines) <= 1,
                              f"{name}: inline stat pieces do not share a baseline {baselines}")
                    occupied = item_box(detail, "used_value_unit")
                    free = item_box(detail, "available_value")
                    separated(occupied, free, f"{name} used/available columns",
                              gap=6)
                    tick = item_box(detail, "warning_tick")
                    check(tick[0] >= mw.D(mw.PAD + 60),
                          f"{name}: warning tick is not near the警戒 point {tick}")

                    if history:
                        title = item_box(detail, "trend_title")
                        scale0 = item_box(detail, "scale_0")
                        separated(title, scale0,
                                  f"{name} trend title/segmented", gap=6)
                        x1, y1, x2, y2 = widget.chart_box
                        metadata = [item for item in detail.find_withtag("chart_dynamic")
                                    if detail.type(item) == "text"]
                        check(len(metadata) == 2,
                              f"{name}: chart must show its observed range and sampling/view metadata")
                        for item in metadata:
                            metadata_box = detail.bbox(item)
                            stacked(scale0, metadata_box, f"{name} segmented/chart metadata")
                            stacked(metadata_box, (x1, y1 + mw.D(30), x2, y2),
                                    f"{name} chart metadata/curve")
                        check(bool(detail.find_withtag("chart_dynamic")),
                              f"{name}: chart did not draw")
                        slack = mw.D(6)

                        def chart_inside(range_index):
                            for item in detail.find_withtag("chart_dynamic"):
                                box = detail.bbox(item)
                                if box is None:
                                    continue
                                check(box[1] >= y1 - slack and box[3] <= y2 + slack
                                      and box[0] >= x1 - slack
                                      and box[2] <= x2 + slack,
                                      f"{name}: range {range_index} chart "
                                      f"{detail.type(item)} escapes its plot box "
                                      f"{box} vs {(x1, y1, x2, y2)}")

                        chart_inside(0)
                        for scale_index in (1, 2):
                            widget._switch_scale(scale_index)
                            widget.root.update()
                            check(bool(detail.find_withtag("chart_dynamic")),
                                  f"{name}: chart empty at range {scale_index}")
                            chart_inside(scale_index)
                        widget._switch_scale(0)
                    else:
                        check(not detail.find_withtag("chart_dynamic"),
                              f"{name}: chart present while trends are off")

                    if top5:
                        for index in range(5):
                            rank = item_box(detail, f"proc_rank_{index}")
                            proc = item_box(detail, f"proc_name_{index}")
                            count = item_box(detail, f"proc_count_{index}")
                            memory = item_box(detail, f"proc_mem_{index}")
                            unit = item_box(detail, f"proc_unit_{index}")
                            separated(rank, proc, f"{name} rank/name {index}",
                                      gap=4)
                            separated(proc, count,
                                      f"{name} name/count {index}", gap=2)
                            separated(count, memory,
                                      f"{name} count/memory {index}", gap=8)
                            separated(memory, unit,
                                      f"{name} memory/unit {index}", gap=2)
                        hint_box = item_box(detail, "action_hint")
                        height = detail.winfo_height()
                        check(hint_box[3] + 6 * scale <= height,
                              f"{name}: action hint touches the card edge "
                              f"{hint_box} vs {height}")
                        check(hint_box[3] - hint_box[1] <= mw.D(22),
                              f"{name}: action hint wrapped to two lines "
                              f"{hint_box}")
                        check(hint_box[2] <= detail.winfo_width() - mw.D(mw.PAD),
                              f"{name}: action hint overflows {hint_box}")
                        meta = item_box(detail, "top5_meta")
                        proc_title = item_box(detail, "top5_title")
                        separated(proc_title, meta, f"{name} top5 title/meta",
                                  gap=8)

                        reads = []
                        original = mw.read_top5
                        # Sorted the way read_top5 returns it, with a name far
                        # too long for the row so truncation is exercised.
                        mw.read_top5 = lambda: reads.append(1) or [
                            ("Worker.exe", 1536.0, 2),
                            ("Editor.exe", 586.0, 1),
                            ("Viewer.exe", 464.0, 1),
                            ("An-Extremely-Long-Protected-Process-Name-That-"
                             "Cannot-Possibly-Fit.exe", 300.0, 4),
                            ("node.exe", 210.0, 3),
                        ]
                        try:
                            widget.top5_next_due = 0.0
                            widget._hide_detail()
                            widget._show_detail()
                            widget.root.update()
                            detail = widget.detail_canvas
                            check(len(reads) == 1,
                                  f"{name}: expected one process read on reopen, "
                                  f"got {len(reads)}")
                            # Re-rendering while the cache is still fresh must
                            # not touch the process list again.
                            widget._refresh_detail(force=True)
                            widget.root.update()
                            check(len(reads) == 1,
                                  f"{name}: Top 5 refetched without being due")
                            shown = detail.itemcget(
                                detail.find_withtag("proc_name_0")[0], "text")
                            shown_count = detail.itemcget("proc_count_0", "text")
                            check(shown == "Worker" and shown_count == "×2",
                                  f"{name}: name/count columns changed group display: {shown} {shown_count}")
                            check(widget.top5_cache[0] == ("Worker.exe", 1536.0, 2),
                                  f"{name}: visual display altered the underlying process group")
                            check(detail.itemcget("proc_mem_0", "text") == "1.5"
                                  and detail.itemcget("proc_unit_0", "text") == "GB",
                                  f"{name}: memory value/unit columns lost the grouped amount")
                            long_name = detail.itemcget(
                                detail.find_withtag("proc_name_3")[0], "text")
                            check(long_name.endswith("…")
                                  and len(long_name) < 60,
                                  f"{name}: long name not truncated: {long_name}")
                            memory_box = detail.bbox(
                                detail.find_withtag("proc_mem_3")[0])
                            count_box = detail.bbox("proc_count_3")
                            name_box = detail.bbox(
                                detail.find_withtag("proc_name_3")[0])
                            separated(name_box, count_box,
                                      f"{name}: truncated name/count", gap=2)
                            check(count_box[2] + mw.D(8) <= memory_box[0],
                                  f"{name}: truncated name/count still overlaps its value")
                            hint_text = detail.itemcget(
                                detail.find_withtag("action_hint")[0], "text")
                            check(hint_text == "关闭 Worker ×2 可释放约 1.5 GB",
                                  f"{name}: unexpected hint: {hint_text}")
                            for index in range(5):
                                value = detail.itemcget(
                                    detail.find_withtag(f"proc_name_{index}")[0],
                                    "text")
                                check("PID" not in value,
                                      f"{name}: PID placeholder in the list")
                            # An empty read must show a quiet empty state, not rows.
                            mw.read_top5 = lambda: []
                            widget.top5_next_due = 0.0
                            widget._refresh_detail(force=True)
                            widget.root.update()
                            empty_hint = detail.itemcget(
                                detail.find_withtag("action_hint")[0], "text")
                            check("未能读取" in empty_hint,
                                  f"{name}: empty state missing: {empty_hint}")
                            for part in ("name", "count", "mem", "unit"):
                                check(not detail.itemcget(f"proc_{part}_0", "text"),
                                      f"{name}: process {part} left over after empty read")
                        finally:
                            mw.read_top5 = original
                    else:
                        check(not detail.find_withtag("proc_name_0"),
                              f"{name}: process rows present while Top 5 is off")

                    settings = widget.settings_canvas
                    all_text_inside(settings, f"{name} settings")
                    title = item_box(settings, "settings_title")
                    subtitle = item_box(settings, "settings_subtitle")
                    stacked(title, subtitle, f"{name} settings header")
                    close = item_box(settings, "settings_close")
                    separated(subtitle, close, f"{name} settings header/done",
                              gap=8)
                    for key in ("pin_top", "hover_expand", "history_enabled",
                                "top5_enabled", "autostart"):
                        row_title = item_box(settings, f"toggle_title_{key}")
                        row_sub = item_box(settings, f"toggle_subtitle_{key}")
                        stacked(row_title, row_sub, f"{name} toggle {key} text")
                        switch = item_box(settings, f"switch_toggle_{key}")
                        separated(row_sub, switch,
                                  f"{name} toggle {key}/switch", gap=10)
                    for key in ("theme", "ring_size", "poll_interval_ms"):
                        row_title = item_box(settings, f"option_title_{key}")
                        row_sub = item_box(settings, f"option_subtitle_{key}")
                        stacked(row_title, row_sub, f"{name} option {key} text")
                        segment = settings.bbox(
                            settings.find_withtag(f"segment_{key}_0")[0])
                        separated(row_sub, segment,
                                  f"{name} option {key}/segmented", gap=10)
                        for index in range(3):
                            check(bool(settings.tag_bind(
                                f"segment_{key}_{index}", "<Button-1>")),
                                f"{name}: segment {key}/{index} is not clickable")
                    for index in range(2):
                        check(item_box(settings, f"group_label_{index}") is not None,
                              f"{name}: group label {index} missing")
                    footer = item_box(settings, "settings_footer")
                    check(footer is not None and
                          footer[3] + mw.D(10) <= settings.winfo_height(),
                          f"{name}: settings footer leaves no bottom margin "
                          f"{footer}")
                    check(settings.winfo_width() == mw.D(mw.SETTINGS_W)
                          and settings.winfo_height() == mw.D(mw.SETTINGS_H),
                          f"{name}: settings canvas is not "
                          f"{mw.D(mw.SETTINGS_W)}x{mw.D(mw.SETTINGS_H)}")
                    # The pane is shown at 100%..200% scale, so compare in
                    # design units: it must never grow taller than authored.
                    # The bound is deliberately just above SETTINGS_H - it is a
                    # tripwire against accidental growth, so raising it is a
                    # conscious act (last raised when 窗口置顶 added a 5th row).
                    check(mw.SETTINGS_H * scale <= 700 * scale,
                          "settings pane grew unexpectedly tall")
                    combinations.append(name)
                finally:
                    # ★ 两个定时器都要撤，**不能只撤 loop_job**。
                    #
                    # `watch_close_request` 是 120 ms 一次的高频关闭监听器
                    # （`WM_CLOSE` 能秒退就靠它）。销毁解释器前不撤掉，`after`
                    # 回调就留在已销毁的 Tcl 上，反复刷
                    #
                    #     invalid command name "...watch_close_request"
                    #
                    # 而每建一个新解释器都会再挂一个 async handler，前一个
                    # 被 destroy 时摘掉的正是"当前"那个 —— 于是游离的
                    # `Tcl_AsyncDelete: async handler deleted by the wrong
                    # thread` 把进程**在打印结论之前**中止掉，退出码
                    # 0x80000003。现象极具迷惑性：50 项组合全过、一条 FAIL
                    # 都没有、结论行却整行不见，`run_gates.py` 只看退出码于是
                    # 报 GATES_FAILED。实测 10 次里错 6 次、完全随机。
                    #
                    # 产品自身的 `quit()` 早就撤了它，漏的是这份测试脚手架。
                    for job_attr in ("loop_job", "watch_job"):
                        job = getattr(widget, job_attr, None)
                        if job:
                            try:
                                widget.root.after_cancel(job)
                            except tk.TclError:
                                pass
                            setattr(widget, job_attr, None)
                    widget.root.destroy()

# Palette hygiene: no blue accent may sneak back in either theme.
for palette_name, colours in mw.THEMES.items():
    for role, value in colours.items():
        red = int(value[1:3], 16)
        green = int(value[3:5], 16)
        blue = int(value[5:7], 16)
        check(not (blue > red + 24 and blue > green + 24),
              f"{palette_name}.{role}={value} reintroduces blue")

# ---------------------------------------------------------------------------
# Interaction round trip on a single instance
# ---------------------------------------------------------------------------

mw.UI_SCALE_OVERRIDE = 1.0
mw.S.update(theme="light", ring_size=72, history_enabled=False,
            top5_enabled=False, poll_interval_ms=2000)
widget = mw.MemoryWidget()
try:
    widget.open_settings()
    widget._set_option("ring_size", 96)
    widget.root.update()
    check(widget.ring_canvas.winfo_width() == 96, "orb resize did not apply")
    widget._set_option("theme", "dark")
    check(mw.theme_name() == "dark", "theme switch did not apply")
    widget._set_option("theme", "light")
    widget._toggle_setting("history_enabled")
    check(widget.history is not None, "history buffer was not created")
    widget._toggle_setting("history_enabled")
    check(widget.history is None, "history buffer was not released")
    widget._toggle_setting("top5_enabled")
    check(mw.S["top5_enabled"] is True, "Top 5 toggle did not apply")
    widget._toggle_setting("top5_enabled")
    widget._set_option("poll_interval_ms", 4000)
    check(mw.S["poll_interval_ms"] == 4000 and widget.loop_job,
          "poll interval did not reschedule")
    original = mw.monitor_work_area
    mw.monitor_work_area = (lambda x, _y:
                            (1920, 0, 3840, 1080) if x >= 1920
                            else (0, 0, 1920, 1080))
    try:
        check(widget._clamped_xy(2500, 90, 96, 96) == (2500, 90),
              "multi-monitor placement broken")
        check(widget._clamped_xy(-400, 90, 96, 96)[0] >= 8,
              "off-screen placement not clamped")
        check(widget._clamped_xy(2500, 5000, 96, 96)[1] <= 1080 - 96 - 8,
              "bottom placement not clamped")
    finally:
        mw.monitor_work_area = original
    widget._show_detail()
    widget.root.update()
    widget._hide_detail()
    check(widget.detail is None, "detail did not close")

    # --- the pin button: checked keeps the widget above other windows ---
    widget._show_detail()
    widget.root.update()
    widget._set_pin(False)
    for window in (widget.root, widget.detail):
        check(window.attributes("-topmost") == 0,
              "unpinned: a window is still topmost")
    widget._set_pin(True)
    for window in (widget.root, widget.detail):
        check(window.attributes("-topmost") == 1,
              "pinned: a window was left behind other windows")
    check(mw.S["pin_top"] is True, "the pin choice was not stored")
    # Clicking the glyph must go through the real button binding, not the panel
    # drag handler.
    canvas = widget.detail_canvas
    glyph = canvas.bbox("pin_head")
    canvas.event_generate("<Button-1>",
                          x=(glyph[0] + glyph[2]) // 2,
                          y=(glyph[1] + glyph[3]) // 2)
    widget.root.update()
    check(mw.S["pin_top"] is False,
          "clicking the pin glyph did not toggle it back off")
    check(widget.root.attributes("-topmost") == 0,
          "unpinning by click did not reach the window")
    check(widget._move_start is None,
          "a pin click was treated as the start of a panel drag")
    # The settings pane reaches the same state, and a fresh window picks up the
    # choice that is live at the time it is created.
    widget.open_settings()
    widget.root.update()
    widget._set_option("pin_top", True)
    widget.root.update()
    check(widget.settings_window.attributes("-topmost") == 1,
          "the settings pane ignored the pin setting")
    widget._set_option("pin_top", False)
    widget.root.update()
    check(widget.root.attributes("-topmost") == 0,
          "turning the pin off in settings left the orb on top")
    widget._hide_detail()
    widget._close_settings()
finally:
    # 同上：关闭监听器也要撤。只撤 loop_job 会留下游离的 `after` 回调，
    # 详见上面那段注释。
    for job_attr in ("loop_job", "watch_job"):
        job = getattr(widget, job_attr, None)
        if job:
            try:
                widget.root.after_cancel(job)
            except tk.TclError:
                pass
            setattr(widget, job_attr, None)
    widget.root.destroy()

# ---------------------------------------------------------------------------
# Rasteriser sanity
# ---------------------------------------------------------------------------

data = mw.plate_png(40, 24, 8, "#123456")
check(data.startswith(b"\x89PNG"), "plate rasteriser did not emit a PNG")
check(len(data) > 60, "plate rasteriser emitted an empty PNG")
check(mw.plate_png(40, 24, 8, "#123456") is data, "plate rasteriser is not cached")
check(mw.plate_png(38, 1, 0, "#EAEAEE").startswith(b"\x89PNG"),
      "hairline rasteriser failed")
meter = mw.bar_png(200, 10, 0.5, 0.85, mw.THEMES["light"])
check(meter.startswith(b"\x89PNG"), "meter rasteriser failed")
check(mw.bar_png(200, 10, 0.5, 0.85, mw.THEMES["light"]) is meter,
      "meter rasteriser is not cached")
for percent in (0, 1, 50, 99, 100):
    orb = mw.ring_png(64, mw.THEMES["light"], percent, "#F59D1F")
    check(orb.startswith(b"\x89PNG"), f"orb rasteriser failed at {percent}%")

# ---------------------------------------------------------------------------
# A Tk image belongs to one interpreter, and tk._default_root is only the
# first root a process ever created. This process has already made and
# destroyed several roots above, so building a widget here proves the image
# cache follows the widget's own interpreter rather than the default root.
# Before that was fixed, this raised "image pyimage1 doesn't exist".
# ---------------------------------------------------------------------------

alien = tk.Tk()
alien.withdraw()
alien.update_idletasks()
mw.S.update({"theme": "dark", "history_enabled": True, "top5_enabled": True})
try:
    foreign = mw.MemoryWidget()
    foreign.root.update_idletasks()
    check(foreign.ring_photo.width() > 0,
          "ring image was not built on the widget's own interpreter")
    foreign._show_detail()
    foreign.root.update_idletasks()
    detail_images = [item for item in foreign.detail_canvas.find_all()
                     if foreign.detail_canvas.type(item) == "image"]
    check(len(detail_images) > 4,
          f"detail panel lost its layered images ({len(detail_images)})")
    foreign.open_settings()
    foreign.root.update_idletasks()
    settings_images = [item for item in foreign.settings_canvas.find_all()
                       if foreign.settings_canvas.type(item) == "image"]
    check(len(settings_images) > 8,
          f"settings panel lost its layered images ({len(settings_images)})")
    foreign._refresh_detail()
    foreign.root.update_idletasks()
    check(mw._PHOTO_MASTER is not None,
          "photo cache did not record the interpreter it belongs to")
    foreign.quit()
except tk.TclError as error:
    check(False, f"widget failed next to an existing Tk root: {error}")
finally:
    alien.destroy()

# ---------------------------------------------------------------------------
# The detail panel is resizable: dragging its bottom-right corner scales the
# whole UI together. Every design unit must grow by `detail_scale`, so text and
# plates stay in the same proportions at any size.
# ---------------------------------------------------------------------------

mw.S.update({"theme": "light", "history_enabled": True, "top5_enabled": True})
resizer = mw.MemoryWidget()
resizer._poll_once()
resizer._show_detail()
try:
    base_w = resizer._detail_width()
    base_h = resizer._detail_height()
    base_hero = item_box(resizer.detail_canvas, "hero_value")
    for scale in (0.7, 1.0, 1.5, 2.0):
        resizer.detail_scale = scale
        resizer._reflow_detail()
        canvas = resizer.detail_canvas
        check(resizer._detail_width() == int(round(base_w * scale)),
              f"scale {scale}: width did not scale proportionally")
        check(resizer._detail_height() == int(round(base_h * scale)),
              f"scale {scale}: height did not scale proportionally")
        all_text_inside(canvas, f"detail scale {scale}")
        hero = item_box(canvas, "hero_value")
        card = item_box(canvas, "detail_bg")
        if hero and card:
            check(hero[0] >= card[0] and hero[2] <= card[2],
                  f"scale {scale}: hero escapes the card {hero} vs {card}")
        if scale != 1.0 and base_hero:
            ratio = (hero[3] - hero[1]) / max(1, base_hero[3] - base_hero[1])
            check(0.6 * scale < ratio < 1.5 * scale,
                  f"scale {scale}: hero font did not scale ({ratio:.2f})")
        # The grip lives in the same bottom band as the action hint; at the
        # smallest scale they are closest together.
        hint = item_box(canvas, "action_hint")
        grip = item_box(canvas, "resize_grip")
        if hint and grip and canvas.itemcget("action_hint", "text"):
            separated(hint, grip, f"scale {scale}: hint vs resize grip", gap=4)

    # Dragging sweeps through many panel sizes, and each size has its own
    # plates. Once the image memo filled up it stopped caching, every new
    # plate became an unreferenced temporary, tkinter collected it, the Tcl
    # image was deleted - and the whole panel rendered transparent. So after a
    # long drag, every image item must still name a *living* Tcl image.
    dead = 0
    for step in range(40):
        resizer.detail_scale = round(mw.MIN_DETAIL_SCALE + 0.04 * step, 3)
        resizer._reflow_detail()
        canvas = resizer.detail_canvas
        living = set(canvas.tk.eval("image names").split())
        for item in canvas.find_all():
            if canvas.type(item) == "image":
                if canvas.itemcget(item, "image") not in living:
                    dead += 1
    check(dead == 0,
          f"after 40 resizes {dead} canvas images referenced a collected "
          f"PhotoImage (the panel would render transparent)")
    resizer.detail_scale = 1.0
    resizer._reflow_detail()
    # The global detail scale must always reset after drawing.
    check(mw._DETAIL_SCALE == 1.0,
          "detail scale leaked out of the drawing methods")

    # The panel background is a handle: press anywhere that is not a button,
    # drag, and the panel moves with the pointer. A press on a button (close,
    # the range switch, the grip) must be left to that button instead.
    canvas = resizer.detail_canvas

    def drag_by(dx, dy):
        hero = canvas.find_withtag("hero_value")[0]
        box = canvas.bbox(hero)
        cx = (box[0] + box[2]) // 2
        cy = (box[1] + box[3]) // 2
        rx = canvas.winfo_rootx() + cx
        ry = canvas.winfo_rooty() + cy
        canvas.event_generate("<Button-1>", x=cx, y=cy)
        resizer.root.update()
        for step in range(1, 11):
            canvas.event_generate("<B1-Motion>", x=cx + dx * step // 10,
                                  y=cy + dy * step // 10,
                                  rootx=rx + dx * step // 10,
                                  rooty=ry + dy * step // 10)
            resizer.root.update()
        canvas.event_generate("<ButtonRelease-1>", x=cx + dx, y=cy + dy,
                              rootx=rx + dx, rooty=ry + dy)
        resizer.root.update()

    before = (resizer.detail.winfo_x(), resizer.detail.winfo_y())
    drag_by(-120, 40)
    after = (resizer.detail.winfo_x(), resizer.detail.winfo_y())
    check(after == (before[0] - 120, before[1] + 40),
          f"panel drag moved it to {after}, expected "
          f"{(before[0] - 120, before[1] + 40)}")
    check(isinstance(mw.S["panel_offset"], list),
          "a panel drag did not persist its offset")
    # The panel keeps the offset relative to the ring when the ring moves,
    # clamped to whichever monitor it lands on.
    ring_before = (resizer.root.winfo_x(), resizer.root.winfo_y())
    resizer.root.geometry("+1500+400")
    resizer.root.update_idletasks()
    resizer.root.update()
    ring_after = (resizer.root.winfo_x(), resizer.root.winfo_y())
    check(ring_after != ring_before,
          f"the ring window did not move for the offset test {ring_before}")
    resizer._place_detail()
    resizer.root.update()          # let Tk process the geometry change
    off = mw.S["panel_offset"]
    width = resizer._detail_width()
    height = resizer._detail_height()
    want_x, want_y = resizer._clamped_xy(
        ring_after[0] + off[0], ring_after[1] + off[1], width, height,
        anchor=(ring_after[0] + off[0] + width // 2,
                ring_after[1] + off[1] + height // 2),
    )
    got = (resizer.detail.winfo_x(), resizer.detail.winfo_y())
    check(got == (want_x, want_y),
          f"the panel did not keep its ring-relative offset: ring {ring_after} "
          f"offset {off} panel {got} expected {(want_x, want_y)} "
          f"scale {resizer.detail_scale} ui {mw.UI_SCALE_OVERRIDE}")

    # Which tags give the press to the button instead of the mover. An empty
    # tag list means "no item under the pointer", which the caller treats as
    # interactive before this rule ever runs.
    for tags, blocks in ((("close", "close_bg"), True),
                         (("pin", "pin_bg"), True),
                         (("scale_0", "scale_bg_0"), True),
                         (("resize_grip", "resize_grip_hit"), True),
                         (("detail_bg",), False),
                         (("proc_name_0",), True),
                         (("proc_count_0",), True),
                         (("proc_mem_0",), True),
                         (("proc_unit_0",), True),
                         ((), False)):
        check(resizer._tags_block_move(tags) is blocks,
              f"tags {tags} were classified as "
              f"{'interactive' if blocks else 'draggable'} incorrectly")

    # --- Top 5 rows are interactive: click a row, get the process actions ---
    resizer.top5_cache = [("notepad.exe", 12.5, 2)]
    resizer._refresh_top5(force=True)
    resizer.root.update_idletasks()
    canvas = resizer.detail_canvas
    killed_calls = []
    real_terminate = mw.terminate_process_group
    mw.terminate_process_group = (lambda name: killed_calls.append(name)
                                  or (2, 0, None))
    mw.read_top5 = lambda: [("notepad.exe", 12.5, 2)]
    try:
        row = canvas.bbox("proc_name_0")
        cx = (row[0] + row[2]) // 2
        cy = (row[1] + row[3]) // 2
        canvas.event_generate("<Button-1>", x=cx, y=cy)
        resizer.root.update()
        menu = resizer.context_window
        check(menu is not None, "clicking a Top 5 row opened no menu")
        if menu is not None:
            mcanvas = menu.winfo_children()[0]
            labels = [mcanvas.itemcget(item, "text")
                      for item in mcanvas.find_all()
                      if mcanvas.type(item) == "text"
                      and mcanvas.itemcget(item, "text")]
            check(any(label.startswith("结束") for label in labels),
                  f"process menu has no end action: {labels}")
            check("复制进程名" in labels,
                  f"process menu has no copy action: {labels}")
            kill = [item for item in mcanvas.find_all()
                    if mcanvas.type(item) == "text"
                    and str(mcanvas.itemcget(item, "text")).startswith("结束")]
            box = mcanvas.bbox(kill[0])
            mcanvas.event_generate("<Button-1>",
                                   x=(box[0] + box[2]) // 2,
                                   y=(box[1] + box[3]) // 2)
            resizer.root.update()
            check(killed_calls == ["notepad.exe"],
                  f"ending the group called with {killed_calls}")
            check(not resizer.context_window,
                  "the process menu stayed open after choosing an action")
    finally:
        mw.terminate_process_group = real_terminate
        resizer._close_context_menu()

    # Ending a protected group is refused before any process is touched.
    for name, reason in (("csrss.exe", True), ("svchost.exe", True),
                         ("MemoryWidget.exe", True), ("notepad.exe", False)):
        blocked = mw._block_reason(name)
        check(bool(blocked) is reason,
              f"{name}: guard verdict {blocked!r} contradicts {reason}")
finally:
    for job_attr in ("loop_job", "watch_job"):
        job = getattr(resizer, job_attr, None)
        if job:
            try:
                resizer.root.after_cancel(job)
            except tk.TclError:
                pass
            setattr(resizer, job_attr, None)
    try:
        resizer.root.destroy()
    except tk.TclError:
        pass

if failures:
    print(f"UI_CHECK_FAILED: {len(failures)} problem(s)")
    for problem in failures[:40]:
        print("  -", problem)
else:
    print(f"UI_CHECK_OK: {len(combinations)} theme/size/feature/scale combinations")
    print("Trend honesty, process merging, text bounds, no-overlap, chart "
          "bounds, Top 5 cache, controls, monitor placement and rasterisers: OK")

# ★ 用 `os._exit` 结束，**不要走正常解释器关闭**。
#
# 这个脚本一轮建销 50 个 `MemoryWidget`，另外再建一个 `resizer`。每个实例都
# 挂过 Tk，而 Tk 的 async handler 是**进程级**的：销毁后到解释器真正收尾之间
# 只要还有别的线程在跑（尤其是从 `subprocess` 里退出来的线程，它的退场清理
# 会走 GC），就会打出
#
#     Tcl_AsyncDelete: async handler deleted by the wrong thread
#
# 并把进程**在打印结论之前**中止，退出码 0x80000003。现象极具迷惑性：
# 50 项组合全过、一条 FAIL 都没有、结论行整行不见，而 `run_gates.py` 只看
# 退出码，于是拿着正确的输出报 GATES_FAILED。实测 16 次里错 5 次、完全随机，
# 曾据此误判成"门槛突然坏了"。
#
# 触发它的两个源头都已经从根上修掉了（`gpu_info` 不再重复起线程、查询不再挂
# 管道读线程、实例销毁时停采样线程），但**这段 `_exit` 仍然要留着**：
# 它把"结论已经算完"和"进程怎么结束"彻底解耦，退出码不会再被收尾阶段的
# 任何竞态改写。`flush` 是必须的 —— `_exit` 不做缓冲区刷写。
sys.stdout.flush()
sys.stderr.flush()
os._exit(1 if failures else 0)
