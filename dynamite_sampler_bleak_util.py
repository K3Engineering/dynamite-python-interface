import asyncio
from typing import Iterable, Optional, Optional

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


# TODO rename main to something that describes that it streams the data to callbacks
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

    def disco(dev):
        print("Disconnecting from device:", dev)
        print("Starting callback clean-up")
        for cbr in callbacks_raw:
            try:
                cbr.cleanup()
            except Exception as e:
                print(f"  cleanup error for {cbr}: {e}")

        for cbfd in callbacks_feeddata:
            try:
                cbfd.cleanup()
            except Exception as e:
                print(f"  cleanup error for {cbfd}: {e}")

        print("Finished callback clean-up")

    print("Connecting to:", device)
    async with bleak.BleakClient(device, disconnected_callback=disco) as client:
        print("Connected!")

        # TODO this is temporary, have the power setting be passed it, or have a callback
        if tx_power is not None:
            print(f"Setting TX power to {tx_power} dBm")
            await write_characteristic(client, ds.TxPower.TxPowerSet, tx_power)

        dev_info_cls = (
            ds.DeviceInfo.FirmwareRevision,
            ds.DeviceInfo.ManufacturerName,
            ds.DeviceInfo.TxPowerLevel,
            ds.DynamiteSampler.LoadCellCalibration,
            ds.DynamiteSampler.ADCConfig,
        )
        dev_info_dict = {
            cls.__name__: await read_characteristic(client, cls) for cls in dev_info_cls
        }

        # TODO figure out how to best print this?
        print("Device information:")
        for key, value in dev_info_dict.items():
            print("\t", key, ":", value)

        # Setting up callbacks
        for cbr in callbacks_raw:
            cbr.setup(dev_info_dict)

        for cbfd in callbacks_feeddata:
            cbfd.setup(dev_info_dict)

        feeddata_queue = asyncio.Queue()

        def notify_callback(chr: bleak.BleakGATTCharacteristic, data: bytearray):
            feeddata_queue.put_nowait(data)

        await client.start_notify(ds.DynamiteSampler.ADCFeed.UUID, notify_callback)
        print("notify started")
        # keep track of the sample sequence number that is expected to come in.
        # This is not a 16 bit number, so it will not overflow unlike the header ssn.
        expected_ssn_unwrap = None
        while True:
            raw_data = await feeddata_queue.get()
            feed_packet = ds.DynamiteSampler.ADCFeed.unpack(raw_data)

            # For the first sample, there is no previously expected count.
            # Can't miss packets you weren't yet listening for.

            # There is no concern of multiple overflows for missed packets.
            # The overflow takes 65 seconds at 1khz, and the BLE will timeout in a shorter period.

            ssn = feed_packet.header.sample_sequence_number
            if expected_ssn_unwrap is None:
                expected_ssn_unwrap = ssn

            # the arithmitic does mod 2**16 to emulate uint16 overflow
            missed_samples = (ssn - expected_ssn_unwrap) % 2**16
            # Unwrap the header so that the value doesn't overflow. This way the callback
            # functions don't have to keep state and figure out when it overflows.
            feed_packet.header.sample_sequence_number = (
                expected_ssn_unwrap + missed_samples
            )
            expected_ssn_unwrap += missed_samples + len(feed_packet.samples)

            # next_expected_count = (ssn + len(feed_packet.samples)) % 2**16

            for cbr in callbacks_raw:
                cbr.callback(raw_data)

            for cbfd in callbacks_feeddata:
                cbfd.callback(feed_packet.header, feed_packet.samples, missed_samples)

    print("Device has disconnected.")
