"""Frame-matching and serialization tests for KvsClient, driven through a
fake bleak transport. The fake answers writes the way the firmware does:
the response notification fires from inside the write call."""

import asyncio

import pytest

import dynamite_sampler_kvs
from dynamite_sampler_kvs import KvsClient, KvsError, KvsRejected, KvsTimeout


class FakeBleakClient:
    """Stands in for bleak.BleakClient; `responder` decides each reply."""

    def __init__(self, responder=None):
        self.writes = []
        self.kvs = None
        # responder(kvs, request_bytes) -> reply frame bytes, or None to
        # answer with nothing (the firmware device-lock silent drop).
        self.responder = responder or (lambda kvs, req: b"1" + req + b"=")

    async def write_gatt_char(self, _uuid, data, response=True):
        request = bytes(data)
        self.writes.append(request)
        reply = self.responder(self.kvs, request)
        if reply is not None:
            self.kvs._on_notify(None, bytearray(reply))


def make_client(responder=None):
    client = KvsClient(FakeBleakClient(responder), "fake")
    client.client.kvs = client
    return client


@pytest.fixture
def short_timeout(monkeypatch):
    monkeypatch.setattr(dynamite_sampler_kvs, "_COMMAND_TIMEOUT_S", 0.05)


def test_timeout_raises_kvs_timeout(short_timeout):
    kvs = make_client(responder=lambda kvs, req: None)

    async def run():
        with pytest.raises(KvsTimeout):
            await kvs.get("F", "exc")

    asyncio.run(run())


def test_kvs_timeout_is_catchable_as_kvs_error_and_timeout_error():
    assert issubclass(KvsTimeout, KvsError)
    assert issubclass(KvsTimeout, TimeoutError)


def test_rejection_raises_kvs_rejected():
    kvs = make_client(responder=lambda kvs, req: b"0" + req)

    async def run():
        with pytest.raises(KvsRejected):
            await kvs.get("F", "nope")

    asyncio.run(run())


def test_get_returns_payload_after_echo_and_separator():
    kvs = make_client(responder=lambda kvs, req: b"1" + req + b"=4.53,nominal")
    assert asyncio.run(kvs.get("F", "exc")) == "4.53,nominal"


def test_stale_prefix_frame_is_not_attributed(short_timeout):
    """A's timed-out request is a strict extension of B's: A's late reply
    must not resolve B (the old prefix-match bug)."""
    stale = b"1GETFabcX=9"  # late reply to the timed-out GETFabcX

    async def run():
        kvs = make_client(responder=lambda kvs, req: None)
        with pytest.raises(KvsTimeout):
            await kvs.get("F", "abcX")

        def respond(kvs, req):
            kvs._on_notify(None, bytearray(stale))  # arrives first
            return b"1" + req + b"=5"

        kvs.client.responder = respond
        # Would have failed with "Device rejected" under prefix matching.
        assert await kvs.get("F", "abc") == "5"

    asyncio.run(run())


def test_frame_with_no_pending_command_is_dropped():
    kvs = make_client()
    kvs._on_notify(None, bytearray(b"1GETFexc=stale"))
    kvs._on_notify(None, bytearray(b"0GETFexc"))
    assert kvs._pending is None


def test_duplicate_frame_does_not_double_complete():
    kvs = make_client()
    fut = asyncio.new_event_loop().create_future()
    kvs._pending = (b"GETFexc", fut)
    frame = bytearray(b"1GETFexc=4.53")
    kvs._on_notify(None, frame)
    kvs._on_notify(None, frame)  # duplicate: must not raise InvalidStateError
    assert fut.result() == b"4.53"


def test_commands_are_serialized_in_order():
    in_flight = 0
    max_in_flight = 0

    async def run():
        nonlocal in_flight, max_in_flight

        async def slow_responder_write(_uuid, data, response=True):
            nonlocal in_flight, max_in_flight
            request = bytes(data)
            kvs.client.writes.append(request)
            in_flight += 1
            max_in_flight = max(max_in_flight, in_flight)
            await asyncio.sleep(0.01)
            kvs._on_notify(None, bytearray(b"1" + request + b"="))
            in_flight -= 1

        kvs = make_client()
        kvs.client.write_gatt_char = slow_responder_write
        return await asyncio.gather(
            kvs.get("F", "ch0.r"), kvs.get("F", "ch1.r"), kvs.get("U", "lc0.cap")
        )

    asyncio.run(run())
    assert max_in_flight == 1


def test_late_reply_after_timeout_does_not_break_next_command(short_timeout):
    """After A times out unanswered, A's late reply (arriving while B is
    outstanding, different request bytes) is dropped; B resolves with its
    own reply."""

    async def run():
        kvs = make_client(responder=lambda kvs, req: None)
        with pytest.raises(KvsTimeout):
            await kvs.get("F", "exc")

        def respond(kvs, req):
            # A's late frame arrives during B, then B's own reply.
            kvs._on_notify(None, bytearray(b"1GETFexc=4.53"))
            return b"1" + req + b"=200"

        kvs.client.responder = respond
        assert await kvs.get("U", "lc0.cap") == "200"

    asyncio.run(run())


def test_identical_request_late_reply_is_accepted(short_timeout):
    """A late reply to a byte-identical timed-out request is
    indistinguishable from the retried command's own reply (no transaction
    ID in the protocol). Accepting it is correct: KVS commands are
    idempotent, and the device did execute that exact command."""

    async def run():
        kvs = make_client(responder=lambda kvs, req: None)
        with pytest.raises(KvsTimeout):
            await kvs.get("F", "exc")

        def respond(kvs, req):
            # The timed-out GET's late reply arrives during the retry.
            kvs._on_notify(None, bytearray(b"1GETFexc=stale"))
            return b"1" + req + b"=4.53"

        kvs.client.responder = respond
        assert await kvs.get("F", "exc") == "stale"

    asyncio.run(run())
