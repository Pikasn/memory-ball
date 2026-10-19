"""Deterministic sampler/raster tests and read-only native Windows sampling."""

from __future__ import annotations

import ctypes
from dataclasses import replace
import json
from pathlib import Path
import statistics
import struct
import sys
import time
import unittest
from unittest.mock import patch
import zlib

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "source"))
import network_info as ni
from network_chart import network_png


COLORS = dict(card="#FFFFFF", grid="#FFFFFF", orange="#FF9900",
              secondary="#777777", chart_area="#FFFAEE")


def adapter(identity=1, received=0, sent=0, **kwargs):
    return ni.AdapterCounters(identity, f"Adapter {identity}", received, sent, **kwargs)


class SequenceReader:
    def __init__(self, *snapshots):
        self.snapshots = iter(snapshots)
        self.calls = 0

    def __call__(self):
        self.calls += 1
        value = next(self.snapshots)
        if isinstance(value, Exception):
            raise value
        return value


def decode_png(data):
    assert data[:8] == b"\x89PNG\r\n\x1a\n"
    offset, packed, dimensions = 8, [], None
    while offset < len(data):
        length = struct.unpack(">I", data[offset:offset + 4])[0]
        kind = data[offset + 4:offset + 8]
        payload = data[offset + 8:offset + 8 + length]
        crc = struct.unpack(">I", data[offset + 8 + length:offset + 12 + length])[0]
        assert crc == zlib.crc32(kind + payload) & 0xFFFFFFFF
        if kind == b"IHDR":
            dimensions = struct.unpack(">IIBBBBB", payload)
        elif kind == b"IDAT":
            packed.append(payload)
        offset += 12 + length
    width, height, depth, color_type, _c, _f, _i = dimensions
    assert (depth, color_type) == (8, 2)
    raw = zlib.decompress(b"".join(packed))
    stride = width * 3 + 1
    assert len(raw) == height * stride
    assert all(raw[y * stride] == 0 for y in range(height))
    return width, height, b"".join(raw[y * stride + 1:(y + 1) * stride]
                                  for y in range(height))


