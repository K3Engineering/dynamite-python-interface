import asyncio
from typing import Iterable, Optional

import dynamite_sampler_api as ds

import bleak


class NotifyCallbackRawData:
    """Abstract callback class for handling raw data from dynamite sampler on notify messages."""

    def setup(self, device_dict: dict):
        """Setup is called after being connected to a dynamite sampler.
        device_dict contains meta data about the device."""
        pass

    def callback(self, rawdata: bytes):
        pass

    def cleanup(self):
        pass


class NotifyCallbackFeeddatas:
    """Abstract callback class for handling parsed data from dynamite sampler on notify messages."""

    def setup(self, device_dict: dict):
        """Setup is called after being connected to a dynamite sampler.
        device_dict contains meta data about the device."""
        pass

    def callback(
        self, header: ds.FeedHeader, feeddatas: list[ds.FeedData], missing: int
    ):
        """Parsed header with the sample sequence number unwrapped
        List of sample feed data
        Count of samples missed (BLE dropped) since the last time this callback was called
        """
        pass

    def cleanup(self):
        pass


async def find_dynamite_samplers() -> (
    list[tuple[bleak.BLEDevice, bleak.AdvertisementData]]
):
    """Return a list of devices & advertising that have a Dynamite sampler UUID.
    List is sorted by RSSI"""
    devices_and_adv = await bleak.BleakScanner.discover(
        return_adv=True, service_uuids=[ds.DynamiteSampler.UUID]
    )

    return sorted(devices_and_adv.values(), key=lambda t: t[1].rssi, reverse=True)


def interactive_select_device(
    devices_and_adv: list[tuple[bleak.BLEDevice, bleak.AdvertisementData]],
) -> Optional[bleak.BLEDevice]:

    if len(devices_and_adv) == 0:
        print("No devices found!")
        return None

    fmt_str = "{:^3}| {:^5}| {:^20}| {:^30}"
    header = fmt_str.format("#", "RSSI", "Address", "Name")
    print(header)
    print("-" * len(header))
    for i, (device, adv_data) in enumerate(devices_and_adv):
        print(fmt_str.format(i, adv_data.rssi, device.address, device.name))

    if i == 0:
        print("Only one device found, no need for user selection")
        i_dev = 0
    else:
        max_i = len(devices_and_adv)
        while not 0 <= (i_dev := int(input("Select device #:"))) < max_i:
            print(f"Invalid selection. Select from [0,{max_i})")

    return devices_and_adv[i_dev][0]


async def read_characteristic(
    client: bleak.BleakClient, cls: type[ds.BLECharacteristicRead[ds._UnpackResultT]]
) -> Optional[ds._UnpackResultT]:
    """Read characteristic, and unpacks the values. Returns None if it doesn't exist"""
    try:
        b = await client.read_gatt_char(cls.UUID)
    except bleak.exc.BleakCharacteristicNotFoundError:
        print("BleakCharacteristicNotFoundError:", cls)
        return None

    try:
        return cls.unpack(b)
    except Exception as e:
        print("Unpack error in:", cls)
        raise e


async def write_characteristic(
    client: bleak.BleakClient,
    cls: type[ds.BLECharacteristicWrite[ds._PackType]],
    data: ds._PackType,
    response: bool = None,
):
    await client.write_gatt_char(cls.UUID, cls.pack(data), response=response)


class SsnUnwrapper:
    """Unwraps the feed's 16-bit sample sequence number to a linear counter
    and counts missed samples, handling the 16-bit rollover (e.g. expected
    65535, got 0). Assumption: connection outages never last a full 16-bit
    cycle (~65 s)."""

    UINT16_MODULO = 2**16

    def __init__(self):
        self._expected = None

    def feed(self, feed_packet) -> int:
        """Rewrite feed_packet.header.sample_sequence_number to the absolute
        (unwrapped) value in place; return samples missed since the previous
        packet. Downstream callbacks can then treat sequence numbers as
        linear/infinite."""
        ssn = feed_packet.header.sample_sequence_number
        if self._expected is None:
            self._expected = ssn  # initialize on the first packet
        missed_samples = (ssn - self._expected) % self.UINT16_MODULO
        unwrapped = self._expected + missed_samples
        feed_packet.header.sample_sequence_number = unwrapped
        self._expected = unwrapped + len(feed_packet.samples)
        return missed_samples


# Idle-poll cadence for mid-stream disconnect detection in FeedSession.
# bleak only reports disconnects through a disconnected_callback passed to
# the BleakClient constructor, but FeedSession receives an already-connected
# client — so the pump polls is_connected rather than trusting the queue.
_DISCONNECT_POLL_S = 1.0


