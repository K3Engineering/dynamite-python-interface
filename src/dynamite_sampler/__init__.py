"""Dynamite Sampler Python API.

    import dynamite_sampler as dms

    with dms.connect() as dev:
        block = dev.read(n=1000, units="mV/V", timeout=5.0)
        print(block.data.shape)
"""

import asyncio

from . import discovery as _discovery
from .block import Block
from .calibration import Calibration, LoadCell
from .device import (
    UNCONFIGURED,
    AsyncDynamiteSampler,
    DeviceInfo,
    DynamiteSampler,
)
from .discovery import FoundDevice
from .errors import (
    BufferOverrun,
    CalibrationError,
    ConnectionLost,
    DeviceNotFound,
    DynamiteError,
    KvsBusy,
    KvsDeviceError,
    KvsError,
    KvsRejected,
    KvsTimeout,
    MultipleDevicesFound,
    ProtocolError,
    ProvisioningError,
    ReadTimeout,
    UnitUnavailable,
)

__version__ = "0.1.0"

__all__ = [
    "discover",
    "connect",
    "FoundDevice",
    "DynamiteSampler",
    "AsyncDynamiteSampler",
    "DeviceInfo",
    "UNCONFIGURED",
    "Block",
    "Calibration",
    "LoadCell",
    "DynamiteError",
    "DeviceNotFound",
    "MultipleDevicesFound",
    "ConnectionLost",
    "ReadTimeout",
    "BufferOverrun",
    "UnitUnavailable",
    "ProtocolError",
    "ProvisioningError",
    "CalibrationError",
    "KvsError",
    "KvsRejected",
    "KvsBusy",
    "KvsDeviceError",
    "KvsTimeout",
]


def discover(timeout=5.0):
    """All Dynamite Samplers in range, sorted by RSSI descending."""
    return asyncio.run(_discovery.discover(timeout))


def connect(address=None):
    """Connect to the one device in range (or the one at ``address``)."""
    return DynamiteSampler.connect(address)
