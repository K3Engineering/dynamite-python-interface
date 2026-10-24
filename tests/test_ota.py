"""OTA flash tests for Ota, driven through a fake bleak transport. The
fake answers Control writes the way the firmware does: the reply
notification fires from inside the write call that caused it, and Data
writes get no reply."""

import asyncio

import pytest

import dynamite_sampler as dms
from dynamite_sampler.errors import ConnectionLost, OtaError, OtaRejected, OtaTimeout
from dynamite_sampler.gatt import OTA
from dynamite_sampler.ota import Ota

IMAGE = bytes(range(256)) * 3


class FakeBleakClient:
    """Stands in for bleak.BleakClient; `responder` decides each Control
    reply."""

    def __init__(self, responder, mtu_size=247):
        self.control_writes = []  # (frame bytes, response flag)
        self.data_writes = []  # frame bytes
        self.mtu_size = mtu_size
        self.is_connected = True
        self.stop_notify_calls = 0
        # responder(request_bytes) -> reply frame bytes, or None to answer
        # with nothing (a dead link; the firmware answers every handshake).
        self.responder = responder

    async def start_notify(self, _uuid, callback):
        self._notify = callback

    async def stop_notify(self, _uuid):
        self.stop_notify_calls += 1

    async def write_gatt_char(self, uuid, data, response=True):
        frame = bytes(data)
        if uuid == OTA.Data.UUID:
            self.data_writes.append(frame)
            return
        self.control_writes.append((frame, response))
        reply = self.responder(frame)
        if reply is not None:
            self._notify(None, bytearray(reply))


def ack_responder(frame):
    """The happy-path device: ACK both handshake steps; silent on Data."""
    opcode = frame[0]
    if opcode == OTA.Control.REQUEST[0]:
        return OTA.Control.REQUEST_ACK
    if opcode == OTA.Control.DONE[0]:
        return OTA.Control.DONE_ACK
    return None


def test_flash_writes_chunked_image_and_reports_progress():
    client = FakeBleakClient(ack_responder)

    async def run():
        ota = await Ota.open(client)
        progress = []
        await ota.flash(IMAGE, on_progress=progress.append)
        return ota, progress

    ota, progress = asyncio.run(run())
    assert ota.chunk_size == min(247 - 3, 244) == 244
    assert client.control_writes == [
        (OTA.Control.REQUEST + len(IMAGE).to_bytes(4, "little"), True),
        # DONE is written without response on purpose.
        (OTA.Control.DONE, False),
    ]
    assert [len(c) for c in client.data_writes] == [244, 244, 244, 36]
    assert b"".join(client.data_writes) == IMAGE
    assert progress == [244, 488, 732, 768]


def test_chunk_size_follows_the_negotiated_mtu():
    client = FakeBleakClient(ack_responder, mtu_size=100)

    async def run():
        ota = await Ota.open(client)
        await ota.flash(IMAGE)

    asyncio.run(run())
    assert [len(c) for c in client.data_writes] == [97] * 7 + [89]


def test_request_nak_raises_ota_rejected_and_sends_no_data():
    client = FakeBleakClient(lambda frame: OTA.Control.REQUEST_NAK)

    async def run():
        ota = await Ota.open(client)
        with pytest.raises(OtaRejected, match="declined to start"):
            await ota.flash(IMAGE)

    asyncio.run(run())
    assert client.data_writes == []


def test_done_nak_raises_ota_rejected():
    def responder(frame):
        opcode = frame[0]
        if opcode == OTA.Control.REQUEST[0]:
            return OTA.Control.REQUEST_ACK
        if opcode == OTA.Control.DONE[0]:
            return OTA.Control.DONE_NAK
        return None

    client = FakeBleakClient(responder)

    async def run():
        ota = await Ota.open(client)
        with pytest.raises(OtaRejected, match="rejected the image"):
            await ota.flash(IMAGE)

    asyncio.run(run())
    assert b"".join(client.data_writes) == IMAGE


def test_unexpected_reply_raises_ota_error():
    client = FakeBleakClient(lambda frame: b"\x09")

    async def run():
        ota = await Ota.open(client)
        with pytest.raises(OtaError, match="Unexpected reply 0x09"):
            await ota.flash(IMAGE)

    asyncio.run(run())


def test_empty_reply_raises_ota_error():
    client = FakeBleakClient(lambda frame: b"")

    async def run():
        ota = await Ota.open(client)
        with pytest.raises(OtaError, match="Empty reply"):
            await ota.flash(IMAGE)

    asyncio.run(run())


def test_silent_device_raises_ota_timeout():
    client = FakeBleakClient(lambda frame: None)

    async def run():
        ota = await Ota.open(client, ack_timeout_s=0.05)
        with pytest.raises(OtaTimeout):
            await ota.flash(IMAGE)

    asyncio.run(run())


def test_ota_errors_join_the_hierarchy():
    assert issubclass(OtaRejected, OtaError)
    assert issubclass(OtaTimeout, OtaError)
    assert issubclass(OtaTimeout, TimeoutError)
    assert issubclass(OtaError, dms.DynamiteError)


def test_fail_pending_settles_the_in_flight_handshake():
    client = FakeBleakClient(lambda frame: None)

    async def run():
        ota = await Ota.open(client)
        task = asyncio.ensure_future(ota.flash(IMAGE))
        while ota._pending is None:
            await asyncio.sleep(0)
        ota.fail_pending(ConnectionLost("device disconnected"))
        with pytest.raises(ConnectionLost):
            await task

    asyncio.run(run())


def test_stale_frame_with_no_live_wait_is_dropped():
    client = FakeBleakClient(ack_responder)

    async def run():
        ota = await Ota.open(client)
        ota._on_notify(None, bytearray(OTA.Control.REQUEST_ACK))
        ota._on_notify(None, bytearray(OTA.Control.REQUEST_NAK))
        await ota.flash(IMAGE)

    asyncio.run(run())


def test_close_tolerates_the_link_dropped_by_the_restart():
    """The device restarts 500 ms after the final ACK; close may run with
    the link already down and must not touch it."""
    client = FakeBleakClient(ack_responder)

    async def run():
        ota = await Ota.open(client)
        await ota.flash(IMAGE)
        client.is_connected = False
        await ota.close()

    asyncio.run(run())
    assert client.stop_notify_calls == 0


def test_close_unsubscribes_on_a_live_link():
    client = FakeBleakClient(ack_responder)

    async def run():
        ota = await Ota.open(client)
        await ota.close()

    asyncio.run(run())
    assert client.stop_notify_calls == 1


def test_empty_image_is_rejected_client_side():
    client = FakeBleakClient(ack_responder)

    async def run():
        ota = await Ota.open(client)
        with pytest.raises(ValueError):
            await ota.flash(b"")

    asyncio.run(run())
    assert client.control_writes == []
