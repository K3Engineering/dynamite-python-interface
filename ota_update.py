#!/usr/bin/env python
"""Perform BLE OTA update on a Dynamite Sampler board."""

import argparse
import asyncio
import datetime

from bleak import BleakClient, BleakScanner
from tqdm import tqdm

# TODO factor this out so its not duplicated in the firmware as well
OTA_DATA_UUID = "23408888-1F40-4CD8-9B89-CA8D45F8A5B0"
OTA_CONTROL_UUID = "7AD671AA-21C0-46A4-B722-270E3AE3D830"

SVR_CHR_OTA_CONTROL_REQUEST = bytearray.fromhex("01")
SVR_CHR_OTA_CONTROL_REQUEST_ACK = bytearray.fromhex("02")
SVR_CHR_OTA_CONTROL_REQUEST_NAK = bytearray.fromhex("03")
SVR_CHR_OTA_CONTROL_DONE = bytearray.fromhex("04")
SVR_CHR_OTA_CONTROL_DONE_ACK = bytearray.fromhex("05")
SVR_CHR_OTA_CONTROL_DONE_NAK = bytearray.fromhex("06")


async def _search_for_esp32(device_name):
    print(f"Searching for '{device_name}'...")
    esp32 = None

    devices_and_adv = await BleakScanner.discover(return_adv=True)
    print("RSSI, Device name:")

    for device, adv_data in devices_and_adv.values():
        print(adv_data.rssi, device.name, end="")
        if device.name == device_name:
            esp32 = device
            print(" - device name matches", end="")
        print("")

    if esp32 is not None:
        print(f"{device_name} found!")
    else:
        print(f"{device_name} has not been found.")
        assert esp32 is not None

    return esp32


async def send_ota(device_name: str, firmware_bin: bytes):
    t0 = datetime.datetime.now()
    queue = asyncio.Queue()

    def on_disconnected(client):
        print(datetime.datetime.now(), "Disconnected callback!")

    esp32 = await _search_for_esp32(device_name)
    async with BleakClient(esp32, disconnected_callback=on_disconnected) as client:

        async def _ota_notification_handler(sender: int, data: bytearray):
            print(datetime.datetime.now(), "Notification", end=": ")
            if data == SVR_CHR_OTA_CONTROL_REQUEST_ACK:
                print("ESP32: OTA request acknowledged.")
                await queue.put("ack1")
            elif data == SVR_CHR_OTA_CONTROL_REQUEST_NAK:
                print("ESP32: OTA request NOT acknowledged.")
                await queue.put("nak1")
                await client.stop_notify(OTA_CONTROL_UUID)
            elif data == SVR_CHR_OTA_CONTROL_DONE_ACK:
                print("ESP32: OTA done acknowledged.")
                await queue.put("ack2")
                try:
                    await client.stop_notify(OTA_CONTROL_UUID)
                except:
                    print("I think the connection died?")
            elif data == SVR_CHR_OTA_CONTROL_DONE_NAK:
                print("ESP32: OTA done NOT acknowledged.")
                await queue.put("nak2")
                try:
                    await client.stop_notify(OTA_CONTROL_UUID)
                except:
                    print("I think the connection died?")
            else:
                print(f"Notification received: sender: {sender}, data: {data}")

        # subscribe to OTA control
        await client.start_notify(OTA_CONTROL_UUID, _ota_notification_handler)

        # compute the packet size. 244 bytes is the most a single packet can handle.
        # Even if the MTU is larger, no need to split the data into multiple packets.
        packet_size = min(client.mtu_size - 3, 244)

        # request the update: opcode + image size as u32 LE
        file_size = len(firmware_bin)
        print(datetime.datetime.now(), f"Sending OTA request, size: {file_size}.")
        await client.write_gatt_char(
            OTA_CONTROL_UUID,
            SVR_CHR_OTA_CONTROL_REQUEST + file_size.to_bytes(4, "little"),
            response=True,
        )

        # wait for the response
        if await queue.get() == "ack1":
            print("Sending data...")
            # sequentially write all packets to OTA data
            num_packages = 0
            with tqdm(total=len(firmware_bin), unit="bytes") as pbar:
                for i in range(0, len(firmware_bin), packet_size):
                    pkg = firmware_bin[i : i + packet_size]
                    pbar.update(n=len(pkg))
                    num_packages += 1

                    # Response=True (write request) is primary choice, since the
                    # OTA handler can't always keep up with the incoming packets. And
                    # for some reason the LL doesn't stop accepting even if the buffer
                    # is full.
                    await client.write_gatt_char(OTA_DATA_UUID, pkg, response=True)

            # write done OP code to OTA Control
            print("Sending OTA done.")
            print(f"Number of packages sent: {num_packages}")
            # response=False has to be set, otherwise it hangs here.
            await client.write_gatt_char(
                OTA_CONTROL_UUID, SVR_CHR_OTA_CONTROL_DONE, response=False
            )

            # wait for the response
            # await asyncio.sleep(1)
            print("Waiting for confirmation...")
            if await queue.get() == "ack2":
                print(f"OTA successful!")
            else:
                print("OTA failed.")

        else:
            print("ESP32 did not acknowledge the OTA request.")
    dt = datetime.datetime.now() - t0
    print(f"Total time: {dt}")


if __name__ == "__main__":
    parser = argparse.ArgumentParser(
        formatter_class=argparse.RawDescriptionHelpFormatter, description=__doc__
    )
    parser.add_argument(
        "-f", "--file", default="./build/debug/dynamite-sampler-firmware.bin"
    )
    parser.add_argument("device_name", metavar="device-name")

    args = parser.parse_args()

    with open(args.file, "rb") as f:
        firmware_bin = f.read()
    asyncio.run(send_ota(args.device_name, firmware_bin))
