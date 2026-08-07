# Run it like so: `python -m tests.test_feed_session`

import asyncio
import unittest

import dynamite_sampler_bleak_util as dsbu

# Don't wait a second per poll cycle in tests.
dsbu._DISCONNECT_POLL_S = 0.01


class FakeClient:
    """Minimal stand-in for a connected bleak.BleakClient."""

    def __init__(self):
        self.is_connected = True
        self.notify_callback = None

    async def start_notify(self, uuid, callback):
        self.notify_callback = callback

    async def stop_notify(self, uuid):
        pass


class FeedSessionTest(unittest.TestCase):
    def test_pump_exits_on_disconnect(self):
        """A mid-stream disconnect must not leave the pump blocked on the
        notification queue forever."""

        async def scenario():
            client = FakeClient()
            session = dsbu.FeedSession(client, device_info={})
            await session.start()
            self.assertIsNotNone(session._pump_task)

            client.is_connected = False  # simulate a drop mid-stream
            await asyncio.wait_for(session.wait_done(), timeout=1.0)

            await session.stop()  # must stay clean after a disconnect

        asyncio.run(scenario())

    def test_stop_while_connected(self):
        async def scenario():
            client = FakeClient()
            session = dsbu.FeedSession(client, device_info={})
            await session.start()
            await session.stop()
            self.assertIsNone(session._pump_task)
            await session.stop()  # idempotent

        asyncio.run(scenario())


if __name__ == "__main__":
    unittest.main()
