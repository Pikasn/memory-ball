"""Isolated GPU/config fault injection; never launches an app or a real driver.

Imports source modules only. Every settings path is redirected to a temporary
directory and every GPU vendor call/thread device is a mock. Native window
restore checks also use mocks, so no existing window can be affected.
"""

import ctypes
import importlib.util
import json
import math
from pathlib import Path
import sys
import tempfile
import threading
import time
from types import SimpleNamespace
import unittest
from unittest.mock import patch

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "source"))
import gpu_devices
import memory_widget as mw


def fresh_gpu():
    spec = importlib.util.spec_from_file_location("gpu_info_faults", ROOT / "source" / "gpu_info.py")
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


class GPUFaultTests(unittest.TestCase):
    def setUp(self):
        self.gpu = fresh_gpu()

    def tearDown(self):
        self.gpu.stop()

    def test_missing_driver_returns_unknown_and_cools_down(self):
        gpu = self.gpu
        with patch.object(gpu._NvmlDevice, "open", return_value=None) as opened:
            with patch.object(gpu, "_query", return_value=None):
                with patch.object(gpu._PdhGpuEngine, "open", return_value=None):
                    gpu.start()
                    thread = gpu._thread
                    if thread:
                        thread.join(1)
                self.assertIsNone(gpu.reading())
                self.assertEqual(gpu.name(), "")
                self.assertFalse(gpu.available())
                gpu.start()
                self.assertEqual(opened.call_count, 1)
                self.assertIsNone(gpu._thread)

    def test_first_probe_failure_clears_previous_adapter(self):
        gpu = self.gpu
        gpu._reading = (56, 4, 100, 8000)
        gpu._name = "NVIDIA old removable GPU"
        gpu._reading_at = time.monotonic()
        with patch.object(gpu._NvmlDevice, "open", return_value=None):
            with patch.object(gpu, "_query", return_value=None):
                with patch.object(gpu._PdhGpuEngine, "open", return_value=None):
                    gpu.start()
                    thread = gpu._thread
                    if thread:
                        thread.join(1)
        self.assertIsNone(gpu.reading(), "old telemetry must not survive a failed initial probe")
        self.assertEqual(gpu.name(), "")

    def test_worker_exception_clears_previous_adapter(self):
        gpu = self.gpu
        gpu._reading = (56, 4, 100, 8000)
        gpu._name = "NVIDIA old removable GPU"
        gpu._reading_at = time.monotonic()
        with patch.object(gpu._NvmlDevice, "open", side_effect=OSError("mock driver failure")):
            gpu.start()
            thread = gpu._thread
            if thread:
                thread.join(1)
        self.assertIsNone(gpu.reading())
        self.assertEqual(gpu.name(), "")
        self.assertIn("mock driver failure", gpu.last_error)
        self.assertIsNone(gpu._thread)

    def test_first_device_read_exception_still_closes_session(self):
        gpu = self.gpu
        device = SimpleNamespace(read=lambda: (_ for _ in ()).throw(RuntimeError("mock initial read")),
                                 close=unittest.mock.Mock())
        with patch.object(gpu._NvmlDevice, "open", return_value=device):
            gpu.start()
            thread = gpu._thread
            if thread:
                thread.join(1)
        device.close.assert_called_once()
        self.assertIsNone(gpu._thread)

    def test_reading_and_name_expire_together(self):
        gpu = self.gpu
        gpu.backend = "nvml"
        gpu._available = True
        gpu._reading = (56, 4, 100, 8000)
        gpu._name = "NVIDIA detected GPU"
        gpu._reading_at = 100.0
        with patch.object(gpu.time, "monotonic", return_value=100.0):
            self.assertEqual(gpu.reading(), (56, 4, 100, 8000))
            self.assertEqual(gpu.name(), "NVIDIA detected GPU")
            self.assertTrue(gpu.available())
        with patch.object(gpu.time, "monotonic", return_value=200.0):
            self.assertIsNone(gpu.reading(), "stale driver readings must expire")
            self.assertEqual(gpu.name(), "")
            self.assertFalse(gpu.available())

    def test_smi_reading_has_longer_but_bounded_freshness(self):
        gpu = self.gpu
        gpu.backend = "smi"
        gpu._available = True
        gpu._reading = (56, None, 0, 0)
        gpu._name = "NVIDIA mocked adapter"
        gpu._reading_at = 100
        with patch.object(gpu.time, "monotonic", return_value=125):
            self.assertIsNotNone(gpu.reading())
        with patch.object(gpu.time, "monotonic", return_value=136):
            self.assertIsNone(gpu.reading())
            self.assertEqual(gpu.name(), "")

    def test_nvml_failed_initialization_and_missing_handle_export_release_dll(self):
        gpu = self.gpu
        for initialized in (False, True):
            with self.subTest(initialized=initialized):
                fake = SimpleNamespace(nvmlInit_v2=lambda: 0 if initialized else 3)
                with patch.object(gpu, "_nvml_paths", return_value=["mock-nvml.dll"]):
                    with patch.object(gpu.os.path, "isfile", return_value=True):
                        with patch.object(gpu.ctypes, "WinDLL", return_value=fake):
                            with patch.object(gpu, "_release_nvml_dll") as release:
                                self.assertIsNone(gpu._NvmlDevice.open())
                                release.assert_called_once_with(fake, initialized)

    def test_nvml_failed_handle_and_successful_close_balance_session(self):
        gpu = self.gpu
        def valid_handle(index, output):
            ctypes.cast(output, ctypes.POINTER(ctypes.c_void_p)).contents.value = 123
            return 0
        for valid in (False, True):
            with self.subTest(valid=valid):
                fake = SimpleNamespace(nvmlInit_v2=lambda: 0,
                                       nvmlDeviceGetHandleByIndex_v2=valid_handle if valid else lambda *_: 3)
                with patch.object(gpu, "_nvml_paths", return_value=["mock-nvml.dll"]):
                    with patch.object(gpu.os.path, "isfile", return_value=True):
                        with patch.object(gpu.ctypes, "WinDLL", return_value=fake):
                            with patch.object(gpu, "_release_nvml_dll") as release:
                                device = gpu._NvmlDevice.open()
                                if valid:
                                    self.assertIsNotNone(device)
                                    release.assert_not_called()
                                    device.close()
                                    device.close()
                                else:
                                    self.assertIsNone(device)
                                release.assert_called_once_with(fake, True)

    def test_smi_unsupported_utilization_is_unknown(self):
        gpu = self.gpu
        value = gpu._parse("NVIDIA Tesla T4, 56, [N/A], 100, 8192")
        self.assertEqual(value, ("NVIDIA Tesla T4", 56, None, 100, 8192))
        self.assertEqual(gpu._parse("NVIDIA Tesla T4, [N/A], 3, 100, 8192"),
                         ("NVIDIA Tesla T4", None, 3, 100, 8192))
        self.assertIsNone(gpu._parse("NVIDIA Tesla T4, [N/A], [N/A], 100, 8192"))

    def test_nvml_unsupported_utilization_is_unknown(self):
        gpu = self.gpu
        def temperature(handle, kind, output):
            ctypes.cast(output, ctypes.POINTER(ctypes.c_uint)).contents.value = 56
            return 0
        fake = SimpleNamespace(nvmlDeviceGetTemperature=temperature,
                               nvmlDeviceGetUtilizationRates=lambda *_: 3,
                               nvmlDeviceGetMemoryInfo=lambda *_: 3)
        device = gpu._NvmlDevice(fake)
        device.name = "NVIDIA mocked adapter"
        self.assertEqual(device.read()[:3], ("NVIDIA mocked adapter", 56, None))

    def test_stop_timeout_retains_slot_then_main_retry_recovers(self):
        gpu = self.gpu
        entered, release = threading.Event(), threading.Event()
        counters = {"opens": 0, "reads": 0}
        def read():
            counters["reads"] += 1
            if counters["reads"] == 2:
                entered.set()
                release.wait(2)
            return "NVIDIA mocked adapter", 55, 3, 100, 8000
        device = SimpleNamespace(read=read, close=lambda: None)
        def open_device():
            counters["opens"] += 1
            return device
        with patch.object(gpu._NvmlDevice, "open", side_effect=open_device):
            with patch.object(gpu, "TIMEOUT", 0), patch.object(gpu, "INTERVAL_NVML", 0.01):
                try:
                    gpu.start()
                    self.assertTrue(entered.wait(1))
                    old_thread = gpu._thread
                    gpu.stop()
                    self.assertIs(gpu._thread, old_thread)
                    gpu.start()  # Reopen while the old driver call remains blocked.
                    self.assertEqual(counters["opens"], 1)
                    self.assertTrue(gpu._stop.is_set())
                    release.set()
                    old_thread.join(1)
                    self.assertIsNone(gpu._thread)
                    # The visible detail refresh should call start again, from
                    # the Tk/main thread, after the previous worker has exited.
                    gpu.start()
                    deadline = time.monotonic() + 1
                    while counters["opens"] < 2 and time.monotonic() < deadline:
                        time.sleep(0.01)
                    self.assertEqual(counters["opens"], 2)
                    self.assertIsNotNone(gpu.reading())
                finally:
                    release.set()
                    gpu.stop()


