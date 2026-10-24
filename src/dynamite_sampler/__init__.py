"""Dynamite Sampler Python API."""

from .errors import (
    ConnectionLost,
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
)

__version__ = "0.1.0"

__all__ = [
    "DynamiteError",
    "DeviceNotFound",
    "MultipleDevicesFound",
    "ConnectionLost",
    "KvsError",
    "KvsRejected",
    "KvsBusy",
    "KvsDeviceError",
    "KvsTimeout",
    "OtaError",
    "OtaRejected",
    "OtaTimeout",
    "FirmwareCatalogError",
]
