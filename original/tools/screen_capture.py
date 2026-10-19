"""Capture a specified application window for optional local UI checks."""

import ctypes
import ctypes.wintypes as wt
import os
import struct
import time


user32 = ctypes.windll.user32
gdi32 = ctypes.windll.gdi32
user32.SetProcessDpiAwarenessContext.argtypes = (ctypes.c_void_p,)
user32.GetDC.argtypes = [wt.HWND]
user32.GetDC.restype = wt.HDC
user32.ReleaseDC.argtypes = [wt.HWND, wt.HDC]
user32.GetWindowRect.argtypes = [wt.HWND, ctypes.POINTER(wt.RECT)]
user32.PrintWindow.argtypes = [wt.HWND, wt.HDC, ctypes.c_uint]
user32.PrintWindow.restype = wt.BOOL
gdi32.CreateCompatibleDC.argtypes = [wt.HDC]
gdi32.CreateCompatibleDC.restype = wt.HDC
gdi32.CreateCompatibleBitmap.argtypes = [wt.HDC, ctypes.c_int, ctypes.c_int]
gdi32.CreateCompatibleBitmap.restype = wt.HBITMAP
gdi32.SelectObject.argtypes = [wt.HDC, wt.HGDIOBJ]
gdi32.SelectObject.restype = wt.HGDIOBJ
gdi32.BitBlt.argtypes = [wt.HDC, ctypes.c_int, ctypes.c_int, ctypes.c_int,
                         ctypes.c_int, wt.HDC, ctypes.c_int, ctypes.c_int,
                         wt.DWORD]
gdi32.BitBlt.restype = wt.BOOL
gdi32.DeleteDC.argtypes = [wt.HDC]
gdi32.DeleteObject.argtypes = [wt.HGDIOBJ]
gdi32.GetDIBits.argtypes = [wt.HDC, wt.HBITMAP, ctypes.c_uint, ctypes.c_uint,
                            ctypes.c_void_p, ctypes.c_void_p, ctypes.c_uint]
gdi32.GetDIBits.restype = ctypes.c_int

SRCCOPY = 0x00CC0020
PW_RENDERFULLCONTENT = 2


class _POINT(wt.POINT):
    pass


def enable_dpi_awareness():
    try:
        user32.SetProcessDpiAwarenessContext(ctypes.c_void_p(-4))
    except (AttributeError, OSError):
        try:
            ctypes.windll.shcore.SetProcessDpiAwareness(2)
        except (AttributeError, OSError):
            pass


def window_rect(hwnd):
    rect = wt.RECT()
    if not user32.GetWindowRect(wt.HWND(hwnd), ctypes.byref(rect)):
        raise ctypes.WinError()
    return rect.left, rect.top, rect.right, rect.bottom


def _write_bmp(path, width, height, pixels):
    """把一段 BGRA 原始像素写成 32 位 bottom-up BMP。"""
    header = struct.pack(
        "<IiiHHIIiiII", 40, width, height, 1, 32, 0,
        width * height * 4, 0, 0, 0, 0,
    )
    offset = 14 + 40
    file_header = struct.pack("<HIHHI", 0x4D42, offset + len(pixels), 0, 0, offset)
    with open(path, "wb") as file:
        file.write(file_header)
        file.write(header)
        file.write(pixels)


def _blit_to_buffer(source_dc, src_x, src_y, width, height):
    """从给定 DC 抠一块到新位图并取回 BGRA 字节。"""
    memory_dc = gdi32.CreateCompatibleDC(source_dc)
    bitmap = gdi32.CreateCompatibleBitmap(source_dc, width, height)
    previous = gdi32.SelectObject(memory_dc, bitmap)
    try:
        if not gdi32.BitBlt(memory_dc, 0, 0, width, height,
                            source_dc, src_x, src_y, SRCCOPY):
            raise ctypes.WinError()
        header = struct.pack(
            "<IiiHHIIiiII", 40, width, height, 1, 32, 0,
            width * height * 4, 0, 0, 0, 0,
        )
        buffer = ctypes.create_string_buffer(width * height * 4)
        info = ctypes.create_string_buffer(header)
        if not gdi32.GetDIBits(memory_dc, bitmap, 0, height, buffer, info, 0):
            raise ctypes.WinError()
        return buffer.raw
    finally:
        gdi32.SelectObject(memory_dc, previous)
        gdi32.DeleteObject(bitmap)
        gdi32.DeleteDC(memory_dc)


