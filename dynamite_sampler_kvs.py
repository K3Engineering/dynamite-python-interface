"""BLE key-value store (KVS) client for the Dynamite Sampler.

Firmware protocol

    request : <Cmd:3><Folder:1><Cmd_data>      e.g. b"SETFexc=4.53,nominal"
    response: '1' <request> '=' <payload>      notification, on success
              '0' <request>                    notification, on failure

Commands: SET / GET / DEL / IDX. Folder: 'F'actory, 'U'ser, 'S'ettings.
Keys <= 15 chars, values <= 128 chars, frames < 240 bytes.

KVS commands are ignored while the ADC feed is streaming (firmware device
lock): sequence KVS access and feed streaming, never concurrently.
"""

import asyncio

import bleak

from dynamite_sampler_bleak_util import find_dynamite_samplers

__all__ = [
    "KVS_CHR_UUID",
    "FOLDER_FACTORY",
    "FOLDER_USER",
    "FOLDER_SETTINGS",
    "FOLDER_NAMES",
    "NVS_TYPE_STR",
    "MAX_KEY_LEN",
    "MAX_VAL_LEN",
    "KvsError",
    "KvsClient",
]

KVS_CHR_UUID = "10adce11-68a6-450b-9810-ca11b39fd283"

FOLDER_FACTORY = "F"
FOLDER_USER = "U"
FOLDER_SETTINGS = "S"

FOLDER_NAMES = {
    FOLDER_FACTORY: "Factory",
    FOLDER_USER: "User",
    FOLDER_SETTINGS: "Settings",
}

# The only entry type readable via GET (the firmware KVS writes/reads
# strings only). IDX reports other types for entries written outside this
# protocol; they are opaque here.
NVS_TYPE_STR = 0x21

MAX_KEY_LEN = 15  # firmware: USER_KVS_MAX_KEY_LEN
MAX_VAL_LEN = 128  # firmware: USER_KVS_MAX_VAL_LEN

_COMMAND_TIMEOUT_S = 5.0


class KvsError(Exception):
    """The device rejected the command, or the reply never arrived."""


class KvsClient:
    """An open BLE connection with the KVS notification plumbing set up.

    Usage:
        async with await KvsClient.connect() as kvs:
            await kvs.set(FOLDER_FACTORY, "exc", "4.53,nominal")
    """

    def __init__(self, client: bleak.BleakClient, device_name: str):
        self.client = client
        self.device_name = device_name
        # Replies are matched to requests by their echo, see _on_notify.
        self._pending: dict[bytes, asyncio.Future[bytes]] = {}

    @classmethod
    async def connect(cls, address: str | None = None) -> "KvsClient":
        """Find a dynamite sampler and connect. With address=None, exactly one
        device must be in range; otherwise pass --address to disambiguate."""
        devices = await find_dynamite_samplers()
        if address:
            matches = [d for d, _ in devices if d.address.upper() == address.upper()]
            if not matches:
                raise KvsError(f"No dynamite sampler with address {address} found")
            device = matches[0]
        elif len(devices) == 1:
            device = devices[0][0]
        elif len(devices) == 0:
            raise KvsError("No dynamite sampler devices found")
        else:
            found = "\n".join(f"  {d.address}  {d.name}" for d, _ in devices)
            raise KvsError(f"{len(devices)} devices found, pass --address:\n{found}")

        print(f"Connecting to {device.address} ({device.name})")
        client = bleak.BleakClient(device)
        await client.connect()
        kvs = cls(client, device.name or "?")
        await client.start_notify(KVS_CHR_UUID, kvs._on_notify)
        return kvs

    async def disconnect(self) -> None:
        if self.client.is_connected:
            await self.client.stop_notify(KVS_CHR_UUID)
            await self.client.disconnect()

    async def __aenter__(self) -> "KvsClient":
        return self

    async def __aexit__(self, *exc) -> None:
        await self.disconnect()

    def _on_notify(self, _sender, data: bytearray) -> None:
        reply = bytes(data).rstrip(b"\x00")
        for request, fut in list(self._pending.items()):
            if not fut.done() and reply[1 : 1 + len(request)] == request:
                del self._pending[request]
                if (
                    reply[:1] == b"1"
                    and reply[1 + len(request) : 2 + len(request)] == b"="
                ):
                    fut.set_result(reply[2 + len(request) :])
                else:
                    fut.set_exception(KvsError(f"Device rejected {request!r}"))
                return
        print(f"Unmatched KVS notification: {reply!r}")

    async def _command(self, cmd: bytes, folder: str, data: str = "") -> bytes:
        """Send a command and return the reply payload (after the '=').

        Raises KvsError if the device reports failure or the reply times out."""
        request = cmd + folder.encode() + data.encode()
        fut: asyncio.Future[bytes] = asyncio.get_running_loop().create_future()
        self._pending[request] = fut
        try:
            await self.client.write_gatt_char(KVS_CHR_UUID, request, response=True)
            return await asyncio.wait_for(fut, _COMMAND_TIMEOUT_S)
        finally:
            self._pending.pop(request, None)

    @staticmethod
    def _check_key_val(key: str, value: str | None = None) -> None:
        # No '=' in keys: the firmware splits SET data at the first '=', so
        # a key containing one would silently write under a truncated key.
        if not key or len(key) > MAX_KEY_LEN or "=" in key:
            raise ValueError(f"Key must be 1..{MAX_KEY_LEN} chars, no '=': {key!r}")
        if value is not None and not (0 < len(value) <= MAX_VAL_LEN):
            raise ValueError(f"Value for {key!r} must be 1..{MAX_VAL_LEN} chars")

    async def set(self, folder: str, key: str, value: str) -> None:
        self._check_key_val(key, value)
        await self._command(b"SET", folder, f"{key}={value}")

    async def get(self, folder: str, key: str) -> str:
        self._check_key_val(key)
        payload = await self._command(b"GET", folder, key)
        return payload.decode()

    async def delete(self, folder: str, key: str) -> None:
        self._check_key_val(key)
        await self._command(b"DEL", folder, key)

    async def list_entries(self, folder: str) -> list[tuple[str, int]]:
        """(key, nvs_type) pairs for the whole namespace, via the IDX command."""
        found = []
        for idx in range(100):  # sanity bound
            try:
                payload = (
                    await self._command(b"IDX", folder, format(idx, "x"))
                ).decode()
            except KvsError:
                break  # IDX past the last key is rejected by the device
            key, _, type_hex = payload.partition("=")  # "<key>=<nvs type, hex>"
            found.append((key, int(type_hex, 16)))
        return found

    async def keys(self, folder: str) -> list[str]:
        """All keys in the namespace."""
        return [key for key, _ in await self.list_entries(folder)]
