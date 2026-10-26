"""The Dynamite Sampler device classes."""

import asyncio
import dataclasses
import datetime
import time

import bleak
import numpy as np

from .calibration import Calibration, Unit
from .csv_io import _GENERATOR, device_metadata
from .discovery import find_single
from .errors import (
    BufferOverrun,
    ConnectionLost,
    DynamiteError,
    ProtocolError,
    ProvisioningError,
    ReadTimeout,
    StreamActive,
    TareError,
)
from .api import (
    DeviceInformation,
    DynamiteSamplerService,
    TxPower,
    TxPowerLevel,
)
from .kvs import Kvs
from .packet import Packet
from .recording import Recording

ADCConfig = DynamiteSamplerService.ADCConfig

UNCONFIGURED = "UNCONFIGURED"

QUEUE_SECONDS = 4
DEFAULT_READ_TIMEOUT_S = 5.0

_SAMPLE_BYTES = DynamiteSamplerService.ADCFeed.SAMPLE_BYTES
_CHANNELS_PER_SAMPLE = _SAMPLE_BYTES // 3


@dataclasses.dataclass(frozen=True)
class DeviceInfo:
    address: str
    name: str | None
    board_model: str
    firmware: str | None
    manufacturer: str | None
    model_number: str | None = None
    serial: str | None = None
    # TODO(compat): Make mandatory once deployed units have Software Revision.
    framework: str | None = None


async def _read_characteristic(client, cls):
    try:
        raw = await client.read_gatt_char(cls.UUID)
    except bleak.exc.BleakCharacteristicNotFoundError:
        return None
    return cls.unpack(raw)


def _decode_samples(payload, num_channels):
    """12-byte little-endian signed samples -> (n, num_channels) float64."""
    raw = np.frombuffer(payload, dtype=np.uint8).reshape(-1, _CHANNELS_PER_SAMPLE, 3)
    raw = raw.astype(np.int32)
    values = raw[:, :, 0] | (raw[:, :, 1] << 8) | (raw[:, :, 2] << 16)
    signed = (values ^ 0x800000) - 0x800000
    return signed[:, :num_channels].astype(np.float64)


class SsnUnwrapper:
    """Unwrap the feed's 16-bit sample sequence number to a linear counter and
    count missed samples, handling the 16-bit rollover (e.g. expected 65535,
    got 0).

    The modular gap is exact only while the silence between packets is under
    one rollover period (65536 / sample_rate: 65 s at 1 ksps). BLE's
    supervision timeout caps that well below the period, so a longer dead
    interval is a dropped link, not an ambiguous count."""

    UINT16_MODULO = 2**16

    def __init__(self):
        self._expected = None

    def unwrap(self, ssn, sample_count):
        """(unwrapped_ssn, missed_samples) for a packet with `sample_count`
        samples starting at wire `ssn`."""
        if self._expected is None:
            self._expected = ssn
        missed = (ssn - self._expected) % self.UINT16_MODULO
        unwrapped = self._expected + missed
        self._expected = unwrapped + sample_count
        return unwrapped, missed