def capture_window(hwnd, path, padding=0, settle=0.35):
    """抓一个窗口的内容并写成 BMP —— **本模块的默认抓屏方式**。

    走窗口自己的 DC（失败退回 `PrintWindow`），不用 `GetDC(0)`
    （原因见模块开头：本机屏幕 DC 拿到的是壁纸层，不是合成画面）。
    `padding` 允许往外多抓一点，用来给有阴影/描边的窗口留边。
    """
    time.sleep(settle)
    left, top, right, bottom = window_rect(hwnd)
    width = right - left + padding * 2
    height = bottom - top + padding * 2
    source = user32.GetDC(wt.HWND(hwnd))
    if source:
        try:
            pixels = _blit_to_buffer(source, -padding, -padding, width, height)
        finally:
            user32.ReleaseDC(wt.HWND(hwnd), source)
    else:
        # 退回 PrintWindow：从窗口表面渲染到一块兼容 DC
        screen = user32.GetDC(None)
        memory_dc = gdi32.CreateCompatibleDC(screen)
        bitmap = gdi32.CreateCompatibleBitmap(screen, width, height)
        previous = gdi32.SelectObject(memory_dc, bitmap)
        try:
            user32.PrintWindow(wt.HWND(hwnd), memory_dc, PW_RENDERFULLCONTENT)
            header = struct.pack(
                "<IiiHHIIiiII", 40, width, height, 1, 32, 0,
                width * height * 4, 0, 0, 0, 0,
            )
            buffer = ctypes.create_string_buffer(width * height * 4)
            info = ctypes.create_string_buffer(header)
            gdi32.GetDIBits(memory_dc, bitmap, 0, height, buffer, info, 0)
            pixels = buffer.raw
        finally:
            gdi32.SelectObject(memory_dc, previous)
            gdi32.DeleteObject(bitmap)
            gdi32.DeleteDC(memory_dc)
            user32.ReleaseDC(None, screen)
    _write_bmp(path, width, height, pixels)
    return left, top, width, height


def capture_screen(x, y, width, height, path, settle=0.35):
    """**已废弃，保留只为让老脚本不炸。**

    曾经是"抓桌面矩形"，但本机 `GetDC(0)` 拿到的是壁纸层（见模块开头），
    所以现在退化成"找一个覆盖该点的窗口并抓它"。真要抓某个窗口请直接用
    `capture_window(hwnd, ...)`，别再依赖坐标反查。
    """
    time.sleep(settle)
    target = window_at(x + width // 2, y + height // 2)
    if target:
        return capture_window(target, path, padding=0, settle=0)[:3]
    # 实在找不到窗口：抓个空白，让调用方的色相判定自然判负
    _write_bmp(path, width, height, b"\x00" * (width * height * 4))
    return x, y, width, height


def window_at(x, y):
    """屏幕上某点最上层的、有面积的窗口句柄（找不到返回 None）。"""
    point = wt.POINT(int(x), int(y))
    hwnd = user32.WindowFromPoint(point)
    while hwnd:
        rect = wt.RECT()
        user32.GetWindowRect(wt.HWND(hwnd), ctypes.byref(rect))
        if rect.right > rect.left and rect.bottom > rect.top:
            return hwnd
        hwnd = user32.GetParent(wt.HWND(hwnd))
    return None


def sample_screen(x, y):
    """屏幕上某点最上层窗口的 RGB（同样避开不合成的 `GetDC(0)`）。"""
    hwnd = window_at(x, y)
    if not hwnd:
        return None
    rect = wt.RECT()
    user32.GetWindowRect(wt.HWND(hwnd), ctypes.byref(rect))
    source = user32.GetDC(wt.HWND(hwnd))
    if not source:
        return None
    try:
        value = gdi32.GetPixel(source, x - rect.left, y - rect.top)
    finally:
        user32.ReleaseDC(wt.HWND(hwnd), source)
    if value == 0xFFFFFFFF:
        return None
    return (value & 0xFF, (value >> 8) & 0xFF, (value >> 16) & 0xFF)


def read_bmp_size(path):
    """读 BMP 的 (宽, 高)，单位像素。"""
    with open(path, "rb") as file:
        header = file.read(54)
    width, height = struct.unpack("<ii", header[18:26])
    return width, height


def read_bmp_pixel(path, x, y):
    """Read one pixel from a bottom-up 32-bit BMP written by this module."""
    with open(path, "rb") as file:
        header = file.read(54)
        width, height = struct.unpack("<ii", header[18:26])
        row = (height - 1 - y) * width * 4
        file.seek(54 + row + x * 4)
        b, g, r, _a = file.read(4)
    return r, g, b


def bmp_to_png(path, out_path, scale=2, backdrop=None):
    from PIL import Image
    image = Image.open(path).convert("RGB")
    width, height = image.size
    if scale != 1:
        image = image.resize((width * scale, height * scale), Image.LANCZOS)
    image.save(out_path)
    return image.size


def ensure_dir(path):
    os.makedirs(path, exist_ok=True)
    return path
