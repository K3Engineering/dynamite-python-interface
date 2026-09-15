"""Feed decoding and block assembly, driven through a fake BLE transport."""

import asyncio

import numpy as np

from dynamite_sampler.device import AsyncDynamiteSampler, _decode_samples
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


def make_device(client):
    adc = ADCConfigData(4, "HIGH_RESOLUTION", 1000, [1, 1, 1, 1])
    return AsyncDynamiteSampler(client, None, adc, FakeKvs())


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
        await asyncio.sleep(0)
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
        await asyncio.sleep(0)
        client.notify(None, packet(0, [[100, 200, 300, 400], [1, 2, 3, 4]]))
        block = await pending
        await agen.aclose()
        return block

    block = asyncio.run(run())
    assert np.array_equal(block.data, block.raw)
    assert block.units == "raw"