class SettingsFaultTests(unittest.TestCase):
    def setUp(self):
        self.directory = tempfile.TemporaryDirectory(prefix="memorynet-config-faults-")
        self.original = (mw.APP_DIR, mw.SETTINGS_PATH, mw.S)
        mw.APP_DIR = self.directory.name
        mw.SETTINGS_PATH = str(Path(mw.APP_DIR) / "settings.json")
        mw.S = dict(mw.DEFAULTS)

    def tearDown(self):
        mw.APP_DIR, mw.SETTINGS_PATH, mw.S = self.original
        self.directory.cleanup()

    def write(self, value):
        Path(mw.SETTINGS_PATH).write_text(json.dumps(value), encoding="utf-8")

    def test_malformed_json_and_nonobject_leave_defaults(self):
        for value in ("{bad", "[]", "null", '"text"'):
            Path(mw.SETTINGS_PATH).write_text(value, encoding="utf-8")
            mw.load_settings()
            self.assertEqual(mw.S, mw.DEFAULTS)

    def test_oversized_or_deeply_nested_json_leaves_defaults(self):
        for value in ('{"theme":"' + 'x' * 70000 + '"}',
                      '[' * 2000 + '0' + ']' * 2000):
            with self.subTest(length=len(value)):
                Path(mw.SETTINGS_PATH).write_text(value, encoding="utf-8")
                mw.load_settings()
                self.assertEqual(mw.S, mw.DEFAULTS)

    def test_nonfinite_thresholds_do_not_abort_startup(self):
        for key in ("warn_yellow", "warn_red"):
            for value in (float("inf"), float("-inf"), float("nan"), "1e999"):
                with self.subTest(key=key, value=value):
                    mw.S = dict(mw.DEFAULTS)
                    self.write({key: value})
                    mw.load_settings()
                    self.assertGreaterEqual(mw.S["warn_yellow"], 40)
                    self.assertLess(mw.S["warn_yellow"], mw.S["warn_red"])
                    self.assertLessEqual(mw.S["warn_red"], 99)

    def test_nonboolean_flags_are_rejected(self):
        for key in ("autostart", "hover_expand", "history_enabled", "top5_enabled", "pin_top"):
            for value in ("false", 1, [], {}):
                with self.subTest(key=key, value=value):
                    mw.S = dict(mw.DEFAULTS)
                    self.write({key: value})
                    mw.load_settings()
                    self.assertIs(type(mw.S[key]), bool)
                    self.assertEqual(mw.S[key], mw.DEFAULTS[key])

    def test_detail_scale_stays_finite_and_bounded(self):
        for value in (float("inf"), float("-inf"), float("nan"), "NaN", None, [], 10**200):
            with self.subTest(value=value):
                mw.S = dict(mw.DEFAULTS)
                self.write({"detail_scale": value})
                mw.load_settings()
                self.assertTrue(math.isfinite(mw.S["detail_scale"]))
                self.assertGreaterEqual(mw.S["detail_scale"], mw.MIN_DETAIL_SCALE)
                self.assertLessEqual(mw.S["detail_scale"], mw.MAX_DETAIL_SCALE)

    def test_coordinate_boolean_rejected_and_huge_int_clamped(self):
        self.write({"pos": [True, False], "panel_offset": [False, True]})
        mw.load_settings()
        self.assertIsNone(mw.S["pos"])
        self.assertIsNone(mw.S["panel_offset"])
        dummy = SimpleNamespace()
        with patch.object(mw, "monitor_work_area", return_value=(0, 0, 1920, 1080)):
            with patch.object(mw, "D", side_effect=lambda x: round(x)):
                x, y = mw.MemoryWidget._clamped_xy(dummy, 10**200, -10**200, 72, 72)
                self.assertEqual((x, y), (1840, 8))
        self.write({"pos": [10**200, -10**200], "panel_offset": [10**200, -10**200]})
        mw.load_settings()
        self.assertIsNone(mw.S["pos"])
        self.assertIsNone(mw.S["panel_offset"])

    def test_data_directory_permission_denial_does_not_throw_on_save(self):
        with patch.object(mw.os, "makedirs", side_effect=PermissionError("mock protected dir")):
            self.assertFalse(mw.save_settings())


