"""Independent network fault injection, bounded fuzzing and raster profiling.

Only local API reads and in-memory fabricated counters are used. This does not
disable adapters, change settings, launch widgets, or create network traffic.
"""

from __future__ import annotations

import ctypes
import gc
import json
import math
from pathlib import Path
import random
import statistics
import sys
import time
import tracemalloc
from unittest.mock import patch

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "source"))
import network_info as ni
from network_chart import network_png

COLORS = dict(card="#FFFFFF", orange="#FF9900", secondary="#777777",
              grid="#F0F1F4", chart_area="#FFF8EB")


def require(condition, explanation):
    if not condition:
        raise AssertionError(explanation)


def fuzz_sampler(seed=2761, iterations=12000):
    rng = random.Random(seed)
    state = {identity: [0, 0] for identity in range(1, 9)}
    fault = False
    rows = []

    def reader():
        if fault:
            raise OSError(31, "fabricated driver temporarily unavailable")
        return rows

    sampler = ni.NetworkSampler(reader)
    now, actual_samples = 1000.0, 0
    statuses = {}
    peak_rate = 0.0
    for index in range(iterations):
        # Simulate bursty transfers, temporary API failures, unplug/replug,
        # interface reset, idle seconds, timer jitter, sleep and clock reversal.
        now += rng.choice((0.2, 0.7, 1.0, 1.2, 2.0, 6.0, 60.0))
        if index % 1001 == 1000:
            now -= 120.0
        rows = []
        fault = rng.random() < .07
        for identity, counters in state.items():
            if rng.random() < .01:
                counters[:] = [0, 0]
            else:
                counters[0] += rng.randrange(0, 50_000_000)
                counters[1] += rng.randrange(0, 2_000_000)
            if rng.random() < .15:
                continue
            rows.append(ni.AdapterCounters(identity, f"Physical {identity}",
                                           *counters, oper_status=1))
        # Virtual/filter/loopback copies have huge counters and must not
        # multiply physical traffic, including during topology changes.
        rows.extend((ni.AdapterCounters(100, "VPN", 2 ** 62, 2 ** 62, flags=0),
                     ni.AdapterCounters(101, "filter", 2 ** 62, 2 ** 62, flags=3),
                     ni.AdapterCounters(102, "loopback", 2 ** 62, 2 ** 62, interface_type=24)))
        before = sampler.revision
        reading = sampler.sample(now)
        actual_samples += sampler.revision - before
        statuses[reading.status] = statuses.get(reading.status, 0) + 1
        require(math.isfinite(reading.download_bps) and reading.download_bps >= 0,
                "finite nonnegative download rate after faults")
        require(math.isfinite(reading.upload_bps) and reading.upload_bps >= 0,
                "finite nonnegative upload rate after faults")
        peak_rate = max(peak_rate, reading.download_bps, reading.upload_bps)
        require(peak_rate < 1e12, "virtual duplicated bytes must not create spikes")
        points = sampler.history(600, now)
        require(len(points) <= 602, "history remains bounded")
        require(all(a[0] < b[0] for a, b in zip(points, points[1:])),
                "history stays ordered after clock reversal")
        require(all(now - 600 <= point[0] <= now for point in points), "history window")
        if reading.status != "active" and sampler.revision > before:
            require(points[-1][1:] == (None, None), "invalid sample records a visible gap")
    return dict(seed=seed, iterations=iterations, snapshots=actual_samples,
                statuses=statuses, peak_rate_bps=peak_rate)


