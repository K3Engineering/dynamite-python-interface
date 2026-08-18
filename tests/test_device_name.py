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
    """KvsClient with get stubbed out; get_device_name runs against real logic."""

    def __init__(self):
        super().__init__(client=None, advertised_name="fake")
        self.store = {}

    async def get(self, folder, key):
        value = self.store.get((folder, key))
        if value is None:
            raise KvsRejected("no such key")  # the '0' reply for a missing key
        return value


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


if __name__ == "__main__":
    unittest.main()
