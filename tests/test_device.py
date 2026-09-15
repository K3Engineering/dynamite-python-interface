"""Feed decoding and block assembly, driven through a fake BLE transport."""

import asyncio
import threading

import numpy as np
import pytest

import dynamite_sampler.discovery as discovery
from dynamite_sampler.device import AsyncDynamiteSampler, DynamiteSampler, _decode_samples
from dynamite_sampler.errors import (
    ConnectionLost,
    DynamiteError,
    ReadTimeout,
    StreamActive,
)
from dynamite_sampler.gatt import ADCConfigData
from dynamite_sampler.ssn import SsnUnwrapper


class FakeKvs:
    snapshot = {"F": {}, "U": {}}

    def set_on_change(self, callback):
        self.callback = callback


class FakeClient:
    is_connected = True

    def __init__(self):
        self.notify = None

    async def start_notify(self, uuid, callback):
        self.notify = callback

    async def stop_notify(self, uuid):
        pass


def packet(ssn, rows):
    payload = bytearray()
    for row in rows:
        for value in row:
            payload += (value & 0xFFFFFF).to_bytes(3, "little")
    return ssn.to_bytes(2, "little") + bytes(payload)


def make_device(client, disconnected=None):
    adc = ADCConfigData(4, "HIGH_RESOLUTION", 1000, [1, 1, 1, 1])
    return AsyncDynamiteSampler(client, None, adc, FakeKvs(), disconnected)


async def wait_notify(client):
    """Yield until the stream has subscribed (start_notify ran)."""
    for _ in range(100):
        if client.notify is not None:
            return
        await asyncio.sleep(0)


def test_decode_samples_round_trips_signed_24bit():
    payload = packet(0, [[-1, 2, -8388608, 8388607]])[2:]
    decoded = _decode_samples(payload, 4)
    assert decoded.tolist() == [[-1.0, 2.0, -8388608.0, 8388607.0]]


def test_ssn_unwrap_handles_rollover():
    unwrapper = SsnUnwrapper()
    assert unwrapper.unwrap(65535, 1) == (65535, 0)
    assert unwrapper.unwrap(0, 1) == (65536, 0)
    assert unwrapper.unwrap(2, 1) == (65538, 1)


def test_stream_inserts_nan_gap_rows_and_arithmetic_time():
    client = FakeClient()
    device = make_device(client)

    async def run():
        agen = device.stream(blocksize=3, units="raw")
        pending = asyncio.ensure_future(agen.__anext__())
        await wait_notify(client)
        client.notify(None, packet(10, [[1, 2, 3, 4], [5, 6, 7, 8]]))
        client.notify(None, packet(13, [[9, 10, 11, 12]]))  # one dropped
        block = await pending
        await agen.aclose()
        return block

    block = asyncio.run(run())
    assert block.raw.shape == (3, 4)
    assert block.ssn0 == 10
    assert np.allclose(block.t, [0.0, 0.001, 0.002])
    assert np.all(np.isnan(block.raw[2]))
    assert np.all(np.isnan(block.data[2]))


def test_stream_converts_units():
    client = FakeClient()
    device = make_device(client)

    async def run():
        agen = device.stream(blocksize=2, units="raw")
        pending = asyncio.ensure_future(agen.__anext__())
        await wait_notify(client)
        client.notify(None, packet(0, [[100, 200, 300, 400], [1, 2, 3, 4]]))
        block = await pending
        await agen.aclose()
        return block

    block = asyncio.run(run())
    assert np.array_equal(block.data, block.raw)
    assert block.units == "raw"


def test_read_returns_one_block_across_packets():
    client = FakeClient()
    device = make_device(client)

    async def run():
        pending = asyncio.ensure_future(device.read(5, units="raw"))
        await wait_notify(client)
        client.notify(None, packet(0, [[1, 2, 3, 4], [5, 6, 7, 8], [9, 10, 11, 12]]))
        client.notify(None, packet(3, [[13, 14, 15, 16], [17, 18, 19, 20]]))
        return await pending

    block = asyncio.run(run())
    assert block.raw.shape == (5, 4)
    assert block.ssn0 == 0
    assert np.allclose(block.t, [0.0, 0.001, 0.002, 0.003, 0.004])


