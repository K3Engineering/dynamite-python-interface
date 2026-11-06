"""Dynamite Sampler Python API."""

import asyncio
import contextlib

from . import discovery as _discovery
from .calibration import Calibration, LoadCell, Unit
from .csv_io import CsvRecorder, read_csv
from .device import (
    UNCONFIGURED,
    AsyncCapture,
    AsyncDynamiteSampler,
    Capture,
    DeviceInfo,
    DynamiteSampler,
)
from .discovery import FoundDevice
from .errors import (
    BufferOverrun,
    CalibrationError,
    ConnectionLost,
    CsvFormatError,
    DeviceNotFound,
    DynamiteError,
    FirmwareCatalogError,
    KvsBusy,
    KvsDeviceError,
    KvsError,
    KvsRejected,
    KvsTimeout,
    MultipleDevicesFound,
    OtaError,
    OtaRejected,
    OtaTimeout,
    ProtocolError,
    ProvisioningError,
    ReadTimeout,
    StreamActive,
    TareError,
    UnitUnavailable,
)
from .packet import Packet
from .recording import Recording

__version__ = "0.1.0"

__all__ = [
    "discover",
    "adiscover",
    "connect",
    "aconnect",
    "FoundDevice",
    "DynamiteSampler",
    "AsyncDynamiteSampler",
    "DeviceInfo",
    "UNCONFIGURED",
    "Packet",
    "Recording",
    "Capture",
    "AsyncCapture",
    "read_csv",
    "Unit",
    "Calibration",
    "LoadCell",
    "CsvRecorder",
    "DynamiteError",
    "CsvFormatError",
    "FirmwareCatalogError",
    "DeviceNotFound",
    "MultipleDevicesFound",
    "ConnectionLost",
    "ReadTimeout",
    "BufferOverrun",
    "StreamActive",
    "TareError",
    "UnitUnavailable",
    "ProtocolError",
    "ProvisioningError",
    "CalibrationError",
    "KvsError",
    "KvsRejected",
    "KvsBusy",
    "KvsDeviceError",
    "KvsTimeout",
    "OtaError",
    "OtaRejected",
    "OtaTimeout",
]


async def adiscover(timeout: float = 5.0) -> list[FoundDevice]:
    """Discover all Dynamite Samplers in range, sorted by RSSI descending."""
    return await _discovery.discover(timeout)


def discover(timeout: float = 5.0) -> list[FoundDevice]:
    """Synchronous discovery for callers not already running an event loop."""
    return asyncio.run(_discovery.discover(timeout))


@contextlib.asynccontextmanager
async def aconnect(address: str | FoundDevice | None = None):
    """Connect to one device as an async context manager."""
    dev = await AsyncDynamiteSampler.connect(address)
    try:
        yield dev
    finally:
        await dev.close()


def connect(address: str | FoundDevice | None = None) -> DynamiteSampler:
    """Connect to one device through the synchronous facade."""
    return DynamiteSampler.connect(address)
