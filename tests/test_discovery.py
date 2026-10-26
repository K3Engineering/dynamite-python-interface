import pytest

import dynamite_sampler.discovery as discovery
from dynamite_sampler.errors import MultipleDevicesFound


async def test_find_single_accepts_found_device(monkeypatch):
    device = discovery.FoundDevice("D4:5E:AA:BB:CC:DD", "ds", -50)

    async def fake_find_by_address(address):
        return device if address.upper() == device.address else None

    monkeypatch.setattr(discovery, "_find_by_address", fake_find_by_address)
    assert await discovery.find_single(device) is device
    assert await discovery.find_single("d4:5e:aa:bb:cc:dd") is device


async def test_find_single_no_address_requires_exactly_one(monkeypatch):
    devices = [discovery.FoundDevice("AA:BB:CC:DD:EE:FF", "ds", -50)]

    async def one(timeout=0):
        return devices

    monkeypatch.setattr(discovery, "discover", one)
    assert await discovery.find_single() is devices[0]

    async def many(timeout=0):
        return devices + [discovery.FoundDevice("11:22:33:44:55:66", "ds2", -60)]

    monkeypatch.setattr(discovery, "discover", many)
    with pytest.raises(MultipleDevicesFound, match="pass an address"):
        await discovery.find_single()


async def test_find_single_rejects_other_types(monkeypatch):
    async def no_scan(timeout=0):
        raise AssertionError("type validation should happen before discovery")

    monkeypatch.setattr(discovery, "discover", no_scan)
    with pytest.raises(TypeError, match="address must be"):
        await discovery.find_single(42)
