#!/usr/bin/env python
"""Perform a BLE OTA update on a Dynamite Sampler board.

The protocol, chunking, and handshake rules live in
``dynamite_sampler.ota``; this script is argparse, device lookup by
advertised name, and the progress bar.
"""

import argparse
import asyncio
import sys
import time
from pathlib import Path

import bleak
from tqdm import tqdm

# Run from a checkout without installing the package (kvs_api_shim idiom).
sys.path.insert(0, str(Path(__file__).resolve().parent / "src"))

import dynamite_sampler as dms  # noqa: E402
from dynamite_sampler import discovery  # noqa: E402
from dynamite_sampler.errors import ConnectionLost, DeviceNotFound  # noqa: E402
from dynamite_sampler.ota import Ota  # noqa: E402


async def _find_by_name(name):
    """The in-range device advertising ``name``, or :class:`DeviceNotFound`.

    ``discover()`` is sorted by RSSI descending, so a duplicated name picks
    the strongest one."""
    devices = await discovery.discover()
    for d in devices:
        if d.name == name:
            return d
    listing = "\n".join(
        f"  {d.address}  {d.name}  (RSSI {d.rssi} dBm)" for d in devices
    )
    raise DeviceNotFound(
        f"No Dynamite Sampler named {name!r} in range. In range:\n{listing}"
    )


async def send_ota(device_name: str, image: bytes) -> None:
    found = await _find_by_name(device_name)
    print(f"Connecting to {found.name} ({found.address}, {found.rssi} dBm)...")
    state = {"ota": None}

    def on_disconnect(_client):
        ota = state["ota"]
        if ota is not None:
            ota.fail_pending(ConnectionLost("device disconnected"))

    t0 = time.monotonic()
    async with bleak.BleakClient(
        found.address, disconnected_callback=on_disconnect
    ) as client:
        ota = await Ota.open(client)
        state["ota"] = ota
        try:
            with tqdm(total=len(image), unit="B", unit_scale=True) as pbar:
                await ota.flash(
                    image, on_progress=lambda sent: pbar.update(sent - pbar.n)
                )
        finally:
            await ota.close()
    print(
        f"OTA successful; the device is restarting into the new image. "
        f"Total time: {time.monotonic() - t0:.1f} s"
    )


def main():
    parser = argparse.ArgumentParser(
        formatter_class=argparse.RawDescriptionHelpFormatter, description=__doc__
    )
    parser.add_argument("-f", "--file", required=True, help="firmware .bin image")
    parser.add_argument("device_name", metavar="device-name")
    args = parser.parse_args()

    with open(args.file, "rb") as f:
        image = f.read()
    try:
        asyncio.run(send_ota(args.device_name, image))
    except dms.DynamiteError as exc:
        sys.exit(f"OTA failed: {exc}")


if __name__ == "__main__":
    main()