class DeviceEnumerationFaultTests(unittest.TestCase):
    def setUp(self):
        self.saved_cache = gpu_devices._cached_names, gpu_devices._cached_at
        gpu_devices._cached_names, gpu_devices._cached_at = None, 0

    def tearDown(self):
        gpu_devices._cached_names, gpu_devices._cached_at = self.saved_cache

    def test_hotplug_disappears_at_cache_expiry_and_failure_yields_empty(self):
        with patch.object(gpu_devices, "_read_devices", side_effect=[
                ["AMD Radeon RX 6800"], ["Intel UHD Graphics"], []]) as read:
            with patch.object(gpu_devices.time, "monotonic", return_value=100):
                self.assertEqual(gpu_devices.display_name(), "AMD Radeon RX 6800")
            with patch.object(gpu_devices.time, "monotonic", return_value=159):
                self.assertEqual(gpu_devices.display_name(), "AMD Radeon RX 6800")
                self.assertEqual(read.call_count, 1)
            with patch.object(gpu_devices.time, "monotonic", return_value=161):
                self.assertEqual(gpu_devices.display_name(), "Intel UHD Graphics")
            with patch.object(gpu_devices.time, "monotonic", return_value=222):
                self.assertEqual(gpu_devices.display_name(), "")

    def test_both_windows_apis_fail_without_throwing(self):
        with patch.object(gpu_devices, "_setupapi_devices", side_effect=OSError("mock failure")):
            with patch.object(gpu_devices, "_display_devices", side_effect=OSError("mock fallback failure")):
                self.assertEqual(gpu_devices.devices(), [])


