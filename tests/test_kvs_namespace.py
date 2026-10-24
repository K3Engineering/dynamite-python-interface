"""KvsNamespace: verified writes against a stubbed BLE transport, and the
group write's marker discipline (delete first, write last).

KvsClient with the BLE transport (_command) stubbed out; the namespace
logic runs against the real code.
"""

import pytest

from dynamite_sampler.kvs import Kvs, KvsClient, KvsRejected


class FakeClient(KvsClient):
    """KvsClient with the BLE transport (_command) stubbed out."""

    def __init__(self, reject_key=None):
        super().__init__(client=None, advertised_name="fake")
        self.store = {}
        self.reject_key = reject_key
        self.commands = []

    async def _command(self, cmd, folder, data=""):
        key, _, value = data.partition("=")
        self.commands.append((cmd, folder, key))
        if key == self.reject_key:
            raise KvsRejected("no such key")
        if cmd == b"SET":
            self.store[(folder, key)] = value
            return b""
        if cmd == b"GET":
            return self.store[(folder, key)].encode()
        if cmd == b"DEL":
            del self.store[(folder, key)]
            return b""
        if cmd == b"IDX":
            keys = [k for (f, k) in self.store if f == folder]
            index = int(key, 16)
            if index >= len(keys):
                raise KvsRejected("IDX past the last entry")
            return f"{keys[index]}=21".encode()
        raise NotImplementedError(cmd)


def make_kvs(**kwargs):
    kvs = Kvs(client=None, advertised_name="fake")
    kvs._client = FakeClient(**kwargs)
    return kvs


def seed(kvs, folder, entries):
    """Seed a folder on the fake device."""
    for key, value in entries.items():
        kvs._client.store[(folder, key)] = value


async def test_set_returns_the_written_value():
    kvs = make_kvs()
    assert await kvs.factory.set("exc", "4.53,nominal") == "4.53,nominal"
    assert kvs._client.store[("F", "exc")] == "4.53,nominal"


async def test_set_raises_on_readback_mismatch():
    kvs = make_kvs()

    async def corrupting_get(folder, key):
        return "corrupted"

    kvs._client.get = corrupting_get  # type: ignore[method-assign]
    with pytest.raises(Exception, match="read-back mismatch"):
        await kvs.factory.set("exc", "4.53")


async def test_set_many_writes_every_key():
    kvs = make_kvs()
    entries = {"ch0.r": "1", "ch0.raw": "2", "cal.date": "now"}
    await kvs.factory.set_many(entries)
    assert kvs._client.store == {("F", k): v for k, v in entries.items()}


async def test_set_many_aborts_midway_and_leaves_earlier_keys():
    kvs = make_kvs(reject_key="ch1.raw")
    with pytest.raises(KvsRejected):
        await kvs.factory.set_many({"ch0.raw": "1", "ch1.raw": "2", "cal.date": "now"})
    assert kvs._client.store == {("F", "ch0.raw"): "1"}


async def test_set_group_deletes_the_marker_before_writing():
    kvs = make_kvs()
    seed(kvs, "U", {"cal.date": "old", "ch0.raw": "1"})
    await kvs.user.set_group({"ch0.raw": "2", "cal.date": "new"}, marker="cal.date")
    # The marker is deleted first and written last: an interrupted rewrite
    # reads as torn, never as old-date-plus-new-data.
    deletes = [c for c in kvs._client.commands if c[0] == b"DEL"]
    assert deletes == [(b"DEL", "U", "cal.date")]
    sets = [c[2] for c in kvs._client.commands if c[0] == b"SET"]
    assert sets == ["ch0.raw", "cal.date"]
    assert kvs._client.store == {("U", "ch0.raw"): "2", ("U", "cal.date"): "new"}


async def test_set_group_without_prior_marker_skips_the_delete():
    kvs = make_kvs()
    await kvs.user.set_group({"ch0.raw": "2", "cal.date": "new"}, marker="cal.date")
    assert not [c for c in kvs._client.commands if c[0] == b"DEL"]
    assert kvs._client.store == {("U", "ch0.raw"): "2", ("U", "cal.date"): "new"}


async def test_set_group_requires_its_marker():
    kvs = make_kvs()
    with pytest.raises(ValueError, match="cal.date"):
        await kvs.user.set_group({"ch0.raw": "2"}, marker="cal.date")


async def test_set_group_leaves_the_marker_deleted_on_failure():
    kvs = make_kvs()
    seed(kvs, "U", {"cal.date": "old"})
    kvs._client.reject_key = "ch1.raw"
    with pytest.raises(KvsRejected):
        await kvs.user.set_group(
            {"ch0.raw": "2", "ch1.raw": "3", "cal.date": "new"}, marker="cal.date"
        )
    # Torn on the device: the old marker is gone, so no reader can mistake
    # the half-written group for a valid one.
    assert ("U", "cal.date") not in kvs._client.store


async def test_read_all_returns_every_string_entry():
    kvs = make_kvs()
    seed(kvs, "F", {"exc": "4.53,nominal", "adc_fsr": "1.2,nominal"})
    seed(kvs, "U", {"lc0.cap": "100"})
    assert await kvs.factory.read_all() == {
        "exc": "4.53,nominal",
        "adc_fsr": "1.2,nominal",
    }
    assert await kvs.read_all() == {
        "F": {"exc": "4.53,nominal", "adc_fsr": "1.2,nominal"},
        "U": {"lc0.cap": "100"},
        "S": {},
    }


async def test_read_all_skips_non_string_entries():
    kvs = make_kvs()
    seed(kvs, "F", {"exc": "4.53"})

    async def entries(folder):
        return [("exc", 0x21), ("blob", 0x42)]  # 0x42 is not a string type

    kvs._client.list_entries = entries  # type: ignore[method-assign]
    assert await kvs.factory.read_all() == {"exc": "4.53"}


async def test_delete_removes_the_key():
    kvs = make_kvs()
    seed(kvs, "U", {"scratch": "1"})
    await kvs.user.delete("scratch")
    assert ("U", "scratch") not in kvs._client.store
