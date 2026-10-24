"""Feed decoding and packet streaming, driven through a fake BLE transport."""

import asyncio
import threading
import time

import numpy as np
import pytest

import dynamite_sampler.discovery as discovery
from dynamite_sampler.device import (
    AsyncDynamiteSampler,
    DynamiteSampler,
    SsnUnwrapper,
    _decode_samples,
)
from dynamite_sampler.errors import (
    ConnectionLost,
    DynamiteError,
    MultipleDevicesFound,
    ReadTimeout,
    StreamActive,
)
from dynamite_sampler.gatt import ADCConfigData, TxPower


class FakeKvs:
    folder = {"F": {}, "U": {}}

    async def read_all(self):
        return self.folder


class FakeClient:
    is_connected = True

    def __init__(self):
        self.notify = None

    async def start_notify(self, uuid, callback):
        self.notify = callback

    async def stop_notify(self, uuid):
        self.notify = None


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


async def test_stream_yields_packets_with_gaps_counted():
    client = FakeClient()
    device = make_device(client)
    agen = device.stream()
    pending = asyncio.ensure_future(agen.__anext__())
    await wait_notify(client)
    client.notify(None, packet(10, [[1, 2, 3, 4], [5, 6, 7, 8]]))
    client.notify(None, packet(13, [[9, 10, 11, 12]]))  # one dropped
    first = await pending
    second = await agen.__anext__()
    await agen.aclose()
    assert np.array_equal(first.raw, [[1, 2, 3, 4], [5, 6, 7, 8]])
    assert first.ssn == 10
    assert first.rows == 2
    assert first.rows_dropped == 0
    assert first.payload_bytes == 2 + 24
    assert second.ssn == 13
    assert second.rows_dropped == 1
    assert second.time >= first.time


async def test_stream_yields_raw_counts_only():
    """The packet layer is raw: conversion is the caller's, through an
    explicit Calibration."""
    client = FakeClient()
    device = make_nominal_device(client)
    agen = device.stream()
    pending = asyncio.ensure_future(agen.__anext__())
    await wait_notify(client)
    client.notify(None, packet(0, [[1000, 0, 0, 0]]))
    pkt = await pending
    await agen.aclose()
    assert not hasattr(pkt, "units")
    assert np.array_equal(pkt.raw, [[1000, 0, 0, 0]])


class NominalKvs(FakeKvs):
    folder = {
        "F": {"adc_fsr": "1.2", "afe_gain": "15.6", "exc": "4.5"},
        "U": {},
        "S": {},
    }


def make_nominal_device(client):
    adc = ADCConfigData(4, "HIGH_RESOLUTION", 1000, [1, 1, 1, 1])
    return AsyncDynamiteSampler(client, None, adc, NominalKvs())


async def test_read_with_an_explicit_calibration_converts():
    client = FakeClient()
    device = make_nominal_device(client)
    calibration = await device.read_calibration()
    pending = asyncio.ensure_future(
        device.read(2, units="mV/V", calibration=calibration)
    )
    await wait_notify(client)
    client.notify(None, packet(0, [[1000, 0, 0, 0], [2000, 0, 0, 0]]))
    recording = await pending
    counts_per_mvv = (1 << 23) * 15.6 / (1.2 * 1000.0) * 4.5
    assert recording.units == "mV/V"
    assert np.allclose(
        recording.data[:, 0], [1000 / counts_per_mvv, 2000 / counts_per_mvv]
    )
    assert np.array_equal(recording.raw[:, 0], [1000, 2000])


class FakePowerClient(FakeClient):
    def __init__(self, power_dbm):
        super().__init__()
        self.power = power_dbm
        self.writes = []

    async def read_gatt_char(self, uuid):
        return bytes([self.power & 0xFF])

    async def write_gatt_char(self, uuid, data, response=True):
        self.writes.append((uuid, bytes(data)))
        self.power = int.from_bytes(bytes(data), signed=True)


async def test_tx_power_read_and_verified_set():
    client = FakePowerClient(-6)
    device = make_device(client)
    assert await device.read_tx_power_dbm() == -6
    assert await device.set_tx_power(-9) == -9
    assert client.writes == [(TxPower.TxPowerSet.UUID, bytes([0xF7]))]


