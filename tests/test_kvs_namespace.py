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

    async def _command(self, cmd, folder, data=""):
        key, _, value = data.partition("=")
        if key == self.reject_key:
            raise KvsRejected("no such key")
        if cmd == b"SET":
            self.store[(folder, key)] = value
            return b""
        if cmd == b"GET":
            return self.store[(folder, key)].encode()
        raise NotImplementedError(cmd)


def make_kvs(**kwargs):
    kvs = Kvs(client=None, advertised_name="fake")
    kvs._client = FakeClient(**kwargs)
    kvs._namespaces = {"F": {}, "U": {}}
    return kvs


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
