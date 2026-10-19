"""Small, dependency-free, anti-aliased trend-chart PNG renderer.

Dimensions are already physical pixels. ``pixel_scale`` changes stroke widths
and endpoint size, not the image dimensions. Values are the existing trend
algorithm's output; the quadratic interpolation only draws its smooth path.
No Tk objects, threads, files or persistent caches are created here.
"""

from __future__ import annotations

from bisect import bisect_right
import math
import struct
import zlib


_MAX_PIXELS = 4_194_304
_MAX_SIDE = 8192
_MAX_SAMPLES = 4096
_X_SAMPLES = (0.125, 0.375, 0.625, 0.875)


def _rgb(color):
    if isinstance(color, str):
        value = color.lstrip("#")
        if len(value) == 3:
            value = "".join(letter * 2 for letter in value)
        if len(value) != 6:
            raise ValueError("chart colors must be #RRGGBB or #RGB")
        return tuple(int(value[i:i + 2], 16) for i in (0, 2, 4))
    value = tuple(int(channel) for channel in color)
    if len(value) != 3 or any(channel < 0 or channel > 255 for channel in value):
        raise ValueError("chart RGB colors need three bytes")
    return value


def _series(values):
    if values is None:
        return []
    if len(values) > _MAX_SAMPLES:
        raise ValueError("too many chart samples")
    result = [float(value) for value in values]
    if any(not math.isfinite(value) for value in result):
        raise ValueError("chart samples must be finite")
    return result


def _blend(pixels, offset, color, coverage):
    if coverage <= 0.0:
        return
    if coverage >= 1.0:
        pixels[offset:offset + 3] = bytes(color)
        return
    inverse = 1.0 - coverage
    pixels[offset] = round(pixels[offset] * inverse + color[0] * coverage)
    pixels[offset + 1] = round(pixels[offset + 1] * inverse + color[1] * coverage)
    pixels[offset + 2] = round(pixels[offset + 2] * inverse + color[2] * coverage)


def _column(pixels, width, height, x, top, bottom, color):
    """Fill a solid vertical span with C-level strided byte assignments."""
    top, bottom = max(0, int(top)), min(height, int(bottom))
    count = bottom - top
    if count <= 0:
        return
    stride = width * 3
    start = (top * width + x) * 3
    stop = start + stride * count
    for channel, value in enumerate(color):
        pixels[start + channel:stop + channel:stride] = bytes((value,)) * count


def _horizontal(pixels, width, height, center, thickness, color):
    top, bottom = center - thickness / 2.0, center + thickness / 2.0
    for y in range(max(0, math.floor(top)), min(height, math.ceil(bottom))):
        coverage = max(0.0, min(y + 1.0, bottom) - max(float(y), top))
        start = y * width * 3
        # Grid lines are drawn on the uniform background before the fill.
        base = pixels[start:start + 3]
        mixed = bytes(round(base[c] * (1.0 - coverage) + color[c] * coverage)
                      for c in range(3))
        pixels[start:start + width * 3] = mixed * width


def _smooth(points):
    """Tk-like quadratic segments through adjacent midpoints (16 steps)."""
    if len(points) < 3:
        return points
    previous = points[0]
    result = [previous]
    for index, control in enumerate(points[1:-1], 1):
        following = points[index + 1]
        end = ((control[0] + following[0]) / 2.0,
               (control[1] + following[1]) / 2.0)
        for step in range(1, 17):
            t = step / 16.0
            remaining = 1.0 - t
            result.append((remaining * remaining * previous[0]
                           + 2.0 * remaining * t * control[0] + t * t * end[0],
                           remaining * remaining * previous[1]
                           + 2.0 * remaining * t * control[1] + t * t * end[1]))
        previous = end
    result.append(points[-1])
    return result


def _columns(points, width):
    """Four horizontal subpixel samples; no full supersampled bitmap."""
    xs = [point[0] for point in points]
    left, right = xs[0], xs[-1]
    result = []
    for x in range(width):
        samples = []
        for fraction in _X_SAMPLES:
            position = x + fraction
            if position < left or position > right:
                samples.append(None)
                continue
            index = min(len(points) - 2, max(0, bisect_right(xs, position) - 1))
            x1, y1 = points[index]
            x2, y2 = points[index + 1]
            slope = (y2 - y1) / max(1e-12, x2 - x1)
            samples.append((y1 + (position - x1) * slope, slope))
        result.append(samples)
    return result


