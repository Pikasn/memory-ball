"""Read-only native enumeration plus branch tests for portable GPU names."""

import ctypes
import os
from pathlib import Path
import sys
import unittest
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "source"))
import gpu_devices as gpu


class GPUDeviceTests(unittest.TestCase):
    def setUp(self):
        gpu._cached_names = None
        gpu._cached_at = 0

    def test_sensor_name_preserved_without_enumeration(self):
        with patch.object(gpu, "devices", side_effect=AssertionError("should not query")):
            self.assertEqual(gpu.display_name("  AMD Radeon RX 7800 XT  "),
                             "AMD Radeon RX 7800 XT")

    def test_dual_adapter_preference_and_intel_only(self):
        with patch.object(gpu, "devices", return_value=["Intel(R) Iris(R) Xe Graphics",
                                                       "NVIDIA GeForce GTX 1650"]):
            self.assertEqual(gpu.display_name(), "NVIDIA GeForce GTX 1650")
            self.assertEqual(gpu.display_name("Intel(R) Iris(R) Xe Graphics"),
                             "Intel(R) Iris(R) Xe Graphics")
        with patch.object(gpu, "devices", return_value=["Intel(R) Iris(R) Xe Graphics",
                                                       "AMD Radeon RX 6800"]):
            self.assertEqual(gpu.display_name(), "AMD Radeon RX 6800")
        with patch.object(gpu, "devices", return_value=["Intel(R) Arc(TM) A770 Graphics"]):
            self.assertEqual(gpu.display_name(), "Intel(R) Arc(TM) A770 Graphics")
        with patch.object(gpu, "devices", return_value=[]):
            self.assertEqual(gpu.display_name(), "")

    def test_cache_copy_expiry_and_force_refresh(self):
        with patch.object(gpu, "_read_devices", return_value=["AMD Radeon Graphics"]) as read:
            with patch.object(gpu.time, "monotonic", return_value=100):
                names = gpu.devices()
                names.clear()
                self.assertEqual(gpu.devices(), ["AMD Radeon Graphics"])
                self.assertEqual(read.call_count, 1)
            with patch.object(gpu.time, "monotonic", return_value=161):
                gpu.devices()
                self.assertEqual(read.call_count, 2)
                gpu.devices(refresh=True)
                self.assertEqual(read.call_count, 3)

    def test_setupapi_preferred_and_names_deduplicated(self):
        with patch.object(gpu.os, "name", "nt"):
            with patch.object(gpu, "_setupapi_devices", return_value=[
                    "AMD Radeon RX 6800", "amd radeon rx 6800", "Intel UHD Graphics"]):
                with patch.object(gpu, "_display_devices", side_effect=AssertionError("fallback")):
                    self.assertEqual(gpu._read_devices(), ["AMD Radeon RX 6800",
                                                          "Intel UHD Graphics"])

    def test_empty_or_failed_setupapi_uses_fallback(self):
        with patch.object(gpu.os, "name", "nt"):
            for value in (None, []):
                with patch.object(gpu, "_setupapi_devices", return_value=value):
                    with patch.object(gpu, "_display_devices", return_value=["Intel HD Graphics"]):
                        self.assertEqual(gpu._read_devices(), ["Intel HD Graphics"])
            with patch.object(gpu, "_setupapi_devices", side_effect=OSError("unavailable")):
                with patch.object(gpu, "_display_devices", return_value=["NVIDIA Tesla T4"]):
                    self.assertEqual(gpu._read_devices(), ["NVIDIA Tesla T4"])
                with patch.object(gpu, "_display_devices", side_effect=OSError("unavailable")):
                    self.assertEqual(gpu._read_devices(), [])

    def test_software_filtered_but_basic_and_guest_kept(self):
        for name in ("Microsoft Remote Display Adapter", "Microsoft Basic Render Driver",
                     "Parsec Virtual Display Adapter", "Citrix Indirect Display Adapter",
                     "IddSampleDriver Device"):
            self.assertFalse(gpu._is_hardware_adapter(name), name)
        self.assertFalse(gpu._is_hardware_adapter("Display", "SWD"))
        self.assertFalse(gpu._is_hardware_adapter("Display", hardware_ids=[r"ROOT\RDPUDD"]))
        for name in ("Microsoft Basic Display Adapter", "VMware SVGA 3D",
                     "Microsoft Hyper-V Video", "Intel(R) UHD Graphics", "AMD Radeon RX 6800"):
            self.assertTrue(gpu._is_hardware_adapter(name), name)

    @unittest.skipUnless(os.name == "nt", "Windows native ABI")
    def test_native_struct_sizes(self):
        self.assertEqual(ctypes.sizeof(gpu._GUID), 16)
        self.assertEqual(ctypes.sizeof(gpu._SP_DEVINFO_DATA),
                         32 if ctypes.sizeof(ctypes.c_void_p) == 8 else 28)
        self.assertEqual(ctypes.sizeof(gpu._DISPLAY_DEVICE), 840)


if __name__ == "__main__":
    suite = unittest.defaultTestLoader.loadTestsFromTestCase(GPUDeviceTests)
    result = unittest.TextTestRunner(verbosity=2).run(suite)
    if not result.wasSuccessful():
        raise SystemExit(1)
    print("NATIVE_SETUPAPI_NAMES:", gpu._setupapi_devices() if os.name == "nt" else [])
    print("NATIVE_DISPLAY_FALLBACK_NAMES:", gpu._display_devices() if os.name == "nt" else [])
    print("GPU_DEVICES_API:", gpu.devices(refresh=True))
    print("GPU_DISPLAY_NAME:", gpu.display_name())