def fault_boundaries():
    findings, passed = [], []
    # Values cannot occur in read_counters() (the Windows API yields uint64),
    # but injected providers should not publish NaN/Infinity into the UI.
    for name, first, second in (
            ("nan counter", 0, float("nan")),
            ("infinite counter", 0, float("inf")),
            ("negative provider counters", -200, -100)):
        current = [ni.AdapterCounters(1, "fault injection", first, 0)]
        sampler = ni.NetworkSampler(lambda: current)
        sampler.sample(0)
        current[:] = [ni.AdapterCounters(1, "fault injection", second, 0)]
        result = sampler.sample(1)
        if result.status == "active" and (
                not math.isfinite(result.download_bps) or first < 0 or second < 0):
            findings.append(dict(case=name, status=result.status,
                                 emitted_rate=repr(result.download_bps),
                                 severity="P3 injected-provider boundary"))
        else:
            passed.append(name)

    current = [ni.AdapterCounters(1, "uint64 extreme", 0, 0)]
    sampler = ni.NetworkSampler(lambda: current)
    sampler.sample(0)
    current[:] = [ni.AdapterCounters(1, "uint64 extreme", 2 ** 64 - 1, 2 ** 64 - 1)]
    maximum = sampler.sample(1)
    require(math.isfinite(maximum.download_bps), "uint64 maximum stays finite")
    rendered = ni.format_rate(maximum.download_bps)

    # Overlarge sample iterables must be rejected; count how many were
    # consumed first to detect materialization ahead of the 4096-point limit.
    consumed = 0

    def many_samples():
        nonlocal consumed
        for index in range(50_000):
            consumed += 1
            yield (index, 1, 1)

    try:
        network_png(100, 30, many_samples(), 0, 50_000, 100, COLORS)
    except ValueError:
        pass
    require(consumed > 0, "generator boundary exercised")
    if consumed > 4097:
        findings.append(dict(case="sample iterable materialized before cap",
                             consumed=consumed, expected_upper_bound=4097,
                             severity="P3 public renderer boundary"))

    # Invalid top-level inputs must fail cleanly. No pointer fuzzing against
    # unallocated addresses is performed; that would violate API contracts.
    for label, call in (
            ("nonfinite sampler timestamp", lambda: sampler.sample(float("nan"))),
            ("negative history window", lambda: sampler.history(-1, 1)),
            ("infinite chart rate", lambda: network_png(100, 30, [(0, float("inf"), 0)],
                                                       0, 60, 100, COLORS)),
            ("too large chart", lambda: network_png(8192, 8192, [], 0, 60, 100, COLORS))):
        try:
            call()
        except (ValueError, TypeError):
            passed.append(label)
        else:
            raise AssertionError(label + " should be rejected")
    return dict(findings=findings, passed=passed,
                maximum_uint64_rate_text=" ".join(rendered))


def fake_native_failures():
    checks = []
    for label, count, status, return_null in (
            ("null successful table", 0, 0, True),
            ("allocation count above limit", 65537, 0, False),
            ("zero entries", 0, 0, False),
            ("failure with allocated table", 0, 8, False)):
        buffer = ctypes.create_string_buffer(8)
        ctypes.c_uint32.from_buffer(buffer).value = count
        freed = []

        class FakeAPI:
            @staticmethod
            def GetIfTable2(pointer):
                if not return_null:
                    ctypes.cast(pointer, ctypes.POINTER(ctypes.c_void_p))[0] = ctypes.addressof(buffer)
                return status

            @staticmethod
            def FreeMibTable(pointer):
                freed.append(pointer.value)

        with patch.object(ni, "_API", FakeAPI()):
            try:
                output = ni.read_counters()
                require(label == "zero entries" and output == (), "zero native rows")
            except OSError:
                require(label != "zero entries", "zero-row table must be legal")
        require(len(freed) == (0 if return_null else 1), "exactly one native free")
        checks.append(label)
    return checks


def raster_fuzz(seed=313, cases=400):
    rng = random.Random(seed)
    count = 0
    for _ in range(cases):
        length = rng.randrange(0, 100)
        ticks = sorted(rng.sample(range(600), length))
        points = [(tick, None if rng.random() < .15 else rng.randrange(0, 2 ** 64),
                   None if rng.random() < .15 else rng.randrange(0, 2 ** 64)) for tick in ticks]
        data = network_png(rng.randrange(1, 180), rng.randrange(1, 50), points,
                           0, 600, rng.choice((1e-300, 1000, 1e10, 2 ** 64)), COLORS,
                           pixel_scale=rng.choice((.25, .7, 1, 3, 8)))
        require(data.startswith(b"\x89PNG\r\n\x1a\n"), "finite input renders safely")
        count += 1
    return dict(seed=seed, cases=count)


