"""Dynamite Sampler Python API."""

from .errors import (
    ConnectionLost,
    DeviceNotFound,
    DynamiteError,
    FirmwareCatalogError,
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
    "OtaError",
    "OtaRejected",
    "OtaTimeout",
    "FirmwareCatalogError",
]
