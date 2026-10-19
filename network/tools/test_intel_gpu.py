"""Mocked Intel GPU counter/driver faults; no real DLL, app or user settings."""
import ctypes as c
from pathlib import Path
import sys
from types import SimpleNamespace
import unittest
from unittest.mock import Mock, patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / 'source'))
import gpu_info as g
import gpu_windows as w


class IntelTests(unittest.TestCase):
    def engine(self, rows, growth=False, signed=False):
        # Use an independent C ABI definition, not the implementation's type.
        class Value(c.Structure):
            _fields_ = [('status', c.c_uint32), ('number', c.c_double)]
        class Item(c.Structure):
            _fields_ = [('name', c.c_wchar_p), ('value', Value)]
        items = (Item * len(rows))()
        for i, (pid, luid, eng, value, status) in enumerate(rows):
            items[i].name = 'pid_%d_luid_0x%08x_0x%08x_phys_0_eng_%d_engtype_3D' % (pid, *luid, eng)
            items[i].value = Value(status, value)
        calls = []
        def formatted(counter, fmt, size, count, buffer):
            calls.append(bool(buffer))
            c.cast(count, c.POINTER(c.c_uint32)).contents.value = len(rows)
            c.cast(size, c.POINTER(c.c_uint32)).contents.value = c.sizeof(items)
            if not buffer or (growth and len(calls) == 2):
                return c.c_int32(g._PDH_MORE_DATA).value if signed else g._PDH_MORE_DATA
            c.memmove(buffer, items, c.sizeof(items))
            return 0
        api = SimpleNamespace(PdhCollectQueryData=lambda *_: 0,
                              PdhGetFormattedCounterArrayW=formatted, PdhCloseQuery=Mock())
        device = g._PdhGpuEngine(api, 1, 2, 'Intel Graphics', (0, 42))
        device._test_keepalive = items
        return device, calls

    def test_counter_abi_and_path(self):
        self.assertEqual(g._PdhGpuEngine._PATH, '\\GPU Engine(*)\\Utilization Percentage')
        self.assertEqual(c.sizeof(g._PdhCounterValue), 16)
        self.assertEqual(g._PdhCounterValue.data.offset, 8)
        self.assertEqual(c.sizeof(g._PdhValueItem), 24 if c.sizeof(c.c_void_p) == 8 else 24)
        self.assertEqual(c.sizeof(w.InitArgs), 36)
        self.assertEqual(c.sizeof(w.TempProperties), 24)
        if c.sizeof(c.c_void_p) == 8:
            self.assertEqual(c.sizeof(w.DeviceProperties), 320)

    def test_aggregate_processes_by_engine_and_filter_other_gpu(self):
        d, _ = self.engine([(1,(0,42),0,20,0), (2,(0,42),0,30,1),
                            (3,(0,42),1,40,0), (4,(0,99),0,99,0)])
        self.assertEqual(d.read(), ('Intel Graphics', None, 50, 0, 0))

    def test_ignore_invalid_status_nan_negative_and_wrong_device(self):
        d, _ = self.engine([(1,(0,42),0,99,0x800007d6), (2,(0,42),0,float('nan'),0),
                            (3,(0,42),0,-1,0), (4,(0,99),0,80,0)])
        self.assertIsNone(d.read())

    def test_zero_is_valid_and_aggregate_is_capped(self):
        for values, expected in [([0], 0), ([80,90], 100)]:
            d, _ = self.engine([(i,(0,42),0,v,0) for i,v in enumerate(values)])
            self.assertEqual(d.read()[2], expected)

    def test_retry_buffer_growth_and_signed_more_data(self):
        d, calls = self.engine([(1,(0,42),0,37,0)], growth=True, signed=True)
        self.assertEqual(d.read()[2], 37)
        self.assertEqual(calls, [False, True, True])

    def test_count_cannot_overrun_buffer(self):
        d, _ = self.engine([(1,(0,42),0,37,0)])
        normal = d.api.PdhGetFormattedCounterArrayW
        def bad(*args):
            result = normal(*args)
            if args[-1]:
                c.cast(args[-2], c.POINTER(c.c_uint32)).contents.value = 999999
            return result
        d.api.PdhGetFormattedCounterArrayW = bad
        self.assertIsNone(d.read())

    def test_temperature_missing_does_not_drop_utilization_and_close_once(self):
        d, _ = self.engine([(1,(0,42),0,37,0)])
        d.temperature = SimpleNamespace(read=lambda: None, close=Mock())
        self.assertEqual(d.read()[1:3], (None,37))
        d.close(); d.close()
        d.temperature.close.assert_called_once()
        d.api.PdhCloseQuery.assert_called_once()
        self.assertIsNone(d.read())

    def test_driver_temperature_finite_range_and_independent_failures(self):
        def read(sensor, output):
            c.cast(output, c.POINTER(c.c_double)).contents.value = sensor
            return 0
        for sensors, expected in [([55.7],56), ([float('nan'),-1,151],None), ([50,61],61)]:
            api = SimpleNamespace(ctlTemperatureGetState=read, ctlClose=Mock())
            sensor = w.IntelTemperature(api, 1, sensors)
            self.assertEqual(sensor.read(),expected)
            sensor.close(); sensor.close()
            self.assertIsNone(sensor.read())
            api.ctlClose.assert_called_once()

    def test_nvml_temperature_unavailable_keeps_utilization(self):
        def utilization(handle, out):
            c.cast(out,c.POINTER(g._NvmlUtilization)).contents.gpu = 37
            return 0
        device = g._NvmlDevice(SimpleNamespace(nvmlDeviceGetTemperature=lambda *_:3,
                   nvmlDeviceGetUtilizationRates=utilization, nvmlDeviceGetMemoryInfo=lambda *_:3))
        self.assertEqual(device.read()[1:3], (None,37))

    def test_initial_transient_pdh_read_retries_and_releases(self):
        g._stop.clear()
        device = SimpleNamespace(read=Mock(side_effect=[None, ('Intel Graphics',None,37,0,0)]), close=Mock())
        with patch.object(g._NvmlDevice,'open',return_value=None), patch.object(g,'_query',return_value=None), \
                patch.object(g._PdhGpuEngine,'open',return_value=device), patch.object(g,'INTERVAL_PDH',0), \
                patch.object(g,'_loop') as loop:
            g._worker()
            self.assertEqual(loop.call_args.args[0](), ('Intel Graphics',None,37,0,0))
        self.assertEqual(device.read.call_count,2)
        device.close.assert_called_once()

    def test_intel_temperature_selects_gpu_sensor_on_same_adapter(self):
        def init(args, handle):
            actual = c.cast(args,c.POINTER(w.InitArgs)).contents
            self.assertEqual(actual.flags,1)
            self.assertEqual(actual.app_version,65536)
            c.cast(handle,c.POINTER(c.c_void_p)).contents.value = 123
            return 0
        def enumerate_handles(values):
            def enum(handle,count,out):
                c.cast(count,c.POINTER(c.c_uint32)).contents.value = len(values)
                if out:
                    for i,v in enumerate(values):
                        out[i] = v
                return 0
            return enum
        def props(device,out):
            p = c.cast(out,c.POINTER(w.DeviceProperties)).contents
            p.vendor,p.device_type = 0x8086,1
            luid = c.cast(p.device_id,c.POINTER(w.LUID)).contents
            luid.low,luid.high = (42 if device.value == 2 else 99),0
            return 0
        def temp_props(sensor,out):
            p = c.cast(out,c.POINTER(w.TempProperties)).contents
            p.type = 1 if sensor.value == 20 else 2
            return 0
        def temp_read(sensor,out):
            c.cast(out,c.POINTER(c.c_double)).contents.value = 56
            return 0
        api = SimpleNamespace(ctlInit=Mock(side_effect=init),ctlClose=Mock(return_value=0),
            ctlEnumerateDevices=Mock(side_effect=enumerate_handles([1,2])),
            ctlGetDeviceProperties=Mock(side_effect=props),
            ctlEnumTemperatureSensors=Mock(side_effect=enumerate_handles([10,20])),
            ctlTemperatureGetProperties=Mock(side_effect=temp_props),
            ctlTemperatureGetState=Mock(side_effect=temp_read))
        with patch.object(w,'system_dll',return_value=api), patch.object(w,'release_dll') as release:
            sensor = w.IntelTemperature.open((0,42))
            self.assertIsNotNone(sensor)
            self.assertEqual([s.value for s in sensor.sensors],[20])
            self.assertEqual(sensor.read(),56)
            release.assert_not_called()
            sensor.close(); sensor.close()
            release.assert_called_once_with(api)
            api.ctlClose.assert_called_once()
        # A driver with no sensor on the chosen adapter must still release its
        # session, and must not use temperature from the other adapter.
        api.ctlClose.reset_mock()
        with patch.object(w,'system_dll',return_value=api), patch.object(w,'release_dll') as release:
            self.assertIsNone(w.IntelTemperature.open((0,777)))
            release.assert_called_once_with(api)
            api.ctlClose.assert_called_once()

    def test_missing_intel_temperature_dll_is_optional(self):
        with patch.object(w,'system_dll',side_effect=OSError('driver missing')):
            self.assertIsNone(w.IntelTemperature.open((0,42)))


if __name__ == '__main__':
    unittest.main(verbosity=2)