class AsyncDynamiteSampler:
    """Async device: connect, stream packets, read, tare, and KVS access.

    The device object holds no measurement state. There is no calibration or
    tare cached on it: :meth:`read_calibration` parses one from the KVS on
    demand, :meth:`tare` returns a tare array, and :meth:`stream` yields raw
    counts. Everything a capture needs is passed in explicitly.
    """

    def __init__(self, client, info, adc_config, kvs, disconnected=None):
        self._client = client
        self.info = info
        self._adc_config = adc_config
        self.kvs = kvs
        self._active = False
        self._disconnected = (
            disconnected if disconnected is not None else asyncio.Event()
        )

    @classmethod
    async def connect(cls, address=None):
        """Connect to the one device in range (or the one at ``address``).

        Reads the identity and ADC characteristics and subscribes to the
        KVS. It does not read the KVS itself, so a board with unprovisioned,
        half-written, or stale keys still connects — which is what repairing
        such a board needs."""
        found = await find_single(address)
        disconnected = asyncio.Event()
        state = {"kvs": None}

        def on_disconnect(_client):
            disconnected.set()
            kvs = state["kvs"]
            if kvs is not None:
                kvs.fail_pending(ConnectionLost("device disconnected"))

        client = bleak.BleakClient(found.address, disconnected_callback=on_disconnect)
        await client.connect()
        try:
            board_model = (
                await _read_characteristic(client, DeviceInformation.HardwareRevision)
                or UNCONFIGURED
            )
            firmware = await _read_characteristic(
                client, DeviceInformation.FirmwareRevision
            )
            manufacturer = await _read_characteristic(
                client, DeviceInformation.ManufacturerName
            )
            model_number = await _read_characteristic(
                client, DeviceInformation.ModelNumber
            )
            serial = await _read_characteristic(client, DeviceInformation.SerialNumber)
            framework = await _read_characteristic(
                client, DeviceInformation.SoftwareRevision
            )
            info = DeviceInfo(
                found.address,
                found.name,
                board_model,
                firmware,
                manufacturer,
                model_number,
                serial,
                framework,
            )
            adc_config = None
            if board_model != UNCONFIGURED:
                adc_config = await _read_characteristic(client, ADCConfig)
                if adc_config is None:
                    raise ProtocolError("ADC config characteristic unreadable")
            kvs = await Kvs.open(client, found.name)
            state["kvs"] = kvs
            return cls(client, info, adc_config, kvs, disconnected)
        except BaseException:
            await client.disconnect()
            raise

    def __repr__(self):
        return f"AsyncDynamiteSampler({self.info.address}, {self.info.board_model})"

    async def __aenter__(self):
        return self

    async def __aexit__(self, *exc):
        await self.close()

    async def close(self):
        try:
            await self.kvs.close()
        finally:
            if self._client.is_connected:
                await self._client.disconnect()

    def _require_adc(self):
        if self._adc_config is None:
            raise ProvisioningError(
                f"device is {UNCONFIGURED} (safe mode); the ADC is disabled"
            )
        return self._adc_config

    @property
    def sample_rate(self) -> int | None:
        return None if self._adc_config is None else self._adc_config.sample_rate

    @property
    def gains(self) -> list[int] | None:
        return None if self._adc_config is None else list(self._adc_config.gains)

    async def read_calibration(self) -> Calibration:
        """Parse the device's calibration from its raw KVS data.

        Raises :class:`CalibrationError` on data that is present and wrong.
        Nothing is cached: call it whenever a capture needs the conversion,
        and pass the result where it is used."""
        return Calibration.from_kvs(await self.kvs.read_all(), self.gains)

    async def read_tx_power_dbm(self) -> int | None:
        """Live read of the DIS TX Power Level (None if unreadable). Read per
        call, not cached: the factory tools log it as a measurement condition."""
        return await _read_characteristic(self._client, TxPowerLevel.Level)

    async def set_tx_power(self, dbm: int) -> int:
        """Set the BLE TX power and verify the read-back. Mismatch raises."""
        await self._client.write_gatt_char(
            TxPower.TxPowerSet.UUID, TxPower.TxPowerSet.pack(dbm), response=True
        )
        readback = await self.read_tx_power_dbm()
        if readback != dbm:
            raise DynamiteError(
                f"TX power read-back {readback} dBm != requested {dbm} dBm"
            )
        return readback

    async def stream(self, inactivity_timeout: float | None = None):
        """Infinite async generator of :class:`Packet`: one item per BLE
        notification, in raw counts.

        ``rows_dropped`` counts the samples lost since the previous packet,
        so the sample timeline is those rows then ``raw``.
        ``inactivity_timeout`` raises :class:`ReadTimeout` after that many
        seconds without a packet.
        """
        config = self._require_adc()
        if self._active:
            raise StreamActive("a stream or read is already active")
        self._active = True

        num_channels = len(config.gains)
        queue = asyncio.Queue(maxsize=config.sample_rate * QUEUE_SECONDS)
        overrun = None
        disc = asyncio.ensure_future(self._disconnected.wait())
        started = False

        def on_notify(_sender, data):
            nonlocal overrun
            if overrun is not None:
                return
            try:
                queue.put_nowait(bytes(data))
            except asyncio.QueueFull:
                overrun = BufferOverrun("consumer slower than the feed")

        unwrapper = SsnUnwrapper()
        try:
            await self._client.start_notify(
                DynamiteSamplerService.ADCFeed.UUID, on_notify
            )
            started = True
            while True:
                if overrun is not None:
                    raise overrun
                try:
                    data = await self._wait_packet(queue, disc, inactivity_timeout)
                except asyncio.TimeoutError:
                    raise ReadTimeout(f"no rows for {inactivity_timeout} s") from None
                ssn, payload = DynamiteSamplerService.ADCFeed.split(data)
                raw = _decode_samples(payload, num_channels)
                unwrapped, missed = unwrapper.unwrap(ssn, raw.shape[0])
                yield Packet(
                    time=time.monotonic(),
                    ssn=unwrapped,
                    rows_dropped=missed,
                    payload_bytes=len(data),
                    raw=raw,
                )
        finally:
            disc.cancel()
            try:
                if started and self._client.is_connected:
                    await self._client.stop_notify(DynamiteSamplerService.ADCFeed.UUID)
            finally:
                self._active = False

    async def tare(self, n: int | None = None) -> np.ndarray:
        """Average ``n`` raw samples per channel (default 1 s of feed) and
        return the result. Storing it is the caller's job. Raises
        :class:`TareError` if any channel got no valid samples."""
        config = self._require_adc()
        if n is None:
            n = config.sample_rate
        if n < 1:
            raise ValueError("n must be >= 1")
        raw, _, _ = await self._read_rows(n, timeout=DEFAULT_READ_TIMEOUT_S)
        valid = np.count_nonzero(~np.isnan(raw), axis=0)
        if np.any(valid == 0):
            empty = [int(i) for i in np.nonzero(valid == 0)[0]]
            raise TareError(f"tare failed: no valid samples on channel(s) {empty}")
        return np.nanmean(raw, axis=0)

    async def _read_rows(self, n: int, timeout: float):
        """(rows, ssn0, first_packet_time): exactly ``n`` rows off the feed,
        or :class:`ReadTimeout`.

        Dropped samples inside the window appear as NaN rows, so row ``i`` is
        sample ``ssn0 + i``. The first packet's gap is 0 (the unwrapper has no
        earlier packet to compare against), so the window always starts on a
        real sample."""
        agen = self.stream(inactivity_timeout=timeout)
        try:
            parts = []
            ssn0 = None
            host_time = None
            got = 0
            while got < n:
                packet = await agen.__anext__()
                if ssn0 is None:
                    ssn0 = packet.ssn
                    host_time = packet.time
                parts.extend(_packet_parts(packet))
                got += packet.rows_dropped + packet.rows
            return np.concatenate(parts)[:n], ssn0, host_time
        finally:
            await agen.aclose()

    async def _read_raw(
        self, n: int, *, units: Unit, calibration, tare_raw, timeout: float
    ) -> Recording:
        config = self._require_adc()
        raw, ssn0, host_time = await self._read_rows(n, timeout)
        return Recording(
            data=calibration.convert(raw, units, tare_raw),
            raw=raw,
            t=np.arange(n) / config.sample_rate,
            ssn0=ssn0,
            units=units,
            host_time=host_time,
            sample_rate=config.sample_rate,
            calibration=calibration,
            tare_raw=None if tare_raw is None else np.array(tare_raw),
            recorded_at=datetime.datetime.now().astimezone(),
            device=device_metadata(self.info, config.gains, calibration),
            generator=_GENERATOR,
        )

    async def read(
        self,
        n: int | None = None,
        *,
        seconds: float | None = None,
        units: Unit = "raw",
        calibration: Calibration | None = None,
        tare_raw=None,
        timeout: float = DEFAULT_READ_TIMEOUT_S,
    ) -> Recording:
        """Exactly ``n`` rows (or ``seconds`` of feed, rounded to rows), or
        :class:`ReadTimeout`. Returns a :class:`Recording`: the rows with the
        conversion inputs frozen in, ready for
        ``to_csv``/``to_dataframe``/``convert``.

        The ``Calibration`` is the one passed, or the device's own, parsed
        (one KVS dump). A Recording is a self-describing artifact, so it
        always carries a usable calibration — pass one to skip the dump.
        """
        if (n is None) == (seconds is None):
            raise ValueError("exactly one of n or seconds")
        config = self._require_adc()
        if seconds is not None:
            n = max(1, round(seconds * config.sample_rate))
        if n < 1:
            raise ValueError("n must be >= 1")
        if calibration is None:
            calibration = await self.read_calibration()
        calibration.check_units(units)
        return await self._read_raw(
            n,
            units=units,
            calibration=calibration,
            tare_raw=None if tare_raw is None else np.array(tare_raw),
            timeout=timeout,
        )

    async def _wait_packet(self, queue, disc, timeout):
        """The next queued packet, or raise ConnectionLost / asyncio.TimeoutError."""
        get = asyncio.ensure_future(queue.get())
        try:
            done, _ = await asyncio.wait(
                {get, disc}, timeout=timeout, return_when=asyncio.FIRST_COMPLETED
            )
        finally:
            if not get.done():
                get.cancel()
        if get in done:
            return get.result()
        if disc in done:
            raise ConnectionLost("device disconnected mid-stream")
        raise asyncio.TimeoutError


def _packet_parts(packet: Packet):
    """A packet's timeline as arrays: its gap rows (NaN) then its samples."""
    if packet.rows_dropped:
        yield np.full((packet.rows_dropped, packet.raw.shape[1]), np.nan)
    yield packet.raw