def _area(pixels, width, height, columns, color):
    for x, samples in enumerate(columns):
        available = [sample for sample in samples if sample is not None]
        if not available:
            continue
        boundaries = [sample[0] for sample in available]
        low, high = min(boundaries), max(boundaries)
        if len(available) == 4:
            _column(pixels, width, height, x, math.ceil(high), height, color)
            bottom = min(height, math.ceil(high))
        else:
            bottom = height
        for y in range(max(0, math.floor(low)), bottom):
            coverage = sum(max(0.0, min(1.0, y + 1.0 - boundary))
                           for boundary in boundaries) / 4.0
            _blend(pixels, (y * width + x) * 3, color, coverage)


def _band(pixels, width, height, upper_columns, lower_columns, color):
    for x, (upper, lower) in enumerate(zip(upper_columns, lower_columns)):
        spans = [(top[0], bottom[0]) for top, bottom in zip(upper, lower)
                 if top is not None and bottom is not None]
        if not spans:
            continue
        top = min(a for a, _b in spans)
        bottom = max(b for _a, b in spans)
        full_top = math.ceil(max(a for a, _b in spans))
        full_bottom = math.floor(min(b for _a, b in spans))
        solid = len(spans) == 4 and full_bottom > full_top
        if solid:
            _column(pixels, width, height, x, full_top, full_bottom, color)
        for y in range(max(0, math.floor(top)), min(height, math.ceil(bottom))):
            if solid and full_top <= y < full_bottom:
                continue
            coverage = sum(max(0.0, min(y + 1.0, b) - max(float(y), a))
                           for a, b in spans) / 4.0
            _blend(pixels, (y * width + x) * 3, color, coverage)


def _stroke(pixels, width, height, columns, color, thickness):
    radius = thickness / 2.0
    for x, samples in enumerate(columns):
        available = [sample for sample in samples if sample is not None]
        if not available:
            continue
        distances = [(y, math.sqrt(1.0 + slope * slope)) for y, slope in available]
        top = max(0, math.floor(min(y - (radius + 0.5) * scale for y, scale in distances)))
        bottom = min(height, math.ceil(max(y + (radius + 0.5) * scale for y, scale in distances)))
        for row in range(top, bottom):
            coverage = sum(max(0.0, min(1.0, radius + 0.5 - abs(row + 0.5 - y) / scale))
                           for y, scale in distances) / 4.0
            _blend(pixels, (row * width + x) * 3, color, coverage)


def _circle(pixels, width, height, x, y, radius, color):
    for row in range(max(0, math.floor(y - radius - 0.5)),
                     min(height, math.ceil(y + radius + 0.5))):
        for column in range(max(0, math.floor(x - radius - 0.5)),
                            min(width, math.ceil(x + radius + 0.5))):
            coverage = max(0.0, min(1.0, radius + 0.5
                                   - math.hypot(column + 0.5 - x, row + 0.5 - y)))
            _blend(pixels, (row * width + column) * 3, color, coverage)


def _threshold(pixels, width, height, y, scale, color):
    thickness = max(0.6, 0.8 * scale)
    top, bottom = y - thickness / 2.0, y + thickness / 2.0
    for row in range(max(0, math.floor(top)), min(height, math.ceil(bottom))):
        vertical = max(0.0, min(row + 1.0, bottom) - max(float(row), top))
        for x in range(width):
            horizontal = sum(1 for fraction in _X_SAMPLES
                             if (x + fraction) % (6.0 * scale) < 2.0 * scale) / 4.0
            _blend(pixels, (row * width + x) * 3, color, horizontal * vertical)


def _png(width, height, pixels):
    def chunk(kind, data):
        return (struct.pack(">I", len(data)) + kind + data
                + struct.pack(">I", zlib.crc32(kind + data) & 0xFFFFFFFF))

    compressor = zlib.compressobj(3)
    packed = []
    stride = width * 3
    view = memoryview(pixels)
    for row in range(height):
        part = compressor.compress(b"\0") + compressor.compress(view[row * stride:(row + 1) * stride])
        if part:
            packed.append(part)
    packed.append(compressor.flush())
    return (b"\x89PNG\r\n\x1a\n"
            + chunk(b"IHDR", struct.pack(">IIBBBBB", width, height, 8, 2, 0, 0, 0))
            + chunk(b"IDAT", b"".join(packed)) + chunk(b"IEND", b""))


