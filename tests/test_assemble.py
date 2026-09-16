"""Block assembly from packets: boundaries, gaps, and the stream sugar."""

import asyncio

import numpy as np

from dynamite_sampler.assemble import BlockAssembler, blocks_from_packets
from dynamite_sampler.calibration import Calibration
from dynamite_sampler.device import AsyncDynamiteSampler
from dynamite_sampler.gatt import ADCConfigData
from dynamite_sampler.packet import Packet


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


def wire_packet(ssn, rows):
    payload = bytearray()
    for row in rows:
        for value in row:
            payload += (value & 0xFFFFFF).to_bytes(3, "little")
    return ssn.to_bytes(2, "little") + bytes(payload)


def make_device(client):
    adc = ADCConfigData(4, "HIGH_RESOLUTION", 1000, [1, 1, 1, 1])
    return AsyncDynamiteSampler(client, None, adc, FakeKvs())


async def wait_notify(client):
    for _ in range(100):
        if client.notify is not None:
            return
        await asyncio.sleep(0)


def pkt(ssn, rows, dropped=0, t=1.0):
    rows = np.array(rows, dtype=np.float64).reshape(-1, 4)
    return Packet(
        time=t,
        ssn=ssn,
        rows_dropped=dropped,
        payload_bytes=2 + 12 * rows.shape[0],
        raw=rows,
    )


def make_assembler(blocksize=3):
    calibration = Calibration.from_kvs({"F": {}, "U": {}}, [1, 1, 1, 1])
    return BlockAssembler(calibration, 1000, blocksize, units="raw")


def test_push_returns_nothing_until_blocksize():
    assembler = make_assembler()
    assert assembler.push(pkt(0, [[1, 2, 3, 4], [5, 6, 7, 8]])) == []
    (block,) = assembler.push(pkt(2, [[9, 10, 11, 12]]))
    assert block.raw.tolist() == [[1, 2, 3, 4], [5, 6, 7, 8], [9, 10, 11, 12]]
    assert block.ssn0 == 0
    assert block.rows_dropped == 0
    assert block.host_time == 1.0


def test_push_splits_packet_across_block_boundary():
    assembler = make_assembler()
    (block,) = assembler.push(pkt(0, [[1, 2, 3, 4]] * 5))
    assert block.raw.shape == (3, 4)
    assert block.ssn0 == 0
    (block2,) = assembler.push(pkt(5, [[6, 7, 8, 9]]))
    assert block2.raw.shape == (3, 4)
    assert block2.ssn0 == 3  # 2 leftover rows from the first packet, then 1
    assert np.array_equal(block2.raw[0], [1, 2, 3, 4])
    assert np.array_equal(block2.raw[2], [6, 7, 8, 9])


def test_push_counts_gap_rows_split_across_blocks():
    assembler = make_assembler()
    assembler.push(pkt(0, [[1, 2, 3, 4], [5, 6, 7, 8]]))
    blocks = assembler.push(pkt(4, [[9, 10, 11, 12], [13, 14, 15, 16]], dropped=2))
    assert len(blocks) == 2
    # Block 1: 2 real rows + the first gap row. Block 2: second gap + 2 real.
    assert blocks[0].rows_dropped == 1
    assert np.all(np.isnan(blocks[0].raw[2]))
    assert blocks[1].rows_dropped == 1
    assert np.all(np.isnan(blocks[1].raw[0]))
    assert blocks[1].ssn0 == 3


def test_push_returns_multiple_blocks_for_tiny_blocksize():
    assembler = make_assembler(blocksize=1)
    blocks = assembler.push(pkt(0, [[1, 2, 3, 4], [5, 6, 7, 8], [9, 10, 11, 12]]))
    assert len(blocks) == 3
    assert [b.ssn0 for b in blocks] == [0, 1, 2]


def test_block_time_is_arithmetic_per_row():
    assembler = make_assembler(blocksize=2)
    (block,) = assembler.push(pkt(0, [[1, 2, 3, 4], [5, 6, 7, 8]]))
    assert np.allclose(block.t, [0.0, 0.001])


async def drain(agen, client, script, n):
    out = []

    async def run():
        async for item in agen:
            out.append(item)
            if len(out) >= n:
                break

    task = asyncio.ensure_future(run())
    await wait_notify(client)
    for frame in script:
        client.notify(None, frame)
    await task
    await agen.aclose()
    return out


SCRIPT = [
    wire_packet(9, [[1, 2, 3, 4], [5, 6, 7, 8]]),
    wire_packet(12, [[9, 10, 11, 12]]),  # one row dropped
    wire_packet(14, [[13, 14, 15, 16], [17, 18, 19, 20]]),  # one dropped
]


async def test_stream_sugar_matches_manual_packet_assembly():
    client = FakeClient()
    sugar = await drain(
        make_device(client).stream(blocksize=3, units="raw"), client, SCRIPT, 2
    )

    client = FakeClient()
    assembler = make_assembler()
    manual = await drain(
        blocks_from_packets(make_device(client).stream_packets(), assembler),
        client,
        SCRIPT,
        2,
    )

    for b_sugar, b_manual in zip(sugar, manual):
        assert np.array_equal(b_sugar.raw, b_manual.raw, equal_nan=True)
        assert b_sugar.ssn0 == b_manual.ssn0
        assert b_sugar.rows_dropped == b_manual.rows_dropped
        assert np.allclose(b_sugar.t, b_manual.t)

    assert [b.rows_dropped for b in sugar] == [1, 1]
    assert [b.ssn0 for b in sugar] == [9, 12]
    assert np.all(np.isnan(sugar[0].raw[2]))
    assert np.all(np.isnan(sugar[1].raw[1]))


async def test_stream_packets_yields_packet_layer_fields():
    client = FakeClient()
    packets = await drain(make_device(client).stream_packets(), client, SCRIPT[:2], 2)

    assert packets[0].ssn == 9
    assert packets[0].rows == 2
    assert packets[0].rows_dropped == 0
    assert packets[0].payload_bytes == 2 + 24
    assert packets[0].raw.tolist() == [[1, 2, 3, 4], [5, 6, 7, 8]]

    assert packets[1].ssn == 12
    assert packets[1].rows_dropped == 1
    assert packets[1].payload_bytes == 2 + 12
    assert packets[1].time >= packets[0].time
