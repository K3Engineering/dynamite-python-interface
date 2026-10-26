"""Dynamite Sampler Python API."""

import contextlib

from . import discovery as _discovery
from .calibration import Calibration, LoadCell, Unit
from .csv_io import CsvRecorder, read_csv
from .device import UNCONFIGURED, AsyncDynamiteSampler, DeviceInfo
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
    "adiscover",
    "aconnect",
    "FoundDevice",
    "AsyncDynamiteSampler",
    "DeviceInfo",
    "UNCONFIGURED",
    "Packet",
    "Recording",
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


@contextlib.asynccontextmanager
async def aconnect(address: str | FoundDevice | None = None):
    """Connect to one device as an async context manager."""
    dev = await AsyncDynamiteSampler.connect(address)
    try:
        yield dev
    finally:
        await dev.close()
