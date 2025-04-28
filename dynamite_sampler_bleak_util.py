import asyncio
from typing import Iterable

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

    def callback(self, feeddatas: list[ds.DynamiteSampler.ADCFeed.FeedData]):
        pass

    def cleanup(self):
        pass


async def find_dynamite_samplers():
    """Return a list of devices & advertising that have a Dynamite sampler UUID.
    List is sorted by RSSI"""
    devices_and_adv = await bleak.BleakScanner.discover(return_adv=True)

    samplers = []
    for device, adv_data in sorted(
        devices_and_adv.values(), key=lambda t: t[1].rssi, reverse=True
    ):
        if ds.DynamiteSampler.UUID in adv_data.service_uuids:
            samplers.append((device, adv_data))

    return samplers


# TODO rename main to something that describes that it streams the data to callbacks
async def dynamite_sampler_connect_notify(
    callbacks_raw: Iterable[NotifyCallbackRawData],
    callbacks_feeddata: Iterable[NotifyCallbackFeeddatas],
):
    print("Looking for dynamite sampler devices")
    # TODO maybe switch to an the async with scan, and have device selection interactive
    devices_and_adv = await find_dynamite_samplers()

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

    device = devices_and_adv[i_dev][0]

    def disco(dev):
        for cbr in callbacks_raw:
            cbr.cleanup()

        for cbfd in callbacks_feeddata:
            cbfd.cleanup()

        print("disconnection callback finished for:", dev)

    print("Connecting to:", device)
    async with bleak.BleakClient(device, disconnected_callback=disco) as client:
        print("Connected!")

        # read the device info & other meta data
        # TODO make this programmatic so that it doesn't fail if the UUID isn't there
        fw_uuid = bleak.uuids.normalize_uuid_16(ds.DeviceInfo.FirmwareRevision.UUID)
        firmware = str(await client.read_gatt_char(fw_uuid), "utf-8")

        manu_uuid = bleak.uuids.normalize_uuid_16(ds.DeviceInfo.ManufacturerName.UUID)
        manufacture = str(await client.read_gatt_char(manu_uuid), "utf-8")

        loadcell_calib = ds.DynamiteSampler.LoadCellCalibration.unpack(
            await client.read_gatt_char(ds.DynamiteSampler.LoadCellCalibration.UUID)
        )
        adc_config = ds.DynamiteSampler.ADCConfig.ConfigData.unpack(
            await client.read_gatt_char(ds.DynamiteSampler.ADCConfig.UUID)
        )
        dev_info = {
            "FirmwareRevision": firmware,
            "ManufacturerName": manufacture,
            "LoadcellCalibration": loadcell_calib,
            "ADCConfig": adc_config,
        }

        # Setting up callbacks
        for cbr in callbacks_raw:
            cbr.setup(dev_info)

        for cbfd in callbacks_feeddata:
            cbfd.setup(dev_info)

        feeddata_queue = asyncio.Queue()

        def notify_callback(chr: bleak.BleakGATTCharacteristic, data: bytearray):
            feeddata_queue.put_nowait(data)

        await client.start_notify(ds.DynamiteSampler.ADCFeed.UUID, notify_callback)
        print("notify started")
        while True:
            raw_data = await feeddata_queue.get()
            fds = ds.DynamiteSampler.ADCFeed.FeedData.unpack_multiple(raw_data)

            for cbr in callbacks_raw:
                cbr.callback(raw_data)

            for cbfd in callbacks_feeddata:
                cbfd.callback(fds)