class NativeCallbackFaultTests(unittest.TestCase):
    def test_restore_failure_keeps_callback_alive(self):
        callback = object()
        dummy = SimpleNamespace(_wndprocs={1234: (callback, 5678)}, _retired_wndprocs=[])
        with patch.object(mw.tray_icon.user32, "IsWindow", return_value=True):
            with patch.object(mw, "_SetWindowLongPtrW", return_value=0):
                mw.MemoryWidget._restore_one_wndproc(dummy, 1234)
        self.assertIs(dummy._wndprocs[1234][0], callback)
        self.assertEqual(dummy._retired_wndprocs, [])

    def test_restore_success_or_destroyed_window_retires_callback(self):
        for alive in (True, False):
            callback = object()
            dummy = SimpleNamespace(_wndprocs={1234: (callback, 5678)}, _retired_wndprocs=[])
            with patch.object(mw.tray_icon.user32, "IsWindow", return_value=alive):
                with patch.object(mw, "_SetWindowLongPtrW", return_value=9999) as restore:
                    mw.MemoryWidget._restore_one_wndproc(dummy, 1234)
                    self.assertEqual(restore.call_count, int(alive))
            self.assertNotIn(1234, dummy._wndprocs)
            self.assertEqual(dummy._retired_wndprocs, [callback])


if __name__ == "__main__":
    unittest.main(verbosity=2)
