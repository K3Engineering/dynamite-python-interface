"""BLE key-value store (KVS) client for the Dynamite Sampler.

Firmware protocol

    request : <Cmd:3><Folder:1><Cmd_data>      e.g. b"SETFexc=4.53,nominal"
    response: <Status> <request> ['=' <payload>]   one notification per command

    '1' success  — payload follows '=' (GET: the value; IDX: key=typeHex)
    '0' rejected — the request's fault: no such key, bad frame, IDX past
        the last entry (this is how list_entries iteration ends)
    'B' busy     — the device is locked (ADC feed streaming); the request
        was not processed. Retrying is the caller's policy; set_verified
        implements a bounded one
    'E' error    — device-side storage (NVS) failure; never a missing key

Commands: SET / GET / DEL / IDX. Folder: 'F'actory, 'U'ser, 'S'ettings.
Keys <= 15 chars, values <= 128 chars, frames < 240 bytes.

Every request gets an answer, so a missing answer (KvsTimeout) means the
link is broken — not a busy device. Still sequence KVS access and feed
streaming: concurrent access now fails fast with KvsBusy instead of
hanging.
"""

import asyncio
import re

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
    "KVS_WRITE_DELAY_S",
    "KEY_DEVICE_NAME",
    "KvsError",
    "KvsRejected",
    "KvsBusy",
    "KvsDeviceError",
    "KvsTimeout",
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

# Settings namespace keys (value grammar: docs/flash-schema-v1.md).
KEY_DEVICE_NAME = "device_name"

# device_name grammar (docs/flash-schema-v1.md): ASCII, 1-29 chars, first
# char alphanumeric, no outer whitespace. Enforced client-side on every
# write — the firmware transport does not validate, and the firmware that
# will apply this value to the GAP identity must not depend on writers.
_DEVICE_NAME_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9 ._()'-]{0,28}$")

_COMMAND_TIMEOUT_S = 5.0

# Backoff between busy ('B') retries in set_verified.
KVS_WRITE_DELAY_S = 0.5


class KvsError(Exception):
    """Base for KVS command failures."""


class KvsRejected(KvsError):
    """The device answered '0': no such key, bad input, or IDX past the
    last entry."""


class KvsBusy(KvsError):
    """The device answered 'B': locked (ADC feed streaming), the request
    was not processed."""


class KvsDeviceError(KvsError):
    """The device answered 'E': a storage-layer (NVS) failure on the
    device. Never a missing key; a mid-iteration error is not
    end-of-keys."""


class KvsTimeout(KvsError, TimeoutError):
    """No reply within the command timeout. The device answers every
    request (a busy device answers 'B'), so this means the link is
    broken. Also catchable as the builtin TimeoutError."""


def _check_device_name(value: str) -> None:
    if value != value.strip():
        raise ValueError(f"device_name must not have outer whitespace: {value!r}")
    if not _DEVICE_NAME_RE.fullmatch(value):
        raise ValueError(
            f"device_name must match {_DEVICE_NAME_RE.pattern!r}: {value!r}"
        )