class FeedSession:
    """ADC feed streaming on an already-connected client, caller-controlled.

    Pumps notifications in a background task so the caller keeps the event
    loop — e.g. to operate a stimulus while streaming, then stop on demand.
    Used by the factory calibration script.

    On a mid-stream disconnect the pump drains the buffered packets and
    exits within _DISCONNECT_POLL_S instead of blocking on the queue
    forever; the caller observes it as "no more data arrives".
    """

    def __init__(
        self,
        client: bleak.BleakClient,
        callbacks_raw: Iterable[NotifyCallbackRawData] = (),
        callbacks_feeddata: Iterable[NotifyCallbackFeeddatas] = (),
        device_info: Optional[dict] = None,
    ):
        self._client = client
        self._callbacks_raw = list(callbacks_raw)
        self._callbacks_feeddata = list(callbacks_feeddata)
        # Passed to the callbacks' setup(); read from the device when not given.
        self._device_info = device_info
        self._queue: Optional[asyncio.Queue] = None
        self._pump_task: Optional[asyncio.Task] = None

    @property
    def device_info(self) -> Optional[dict]:
        """Device metadata passed to the callbacks' setup(); read from the
        device by start() unless given to the constructor."""
        return self._device_info

    async def start(self):
        if self._device_info is None:
            dev_info_cls = (
                ds.DeviceInfo.FirmwareRevision,
                ds.DeviceInfo.ManufacturerName,
                ds.DeviceInfo.TxPowerLevel,
                ds.DynamiteSampler.ADCConfig,
            )
            self._device_info = {
                cls.__name__: await read_characteristic(self._client, cls)
                for cls in dev_info_cls
            }
        for cb in (*self._callbacks_raw, *self._callbacks_feeddata):
            cb.setup(self._device_info)

        self._queue = asyncio.Queue()

        def notify_callback(sender: bleak.BleakGATTCharacteristic, data: bytearray):
            self._queue.put_nowait(data)

        await self._client.start_notify(
            ds.DynamiteSampler.ADCFeed.UUID, notify_callback
        )
        self._pump_task = asyncio.create_task(self._pump())

    async def _pump(self):
        unwrapper = SsnUnwrapper()
        while True:
            try:
                raw_data = await asyncio.wait_for(
                    self._queue.get(), _DISCONNECT_POLL_S
                )
            except asyncio.TimeoutError:
                if not self._client.is_connected:
                    print("FeedSession: device disconnected, feed pump stopped")
                    return
                continue
            feed_packet = ds.DynamiteSampler.ADCFeed.unpack(raw_data)
            missed_samples = unwrapper.feed(feed_packet)

            for cbr in self._callbacks_raw:
                cbr.callback(raw_data)
            for cbfd in self._callbacks_feeddata:
                cbfd.callback(feed_packet.header, feed_packet.samples, missed_samples)

    async def wait_done(self):
        """Block until the feed pump exits — on a mid-stream disconnect, or
        after stop() has been called."""
        if self._pump_task is not None:
            await self._pump_task

    async def stop(self):
        """Stop streaming. Safe to call twice, and after a partial start."""
        if self._pump_task is not None:
            self._pump_task.cancel()
            try:
                await self._pump_task
            except asyncio.CancelledError:
                pass
            self._pump_task = None
        if self._client.is_connected:
            try:
                await self._client.stop_notify(ds.DynamiteSampler.ADCFeed.UUID)
            except Exception:
                pass  # never subscribed, or the backend already tore it down
        for cb in (*self._callbacks_raw, *self._callbacks_feeddata):
            try:
                cb.cleanup()
            except Exception as e:
                print(f"  cleanup error for {cb}: {e}")


async def dynamite_sampler_connect_notify(
    callbacks_raw: Iterable[NotifyCallbackRawData],
    callbacks_feeddata: Iterable[NotifyCallbackFeeddatas],
    tx_power: Optional[int] = None,
):
    print("Looking for dynamite sampler devices")
    devices_and_adv = await find_dynamite_samplers()

    device = interactive_select_device(devices_and_adv)

    if not device:
        return

    print("Connecting to:", device)
    async with bleak.BleakClient(device) as client:
        print("Connected!")

        # TODO this is temporary, have the power setting be passed it, or have a callback
        if tx_power is not None:
            print(f"Setting TX power to {tx_power} dBm")
            await write_characteristic(client, ds.TxPower.TxPowerSet, tx_power)

        session = FeedSession(client, callbacks_raw, callbacks_feeddata)
        await session.start()
        try:
            # TODO figure out how to best print this?
            print("Device information:")
            for key, value in session.device_info.items():
                print("\t", key, ":", value)

            print("notify started")
            await session.wait_done()  # returns on mid-stream disconnect
        finally:
            print("Disconnecting from device:", device)
            print("Starting callback clean-up")
            await session.stop()
            print("Finished callback clean-up")

    print("Device has disconnected.")