def raster_profile():
    result = []
    for scale in (1, 3, 6):
        width, height = round(292 * scale), round(59 * scale) + 1
        for shape in ("steady", "alternating spikes", "repeated reconnect gaps"):
            points = []
            for second in range(601):
                if shape == "repeated reconnect gaps" and second % 5 in (2, 3):
                    points.append((second, None, None))
                else:
                    down = 20_000_000 if shape == "alternating spikes" and second % 2 else 200_000
                    points.append((second, down, down / 10))
            start = time.perf_counter()
            png = network_png(width, height, points, 0, 600, 20_000_000, COLORS,
                              pixel_scale=scale)
            result.append(dict(scale=scale, width=width, height=height, shape=shape,
                               ms=round((time.perf_counter() - start) * 1000, 2), bytes=len(png)))
    # This is the supported maximum 3× DPI × 2× detail size, with a lawful
    # history pattern following repeated one-second API failures/reconnects.
    points = [(second, None, None) if second % 5 in (2, 3)
              else (second, 200_000, 20_000) for second in range(601)]
    gc.collect()
    tracemalloc.start()
    network_png(1752, 355, points, 0, 600, 1000_000, COLORS, pixel_scale=6)
    _, peak = tracemalloc.get_traced_memory()
    tracemalloc.stop()
    return dict(frames=result, fragmented_peak_tracemalloc_mb=round(peak / 1024 ** 2, 2))


def native_stress(calls=400):
    durations = []
    counts = []
    for _ in range(calls):
        start = time.perf_counter()
        rows = ni.read_counters()
        durations.append((time.perf_counter() - start) * 1000)
        counts.append(len(rows))
        require(all(0 <= row.received_bytes <= 2 ** 64 - 1 and
                    0 <= row.sent_bytes <= 2 ** 64 - 1 for row in rows), "native uint64 bounds")
    return dict(calls=calls, interface_count_min=min(counts), interface_count_max=max(counts),
                median_ms=round(statistics.median(durations), 3),
                maximum_ms=round(max(durations), 3))


def repeated_render_memory(frames=100):
    """Inspect Windows process private bytes, including Pillow's C buffers."""
    class Counters(ctypes.Structure):
        _fields_ = [("cb", ctypes.c_uint32), ("faults", ctypes.c_uint32),
                    ("values", ctypes.c_size_t * 9)]

    kernel = ctypes.WinDLL("kernel32", use_last_error=True)
    kernel.GetCurrentProcess.argtypes = []
    kernel.GetCurrentProcess.restype = ctypes.c_void_p
    psapi = ctypes.WinDLL("psapi", use_last_error=True)
    psapi.GetProcessMemoryInfo.argtypes = [ctypes.c_void_p, ctypes.POINTER(Counters), ctypes.c_uint32]
    psapi.GetProcessMemoryInfo.restype = ctypes.c_int

    def private_bytes():
        counters = Counters()
        counters.cb = ctypes.sizeof(counters)
        require(psapi.GetProcessMemoryInfo(kernel.GetCurrentProcess(), ctypes.byref(counters),
                                          counters.cb), "read own process memory")
        return counters.values[-1]

    points = [(second, None, None) if second % 5 in (2, 3)
              else (second, 20_000_000 if second % 2 else 0, 640_000) for second in range(601)]
    for _ in range(10):
        network_png(1752, 355, points, 0, 600, 20_000_000, COLORS, pixel_scale=6)
    gc.collect()
    readings = [private_bytes()]
    start = time.perf_counter()
    for index in range(frames):
        network_png(1752, 355, points, 0, 600, 20_000_000, COLORS, pixel_scale=6)
        if index % 20 == 19:
            gc.collect()
            readings.append(private_bytes())
    spread = (max(readings) - min(readings)) / 1024 ** 2
    require(spread < 16, "100 large raster frames must not accumulate width-sized segment caches")
    return dict(frames=frames, private_mb=[round(value / 1024 ** 2, 2) for value in readings],
                private_spread_mb=round(spread, 2),
                mean_frame_ms=round((time.perf_counter() - start) * 1000 / frames, 2))


if __name__ == "__main__":
    report = dict(sampler_fuzz=fuzz_sampler(), provider_boundaries=fault_boundaries(),
                  native_allocation_faults=fake_native_failures(), raster_fuzz=raster_fuzz())
    print(json.dumps(report, ensure_ascii=True), flush=True)
    print(json.dumps(dict(raster_profile=raster_profile()), ensure_ascii=True), flush=True)
    print(json.dumps(dict(native_stress=native_stress()), ensure_ascii=True), flush=True)