class KvsClient:
    """An open BLE connection with the KVS notification plumbing set up.

    Usage:
        async with await KvsClient.connect() as kvs:
            await kvs.set(FOLDER_FACTORY, "exc", "4.53,nominal")
    """

    def __init__(self, client: bleak.BleakClient, advertised_name: str):
        self.client = client
        # The BLE advertisement name the device was found under, NOT the
        # user-assigned Settings name (see get_device_name).
        self.advertised_name = advertised_name
        # Commands are strictly serialized (the firmware answers one write
        # at a time anyway, and out-of-order replies need exact-echo
        # matching to be attributable at all). _pending is the single
        # outstanding (request, future); see _on_notify.
        self._lock = asyncio.Lock()
        self._pending: tuple[bytes, asyncio.Future[bytes]] | None = None

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
            found = "\n".join(
                f"  {d.address}  {d.name}  (RSSI {adv.rssi} dBm)"
                for d, adv in devices
            )
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
        pending = self._pending
        if pending is None:
            return  # stale frame, e.g. arrived after its command timed out
        request, fut = pending
        # The echo sits at a fixed position; success answers continue with
        # '=' and all others end at the echo, so matching is prefix-free
        # (a late "1GETFabcX=v" does not settle a pending "GETFabc").
        # Anything not answering the pending command (stale, foreign, or
        # malformed) is ignored; the command's own reply or timeout
        # settles it.
        if reply[1 : 1 + len(request)] != request:
            return
        status, rest = reply[:1], reply[1 + len(request) :]
        if status == b"1" and rest.startswith(b"="):
            self._pending = None
            fut.set_result(rest[1:])
        elif status == b"0" and not rest:
            self._pending = None
            fut.set_exception(KvsRejected(f"Device rejected {request!r}"))
        elif status == b"B" and not rest:
            self._pending = None
            fut.set_exception(KvsBusy(f"Device busy (locked/streaming): {request!r}"))
        elif status == b"E" and not rest:
            self._pending = None
            fut.set_exception(KvsDeviceError(f"Device storage error: {request!r}"))
        elif status not in (b"0", b"1", b"B", b"E"):
            # An unknown status byte answering this command is a protocol
            # break; fail loudly instead of riding out the timeout.
            self._pending = None
            fut.set_exception(
                KvsError(f"Unknown KVS status byte {status!r} in {reply!r}")
            )

    async def _command(self, cmd: bytes, folder: str, data: str = "") -> bytes:
        """Send a command and return the reply payload (after the '=').

        Raises KvsRejected on '0', KvsBusy on 'B', KvsDeviceError on 'E',
        and KvsTimeout when no matching reply arrives within
        _COMMAND_TIMEOUT_S (stale, foreign, or malformed frames are
        ignored — see _on_notify)."""
        request = cmd + folder.encode() + data.encode()
        async with self._lock:
            fut: asyncio.Future[bytes] = asyncio.get_running_loop().create_future()
            self._pending = (request, fut)
            try:
                await self.client.write_gatt_char(KVS_CHR_UUID, request, response=True)
                return await asyncio.wait_for(fut, _COMMAND_TIMEOUT_S)
            except asyncio.TimeoutError:
                raise KvsTimeout(f"No reply to {request!r}") from None
            finally:
                self._pending = None

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
        if folder == FOLDER_SETTINGS and key == KEY_DEVICE_NAME:
            _check_device_name(value)
        await self._command(b"SET", folder, f"{key}={value}")

    async def get(self, folder: str, key: str) -> str:
        self._check_key_val(key)
        payload = await self._command(b"GET", folder, key)
        return payload.decode()

    async def get_device_name(self) -> str | None:
        """The user-assigned device name (Settings namespace), or None when
        unset — the device then goes by its advertised name. Value grammar:
        docs/flash-schema-v1.md. A missing key is the rejection case here;
        transport and framing failures raise."""
        try:
            return await self.get(FOLDER_SETTINGS, KEY_DEVICE_NAME)
        except KvsRejected:
            return None

    async def delete(self, folder: str, key: str) -> None:
        self._check_key_val(key)
        await self._command(b"DEL", folder, key)

    async def list_entries(self, folder: str) -> list[tuple[str, int]]:
        """(key, nvs_type) pairs for the whole namespace, via the IDX command.

        Iteration ends at the first rejection (IDX past the last entry). A
        mid-iteration storage error raises KvsDeviceError instead — a
        truncated listing cannot pass as complete."""
        found = []
        for idx in range(100):  # sanity bound
            try:
                payload = (
                    await self._command(b"IDX", folder, format(idx, "x"))
                ).decode()
            except KvsRejected:
                break  # IDX past the last key is rejected by the device
            key, _, type_hex = payload.partition("=")  # "<key>=<nvs type, hex>"
            found.append((key, int(type_hex, 16)))
        return found

    async def keys(self, folder: str) -> list[str]:
        """All keys in the namespace."""
        return [key for key, _ in await self.list_entries(folder)]

    async def set_verified(
        self, folder: str, key: str, value: str, attempts: int = 3
    ) -> str:
        """SET + read-back verify. Returns the readback (compare against
        `value` to confirm the write; a mismatch is returned, not
        retried). Retries only while the device answers 'B' (busy);
        rejection, device error, and timeout raise immediately."""
        for attempt in range(attempts):
            try:
                await self.set(folder, key, value)
                return await self.get(folder, key)
            except KvsBusy:
                if attempt + 1 == attempts:
                    raise
                await asyncio.sleep(KVS_WRITE_DELAY_S)
        raise ValueError(f"attempts must be >= 1 (got {attempts})")

    async def set_many_verified(
        self, folder: str, entries: dict[str, str]
    ) -> dict[str, str]:
        """set_verified over a {key: value} mapping; returns {key: readback}."""
        return {
            key: await self.set_verified(folder, key, value)
            for key, value in entries.items()
        }
