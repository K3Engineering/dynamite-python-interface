"""BLE OTA firmware-update client for the Dynamite Sampler.

Wire sequence (see firmware/sampler-firmware/main/ble_ota.cpp). The device
sends exactly one Control notification per handshake step, and none during
the Data writes:

    request(+size) -> ACK/NAK -> image bytes on Data -> done -> ACK/NAK

After the final ACK the device sleeps 500 ms and restarts into the new
image, dropping the link; ``close`` tolerates a link that is already down.

Two write rules:

- Image chunks are written WITH response: the firmware applies each chunk
  inside its write handler and the ATT ack is the flow control. Chunking
  to write-without-response would silently overflow it.
- The final DONE is written WITHOUT response: a with-response DONE hangs.
  Carried over from the standalone script and the app's OtaClient; both
  flag it for re-verification against current firmware.

``Ota`` mirrors ``Kvs``: a session over an already-connected client, not
connection-owning; the owner routes link loss into ``fail_pending``. One
``flash`` per instance.
"""

import asyncio
import collections.abc

import bleak

from .errors import OtaError, OtaRejected, OtaTimeout
from .gatt import OTA

__all__ = [
    "Ota",
    "MAX_CHUNK_BYTES",
]

# Upper bound on one Control round trip. The request wait covers
# pre-erasing the OTA slot; the done wait covers digest-checking a ~1 MB
# image. (Value from the app's OtaClient, not measured here.)
_ACK_TIMEOUT_S = 30.0

# Even with a larger negotiated MTU there is no gain in splitting the
# write into multiple packets.
MAX_CHUNK_BYTES = 244


class Ota:
    """The OTA flash session over an already-connected BLE client.

    Open it with :meth:`open`, hand the connection's disconnect callback
    :meth:`fail_pending`, and call :meth:`flash` once. :meth:`close` only
    unsubscribes; closing the link is the owner's job.
    """

    def __init__(self, client: bleak.BleakClient, ack_timeout_s=None, chunk_size=None):
        self._client = client
        self._ack_timeout_s = (
            ack_timeout_s if ack_timeout_s is not None else _ACK_TIMEOUT_S
        )
        self.chunk_size = (
            chunk_size
            if chunk_size is not None
            else min(client.mtu_size - 3, MAX_CHUNK_BYTES)
        )
        # The single outstanding handshake wait; see _transact.
        self._pending = None

    @classmethod
    async def open(
        cls, client: bleak.BleakClient, *, ack_timeout_s=None, chunk_size=None
    ) -> "Ota":
        """Subscribe to the OTA Control characteristic."""
        ota = cls(client, ack_timeout_s, chunk_size)
        await client.start_notify(OTA.Control.UUID, ota._on_notify)
        return ota

    async def close(self) -> None:
        """Stop OTA notifications (does not disconnect the client).

        Called after ``flash``: the device restarts 500 ms after the final
        ACK, so the link may already be down here."""
        if self._client.is_connected:
            await self._client.stop_notify(OTA.Control.UUID)

    def fail_pending(self, exc: Exception) -> None:
        """Settle an in-flight handshake with ``exc`` (called on link loss)."""
        fut = self._pending
        self._pending = None
        if fut is not None and not fut.done():
            fut.set_exception(exc)

    def _on_notify(self, _sender, data) -> None:
        # A notification settles only the live wait; the device sends
        # exactly one reply per handshake step and none during the data
        # writes, so an unsettled wait is impossible to misattribute.
        fut = self._pending
        if fut is None or fut.done():
            return  # stale frame
        fut.set_result(bytes(data))

    async def _transact(self, frame: bytes, what: str, *, response: bool) -> bytes:
        """One handshake round trip: write, then return the single reply.

        The wait is armed BEFORE the write: the device notifies inside its
        write handler, so the reply can reach the host ahead of the write's
        completion, and a wait armed after the write would race (and
        silently drop) the single, unretried reply."""
        fut = asyncio.get_running_loop().create_future()
        self._pending = fut
        try:
            await self._client.write_gatt_char(
                OTA.Control.UUID, frame, response=response
            )
            return await asyncio.wait_for(fut, self._ack_timeout_s)
        except asyncio.TimeoutError:
            raise OtaTimeout(
                f"No reply while waiting for the device to {what}"
            ) from None
        finally:
            self._pending = None

    async def flash(
        self, image: bytes, on_progress: collections.abc.Callable[[int], None] = None
    ) -> None:
        """Flash ``image`` onto the device's next OTA slot.

        Returns once the device has accepted the image (it then reboots
        into it on its own). ``on_progress`` is called with cumulative
        bytes written after each chunk. Raises :class:`OtaRejected` on a
        device refusal, :class:`OtaTimeout` on a silent device,
        :class:`OtaError` on a protocol surprise; transport errors from
        bleak propagate."""
        if not image:
            raise ValueError("image must be non-empty")

        # opcode + image size as u32 LE
        request = await self._transact(
            OTA.Control.REQUEST + len(image).to_bytes(4, "little"),
            "start the update",
            response=True,
        )
        if request == OTA.Control.REQUEST_NAK:
            raise OtaRejected("The device declined to start the update.")
        if request != OTA.Control.REQUEST_ACK:
            raise OtaError(_unexpected(request, "start the update"))

        for offset in range(0, len(image), self.chunk_size):
            await self._client.write_gatt_char(
                OTA.Data.UUID, image[offset : offset + self.chunk_size], response=True
            )
            if on_progress is not None:
                on_progress(min(offset + self.chunk_size, len(image)))

        # Without response on purpose; see the module doc.
        done = await self._transact(
            OTA.Control.DONE, "finalize the update", response=False
        )
        if done == OTA.Control.DONE_NAK:
            raise OtaRejected("The device rejected the image (integrity check failed).")
        if done != OTA.Control.DONE_ACK:
            raise OtaError(_unexpected(done, "finalize the update"))


def _unexpected(reply: bytes, what: str) -> str:
    if not reply:
        return f"Empty reply while waiting for the device to {what}."
    return f"Unexpected reply 0x{reply.hex()} while waiting for the device to {what}."
