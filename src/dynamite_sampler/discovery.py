"""Device discovery and the one-device-in-range connect rule."""

import dataclasses

import bleak

from .errors import DeviceNotFound, MultipleDevicesFound

DEFAULT_DISCOVER_TIMEOUT_S = 5.0


@dataclasses.dataclass(frozen=True)
class FoundDevice:
    """A discovered Dynamite Sampler advertisement."""

    address: str
    name: str | None
    rssi: int


async def discover(timeout=DEFAULT_DISCOVER_TIMEOUT_S):
    """All Dynamite Samplers in range, sorted by RSSI descending."""
    from .gatt import DynamiteSamplerService

    devices_and_adv = await bleak.BleakScanner.discover(
        timeout=timeout,
        return_adv=True,
        service_uuids=[DynamiteSamplerService.UUID],
    )
    found = [
        FoundDevice(device.address, device.name, adv.rssi)
        for device, adv in devices_and_adv.values()
    ]
    found.sort(key=lambda d: d.rssi, reverse=True)
    return found


async def find_single(address=None):
    """The one device in range (or the one matching ``address``).

    ``address`` may be an address string or a :class:`FoundDevice`.
    Raises ``DeviceNotFound``/``MultipleDevicesFound``; never prompts."""
    if isinstance(address, FoundDevice):
        address = address.address
    elif address is not None and not isinstance(address, str):
        raise TypeError(
            f"address must be a string or FoundDevice, got {type(address).__name__}"
        )
    devices = await discover()
    if address is not None:
        wanted = address.upper()
        matches = [d for d in devices if d.address.upper() == wanted]
        if not matches:
            raise DeviceNotFound(f"No Dynamite Sampler with address {address} found")
        return matches[0]
    if not devices:
        raise DeviceNotFound("No Dynamite Sampler devices found")
    if len(devices) > 1:
        listing = "\n".join(
            f"  {d.address}  {d.name}  (RSSI {d.rssi} dBm)" for d in devices
        )
        raise MultipleDevicesFound(
            f"{len(devices)} devices found; pass an address:\n{listing}"
        )
    return devices[0]
