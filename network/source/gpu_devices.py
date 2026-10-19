"""Read complete Windows GPU model names independently of optional sensors.

Only the Python standard library and Windows system DLLs are used. SetupAPI
enumerates present display adapters, including a laptop's inactive discrete GPU.
EnumDisplayDevices is a fallback when SetupAPI is unavailable. This module
never starts a program, initializes a vendor driver, or creates a Tk object.

``devices()`` returns a copy of the cached model list. ``display_name(name)``
keeps a sensor's real model name when one is supplied, so an Intel label cannot
accidentally be paired with an NVIDIA temperature. Without a sensor, it prefers
an NVIDIA/AMD model over Intel and returns the full name without truncation.
Sensor availability is deliberately not represented here.
"""

import ctypes
import ctypes.wintypes as wt
import os
import threading
import time
import uuid


_CACHE_SECONDS = 60.0
_lock = threading.Lock()
_cached_names = None
_cached_at = 0.0

_DIGCF_PRESENT = 0x00000002
_SPDRP_DEVICEDESC = 0
_SPDRP_HARDWAREID = 1
_SPDRP_FRIENDLYNAME = 12
_SPDRP_ENUMERATOR_NAME = 22
_ERROR_INSUFFICIENT_BUFFER = 122
_ERROR_NO_MORE_ITEMS = 259
_DISPLAY_DEVICE_MIRRORING_DRIVER = 0x00000008
_INVALID_HANDLE_VALUE = ctypes.c_void_p(-1).value


class _GUID(ctypes.Structure):
    _fields_ = [("Data1", wt.DWORD), ("Data2", wt.WORD), ("Data3", wt.WORD),
                ("Data4", wt.BYTE * 8)]


class _SP_DEVINFO_DATA(ctypes.Structure):
    _fields_ = [("cbSize", wt.DWORD), ("ClassGuid", _GUID),
                ("DevInst", wt.DWORD), ("Reserved", ctypes.c_size_t)]


class _DISPLAY_DEVICE(ctypes.Structure):
    _fields_ = [("cb", wt.DWORD), ("DeviceName", wt.WCHAR * 32),
                ("DeviceString", wt.WCHAR * 128), ("StateFlags", wt.DWORD),
                ("DeviceID", wt.WCHAR * 128), ("DeviceKey", wt.WCHAR * 128)]


def _system_dll(name):
    # An absolute system DLL path avoids loading a DLL beside a portable exe.
    directory = "Sysnative" if ctypes.sizeof(ctypes.c_void_p) == 4 else "System32"
    path = os.path.join(os.environ.get("SystemRoot", r"C:\Windows"), directory,
                        name)
    # Sysnative exists only for a 32-bit process on 64-bit Windows.
    if directory == "Sysnative" and not os.path.isfile(path):
        path = os.path.join(os.environ.get("SystemRoot", r"C:\Windows"),
                            "System32", name)
    return ctypes.WinDLL(path, use_last_error=True)


def _clean_name(value):
    return " ".join(str(value or "").replace("\x00", " ").split())


def _is_hardware_adapter(name, enumerator="", hardware_ids=()):
    """Remove software/remote displays while retaining real and VM adapters.

    The system's Basic Display Adapter is useful when a GPU vendor driver is
    missing. VMware/Hyper-V adapters also remain valid on a guest computer.
    Their reported names must not be replaced with a guessed physical GPU.
    """
    lower = _clean_name(name).casefold()
    if not lower:
        return False
    software_names = ("remote display", "remote desktop", "basic render driver",
                      "virtual display", "virtual monitor", "rdp display",
                      "rdpudd", "citrix", "parsec", "spacedesk", "iddsample")
    if any(part in lower for part in software_names):
        return False
    if enumerator.upper() in ("SWD", "RDP", "RDPBUS"):
        return False
    ids = tuple(str(value).upper() for value in hardware_ids)
    if any(value.startswith(("ROOT\\RDP", "ROOT\\IDD", "SWD\\")) for value in ids):
        return False
    # ROOT alone is not sufficient to reject: Basic Display and real guest
    # adapters can be root enumerated. Explicit software names/IDs above are
    # the safer distinction than removing every device without a PCI ID.
    return True


def _property(api, handle, info, property_id):
    required = wt.DWORD()
    property_type = wt.DWORD()
    api.SetupDiGetDeviceRegistryPropertyW(
        handle, ctypes.byref(info), property_id, ctypes.byref(property_type),
        None, 0, ctypes.byref(required))
    if not required.value or required.value > 65536:
        return ""
    if ctypes.get_last_error() != _ERROR_INSUFFICIENT_BUFFER:
        return ""
    buffer = (wt.BYTE * required.value)()
    if not api.SetupDiGetDeviceRegistryPropertyW(
            handle, ctypes.byref(info), property_id, ctypes.byref(property_type),
            buffer, ctypes.sizeof(buffer), ctypes.byref(required)):
        return ""
    if property_type.value not in (1, 7):  # REG_SZ or REG_MULTI_SZ
        return ""
    return bytes(buffer).decode("utf-16-le", "replace").rstrip("\x00")


