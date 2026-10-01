"""Dynamite Sampler Python API.

import dynamite_sampler as dms

with dms.connect() as dev:
    recording = dev.read(n=1000, units="mV/V", timeout=5.0)
    print(recording.data.shape)

rec = dms.read_csv("session.csv")  # the file twin of dev.read()
"""

import asyncio
import contextlib

from . import discovery as _discovery
from .assemble import BlockAssembler
from .block import Block
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
    "Block",
    "BlockAssembler",
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


def discover(timeout: float = 5.0) -> list[FoundDevice]:
    """All Dynamite Samplers in range, sorted by RSSI descending."""
    return asyncio.run(_discovery.discover(timeout))


async def adiscover(timeout: float = 5.0) -> list[FoundDevice]:
    """Async :func:`discover` — for callers already inside an event loop
    (Jupyter notebooks included), where :func:`discover` cannot run."""
    return await _discovery.discover(timeout)


def connect(address: str | FoundDevice | None = None) -> DynamiteSampler:
    """Connect to the one device in range (or the one at ``address``)."""
    return DynamiteSampler.connect(address)


@contextlib.asynccontextmanager
async def aconnect(address: str | FoundDevice | None = None):
    """Async :func:`connect`, as an async context manager::

    async with dms.aconnect() as dev:
        async for block in dev.stream():
            ...
    """
    dev = await AsyncDynamiteSampler.connect(address)
    try:
        yield dev
    finally:
        await dev.close()
