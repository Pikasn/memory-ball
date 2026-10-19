"""Windows adapter identities and optional Intel driver temperature sensors.

Uses only system-installed DLLs. DXGI LUIDs keep GPU counters paired with the
correct adapter on hybrid laptops. IGCL temperature is optional and never
substitutes a CPU, memory or board sensor for a GPU sensor.
"""
import ctypes as c
import ctypes.wintypes as w
import math
import os
import uuid


class GUID(c.Structure):
    _fields_ = [('a', w.DWORD), ('b', w.WORD), ('d', w.WORD), ('e', w.BYTE * 8)]


class LUID(c.Structure):
    _fields_ = [('low', w.DWORD), ('high', w.LONG)]

    def key(self):
        return self.high & 0xffffffff, self.low


class AdapterDesc(c.Structure):
    _fields_ = [('description', w.WCHAR * 128), ('vendor', w.UINT),
                ('device', w.UINT), ('subsystem', w.UINT), ('revision', w.UINT),
                ('video_memory', c.c_size_t), ('system_memory', c.c_size_t),
                ('shared_memory', c.c_size_t), ('luid', LUID), ('flags', w.UINT)]


def system_dll(name, loader=c.WinDLL):
    # Both DLLs match the process architecture; use System32 and its normal
    # WoW64 redirection for 32-bit processes, rather than loading a 64-bit DLL.
    return loader(os.path.join(os.environ.get('SystemRoot', r'C:\Windows'),
                               'System32', name), winmode=0x00000800)


def release_dll(api):
    handle = getattr(api, '_handle', None)
    if isinstance(handle, int) and handle:
        kernel = c.WinDLL('kernel32', use_last_error=True)
        kernel.FreeLibrary.argtypes = [c.c_void_p]
        kernel.FreeLibrary.restype = w.BOOL
        kernel.FreeLibrary(c.c_void_p(handle))


def _com_method(pointer, index, result, *args):
    table = c.cast(pointer, c.POINTER(c.POINTER(c.c_void_p))).contents
    return c.WINFUNCTYPE(result, c.c_void_p, *args)(table[index])


def intel_adapters():
    """Return [(full_name, (luid_high, luid_low))] for live Intel adapters."""
    if os.name != 'nt':
        return []
    factory = c.c_void_p()
    api = None
    adapters = []
    try:
        api = system_dll('dxgi.dll')
        create = api.CreateDXGIFactory1
        create.argtypes = [c.POINTER(GUID), c.POINTER(c.c_void_p)]
        create.restype = w.LONG
        iid = GUID.from_buffer_copy(uuid.UUID('770aae78-f26f-4dba-a829-253c83d1b387').bytes_le)
        if create(c.byref(iid), c.byref(factory)) != 0 or not factory.value:
            return []
        enum = _com_method(factory, 12, w.LONG, w.UINT, c.POINTER(c.c_void_p))
        for index in range(64):
            adapter = c.c_void_p()
            if enum(factory, index, c.byref(adapter)) != 0 or not adapter.value:
                break
            try:
                desc = AdapterDesc()
                get_desc = _com_method(adapter, 10, w.LONG, c.POINTER(AdapterDesc))
                if get_desc(adapter, c.byref(desc)) == 0 and desc.vendor == 0x8086 and not desc.flags & 2:
                    adapters.append((desc.description.strip(), desc.luid.key()))
            finally:
                _com_method(adapter, 2, w.ULONG)(adapter)
    except (AttributeError, OSError, ValueError):
        return []
    finally:
        if factory.value:
            _com_method(factory, 2, w.ULONG)(factory)
        if api is not None:
            release_dll(api)
    return adapters


class InitArgs(c.Structure):
    _fields_ = [('size', c.c_uint32), ('version', c.c_uint8),
                ('app_version', c.c_uint32), ('flags', c.c_uint32),
                ('supported_version', c.c_uint32), ('uid', GUID)]


class Firmware(c.Structure):
    _fields_ = [('major', c.c_uint64), ('minor', c.c_uint64), ('build', c.c_uint64)]


class BDF(c.Structure):
    _fields_ = [('bus', c.c_uint8), ('device', c.c_uint8), ('function', c.c_uint8)]


