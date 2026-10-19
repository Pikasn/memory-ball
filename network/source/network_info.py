"""Local network transfer rates, without dependencies or remote requests.

The host calls ``NetworkSampler.sample()`` from its existing timer. Calls are
throttled to once a second; there are no worker threads, Tk calls, subprocesses,
registry writes, or persistent files. Rate values are bytes per second, not
bits per second. ``None`` in history marks an interval that cannot be measured.
"""

from __future__ import annotations

import ctypes
from collections import deque
from dataclasses import dataclass
import math
import os
import time


_U8 = ctypes.c_uint8
_U16 = ctypes.c_uint16
_U32 = ctypes.c_uint32
_U64 = ctypes.c_uint64
_MAX_INTERFACES = 65536
_API = None


class _GUID(ctypes.Structure):
    _fields_ = [("Data1", _U32), ("Data2", _U16), ("Data3", _U16),
                ("Data4", _U8 * 8)]


class _MIB_IF_ROW2(ctypes.Structure):
    # Natural Windows ABI alignment is essential: the byte-sized flags are
    # followed by 32-bit enums, and the counters are aligned to eight bytes.
    _fields_ = [
        ("InterfaceLuid", _U64), ("InterfaceIndex", _U32),
        ("InterfaceGuid", _GUID), ("Alias", ctypes.c_wchar * 257),
        ("Description", ctypes.c_wchar * 257),
        ("PhysicalAddressLength", _U32), ("PhysicalAddress", _U8 * 32),
        ("PermanentPhysicalAddress", _U8 * 32),
        ("Mtu", _U32), ("Type", _U32), ("TunnelType", _U32),
        ("MediaType", _U32), ("PhysicalMediumType", _U32),
        ("AccessType", _U32), ("DirectionType", _U32),
        ("InterfaceAndOperStatusFlags", _U8),
        ("OperStatus", _U32), ("AdminStatus", _U32),
        ("MediaConnectState", _U32), ("NetworkGuid", _GUID),
        ("ConnectionType", _U32), ("TransmitLinkSpeed", _U64),
        ("ReceiveLinkSpeed", _U64), ("InOctets", _U64),
        ("InUcastPkts", _U64), ("InNUcastPkts", _U64),
        ("InDiscards", _U64), ("InErrors", _U64),
        ("InUnknownProtos", _U64), ("InUcastOctets", _U64),
        ("InMulticastOctets", _U64), ("InBroadcastOctets", _U64),
        ("OutOctets", _U64), ("OutUcastPkts", _U64),
        ("OutNUcastPkts", _U64), ("OutDiscards", _U64),
        ("OutErrors", _U64), ("OutUcastOctets", _U64),
        ("OutMulticastOctets", _U64), ("OutBroadcastOctets", _U64),
        ("OutQLen", _U64),
    ]


class _MIB_IF_TABLE2(ctypes.Structure):
    _fields_ = [("NumEntries", _U32), ("Table", _MIB_IF_ROW2 * 1)]


@dataclass(frozen=True)
class AdapterCounters:
    """Immutable copy of one interface; no pointers survive FreeMibTable."""

    identity: int
    name: str
    received_bytes: int
    sent_bytes: int
    interface_type: int = 6
    tunnel_type: int = 0
    flags: int = 1
    oper_status: int = 1


@dataclass(frozen=True)
class NetworkReading:
    timestamp: float
    download_bps: float = 0.0
    upload_bps: float = 0.0
    available: bool = False
    status: str = "starting"
    adapter_count: int = 0
    adapter_names: tuple[str, ...] = ()
    error: str = ""


def _api():
    global _API
    if _API is None:
        if os.name != "nt":
            raise OSError("Network transfer monitoring requires Windows")
        # A wrong ctypes layout would read memory past the native rows.
        if (ctypes.sizeof(_MIB_IF_ROW2) != 1352
                or _MIB_IF_TABLE2.Table.offset != 8
                or _MIB_IF_ROW2.InOctets.offset != 1208
                or _MIB_IF_ROW2.OutOctets.offset != 1280):
            raise OSError("Unsupported Windows network structure layout")
        dll = ctypes.WinDLL("iphlpapi", use_last_error=True)
        dll.GetIfTable2.argtypes = [ctypes.POINTER(ctypes.c_void_p)]
        dll.GetIfTable2.restype = _U32
        dll.FreeMibTable.argtypes = [ctypes.c_void_p]
        dll.FreeMibTable.restype = None
        _API = dll
    return _API


def read_counters() -> tuple[AdapterCounters, ...]:
    """Read the local interface table once and always release its allocation."""
    dll = _api()
    pointer = ctypes.c_void_p()
    result = dll.GetIfTable2(ctypes.byref(pointer))
    try:
        if result:
            raise OSError(int(result), "GetIfTable2 failed")
        if not pointer.value:
            raise OSError("GetIfTable2 returned no interface table")
        count = _U32.from_address(pointer.value).value
        if count > _MAX_INTERFACES:
            raise OSError("GetIfTable2 returned an invalid interface count")
        rows = (_MIB_IF_ROW2 * count).from_address(
            pointer.value + _MIB_IF_TABLE2.Table.offset)
        return tuple(AdapterCounters(
            int(row.InterfaceLuid), row.Alias or row.Description,
            int(row.InOctets), int(row.OutOctets), int(row.Type),
            int(row.TunnelType), int(row.InterfaceAndOperStatusFlags),
            int(row.OperStatus)) for row in rows)
    finally:
        if pointer.value:
            dll.FreeMibTable(pointer)


