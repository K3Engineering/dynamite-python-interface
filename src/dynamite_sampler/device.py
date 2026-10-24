"""The Dynamite Sampler device classes."""

import asyncio
import concurrent.futures
import dataclasses
import datetime
import threading
import time

import bleak
import numpy as np

from .calibration import Calibration, Unit
from .csv_io import _GENERATOR, CsvRecorder, device_metadata, read_csv
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
from .gatt import DeviceInformation, DynamiteSamplerService, TxPower
from .kvs import Kvs
from .packet import Packet
from .recording import Recording

ADCConfig = DynamiteSamplerService.ADCConfig

UNCONFIGURED = "UNCONFIGURED"

QUEUE_SECONDS = 4
DEFAULT_READ_TIMEOUT_S = 5.0

# Poll granularity of the sync facade's wait, so Ctrl+C lands promptly on
# Windows (concurrent.futures ``Future.result()`` is not interruptible).
_RUN_POLL_S = 0.25

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
            info = DeviceInfo(
                found.address,
                found.name,
                board_model,
                firmware,
                manufacturer,
                model_number,
                serial,
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
        await self.kvs.close()
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
        return await _read_characteristic(self._client, DeviceInformation.TxPowerLevel)

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
        overrun = []
        disc = asyncio.ensure_future(self._disconnected.wait())

        def on_notify(_sender, data):
            try:
                queue.put_nowait(bytes(data))
            except asyncio.QueueFull:
                overrun.append(BufferOverrun("consumer slower than the feed"))

        await self._client.start_notify(DynamiteSamplerService.ADCFeed.UUID, on_notify)
        unwrapper = SsnUnwrapper()
        try:
            while True:
                if overrun:
                    raise overrun[0]
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
            if self._client.is_connected:
                await self._client.stop_notify(DynamiteSamplerService.ADCFeed.UUID)
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

    def recording(
        self,
        units: Unit = "raw",
        path=None,
        calibration: Calibration | None = None,
        tare_raw=None,
    ) -> "AsyncCapture":
        """An open-ended background capture, as an async context manager.

        With ``path`` packets stream through a :class:`CsvRecorder` to disk
        and never accumulate in memory (hours-long runs; a crash still
        leaves a valid, flushed file), and the :class:`Recording` is loaded
        back from the file on exit. Without a path they accumulate in memory
        — the bounded-capture form. On exit (or ``stop()``) the partial data
        is finalized into :attr:`AsyncCapture.recording`, including on an
        exception in the body: a Ctrl+C ends the capture, it does not lose
        it. An acquisition failure mid-capture (:class:`ConnectionLost`,
        :class:`BufferOverrun`) finalizes what arrived and re-raises on exit.
        """
        self._require_adc()
        if calibration is not None:
            calibration.check_units(units)
        return AsyncCapture(self, units, path, calibration, tare_raw)

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


class AsyncCapture:
    """The async context manager returned by
    :meth:`AsyncDynamiteSampler.recording`.

    Yielded on entry; while the capture runs, :attr:`rows` counts samples
    arrived. After exit (or :meth:`stop`), :attr:`recording` holds the
    finalized :class:`Recording` (``None`` only if nothing arrived). A
    path-backed recording is loaded back from its own file — the same shape
    ``read_csv`` returns: ``host_time`` is NaN and ``data`` is the file's
    fixed-point conversion.
    """

    def __init__(self, dev, units, path, calibration, tare_raw):
        self._dev = dev
        self._units = units
        self._path = path
        self._calibration = calibration
        self._tare_raw = None if tare_raw is None else np.array(tare_raw)
        self._raw = []
        self._rows = 0
        self._recorder = None
        self._task = None
        self._frozen = None
        self._finalized = False
        self.recording: Recording | None = None

    @property
    def rows(self) -> int:
        """Samples captured so far."""
        return self._rows

    async def __aenter__(self) -> "AsyncCapture":
        dev = self._dev
        config = dev._require_adc()
        calibration = self._calibration
        if calibration is None and self._units != "raw":
            calibration = await dev.read_calibration()
        if calibration is not None:
            calibration.check_units(self._units)
        else:
            calibration = Calibration.from_kvs({}, config.gains)
        self._frozen = (
            calibration,
            self._tare_raw,
            config.sample_rate,
            datetime.datetime.now().astimezone(),
            device_metadata(dev.info, config.gains, calibration),
        )
        if self._path is not None:
            self._recorder = CsvRecorder(
                dev,
                self._path,
                units=self._units,
                calibration=calibration,
                tare_raw=self._tare_raw,
            )
        self._task = asyncio.ensure_future(self._pump())
        return self

    async def __aexit__(self, exc_type, exc, tb):
        await self._finish(exc)
        return False

    async def stop(self) -> None:
        """End the capture early (finalizes like a normal exit; idempotent)."""
        if not self._finalized:
            await self._finish(None)

    async def _pump(self):
        async for packet in self._dev.stream():
            if self._recorder is not None:
                self._recorder.write(packet)
            else:
                self._raw.append(packet)
            self._rows += packet.rows

    async def _finish(self, body_exc):
        if self._finalized:
            return
        self._finalized = True
        task, self._task = self._task, None
        task_error = None
        if task is not None:
            try:
                if not task.done():
                    # Give the pump a few loop beats to settle a pending
                    # failure (link loss, recorder error) before cancelling,
                    # so an acquisition death surfaces instead of a cancel.
                    for _ in range(3):
                        await asyncio.sleep(0)
                        if task.done():
                            break
                if not task.done():
                    task.cancel()
                await task
            except asyncio.CancelledError:
                pass
            except Exception as err:  # the feed died mid-capture
                task_error = err
        if self._recorder is not None:
            recorder, self._recorder = self._recorder, None
            recorder.close()
            # The file is the single buffered representation; load the
            # Recording back from it. Nothing arrived -> no file -> None.
            if self._rows:
                self.recording = await asyncio.to_thread(read_csv, self._path)
        elif self._raw:
            self.recording = self._join()
        # A body exception (Ctrl+C included) wins; an acquisition failure is
        # raised here only when the body itself was clean.
        if body_exc is None and task_error is not None:
            raise task_error

    def _join(self) -> Recording:
        calibration, tare, sample_rate, recorded_at, device_meta = self._frozen
        raw = np.concatenate(
            [part for packet in self._raw for part in _packet_parts(packet)]
        )
        return Recording(
            data=calibration.convert(raw, self._units, tare),
            raw=raw,
            t=np.arange(raw.shape[0]) / sample_rate,
            ssn0=self._raw[0].ssn,
            units=self._units,
            host_time=self._raw[0].time,
            sample_rate=sample_rate,
            calibration=calibration,
            tare_raw=tare,
            recorded_at=recorded_at,
            device=device_meta,
            generator=_GENERATOR,
        )


def _packet_parts(packet: Packet):
    """A packet's timeline as arrays: its gap rows (NaN) then its samples."""
    if packet.rows_dropped:
        yield np.full((packet.rows_dropped, packet.raw.shape[1]), np.nan)
    yield packet.raw


def _block_on(coro, loop):
    """Run ``coro`` on ``loop`` (another thread) and block, interruptibly.

    ``Future.result()`` is a lock acquire and is not interruptible on
    Windows; polling lets Ctrl+C land, and cancelling the *task* (not just
    the cross-thread future) runs the coroutine's finally blocks."""
    holder = []

    async def runner():
        task = asyncio.ensure_future(coro)
        holder.append(task)
        return await task

    fut = asyncio.run_coroutine_threadsafe(runner(), loop)
    while True:
        try:
            return fut.result(timeout=_RUN_POLL_S)
        except concurrent.futures.TimeoutError:
            # The wait timed out iff the future is not done. If it is done,
            # the coroutine raised a TimeoutError subclass
            # (ReadTimeout/KvsTimeout): deliver that.
            if fut.done():
                raise
        except KeyboardInterrupt:
            loop.call_soon_threadsafe(
                lambda: holder[0].cancel() if holder else fut.cancel()
            )
            # Wait for the cancellation's finally blocks to run, so the
            # device is left idle, then re-raise.
            try:
                fut.result()
            except BaseException:
                pass
            raise


class _SyncNamespace:
    def __init__(self, dev, namespace):
        self._dev = dev
        self._ns = namespace

    def get(self, key: str) -> str:
        return self._dev._run(self._ns.get(key))

    def set(self, key: str, value: str) -> str:
        return self._dev._run(self._ns.set(key, value))

    def set_many(self, entries: dict[str, str]) -> None:
        return self._dev._run(self._ns.set_many(entries))

    def set_group(self, entries: dict[str, str], marker: str) -> None:
        return self._dev._run(self._ns.set_group(entries, marker))

    def delete(self, key: str) -> None:
        return self._dev._run(self._ns.delete(key))

    def keys(self) -> list[str]:
        return self._dev._run(self._ns.keys())

    def read_all(self) -> dict[str, str]:
        return self._dev._run(self._ns.read_all())


class _SyncKvs:
    def __init__(self, dev):
        self._dev = dev
        self.factory = _SyncNamespace(dev, dev._async.kvs.factory)
        self.user = _SyncNamespace(dev, dev._async.kvs.user)
        self.settings = _SyncNamespace(dev, dev._async.kvs.settings)

    def read_all(self) -> dict[str, dict[str, str]]:
        return self._dev._run(self._dev._async.kvs.read_all())

    def get_device_name(self) -> str | None:
        return self._dev._run(self._dev._async.kvs.get_device_name())


class DynamiteSampler:
    """Synchronous facade over :class:`AsyncDynamiteSampler`.

    Runs a private event loop in a daemon thread; one implementation, one
    place where the math lives."""

    def __init__(
        self,
        async_dev: AsyncDynamiteSampler,
        loop: asyncio.AbstractEventLoop,
        thread: threading.Thread,
    ):
        self._async = async_dev
        self._loop = loop
        self._thread = thread
        self.kvs = _SyncKvs(self)

    @classmethod
    def connect(cls, address=None):
        loop = asyncio.new_event_loop()
        thread = threading.Thread(target=loop.run_forever, daemon=True)
        thread.start()
        try:
            async_dev = _block_on(AsyncDynamiteSampler.connect(address), loop)
        except BaseException:
            loop.call_soon_threadsafe(loop.stop)
            thread.join()
            loop.close()
            raise
        return cls(async_dev, loop, thread)

    def _run(self, coro):
        if self._loop is None:
            raise DynamiteError("device is closed")
        return _block_on(coro, self._loop)

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        self.close()

    def close(self):
        if self._loop is None:
            return
        loop = self._loop
        try:
            self._run(self._async.close())
        finally:
            loop.call_soon_threadsafe(loop.stop)
            self._thread.join()
            loop.close()
            self._loop = None

    @property
    def info(self) -> DeviceInfo:
        return self._async.info

    @property
    def sample_rate(self) -> int | None:
        return self._async.sample_rate

    @property
    def gains(self) -> list[int] | None:
        return self._async.gains

    def read_calibration(self) -> Calibration:
        return self._run(self._async.read_calibration())

    def read_tx_power_dbm(self) -> int | None:
        return self._run(self._async.read_tx_power_dbm())

    def set_tx_power(self, dbm: int) -> int:
        return self._run(self._async.set_tx_power(dbm))

    def read(
        self,
        n: int | None = None,
        *,
        seconds: float | None = None,
        units: Unit = "raw",
        calibration: Calibration | None = None,
        tare_raw=None,
        timeout: float = DEFAULT_READ_TIMEOUT_S,
    ) -> Recording:
        """Exactly ``n`` rows (or ``seconds`` of feed), or :class:`ReadTimeout`;
        the result is a :class:`Recording`."""
        return self._run(
            self._async.read(
                n,
                seconds=seconds,
                units=units,
                calibration=calibration,
                tare_raw=tare_raw,
                timeout=timeout,
            )
        )

    def tare(self, n: int | None = None):
        return self._run(self._async.tare(n))

    def recording(
        self,
        units: Unit = "raw",
        path=None,
        calibration: Calibration | None = None,
        tare_raw=None,
    ) -> "Capture":
        """An open-ended background capture, as a context manager; see
        :meth:`AsyncDynamiteSampler.recording`. The pump runs on this
        facade's private loop, so the ``with`` body owns the main thread:
        drive the rig, wait, plot — a Ctrl+C ends the capture with the
        partial data intact on :attr:`Capture.recording`."""
        return Capture(self, self._async.recording(units, path, calibration, tare_raw))

    def _iter_async(self, agen):
        try:
            while True:
                try:
                    yield self._run(agen.__anext__())
                except StopAsyncIteration:
                    return
        finally:
            if self._loop is not None:
                self._run(agen.aclose())

    def stream(self, inactivity_timeout: float | None = None):
        """Infinite generator of :class:`Packet`, one per BLE notification.

        The sync form blocks the calling thread between packets. For a
        background capture use :meth:`recording`; for a GUI event loop use a
        thread or the async class."""
        return self._iter_async(self._async.stream(inactivity_timeout))


class Capture:
    """The context manager returned by :meth:`DynamiteSampler.recording`.

    Wraps the async capture on this facade's loop. ``rows`` counts samples
    live; ``recording`` is the finalized :class:`Recording` after exit or
    :meth:`stop` (``None`` when nothing arrived).
    """

    def __init__(self, dev, async_capture):
        self._dev = dev
        self._capture = async_capture

    @property
    def rows(self) -> int:
        return self._capture.rows

    @property
    def recording(self) -> Recording | None:
        return self._capture.recording

    def stop(self) -> None:
        """End the capture early (idempotent)."""
        self._dev._run(self._capture.stop())

    def __enter__(self) -> "Capture":
        self._dev._run(self._capture.__aenter__())
        return self

    def __exit__(self, *exc):
        self._dev._run(self._capture.__aexit__(*exc))
        return False