class SamplerTests(unittest.TestCase):
    def test_actual_elapsed_time_and_64_bit_counters(self):
        base = 2 ** 40
        reader = SequenceReader([adapter(received=base, sent=base)],
                                [adapter(received=base + 25_600_000,
                                         sent=base + 1_280_000)])
        sampler = ni.NetworkSampler(reader)
        self.assertEqual(sampler.sample(10).status, "starting")
        reading = sampler.sample(12)
        self.assertEqual(reading.status, "active")
        self.assertEqual((reading.download_bps, reading.upload_bps), (12_800_000, 640_000))
        self.assertEqual(sampler.history(60, 12), [(10, None, None), (12, 12_800_000, 640_000)])

    def test_throttle_uses_cached_snapshot(self):
        reader = SequenceReader([adapter()], [adapter(received=400, sent=100)])
        sampler = ni.NetworkSampler(reader)
        first = sampler.sample(0)
        self.assertIs(sampler.sample(0.9), first)
        self.assertEqual(reader.calls, 1)
        self.assertEqual(sampler.revision, 1)
        self.assertEqual(sampler.sample(1).download_bps, 400)

    def test_physical_aggregation_excludes_duplicate_virtual_filter_and_loopback(self):
        first = [adapter(1), adapter(2), adapter(3, flags=0),
                 adapter(4, flags=3), adapter(5, interface_type=24),
                 adapter(6, interface_type=131), adapter(7, oper_status=2)]
        later = [replace(row, received_bytes=100 * row.identity, sent_bytes=10 * row.identity)
                 for row in first]
        sampler = ni.NetworkSampler(SequenceReader(first, later))
        sampler.sample(0)
        reading = sampler.sample(1)
        self.assertEqual((reading.adapter_count, reading.download_bps, reading.upload_bps),
                         (2, 300, 30))

    def test_virtual_machine_fallback(self):
        rows = [adapter(1, flags=0), adapter(2, interface_type=131, flags=0)]
        self.assertEqual(ni.selected_adapters(rows), (rows[0],))

    def test_adapter_arrival_and_departure_do_not_invent_rate(self):
        sampler = ni.NetworkSampler(SequenceReader(
            [adapter(1, 1000, 100)],
            [adapter(1, 1100, 110), adapter(2, 10 ** 10, 10 ** 9)],
            [adapter(2, 10 ** 10 + 250, 10 ** 9 + 25)]))
        sampler.sample(0)
        self.assertEqual(sampler.sample(1).download_bps, 100)
        self.assertEqual(sampler.sample(2).download_bps, 250)

    def test_counter_reset_is_a_gap_then_recovers(self):
        sampler = ni.NetworkSampler(SequenceReader(
            [adapter(1, 9999, 9999)], [adapter(1, 20, 10)], [adapter(1, 70, 20)]))
        sampler.sample(0)
        self.assertEqual(sampler.sample(1).status, "starting")
        self.assertEqual(sampler.sample(2).download_bps, 50)
        self.assertEqual(sampler.history(60, 2)[1], (1, None, None))

    def test_suspend_gap_is_not_a_fake_average(self):
        sampler = ni.NetworkSampler(SequenceReader(
            [adapter()], [adapter(received=50_000_000)], [adapter(received=50_001_000)]))
        sampler.sample(0)
        self.assertEqual(sampler.sample(20).status, "starting")
        self.assertEqual(sampler.sample(21).download_bps, 1000)

    def test_disconnected_error_and_reconnect(self):
        sampler = ni.NetworkSampler(SequenceReader(
            [adapter()], [], OSError("test provider unavailable"),
            [adapter(received=999_999)], [adapter(received=1_000_099)]))
        sampler.sample(0)
        self.assertEqual(sampler.sample(1).status, "disconnected")
        self.assertEqual(sampler.sample(2).status, "unavailable")
        self.assertEqual(sampler.sample(3).status, "starting")
        self.assertEqual(sampler.sample(4).download_bps, 100)
        self.assertEqual(sampler.history(10, 4)[1:4],
                         [(1, None, None), (2, None, None), (3, None, None)])

    def test_idle_zero_is_real_data(self):
        sampler = ni.NetworkSampler(SequenceReader([adapter()], [adapter()]))
        sampler.sample(0)
        self.assertEqual(sampler.sample(1).status, "active")
        self.assertEqual(sampler.history(60, 1)[-1], (1, 0, 0))

    def test_history_retains_ten_minutes_and_is_bounded(self):
        sampler = ni.NetworkSampler(lambda: [adapter()])
        for tick in range(1000):
            sampler.sample(tick)
        points = sampler.history(600, 999)
        self.assertEqual((len(points), points[0][0], points[-1][0]), (601, 399, 999))
        self.assertLessEqual(len(sampler._history), 602)

    def test_clock_reversal_resets_baseline(self):
        sampler = ni.NetworkSampler(SequenceReader([adapter()], [adapter(received=500)]))
        sampler.sample(20)
        self.assertEqual(sampler.sample(10).status, "starting")
        self.assertEqual(sampler.history(600, 10), [(10, None, None)])

    def test_units_are_decimal_bytes(self):
        self.assertEqual(ni.format_rate(12_800_000), ("12.8", "MB/s"))
        self.assertEqual(ni.format_rate(640_000), ("640.0", "KB/s"))
        self.assertEqual(ni.format_rate(0), ("0", "B/s"))
        with self.assertRaises(ValueError):
            ni.format_rate(float("nan"))

    def test_invalid_provider_counters_fail_closed_and_reset_baseline(self):
        for bad in (float("nan"), float("inf"), -1, 2 ** 64, 1.5, True):
            for field in ("received_bytes", "sent_bytes"):
                current = [adapter(received=100, sent=100)]
                sampler = ni.NetworkSampler(lambda: current)
                sampler.sample(0)
                current[:] = [replace(adapter(received=200, sent=200), **{field: bad})]
                result = sampler.sample(1)
                self.assertEqual(result.status, "unavailable")
                self.assertFalse(result.available)
                self.assertEqual(sampler.history(60, 1)[-1], (1, None, None))
                current[:] = [adapter(received=1000, sent=1000)]
                self.assertEqual(sampler.sample(2).status, "starting")
                current[:] = [adapter(received=1100, sent=1050)]
                result = sampler.sample(3)
                self.assertEqual((result.download_bps, result.upload_bps), (100, 50))


