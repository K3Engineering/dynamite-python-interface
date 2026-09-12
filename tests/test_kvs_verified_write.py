import asyncio

import pytest

from dynamite_sampler_kvs import (
    KvsBusy,
    KvsClient,
    KvsDeviceError,
    KvsError,
    KvsTimeout,
)


class FakeClient(KvsClient):
    """KvsClient with set/get stubbed out; only the verified-write helpers
    under test run against real logic."""

    def __init__(self, fail_sets=0, corrupt_key=None):
        super().__init__(client=None, advertised_name="fake")
        self.store = {}
        # KvsBusy exceptions to raise from set() before succeeding — the
        # firmware device lock's answer while the ADC feed streams.
        self.fail_sets = fail_sets
        self.corrupt_key = corrupt_key  # key whose readback never matches
        self.set_calls = 0

    async def set(self, folder, key, value):
        self.set_calls += 1
        if self.fail_sets > 0:
            self.fail_sets -= 1
            raise KvsBusy("device locked ('B')")
        stored = "corrupted" if key == self.corrupt_key else value
        self.store[(folder, key)] = stored

    async def get(self, folder, key):
        return self.store[(folder, key)]


@pytest.fixture(autouse=True)
def no_sleep(monkeypatch):
    async def instant(_seconds):
        pass

    monkeypatch.setattr(asyncio, "sleep", instant)


def test_set_verified_success():
    client = FakeClient()
    readback = asyncio.run(client.set_verified("F", "exc", "4.53,nominal"))
    assert readback == "4.53,nominal"
    assert client.set_calls == 1


def test_set_verified_mismatch_returns_readback():
    client = FakeClient(corrupt_key="exc")
    readback = asyncio.run(client.set_verified("F", "exc", "4.53,nominal"))
    assert readback == "corrupted"
    assert readback != "4.53,nominal"
    assert client.set_calls == 1  # a mismatch is not retried


def test_set_verified_retries_busy():
    client = FakeClient(fail_sets=2)
    readback = asyncio.run(client.set_verified("F", "exc", "4.53", attempts=3))
    assert readback == "4.53"
    assert client.set_calls == 3


def test_set_verified_reraises_busy_after_retries():
    client = FakeClient(fail_sets=10)
    with pytest.raises(KvsBusy):
        asyncio.run(client.set_verified("F", "exc", "4.53", attempts=2))
    assert client.set_calls == 2


@pytest.mark.parametrize("error", [KvsTimeout, KvsDeviceError, KvsError])
def test_set_verified_does_not_retry_non_busy_errors(error):
    """Timeout means a broken link and 'E' a device failure; neither is
    worth a retry (only the busy 'B' is)."""
    client = FakeClient()

    async def fail(folder, key, value):
        client.set_calls += 1
        raise error("nope")

    client.set = fail
    with pytest.raises(error):
        asyncio.run(client.set_verified("F", "exc", "4.53", attempts=3))
    assert client.set_calls == 1


def test_set_verified_rejects_zero_attempts():
    with pytest.raises(ValueError):
        asyncio.run(FakeClient().set_verified("F", "exc", "4.53", attempts=0))


def test_set_many_verified():
    client = FakeClient(corrupt_key="bad")
    entries = {"a": "1", "bad": "2", "c": "3"}
    readbacks = asyncio.run(client.set_many_verified("F", entries))
    assert readbacks == {"a": "1", "bad": "corrupted", "c": "3"}