class DeviceProperties(c.Structure):
    # Intel igcl_api.h, ctl_device_adapter_properties_t. Version zero keeps
    # fields supported by older driver versions; later fields remain zero.
    _fields_ = [('size', c.c_uint32), ('version', c.c_uint8),
                ('device_id', c.c_void_p), ('device_id_size', c.c_uint32),
                ('device_type', c.c_int32), ('functions', c.c_uint32),
                ('driver_version', c.c_uint64), ('firmware', Firmware),
                ('vendor', c.c_uint32), ('device', c.c_uint32), ('revision', c.c_uint32),
                ('eus', c.c_uint32), ('subslices', c.c_uint32), ('slices', c.c_uint32),
                ('name', c.c_char * 100), ('flags', c.c_uint32),
                ('frequency', c.c_uint32), ('subsystem', c.c_uint16),
                ('subvendor', c.c_uint16), ('bdf', BDF), ('cores', c.c_uint32),
                ('reserved', c.c_char * 108)]


class TempProperties(c.Structure):
    _fields_ = [('size', c.c_uint32), ('version', c.c_uint8),
                ('type', c.c_int32), ('maximum', c.c_double)]


class IntelTemperature:
    def __init__(self, api, handle, sensors):
        self.api, self.handle, self.sensors = api, handle, sensors
        self.closed = False

    @staticmethod
    def _enumerate(function, handle):
        count = c.c_uint32()
        if function(handle, c.byref(count), None) != 0 or not 0 < count.value <= 64:
            return []
        capacity = count.value
        values = (c.c_void_p * capacity)()
        if function(handle, c.byref(count), values) != 0 or count.value > capacity:
            return []
        return [c.c_void_p(values[i]) for i in range(count.value) if values[i]]

    @classmethod
    def open(cls, luid):
        if os.name != 'nt' or c.sizeof(c.c_void_p) != 8:
            return None
        handle = c.c_void_p()
        api = None
        transferred = False
        try:
            # IGCL uses __cdecl, not __stdcall. Loader path follows Intel's
            # official cApiWrapper.cpp; never load DLLs next to the EXE.
            api = system_dll('ControlLib.dll', c.CDLL)
            definitions = {
                'ctlInit': [c.POINTER(InitArgs), c.POINTER(c.c_void_p)],
                'ctlClose': [c.c_void_p],
                'ctlEnumerateDevices': [c.c_void_p, c.POINTER(c.c_uint32), c.POINTER(c.c_void_p)],
                'ctlGetDeviceProperties': [c.c_void_p, c.POINTER(DeviceProperties)],
                'ctlEnumTemperatureSensors': [c.c_void_p, c.POINTER(c.c_uint32), c.POINTER(c.c_void_p)],
                'ctlTemperatureGetProperties': [c.c_void_p, c.POINTER(TempProperties)],
                'ctlTemperatureGetState': [c.c_void_p, c.POINTER(c.c_double)],
            }
            for name, args in definitions.items():
                function = getattr(api, name)
                function.argtypes, function.restype = args, c.c_uint32
            args = InitArgs(size=c.sizeof(InitArgs), app_version=1 << 16, flags=1)
            if api.ctlInit(c.byref(args), c.byref(handle)) != 0 or not handle.value:
                return None
            for device in cls._enumerate(api.ctlEnumerateDevices, handle):
                device_luid = LUID()
                properties = DeviceProperties(size=c.sizeof(DeviceProperties),
                                              device_id=c.addressof(device_luid),
                                              device_id_size=c.sizeof(LUID))
                if api.ctlGetDeviceProperties(device, c.byref(properties)) != 0:
                    continue
                if properties.vendor != 0x8086 or properties.device_type != 1 or device_luid.key() != luid:
                    continue
                sensors = []
                for sensor in cls._enumerate(api.ctlEnumTemperatureSensors, device):
                    properties = TempProperties(size=c.sizeof(TempProperties))
                    if api.ctlTemperatureGetProperties(sensor, c.byref(properties)) == 0 and properties.type == 1:
                        sensors.append(sensor)
                if sensors:
                    transferred = True
                    return cls(api, handle, sensors)
        except (AttributeError, OSError, ValueError):
            return None
        finally:
            if not transferred and api is not None:
                try:
                    if handle.value:
                        api.ctlClose(handle)
                finally:
                    release_dll(api)
        return None

    def read(self):
        if self.closed:
            return None
        values = []
        for sensor in self.sensors:
            value = c.c_double()
            try:
                if self.api.ctlTemperatureGetState(sensor, c.byref(value)) == 0:
                    if math.isfinite(value.value) and 0 <= value.value <= 150:
                        values.append(value.value)
            except (AttributeError, OSError, ValueError):
                pass
        return round(max(values)) if values else None

    def close(self):
        if not self.closed:
            self.closed = True
            try:
                self.api.ctlClose(self.handle)
            except (AttributeError, OSError, ValueError):
                pass
            finally:
                release_dll(self.api)
