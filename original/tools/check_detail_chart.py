"""Isolated renderer checks, benchmark and reference image; no Tk/app startup."""
import io
import math
from pathlib import Path
import statistics
import struct
import sys
import time
import zlib

from PIL import Image, ImageDraw

ROOT = Path(__file__).resolve().parent
SOURCE = ROOT.parent / "source"
sys.path.insert(0, str(SOURCE))
from detail_chart import chart_png
import memory_widget as mw

palettes = {
    "light": dict(card="#FFFFFF", orange="#F59D1F", grid="#F0F1F4",
                  chart_area="#FFF8EB", chart_band="#F7EBD8"),
    "dark": dict(card="#202125", orange="#F5A32B", grid="#2C2E34",
                 chart_area="#2C271F", chart_band="#3D3020"),
}


def decode(png, width, height):
    assert png[:8] == b"\x89PNG\r\n\x1a\n"
    offset, packed = 8, bytearray()
    while offset < len(png):
        length = struct.unpack(">I", png[offset:offset + 4])[0]
        kind = png[offset + 4:offset + 8]
        body = png[offset + 8:offset + 8 + length]
        checksum = struct.unpack(">I", png[offset + 8 + length:offset + 12 + length])[0]
        assert checksum == zlib.crc32(kind + body) & 0xFFFFFFFF
        if kind == b"IHDR":
            assert struct.unpack(">IIBBBBB", body) == (width, height, 8, 2, 0, 0, 0)
        elif kind == b"IDAT":
            packed.extend(body)
        offset += length + 12
    raw = zlib.decompress(packed)
    stride = width * 3 + 1
    assert len(raw) == height * stride
    assert all(raw[row * stride] == 0 for row in range(height))
    image = Image.open(io.BytesIO(png))
    image.load()
    assert image.mode == "RGB" and image.size == (width, height)
    return image


count = 0
for theme, colors in palettes.items():
    for scale in (0.7, 1.0, 1.15, 2.3, 6.0):
        w, h = round(292 * scale), round(77 * scale)
        for n in (0, 1, 2, 3, 6, 7, 72, 261):
            for index in (0, 1, 2):
                raw = [76.7 + .017 * i + .47 * math.sin(i * .24)
                       + .16 * math.sin(i * .8) for i in range(n)]
                line, low, high, bl, bh, _ = mw.trend_series(raw, index)
                png = chart_png(w, h, line, low, high, colors, pixel_scale=scale,
                                band_low=bl, band_high=bh, threshold=(low + high) / 2)
                decode(png, w, h)
                count += 1
        for values in ([0.0] * 10, [100.0] * 10, [50.0] * 10, [0.0, 100.0]):
            png = chart_png(w, h, values, 0, 100, colors, pixel_scale=scale,
                            band_low=[0.0] * len(values), band_high=[100.0] * len(values),
                            threshold=50)
            decode(png, w, h)
            count += 1

for dimensions in ((1, 1), (2, 2), (3, 2), (1, 77)):
    for values in ([], [50], [0, 100]):
        decode(chart_png(*dimensions, values, 0, 100, palettes["light"]), *dimensions)
        count += 1

for arguments in (
    dict(width=0), dict(width=8193), dict(width=4096, height=4096),
    dict(values=[float("nan")]), dict(view_low=float("nan")),
    dict(view_low=100, view_high=0), dict(pixel_scale=0),
    dict(band_low=[40]), dict(band_low=[40], band_high=[60]),
    dict(band_low=[60, 60], band_high=[40, 40]), dict(threshold=float("inf")),
    dict(values=[50] * 4097),
):
    kwargs = dict(width=292, height=77, values=[40, 60], view_low=0,
                  view_high=100, colors=palettes["light"])
    kwargs.update(arguments)
    try:
        chart_png(**kwargs)
    except ValueError:
        count += 1
    else:
        raise AssertionError(arguments)

flat = decode(chart_png(292, 77, [50] * 10, 0, 100, palettes["light"],
                        band_low=[40] * 10, band_high=[60] * 10, threshold=80), 292, 77)
assert flat.getpixel((100, 65)) == (255, 248, 235), "area missing"
assert flat.getpixel((100, 33)) == (247, 235, 216), "upper ribbon missing"
assert flat.getpixel((100, 43)) == (247, 235, 216), "lower ribbon missing"
assert len(set(flat.getdata())) > 12, "anti-aliasing absent"

out = ROOT.parent / "build" / "checks"
out.mkdir(parents=True, exist_ok=True)
board = Image.new("RGB", (620, 400), "#E8EBF0")
draw = ImageDraw.Draw(board)
for column, (theme, colors) in enumerate(palettes.items()):
    x = 10 + column * 310
    draw.rectangle((x - 3, 0, x + 300, 400), fill=colors["card"])
    for row, n in enumerate((72, 120, 261)):
        raw = [76.7 + .017 * i + .47 * math.sin(i * .24) + .16 * math.sin(i * .8)
               for i in range(n)]
        line, low, high, bl, bh, _ = mw.trend_series(raw, row)
        y = 28 + row * 126
        label = ("5 minute", "30 minute", "2 hours + 20-80% band")[row]
        draw.text((x, y - 18), label, fill="#626874" if theme == "light" else "#B4B9C4")
        image = decode(chart_png(292, 77, line, low, high, colors,
                                  band_low=bl, band_high=bh), 292, 77)
        board.paste(image, (x, y))
board.save(out / "detail-chart-light-dark.png")

for scale in (0.7, 1.0, 1.15, 2.3, 6.0):
    for index in (0, 2):
        raw = [76.7 + .017 * i + .47 * math.sin(i * .24) + .16 * math.sin(i * .8)
               for i in range(261)]
        line, low, high, bl, bh, _ = mw.trend_series(raw, index)
        durations = []
        for repetition in range(5):
            start = time.perf_counter()
            png = chart_png(round(292 * scale), round(77 * scale), line, low, high,
                            palettes["light"], pixel_scale=scale, band_low=bl, band_high=bh)
            durations.append((time.perf_counter() - start) * 1000)
        print(f"BENCH scale={scale} range={index} {round(292*scale)}x{round(77*scale)} "
              f"median_ms={statistics.median(durations):.2f} max_ms={max(durations):.2f} bytes={len(png)}")
print(f"PASS {count} renderer cases; CRC/PNG/native dimensions, short histories, "
      "flat/extreme/ribbon/threshold, input bounds and anti-aliasing")