def test_read_times_out_without_rows():
    client = FakeClient()
    device = make_device(client)

    async def run():
        with pytest.raises(ReadTimeout):
            await device.read(5, units="raw", timeout=0.05)

    asyncio.run(run())


def test_block_host_time_is_first_packet():
    import time

    client = FakeClient()
    device = make_device(client)

    async def run():
        agen = device.stream(blocksize=3, units="raw")
        pending = asyncio.ensure_future(agen.__anext__())
        await wait_notify(client)
        t0 = time.monotonic()
        client.notify(None, packet(0, [[1, 2, 3, 4]]))  # first packet
        await asyncio.sleep(0.02)  # let _assemble process it
        t1 = time.monotonic()
        client.notify(None, packet(1, [[5, 6, 7, 8], [9, 10, 11, 12]]))
        block = await pending
        await agen.aclose()
        return t0, t1, block

    t0, t1, block = asyncio.run(run())
    assert t0 <= block.host_time < t1


def test_stream_connection_lost_mid_stream():
    client = FakeClient()
    event = asyncio.Event()
    device = make_device(client, disconnected=event)

    async def run():
        agen = device.stream(blocksize=3, units="raw")
        pending = asyncio.ensure_future(agen.__anext__())
        await wait_notify(client)
        event.set()
        with pytest.raises(ConnectionLost):
            await pending
        await agen.aclose()

    asyncio.run(run())


def test_second_stream_while_active_raises():
    client = FakeClient()
    device = make_device(client)

    async def run():
        agen = device.stream(blocksize=3, units="raw")
        pending = asyncio.ensure_future(agen.__anext__())
        await wait_notify(client)
        with pytest.raises(StreamActive):
            await device.stream(blocksize=3, units="raw").__anext__()
        pending.cancel()
        with pytest.raises(asyncio.CancelledError):
            await pending

    asyncio.run(run())


class _StubAsync:
    class _kvs:
        factory = user = settings = None

    kvs = _kvs()


def _sync_facade():
    loop = asyncio.new_event_loop()
    thread = threading.Thread(target=loop.run_forever, daemon=True)
    thread.start()
    return DynamiteSampler(_StubAsync(), loop, thread), loop, thread


def _stop_facade(loop, thread):
    loop.call_soon_threadsafe(loop.stop)
    thread.join()
    loop.close()


def test_sync_run_preserves_timeouterror_subclass():
    dev, loop, thread = _sync_facade()
    try:

        async def boom():
            raise ReadTimeout("nope")

        with pytest.raises(ReadTimeout):
            dev._run(boom())
    finally:
        _stop_facade(loop, thread)


def test_sync_run_after_close_raises():
    dev, loop, thread = _sync_facade()
    try:
        dev._loop = None
        with pytest.raises(DynamiteError, match="closed"):
            dev._run(None)
    finally:
        _stop_facade(loop, thread)


def test_find_single_accepts_found_device(monkeypatch):
    device = discovery.FoundDevice("D4:5E:AA:BB:CC:DD", "ds", -50)

    async def fake_find_by_address(address):
        return device if address.upper() == device.address.upper() else None

    monkeypatch.setattr(discovery, "_find_by_address", fake_find_by_address)
    assert asyncio.run(discovery.find_single(device)) is device
    assert asyncio.run(discovery.find_single("d4:5e:aa:bb:cc:dd")) is device


def test_find_single_no_address_requires_exactly_one(monkeypatch):
    devices = [discovery.FoundDevice("AA:BB:CC:DD:EE:FF", "ds", -50)]

    async def one(timeout=0):
        return devices

    monkeypatch.setattr(discovery, "discover", one)
    assert asyncio.run(discovery.find_single()) is devices[0]

    async def many(timeout=0):
        return devices + [discovery.FoundDevice("11:22:33:44:55:66", "ds2", -60)]

    from dynamite_sampler.errors import MultipleDevicesFound

    monkeypatch.setattr(discovery, "discover", many)
    with pytest.raises(MultipleDevicesFound):
        asyncio.run(discovery.find_single())


def test_find_single_rejects_other_types(monkeypatch):
    with pytest.raises(TypeError):
        asyncio.run(discovery.find_single(42))