class NativeOwnershipTests(unittest.TestCase):
    def test_table_alignment_copies_unicode_and_uint64_before_free(self):
        buffer = ctypes.create_string_buffer(8 + ctypes.sizeof(ni._MIB_IF_ROW2))
        ctypes.c_uint32.from_buffer(buffer).value = 1
        row = ni._MIB_IF_ROW2.from_buffer(buffer, 8)
        row.InterfaceLuid = 123
        row.Alias = "本机网卡"
        row.InOctets, row.OutOctets = 2 ** 40 + 123, 2 ** 41 + 456
        row.Type, row.OperStatus, row.InterfaceAndOperStatusFlags = 6, 1, 5
        freed = []

        class FakeAPI:
            @staticmethod
            def GetIfTable2(pointer):
                ctypes.cast(pointer, ctypes.POINTER(ctypes.c_void_p))[0] = ctypes.addressof(buffer)
                return 0

            @staticmethod
            def FreeMibTable(pointer):
                freed.append(pointer.value)
                row.Alias = "changed after copy"

        with patch.object(ni, "_API", FakeAPI()):
            counters = ni.read_counters()
        self.assertEqual(freed, [ctypes.addressof(buffer)])
        self.assertEqual((counters[0].name, counters[0].received_bytes,
                          counters[0].sent_bytes), ("本机网卡", 2 ** 40 + 123, 2 ** 41 + 456))

    def test_error_with_allocated_table_still_frees_it(self):
        freed = []

        class FakeAPI:
            @staticmethod
            def GetIfTable2(pointer):
                ctypes.cast(pointer, ctypes.POINTER(ctypes.c_void_p))[0] = 12345
                return 5

            @staticmethod
            def FreeMibTable(pointer):
                freed.append(pointer.value)

        with patch.object(ni, "_API", FakeAPI()):
            with self.assertRaises(OSError):
                ni.read_counters()
        self.assertEqual(freed, [12345])


