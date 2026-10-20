"""KvsNamespace: verified writes update the snapshot, the calibration
rebuild (the on-change callback) runs once per set / per set_many batch,
and a rebuild failure raises out of the change that caused it.

KvsClient with the BLE transport (_command) stubbed out; the namespace
logic runs against the real code.
"""

import pytest

from dynamite_sampler.errors import CalibrationError
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
        raise NotImplementedError(cmd)


def make_kvs(**kwargs):
    kvs = Kvs(client=None, advertised_name="fake")
    kvs._client = FakeClient(**kwargs)
    kvs._namespaces = {"F": {}, "U": {}}
    return kvs


def seed(kvs, folder, entries):
    """Seed a folder on both sides: the cached snapshot and the fake device."""
    kvs._namespaces[folder] = dict(entries)
    for key, value in entries.items():
        kvs._client.store[(folder, key)] = value


async def test_set_updates_snapshot_and_fires_change():
    kvs = make_kvs()
    changes = []
    kvs.set_on_change(changes.append)
    assert await kvs.factory.set("exc", "4.53,nominal") == "4.53,nominal"
    assert kvs.snapshot["F"]["exc"] == "4.53,nominal"
    assert len(changes) == 1


async def test_set_raises_the_rebuild_error():
    kvs = make_kvs()

    def bad_rebuild(_snapshot):
        raise CalibrationError("present and wrong")

    kvs.set_on_change(bad_rebuild)
    with pytest.raises(CalibrationError, match="present and wrong"):
        await kvs.factory.set("exc", "4.53")
    # The write itself did land — the snapshot reflects the device.
    assert kvs.snapshot["F"]["exc"] == "4.53"


async def test_set_many_rebuilds_once_after_all_keys():
    kvs = make_kvs()
    seen = []
    kvs.set_on_change(lambda snapshot: seen.append(dict(snapshot["F"])))
    await kvs.factory.set_many({"ch0.r": "1", "ch0.raw": "2", "cal.date": "now"})
    assert kvs.snapshot["F"] == {"ch0.r": "1", "ch0.raw": "2", "cal.date": "now"}
    # One rebuild, and it saw the complete batch (writing the group
    # key-by-key would rebuild against a torn group and raise).
    assert seen == [{"ch0.r": "1", "ch0.raw": "2", "cal.date": "now"}]


async def test_set_many_aborts_midway_without_rebuild():
    kvs = make_kvs(reject_key="ch1.raw")
    changes = []
    kvs.set_on_change(changes.append)
    with pytest.raises(KvsRejected):
        await kvs.factory.set_many({"ch0.raw": "1", "ch1.raw": "2", "cal.date": "now"})
    # Keys before the failure landed; no rebuild ran against the partial
    # batch, so the device's calibration is untouched (still valid).
    assert kvs.snapshot["F"] == {"ch0.raw": "1"}
    assert changes == []


async def test_set_group_deletes_the_marker_before_writing():
    kvs = make_kvs()
    seed(kvs, "U", {"cal.date": "old", "ch0.raw": "1"})
    seen = []
    kvs.set_on_change(lambda snapshot: seen.append(dict(snapshot["U"])))
    await kvs.user.set_group({"ch0.raw": "2", "cal.date": "new"}, marker="cal.date")
    assert kvs.snapshot["U"] == {"ch0.raw": "2", "cal.date": "new"}
    # The marker is deleted first and written last: an interrupted rewrite
    # reads as torn, never as old-date-plus-new-data.
    deletes = [c for c in kvs._client.commands if c[0] == b"DEL"]
    assert deletes == [(b"DEL", "U", "cal.date")]
    sets = [c[2] for c in kvs._client.commands if c[0] == b"SET"]
    assert sets == ["ch0.raw", "cal.date"]
    # Rebuild ran once, against the complete group.
    assert seen == [{"ch0.raw": "2", "cal.date": "new"}]


async def test_set_group_without_prior_marker_skips_the_delete():
    kvs = make_kvs()
    await kvs.user.set_group({"ch0.raw": "2", "cal.date": "new"}, marker="cal.date")
    assert not [c for c in kvs._client.commands if c[0] == b"DEL"]
    assert kvs.snapshot["U"] == {"ch0.raw": "2", "cal.date": "new"}


async def test_set_group_requires_its_marker():
    kvs = make_kvs()
    with pytest.raises(ValueError, match="cal.date"):
        await kvs.user.set_group({"ch0.raw": "2"}, marker="cal.date")


async def test_set_group_leaves_the_marker_deleted_on_failure():
    kvs = make_kvs()
    seed(kvs, "U", {"cal.date": "old"})
    kvs._client.reject_key = "ch1.raw"
    changes = []
    kvs.set_on_change(changes.append)
    with pytest.raises(KvsRejected):
        await kvs.user.set_group(
            {"ch0.raw": "2", "ch1.raw": "3", "cal.date": "new"}, marker="cal.date"
        )
    # Torn on the device: the old marker is gone, so no reader can mistake
    # the half-written group for a valid one.
    assert "cal.date" not in kvs.snapshot["U"]
    assert changes == []
