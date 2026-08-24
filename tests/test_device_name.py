# Run it like so: `python -m tests.test_device_name`

import asyncio
import unittest

from dynamite_sampler_kvs import (
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


class GetDeviceNameTest(unittest.TestCase):
    def test_unset_returns_none(self):
        self.assertIsNone(asyncio.run(FakeClient().get_device_name()))

    def test_set_returns_value(self):
        client = FakeClient()
        client.store[(FOLDER_SETTINGS, KEY_DEVICE_NAME)] = "Rack 4 (West)"
        self.assertEqual(asyncio.run(client.get_device_name()), "Rack 4 (West)")

    def test_reads_settings_namespace_only(self):
        # A same-named key in another folder must not leak in.
        client = FakeClient()
        client.store[("F", KEY_DEVICE_NAME)] = "wrong folder"
        self.assertIsNone(asyncio.run(client.get_device_name()))


class SetDeviceNameTest(unittest.TestCase):
    def test_valid_names_written_verbatim(self):
        for name in ["Rack 4 (West)", "A", "x" * 29, "DUT-04.2 'main'"]:
            client = FakeClient()
            asyncio.run(client.set(FOLDER_SETTINGS, KEY_DEVICE_NAME, name))
            self.assertEqual(client.store[(FOLDER_SETTINGS, KEY_DEVICE_NAME)], name)

    def test_invalid_names_raise_before_any_write(self):
        invalid = [
            "",             # empty is a DEL, not a SET
            " Rack 4",      # outer whitespace
            "Rack 4 ",      # passes the bare regex — still invalid
            "Rack\t4",      # control whitespace
            "x" * 30,       # too long
            "Räck 4",       # non-ASCII
            "Rack&4",       # outside the charset
            "'Rack 4",      # first char must be alphanumeric
        ]
        for name in invalid:
            client = FakeClient()
            with self.assertRaises(ValueError, msg=name):
                asyncio.run(client.set(FOLDER_SETTINGS, KEY_DEVICE_NAME, name))
            self.assertEqual(client.store, {}, name)

    def test_other_settings_keys_not_policed(self):
        # Unknown keys pass through untouched (preserve-unknown-keys rule);
        # no grammar exists for them.
        client = FakeClient()
        asyncio.run(client.set(FOLDER_SETTINGS, "future_key", "?! \x01"))
        self.assertEqual(client.store[(FOLDER_SETTINGS, "future_key")], "?! \x01")

    def test_same_key_in_other_folders_not_policed(self):
        client = FakeClient()
        asyncio.run(client.set("U", KEY_DEVICE_NAME, "anything & co!!"))
        self.assertEqual(client.store[("U", KEY_DEVICE_NAME)], "anything & co!!")


if __name__ == "__main__":
    unittest.main()
