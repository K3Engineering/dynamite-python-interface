#!/usr/bin/env python
"""Perform a BLE OTA update on a Dynamite Sampler board.

Flash a local firmware .bin (-f), or go through the GitHub release
catalog: --check reports the installed firmware versus the channel
target, --latest downloads, verifies, and flashes it. The BLE protocol
lives in dynamite_sampler.ota; the release rules in
dynamite_sampler.releases; this script is argparse, device lookup by
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
from dynamite_sampler.errors import (  # noqa: E402
    ConnectionLost,
    DeviceNotFound,
    FirmwareCatalogError,
)
from dynamite_sampler.gatt import DeviceInformation  # noqa: E402
from dynamite_sampler.ota import Ota  # noqa: E402
from dynamite_sampler.releases import (  # noqa: E402
    FirmwareChannel,
    GithubReleaseCatalog,
    describe_matches_tag,
    parse_firmware_rev,
)


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


async def _read_firmware_rev(client):
    """The DIS Firmware Revision string, or None when unreadable."""
    try:
        raw = await client.read_gatt_char(DeviceInformation.FirmwareRevision.UUID)
    except bleak.exc.BleakCharacteristicNotFoundError:
        return None
    return DeviceInformation.FirmwareRevision.unpack(raw)


async def _release_image(client, channel_name, check_only):
    """The channel target's image bytes, or None when there is nothing to
    flash (check only, no release, or the device already runs it)."""
    rev = await _read_firmware_rev(client)
    parsed = rev and parse_firmware_rev(rev)
    if parsed is None:
        raise FirmwareCatalogError(
            f"Cannot parse the device firmware revision ({rev!r}); "
            "expected '<board>|<version>'."
        )
    channel = FirmwareChannel(channel_name)
    target = await asyncio.to_thread(GithubReleaseCatalog().latest_for, channel)
    print(f"Installed: {parsed.describe} (board {parsed.board})")
    print(
        f"{channel.value.capitalize()} target: "
        f"{target.tag if target else 'no release available'}"
    )
    if target is None:
        return None
    up_to_date = describe_matches_tag(parsed.describe, target.tag)
    if check_only:
        print("up to date" if up_to_date else "update available")
        return None
    if up_to_date:
        print("Already running the channel target; nothing to flash.")
        return None
    print(f"Downloading {target.asset_name}...")
    return await asyncio.to_thread(GithubReleaseCatalog().download_image, target)


async def run(args, image) -> None:
    found = await _find_by_name(args.device_name)
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
        if args.file is not None:
            rev = await _read_firmware_rev(client)
            if rev is not None:
                print(f"Installed: {rev}")
        else:
            image = await _release_image(client, args.channel, args.check)
            if image is None:
                return
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
    source = parser.add_mutually_exclusive_group(required=True)
    source.add_argument("-f", "--file", help="flash a local firmware .bin image")
    source.add_argument(
        "--check",
        action="store_true",
        help="report installed vs channel target; flash nothing",
    )
    source.add_argument(
        "--latest",
        action="store_true",
        help="download, verify, and flash the channel target",
    )
    parser.add_argument(
        "--channel",
        choices=["stable", "beta"],
        default="stable",
        help="release channel for --check/--latest (default: %(default)s)",
    )
    parser.add_argument("device_name", metavar="device-name")
    args = parser.parse_args()

    image = None
    if args.file is not None:
        if args.channel != "stable":
            parser.error("--channel applies only with --check or --latest")
        try:
            with open(args.file, "rb") as f:
                image = f.read()
        except OSError as exc:
            sys.exit(f"Cannot read {args.file}: {exc}")
    try:
        asyncio.run(run(args, image))
    except dms.DynamiteError as exc:
        sys.exit(str(exc))


if __name__ == "__main__":
    main()
