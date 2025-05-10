import asyncio
from typing import Iterable, Optional, TypeVar, Optional, Callable, Awaitable
import functools

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


## All of this getter stuff smells funky. Should be nicer, but it gets the job done for now.
# It smells cause I have to make a getter for every unpack function. Maybe have a
# get characteristic function that handles this better? Would be nice to have the
# characterisitcs typed with a protocol class, and I just pass in the class that I want.
# The class would have the UUID and the unpack function.
T = TypeVar("T")


def catch_characteristic_exc(
    func: Callable[..., Awaitable[T]],
) -> Callable[..., Awaitable[Optional[T]]]:
    """Util for reading characteristics. Return None instead of raising exception if
    they don't exist. The Type hints just that that it changes the wrapped function
    return into optional return of the same type.
    """

    @functools.wraps(func)
    async def wrapper(*args, **kwargs) -> Optional[T]:
        try:
            return await func(*args, **kwargs)
        except bleak.exc.BleakCharacteristicNotFoundError:
            return None

    return wrapper


@catch_characteristic_exc
async def get_firmware_revision(client: bleak.BleakClient) -> str:
    fw_uuid = bleak.uuids.normalize_uuid_16(ds.DeviceInfo.FirmwareRevision.UUID)
    return str(await client.read_gatt_char(fw_uuid), "utf-8")


@catch_characteristic_exc
async def get_manufacture(client: bleak.BleakClient) -> str:
    manu_uuid = bleak.uuids.normalize_uuid_16(ds.DeviceInfo.ManufacturerName.UUID)
    return str(await client.read_gatt_char(manu_uuid), "utf-8")


@catch_characteristic_exc
async def get_loadcell_calibration(client: bleak.BleakClient):
    return ds.DynamiteSampler.LoadCellCalibration.unpack(
        await client.read_gatt_char(ds.DynamiteSampler.LoadCellCalibration.UUID)
    )


@catch_characteristic_exc
async def get_adc_info(client: bleak.BleakClient):
    return ds.DynamiteSampler.ADCConfig.ConfigData.unpack(
        await client.read_gatt_char(ds.DynamiteSampler.ADCConfig.UUID)
    )


# TODO rename main to something that describes that it streams the data to callbacks
async def dynamite_sampler_connect_notify(
    callbacks_raw: Iterable[NotifyCallbackRawData],
    callbacks_feeddata: Iterable[NotifyCallbackFeeddatas],
):
    print("Looking for dynamite sampler devices")
    devices_and_adv = await find_dynamite_samplers()

    device = interactive_select_device(devices_and_adv)

    if not device:
        return

    def disco(dev):
        for cbr in callbacks_raw:
            cbr.cleanup()

        for cbfd in callbacks_feeddata:
            cbfd.cleanup()

        print("disconnection callback finished for:", dev)

    print("Connecting to:", device)
    async with bleak.BleakClient(device, disconnected_callback=disco) as client:
        print("Connected!")

        dev_info = {
            "FirmwareRevision": await get_firmware_revision(client),
            "ManufacturerName": await get_manufacture(client),
            "LoadcellCalibration": await get_loadcell_calibration(client),
            "ADCConfig": await get_adc_info(client),
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

    print("Device has disconnected.")
