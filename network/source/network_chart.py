"""Opaque antialiased network history PNGs, with one shared bytes/s axis.

Dimensions are physical pixels. Samples carry monotonic timestamps; ``None``
rates and long gaps break curves. No Tk objects or persistent caches are used.
Pillow draws at twice the physical pixel density in native code, followed by
Lanczos downsampling. All samples and their peaks remain part of the path.
"""

from __future__ import annotations

import math
from itertools import islice

from PIL import Image, ImageDraw

from detail_chart import _MAX_PIXELS, _MAX_SIDE, _MAX_SAMPLES, _rgb, _png


_SUPERSAMPLE = 2


def network_png(width, height, samples, start_time, end_time, axis_max, colors,
                pixel_scale=1.0, gap_seconds=2.5):
    """Return ``width × height`` RGB PNG for download and upload byte rates.

    ``samples`` are ``(timestamp, download_bps_or_None, upload_bps_or_None)``
    ordered by time. Download uses ``colors['orange']`` and ``chart_area``;
    upload uses optional ``network_upload`` or ``secondary``. Both use the
    exact same 0..``axis_max`` scale. Values over axis_max are visually clipped;
    the caller should select a suitable maximum and show its numeric label.
    Empty histories render a grid, and a lone sample draws at its real time.
    """
    width, height = int(width), int(height)
    if (width < 1 or height < 1 or width > _MAX_SIDE or height > _MAX_SIDE
            or width * height > _MAX_PIXELS):
        raise ValueError("Network chart dimensions must be positive and bounded")
    start, end, maximum = float(start_time), float(end_time), float(axis_max)
    scale, gap = float(pixel_scale), float(gap_seconds)
    if (not all(math.isfinite(value) for value in (start, end, maximum, scale, gap))
            or end <= start or maximum <= 0.0 or not 0.25 <= scale <= 8.0
            or gap <= 0.0):
        raise ValueError("Invalid network chart time, axis, scale, or gap")
    # Read only enough to establish the cap; do not exhaust an oversized or
    # endless iterable before rejecting it.
    source = [] if samples is None else list(islice(samples, _MAX_SAMPLES + 1))
    if len(source) > _MAX_SAMPLES:
        raise ValueError("Too many network chart samples")
    normalized = []
    previous = None
    for point in source:
        if len(point) != 3:
            raise ValueError("Network chart samples need timestamp and two rates")
        timestamp = float(point[0])
        rates = tuple(None if value is None else float(value) for value in point[1:])
        if (not math.isfinite(timestamp)
                or any(value is not None and (not math.isfinite(value) or value < 0)
                       for value in rates)
                or previous is not None and timestamp <= previous):
            raise ValueError("Network chart samples must be finite and ordered")
        previous = timestamp
        if start <= timestamp <= end:
            normalized.append((timestamp, *rates))

    background = _rgb(colors["card"])
    grid = _rgb(colors.get("grid", colors["card"]))
    download = _rgb(colors["orange"])
    upload = _rgb(colors.get("network_upload", colors["secondary"]))
    area = _rgb(colors.get("chart_area", colors["card"]))
    image = Image.new("RGB", (width * _SUPERSAMPLE, height * _SUPERSAMPLE), background)
    draw = ImageDraw.Draw(image)
    radius = min(2.0 * scale, max(0.25, (min(width, height) - 1) / 4.0))
    margin_x = min(radius + 0.5, max(0.0, (width - 1) / 2.0))
    margin_y = min(radius + 0.5, max(0.0, (height - 1) / 2.0))
    left, right = margin_x, width - 1.0 - margin_x
    top, bottom = margin_y, height - 1.0 - margin_y
    def path(points):
        return [(x * _SUPERSAMPLE, y * _SUPERSAMPLE) for x, y in points]

    def horizontal(y):
        draw.line(((0, y * _SUPERSAMPLE), (width * _SUPERSAMPLE, y * _SUPERSAMPLE)),
                  fill=grid, width=max(1, round(max(0.5, 0.6 * scale) * _SUPERSAMPLE)))

    def circle(point, circle_radius, ink):
        x, y = point
        draw.ellipse(((x - circle_radius) * _SUPERSAMPLE,
                      (y - circle_radius) * _SUPERSAMPLE,
                      (x + circle_radius) * _SUPERSAMPLE,
                      (y + circle_radius) * _SUPERSAMPLE), fill=ink)

    for y in dict.fromkeys((top, (top + bottom) / 2.0, bottom)):
        horizontal(y)

    def segments(value_index):
        result, segment = [], []
        last_time = None
        for point in normalized:
            timestamp, value = point[0], point[value_index]
            if (value is None or last_time is not None and timestamp - last_time > gap):
                if segment:
                    result.append(segment)
                    segment = []
            if value is not None:
                segment.append((left + (right - left) * (timestamp - start) / (end - start),
                                bottom - (bottom - top) * min(1.0, value / maximum)))
            last_time = timestamp
        if segment:
            result.append(segment)
        return result

    down_segments, up_segments = segments(1), segments(2)
    for points in down_segments:
        if len(points) > 1 and right > left:
            draw.polygon(path(points + [(points[-1][0], bottom), (points[0][0], bottom)]),
                         fill=area)
    # Keep the zero baseline visible below a download area's translucent tint.
    horizontal(bottom)
    thickness = max(0.7, 1.35 * scale)
    for paths, stroke_color, value_index in (
            (down_segments, download, 1), (up_segments, upload, 2)):
        for points in paths:
            if len(points) >= 2 and right > left:
                draw.line(path(points), fill=stroke_color,
                          width=max(1, round(thickness * _SUPERSAMPLE)), joint="curve")
                # Rounded caps are native draws; no scan over every row in a
                # steep column and no width-sized arrays for disconnected paths.
                circle(points[0], thickness / 2.0, stroke_color)
                circle(points[-1], thickness / 2.0, stroke_color)
            else:
                circle(points[0], max(0.35, 0.8 * scale), stroke_color)
        if paths and normalized and normalized[-1][value_index] is not None:
            endpoint = paths[-1][-1]
            circle(endpoint, radius, background)
            circle(endpoint, max(0.2, radius - 0.9 * scale), stroke_color)
    image = image.resize((width, height), Image.Resampling.LANCZOS)
    return _png(width, height, image.tobytes())
