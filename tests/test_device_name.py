"""Fake-driven checks of the Settings ``device_name`` key (grammar per
docs/flash-schema-v2.md).

KvsClient with the transport (_command) stubbed out; client-side validation
and get_device_name run against the real logic.
"""

import pytest

from dynamite_sampler.kvs import (
    FOLDER_SETTINGS,
    KEY_DEVICE_NAME,
    KvsClient,
    KvsRejected,
)


class FakeClient(KvsClient):
    """KvsClient with the BLE transport (_command) stubbed out; client-side
    validation and get_device_name run against the real logic."""

    def __init__(self):
        super().__init__(client=None, advertised_name="fake")
        self.store = {}

    async def _command(self, cmd, folder, data=""):
        key, _, value = data.partition("=")
        if cmd == b"SET":
            self.store[(folder, key)] = value
            return b""
        if cmd == b"GET":
            if (folder, key) not in self.store:
                raise KvsRejected("no such key")  # the '0' reply for a missing key
            return self.store[(folder, key)].encode()
        raise NotImplementedError(cmd)


async def test_unset_returns_none():
    assert await FakeClient().get_device_name() is None


async def test_set_returns_value():
    client = FakeClient()
    client.store[(FOLDER_SETTINGS, KEY_DEVICE_NAME)] = "Rack 4 (West)"
    assert await client.get_device_name() == "Rack 4 (West)"


async def test_reads_settings_namespace_only():
    # A same-named key in another folder must not leak in.
    client = FakeClient()
    client.store[("F", KEY_DEVICE_NAME)] = "wrong folder"
    assert await client.get_device_name() is None


async def test_valid_names_written_verbatim():
    for name in ["Rack 4 (West)", "A", "x" * 29, "DUT-04.2 'main'"]:
        client = FakeClient()
        await client.set(FOLDER_SETTINGS, KEY_DEVICE_NAME, name)
        assert client.store[(FOLDER_SETTINGS, KEY_DEVICE_NAME)] == name


async def test_invalid_names_raise_before_any_write():
    invalid = [
        "",  # empty is a DEL, not a SET
        " Rack 4",  # outer whitespace
        "Rack 4 ",  # passes the bare regex — still invalid
        "Rack\t4",  # control whitespace
        "x" * 30,  # too long
        "Räck 4",  # non-ASCII
        "Rack&4",  # outside the charset
        "'Rack 4",  # first char must be alphanumeric
    ]
    for name in invalid:
        client = FakeClient()
        with pytest.raises(ValueError):
            await client.set(FOLDER_SETTINGS, KEY_DEVICE_NAME, name)
        assert client.store == {}, name


async def test_other_settings_keys_not_policed():
    # Unknown keys pass through untouched (preserve-unknown-keys rule);
    # no grammar exists for them.
    client = FakeClient()
    await client.set(FOLDER_SETTINGS, "future_key", "?! \x01")
    assert client.store[(FOLDER_SETTINGS, "future_key")] == "?! \x01"


async def test_same_key_in_other_folders_not_policed():
    client = FakeClient()
    await client.set("U", KEY_DEVICE_NAME, "anything & co!!")
    assert client.store[("U", KEY_DEVICE_NAME)] == "anything & co!!"