class RasterTests(unittest.TestCase):
    def test_unbounded_iterator_stops_at_sample_cap(self):
        consumed = []

        def points():
            tick = 0
            while True:
                consumed.append(tick)
                yield (tick, 1, 1)
                tick += 1

        with self.assertRaises(ValueError):
            network_png(100, 30, points(), 0, 10_000, 100, COLORS)
        self.assertEqual(len(consumed), 4097)

    def test_empty_and_single_sample_png_valid_at_multiple_pixel_scales(self):
        for scale in (0.7, 1, 1.5, 2, 3, 6):
            for points in ([], [(15, 12_800_000, 640_000)]):
                width, height = round(292 * scale), round(75 * scale)
                data = network_png(width, height, points, 0, 60, 20_000_000,
                                   COLORS, pixel_scale=scale)
                self.assertEqual(decode_png(data)[:2], (width, height))

    def test_single_point_keeps_timestamp_x_instead_of_last_edge(self):
        width, height, pixels = decode_png(network_png(
            203, 101, [(15, 100, None)], 0, 60, 100, COLORS))
        colored_x = [x for x in range(width) for y in range(height)
                     if pixels[(y * width + x) * 3:(y * width + x) * 3 + 3] != b"\xff\xff\xff"]
        self.assertTrue(colored_x)
        self.assertTrue(all(48 <= x <= 56 for x in colored_x))

    def test_none_and_long_gaps_leave_blank_columns(self):
        for points in ([(0, 100, None), (1, 100, None), (2, None, None),
                        (7, 100, None), (8, 100, None)],
                       [(0, 100, None), (1, 100, None), (7, 100, None), (8, 100, None)]):
            width, height, pixels = decode_png(network_png(203, 101, points, 0, 10, 100, COLORS))
            for x in range(55, 115):
                self.assertTrue(all(pixels[(y * width + x) * 3:(y * width + x) * 3 + 3]
                                    == b"\xff\xff\xff" for y in range(height)))

    def test_shared_axis_places_upload_lower(self):
        width, height, pixels = decode_png(network_png(
            203, 101, [(tick, 100, 5) for tick in range(11)], 0, 10, 100, COLORS))
        download_rows, upload_rows = [], []
        x = width // 2
        for y in range(height):
            r, g, b = pixels[(y * width + x) * 3:(y * width + x) * 3 + 3]
            if r > 220 and 80 < g < 220 and b < 100:
                download_rows.append(y)
            if max(r, g, b) - min(r, g, b) <= 2 and r < 240:
                upload_rows.append(y)
        self.assertTrue(download_rows and upload_rows)
        self.assertLess(max(download_rows), 10)
        self.assertGreater(min(upload_rows), 85)

    def test_one_sample_spike_is_preserved_at_its_real_timestamp(self):
        points = [(tick, 100 if tick == 137 else 0, 0) for tick in range(601)]
        width, height, pixels = decode_png(network_png(876, 178, points, 0, 600, 100, COLORS,
                                                       pixel_scale=3))
        expected_x = round(6.5 + (width - 1 - 13) * 137 / 600)
        near_peak = []
        for y in range(15):
            for x in range(expected_x - 6, expected_x + 7):
                r, g, b = pixels[(y * width + x) * 3:(y * width + x) * 3 + 3]
                if r > 220 and 80 < g < 220 and b < 100:
                    near_peak.append((x, y))
        self.assertTrue(near_peak, "a one-second peak must not disappear into a smoothed average")

    def test_rejects_nonfinite_or_unordered_input_and_bad_bounds(self):
        for points in ([(0, float("nan"), 0)], [(0, -1, 0)],
                       [(1, 1, 1), (0, 1, 1)], [(1, 1, 1), (1, 2, 2)]):
            with self.assertRaises(ValueError):
                network_png(100, 50, points, 0, 60, 100, COLORS)
        for args in ((0, 50, [], 0, 60, 100), (100, 50, [], 0, 0, 100),
                     (100, 50, [], 0, 60, 0), (8192, 8192, [], 0, 60, 100)):
            with self.assertRaises(ValueError):
                network_png(*args, COLORS)


def native_check():
    sampler = ni.NetworkSampler()
    durations, readings = [], []
    for index in range(4):
        start = time.perf_counter()
        reading = sampler.sample()
        durations.append((time.perf_counter() - start) * 1000)
        readings.append(dict(status=reading.status, available=reading.available,
                             down_bps=round(reading.download_bps, 2),
                             up_bps=round(reading.upload_bps, 2),
                             adapters=reading.adapter_count, error=reading.error))
        if index < 3:
            time.sleep(1.03)
    assert all(reading["available"] for reading in readings), readings
    assert all(reading["status"] == "active" for reading in readings[1:]), readings
    print(json.dumps(dict(
        native_readings=readings, table_row_bytes=ctypes.sizeof(ni._MIB_IF_ROW2),
        table_rows_offset=ni._MIB_IF_TABLE2.Table.offset,
        call_median_ms=round(statistics.median(durations), 3),
        call_max_ms=round(max(durations), 3), history_points=len(sampler.history(60))),
        ensure_ascii=True))


if __name__ == "__main__":
    if "--native" in sys.argv:
        native_check()
    else:
        unittest.main(verbosity=2)