def _setupapi_devices():
    """Return names or None on an API failure; always release the info set."""
    api = _system_dll("setupapi.dll")
    api.SetupDiGetClassDevsW.argtypes = [ctypes.POINTER(_GUID), wt.LPCWSTR,
                                       wt.HWND, wt.DWORD]
    api.SetupDiGetClassDevsW.restype = ctypes.c_void_p
    api.SetupDiEnumDeviceInfo.argtypes = [ctypes.c_void_p, wt.DWORD,
                                        ctypes.POINTER(_SP_DEVINFO_DATA)]
    api.SetupDiEnumDeviceInfo.restype = wt.BOOL
    api.SetupDiGetDeviceRegistryPropertyW.argtypes = [
        ctypes.c_void_p, ctypes.POINTER(_SP_DEVINFO_DATA), wt.DWORD,
        ctypes.POINTER(wt.DWORD), ctypes.POINTER(wt.BYTE), wt.DWORD,
        ctypes.POINTER(wt.DWORD)]
    api.SetupDiGetDeviceRegistryPropertyW.restype = wt.BOOL
    api.SetupDiDestroyDeviceInfoList.argtypes = [ctypes.c_void_p]
    api.SetupDiDestroyDeviceInfoList.restype = wt.BOOL

    # GUID_DEVCLASS_DISPLAY, a setup class rather than a monitor interface.
    guid = _GUID.from_buffer_copy(uuid.UUID(
        "4d36e968-e325-11ce-bfc1-08002be10318").bytes_le)
    handle = api.SetupDiGetClassDevsW(ctypes.byref(guid), None, None,
                                     _DIGCF_PRESENT)
    if not handle or handle == _INVALID_HANDLE_VALUE:
        return None
    names = []
    try:
        # The cap guards against a broken provider that never ends enumeration.
        for index in range(256):
            info = _SP_DEVINFO_DATA()
            info.cbSize = ctypes.sizeof(info)
            if not api.SetupDiEnumDeviceInfo(handle, index, ctypes.byref(info)):
                if ctypes.get_last_error() != _ERROR_NO_MORE_ITEMS:
                    return None
                break
            name = (_property(api, handle, info, _SPDRP_FRIENDLYNAME)
                    or _property(api, handle, info, _SPDRP_DEVICEDESC))
            enumerator = _property(api, handle, info, _SPDRP_ENUMERATOR_NAME)
            hardware_ids = _property(api, handle, info, _SPDRP_HARDWAREID).split("\x00")
            name = _clean_name(name)
            if _is_hardware_adapter(name, enumerator, hardware_ids):
                names.append(name)
        return names
    finally:
        api.SetupDiDestroyDeviceInfoList(handle)


def _display_devices():
    api = _system_dll("user32.dll")
    api.EnumDisplayDevicesW.argtypes = [wt.LPCWSTR, wt.DWORD,
                                      ctypes.POINTER(_DISPLAY_DEVICE), wt.DWORD]
    api.EnumDisplayDevicesW.restype = wt.BOOL
    names = []
    for index in range(256):
        device = _DISPLAY_DEVICE()
        device.cb = ctypes.sizeof(device)
        if not api.EnumDisplayDevicesW(None, index, ctypes.byref(device), 0):
            break
        name = _clean_name(device.DeviceString)
        if (not device.StateFlags & _DISPLAY_DEVICE_MIRRORING_DRIVER
                and _is_hardware_adapter(name, hardware_ids=(device.DeviceID,))):
            names.append(name)
    return names


def _read_devices():
    if os.name != "nt":
        return []
    try:
        names = _setupapi_devices()
    except (OSError, AttributeError, ValueError):
        names = None
    if not names:
        try:
            names = _display_devices()
        except (OSError, AttributeError, ValueError):
            names = []
    unique = {}
    for name in names or ():
        name = _clean_name(name)
        if name:
            unique.setdefault(name.casefold(), name)
    return list(unique.values())


def devices(refresh=False):
    """Return present full model names, cached for 60 seconds (copy on read).

    A settings/device refresh can request ``refresh=True``. Driver absence or
    API failure returns an empty list instead of a guessed GPU model.
    """
    global _cached_names, _cached_at
    now = time.monotonic()
    with _lock:
        if (refresh or _cached_names is None
                or now - _cached_at >= _CACHE_SECONDS):
            _cached_names = _read_devices()
            _cached_at = time.monotonic()
        return list(_cached_names)


def _preference(name):
    lower = name.casefold()
    if "nvidia" in lower:
        return 0
    if "amd" in lower or "radeon" in lower:
        return 1
    if "intel" in lower:
        return 2
    return 3


def display_name(sensor_name=""):
    """Keep a sensor's actual name; otherwise choose a detected model.

    Never combine different adapters' model and sensor values. The heuristic
    below is a display preference only, not proof that a GPU is active/discrete.
    Empty means Windows supplied no usable model and should be shown as unknown.
    """
    sensor_name = _clean_name(sensor_name)
    if sensor_name:
        return sensor_name
    names = devices()
    return min(names, key=_preference) if names else ""