def selected_adapters(rows) -> tuple[AdapterCounters, ...]:
    """Prefer active hardware to avoid counting VPN traffic a second time.

    On virtual machines that expose no hardware interface, active non-tunnel
    Ethernet/Wi-Fi/PPP/mobile interfaces are used as a fallback. This measures
    interface traffic, including LAN transfers, rather than internet bandwidth.
    """
    eligible = []
    for row in rows:
        # The native API supplies uint64 integers. An invalid alternate
        # provider must fail before it can publish NaN/Infinity or fabricate
        # a positive delta from negative counters into the UI/history.
        for counter in (row.received_bytes, row.sent_bytes):
            if type(counter) is not int or not 0 <= counter <= 2 ** 64 - 1:
                raise ValueError("Network counters must be uint64 integers")
        if (row.oper_status == 1 and row.interface_type not in (24, 131)
                and row.tunnel_type == 0 and not row.flags & (2 | 16 | 32 | 128)):
            eligible.append(row)
    eligible = tuple(eligible)
    physical = tuple(row for row in eligible if row.flags & 1)
    if physical:
        return physical
    return tuple(row for row in eligible
                 if row.interface_type in (6, 23, 71, 243, 244))


class NetworkSampler:
    """One-second snapshots and a bounded ten-minute timestamped history.

    ``available`` means an eligible adapter exists and the API works. Status
    ``starting`` means a baseline is being established, ``active`` means a
    rate was measured, ``disconnected`` means no eligible adapter is up, and
    ``unavailable`` means the API failed. ``revision`` advances only when a
    new snapshot is taken. An injected reader is supported for deterministic
    tests; production uses ``read_counters``.
    """

    def __init__(self, reader=None, interval=1.0, history_seconds=600.0,
                 max_gap_seconds=5.0):
        self.interval = float(interval)
        self.history_seconds = float(history_seconds)
        self.max_gap_seconds = float(max_gap_seconds)
        if (not all(math.isfinite(value) for value in (
                self.interval, self.history_seconds, self.max_gap_seconds))
                or self.interval <= 0.0 or self.history_seconds < 600.0
                or self.max_gap_seconds <= self.interval):
            raise ValueError("Invalid network sampling bounds")
        self._reader = reader or read_counters
        self._previous = {}
        self._previous_time = None
        self._last_sample_time = None
        self._history = deque(maxlen=math.ceil(self.history_seconds / self.interval) + 2)
        self.reading = NetworkReading(0.0)
        self.revision = 0

    def sample(self, now=None) -> NetworkReading:
        now = time.monotonic() if now is None else float(now)
        if not math.isfinite(now):
            raise ValueError("Network sample time must be finite")
        if self._last_sample_time is not None:
            elapsed = now - self._last_sample_time
            if 0.0 <= elapsed < self.interval:
                return self.reading
            if elapsed < 0.0:
                self._history.clear()
                self._previous.clear()
                self._previous_time = None
        self._last_sample_time = now
        history_down = history_up = None
        try:
            adapters = selected_adapters(self._reader())
            current = {row.identity: row for row in adapters}
            if not current:
                reading = NetworkReading(now, status="disconnected")
            else:
                dt = (now - self._previous_time
                      if self._previous_time is not None else 0.0)
                down = up = 0
                comparable = 0
                if 0.0 < dt <= self.max_gap_seconds:
                    for identity, row in current.items():
                        before = self._previous.get(identity)
                        if (before is None
                                or row.received_bytes < before.received_bytes
                                or row.sent_bytes < before.sent_bytes):
                            continue
                        down += row.received_bytes - before.received_bytes
                        up += row.sent_bytes - before.sent_bytes
                        comparable += 1
                status = "active" if comparable else "starting"
                reading = NetworkReading(now, down / dt if comparable else 0.0,
                    up / dt if comparable else 0.0, True, status, len(current),
                    tuple(row.name for row in adapters))
                if comparable:
                    history_down, history_up = reading.download_bps, reading.upload_bps
            self._previous = current
            self._previous_time = now if current else None
        except Exception as exc:
            self._previous.clear()
            self._previous_time = None
            reading = NetworkReading(now, status="unavailable", error=str(exc)[:240])
        self.reading = reading
        self._history.append((now, history_down, history_up))
        cutoff = now - self.history_seconds
        while self._history and self._history[0][0] < cutoff:
            self._history.popleft()
        self.revision += 1
        return reading

    def history(self, window_seconds, now=None):
        """Return points in the requested monotonic-time window; gaps are None."""
        window = float(window_seconds)
        now = time.monotonic() if now is None else float(now)
        if (not math.isfinite(window) or window < 0.0
                or not math.isfinite(now)):
            raise ValueError("Network history window must be finite and nonnegative")
        return [point for point in self._history if now - window <= point[0] <= now]


def format_rate(bytes_per_second):
    """Return (number, unit) using decimal B/s, KB/s, MB/s, or GB/s."""
    value = float(bytes_per_second)
    if not math.isfinite(value) or value < 0.0:
        raise ValueError("Network speed must be finite and nonnegative")
    for unit in ("B/s", "KB/s", "MB/s", "GB/s"):
        if value < 1000.0 or unit == "GB/s":
            return (f"{value:.0f}" if unit == "B/s" else f"{value:.1f}", unit)
        value /= 1000.0