async def test_set_tx_power_readback_mismatch_raises():
    class StuckClient(FakePowerClient):
        async def write_gatt_char(self, uuid, data, response=True):
            pass  # the set never takes effect

    device = make_device(StuckClient(0))
    with pytest.raises(DynamiteError, match="read-back"):
        await device.set_tx_power(-9)


async def test_read_spans_packets_into_one_recording():
    client = FakeClient()
    device = make_device(client)
    pending = asyncio.ensure_future(device.read(5, units="raw"))
    await wait_notify(client)
    client.notify(None, packet(0, [[1, 2, 3, 4], [5, 6, 7, 8], [9, 10, 11, 12]]))
    client.notify(None, packet(3, [[13, 14, 15, 16], [17, 18, 19, 20]]))
    recording = await pending
    assert recording.raw.shape == (5, 4)
    assert recording.ssn0 == 0
    assert np.allclose(recording.t, [0.0, 0.001, 0.002, 0.003, 0.004])


async def test_read_inserts_nan_gap_rows_and_keeps_ssn_arithmetic():
    client = FakeClient()
    device = make_device(client)
    pending = asyncio.ensure_future(device.read(4, units="raw"))
    await wait_notify(client)
    client.notify(None, packet(10, [[1, 2, 3, 4]]))
    client.notify(None, packet(13, [[9, 10, 11, 12]]))  # two dropped
    recording = await pending
    assert recording.ssn0 == 10
    assert np.isnan(recording.raw[1:3]).all()
    assert np.array_equal(recording.raw[0], [1, 2, 3, 4])
    assert np.array_equal(recording.raw[3], [9, 10, 11, 12])
    assert recording.rows_dropped == 2


async def test_read_releases_the_feed_deterministically():
    """A clean read close must not leave the feed active; teardown by
    asyncgen finalization would let the next read race into StreamActive."""
    client = FakeClient()
    device = make_device(client)
    pending = asyncio.ensure_future(device.read(2, units="raw"))
    await wait_notify(client)
    client.notify(None, packet(0, [[1, 2, 3, 4]] * 2))
    await pending
    assert not device._active
    pending = asyncio.ensure_future(device.read(2, units="raw"))
    await wait_notify(client)
    client.notify(None, packet(2, [[5, 6, 7, 8]] * 2))
    await pending
    assert not device._active


async def test_stream_close_releases_the_feed_deterministically():
    client = FakeClient()
    device = make_device(client)
    agen = device.stream()
    pending = asyncio.ensure_future(agen.__anext__())
    await wait_notify(client)
    client.notify(None, packet(0, [[1, 2, 3, 4]] * 2))
    await pending
    await agen.aclose()
    assert not device._active


async def test_read_times_out_without_rows():
    client = FakeClient()
    device = make_device(client)
    with pytest.raises(ReadTimeout):
        await device.read(5, units="raw", timeout=0.05)


async def test_packet_time_is_its_arrival():
    client = FakeClient()
    device = make_device(client)
    agen = device.stream()
    pending = asyncio.ensure_future(agen.__anext__())
    await wait_notify(client)
    t0 = time.monotonic()
    client.notify(None, packet(0, [[1, 2, 3, 4]]))
    pkt = await pending
    t1 = time.monotonic()
    await agen.aclose()
    assert t0 <= pkt.time <= t1


async def test_stream_connection_lost_mid_stream():
    client = FakeClient()
    event = asyncio.Event()
    device = make_device(client, disconnected=event)
    agen = device.stream()
    pending = asyncio.ensure_future(agen.__anext__())
    await wait_notify(client)
    event.set()
    with pytest.raises(ConnectionLost):
        await pending
    await agen.aclose()


async def test_second_stream_while_active_raises():
    client = FakeClient()
    device = make_device(client)
    agen = device.stream()
    pending = asyncio.ensure_future(agen.__anext__())
    await wait_notify(client)
    with pytest.raises(StreamActive):
        await device.stream().__anext__()
    pending.cancel()
    with pytest.raises(asyncio.CancelledError):
        await pending


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


async def test_find_single_accepts_found_device(monkeypatch):
    device = discovery.FoundDevice("D4:5E:AA:BB:CC:DD", "ds", -50)

    async def fake_find_by_address(address):
        return device if address.upper() == device.address.upper() else None

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
    with pytest.raises(MultipleDevicesFound):
        await discovery.find_single()


async def test_find_single_rejects_other_types(monkeypatch):
    with pytest.raises(TypeError):
        await discovery.find_single(42)