def chart_png(width, height, values, view_low, view_high, colors,
              pixel_scale=1.0, band_low=None, band_high=None, threshold=None):
    """Return an opaque, anti-aliased RGB PNG at exactly ``width × height``.

    Supply the line and optional bands returned by ``trend_series``. Empty
    histories render the grid; a single sample renders only its endpoint.
    Mismatched bands or non-finite input are rejected before rasterization.
    Native dimensions are bounded to 4 million pixels, samples to 4096, and
    scale to 0.25–8; the shipped chart uses fewer than 1 million pixels/261
    samples across its supported display and detail scales.
    """
    width, height = int(width), int(height)
    if (width < 1 or height < 1 or width > _MAX_SIDE or height > _MAX_SIDE
            or width * height > _MAX_PIXELS):
        raise ValueError("chart dimensions must be positive and bounded")
    scale = float(pixel_scale)
    if not math.isfinite(scale) or not 0.25 <= scale <= 8.0:
        raise ValueError("chart pixel scale must be between 0.25 and 8")
    low, high = float(view_low), float(view_high)
    if not math.isfinite(low) or not math.isfinite(high) or high < low:
        raise ValueError("chart view range must be finite and ordered")
    line = _series(values)
    lower, upper = _series(band_low), _series(band_high)
    if (band_low is None) != (band_high is None):
        raise ValueError("both chart bands must be supplied together")
    if band_low is not None:
        if len(lower) != len(upper) or len(lower) != len(line):
            raise ValueError("chart band and line lengths must match")
        if any(a > b for a, b in zip(lower, upper)):
            raise ValueError("chart bands must be ordered")
    if threshold is not None and not math.isfinite(float(threshold)):
        raise ValueError("chart threshold must be finite")

    background = _rgb(colors["card"])
    accent = _rgb(colors["orange"])
    pixels = bytearray(bytes(background) * (width * height))
    grid = _rgb(colors.get("grid", colors["card"]))
    for y in dict.fromkeys((0.5, height / 2.0, height - 0.5)):
        _horizontal(pixels, width, height, y, max(0.5, 0.6 * scale), grid)

    # Inset enough to retain the endpoint's anti-aliased edge even at 0/100%.
    radius = min(2.0 * scale, max(0.25, (min(width, height) - 1) / 4.0))
    margin_x = min(radius + 0.5, max(0.0, (width - 1) / 2.0))
    margin_y = min(radius + 0.5, max(0.0, (height - 1) / 2.0))
    left, right = margin_x, width - 1.0 - margin_x
    top, bottom = margin_y, height - 1.0 - margin_y
    span = max(0.001, high - low)

    def points(series):
        count = max(1, len(series) - 1)
        return [(left + (right - left) * index / count,
                 bottom - (bottom - top) * max(0.0, min(1.0, (value - low) / span)))
                for index, value in enumerate(series)]

    if len(line) >= 2 and right > left:
        line_points = _smooth(points(line))
        columns = _columns(line_points, width)
        _area(pixels, width, height, columns, _rgb(colors["chart_area"]))
        if len(lower) >= 2:
            _band(pixels, width, height, _columns(_smooth(points(upper)), width),
                  _columns(_smooth(points(lower)), width), _rgb(colors["chart_band"]))
        if threshold is not None and low < float(threshold) < high:
            y = bottom - (bottom - top) * (float(threshold) - low) / span
            _threshold(pixels, width, height, y, scale, accent)
        _stroke(pixels, width, height, columns, accent, max(0.7, 1.35 * scale))
        _circle(pixels, width, height, *line_points[0], max(0.35, 0.675 * scale), accent)
        endpoint = line_points[-1]
    elif line:
        endpoint = (right, points(line)[-1][1])
    else:
        endpoint = None
    if endpoint is not None:
        _circle(pixels, width, height, *endpoint, radius, background)
        _circle(pixels, width, height, *endpoint, max(0.2, radius - 0.9 * scale), accent)
    return _png(width, height, pixels)
