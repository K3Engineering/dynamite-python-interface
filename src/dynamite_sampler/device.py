"""The Dynamite Sampler device classes."""

import asyncio
import concurrent.futures
import contextlib
import dataclasses
import datetime
import threading
import time

import bleak
import numpy as np

from .assemble import BlockAssembler, _blocks_from_packets
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
DEFAULT_BLOCKSIZE = 100
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
    """Async device: connect, stream, read, tare, and KVS access."""

    def __init__(self, client, info, adc_config, kvs, disconnected=None):
        self._client = client
        self.info = info
        self._adc_config = adc_config
        self.kvs = kvs
        self._pga_gains = None if adc_config is None else list(adc_config.gains)
        self.tare_raw = None
        self._active = False
        self._disconnected = (
            disconnected if disconnected is not None else asyncio.Event()
        )
        # The initial parse raises CalibrationError (present and wrong
        # fails connect); a later rebuild failure raises out of the write.
        self.calibration = Calibration.from_kvs(kvs.snapshot, self._pga_gains)
        kvs.set_on_change(self._rebuild_calibration)

    @classmethod
    async def connect(cls, address=None):
        """Connect to the one device in range (or the one at ``address``)."""
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

    def _rebuild_calibration(self, snapshot):
        """Rebuild after a KVS write. A write that leaves the cal data wrong
        raises here — out of the write that caused it; ``calibration``
        keeps its last valid value."""
        self.calibration = Calibration.from_kvs(snapshot, self._pga_gains)

    def assembler(self, blocksize: int = DEFAULT_BLOCKSIZE) -> BlockAssembler:
        """A :class:`BlockAssembler` for this device's sample rate."""
        config = self._require_adc()
        return BlockAssembler(config.sample_rate, blocksize)

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

    async def stream_packets(
        self, units: Unit = "raw", inactivity_timeout: float | None = None
    ):
        """Infinite async generator of :class:`Packet`: one item per BLE
        notification.

        The layer beneath :meth:`stream`: consume it directly for
        per-packet latency (closed-loop control), link metrics, or both,
        folding it into blocks with your own :class:`BlockAssembler` (or
        :meth:`assembler`) when blocks are needed too. ``raw`` is always
        counts; ``data``/``units`` are the converted view — the packet
        layer is not raw-only, so a control loop can run in force. The
        calibration is frozen at the stream's start; ``tare_raw`` is read
        per packet. ``inactivity_timeout`` raises :class:`ReadTimeout`
        after that many seconds without a packet.
        """
        config = self._require_adc()
        calibration = self.calibration
        calibration.check_units(units)
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
                samples = _decode_samples(payload, num_channels)
                unwrapped, missed = unwrapper.unwrap(ssn, samples.shape[0])
                yield Packet(
                    time=time.monotonic(),
                    ssn=unwrapped,
                    rows_dropped=missed,
                    payload_bytes=len(data),
                    raw=samples,
                    data=calibration.convert(samples, units, self.tare_raw),
                    units=units,
                )
        finally:
            disc.cancel()
            if self._client.is_connected:
                await self._client.stop_notify(DynamiteSamplerService.ADCFeed.UUID)
            self._active = False

    async def _stream_blocks(self, blocksize, units, inactivity_timeout=None):
        """Blocks out of the packet feed, converted per block.

        The calibration is frozen at the stream's start; ``tare_raw`` is
        read per block, so a mid-stream re-tare takes effect on the next
        block."""
        config = self._require_adc()
        calibration = self.calibration
        calibration.check_units(units)
        assembler = BlockAssembler(config.sample_rate, blocksize)
        # aclosing: without an explicit aclose the packet generator's
        # teardown (stop notify, _active = False) waits for asyncgen
        # finalization, and the next read() races it into StreamActive.
        async with contextlib.aclosing(
            self.stream_packets(inactivity_timeout=inactivity_timeout)
        ) as packets:
            async for block in _blocks_from_packets(packets, assembler):
                # convert() copies (even for "raw"): a user mutating .data
                # must never corrupt .raw.
                yield dataclasses.replace(
                    block,
                    data=calibration.convert(block.raw, units, self.tare_raw),
                    units=units,
                )

    async def stream(self, blocksize: int = DEFAULT_BLOCKSIZE, units: str = "raw"):
        """Infinite async generator of :class:`Block`.

        Sugar over :meth:`stream_packets` + :class:`BlockAssembler`;
        compose those directly to consume both layers.
        """
        # Same aclosing need as _stream_blocks: closing this generator must
        # release the feed deterministically, not at asyncgen finalization.
        async with contextlib.aclosing(self._stream_blocks(blocksize, units)) as blocks:
            async for block in blocks:
                yield block

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

    async def read(
        self,
        n: int | None = None,
        *,
        seconds: float | None = None,
        units: Unit = "raw",
        timeout: float = DEFAULT_READ_TIMEOUT_S,
    ) -> Recording:
        """Exactly ``n`` rows (or ``seconds`` of feed, rounded to rows),
        or :class:`ReadTimeout`. Returns a :class:`Recording`: the rows
        with the calibration, tare, and device snapshot frozen in, ready
        for ``to_csv``/``to_dataframe``/``convert``.
        """
        if (n is None) == (seconds is None):
            raise ValueError("exactly one of n or seconds")
        config = self._require_adc()
        calibration = self.calibration
        if seconds is not None:
            n = max(1, round(seconds * config.sample_rate))
        if n < 1:
            raise ValueError("n must be >= 1")
        tare = None if self.tare_raw is None else np.array(self.tare_raw)
        agen = self._stream_blocks(n, units, inactivity_timeout=timeout)
        try:
            block = await agen.__anext__()
            return Recording.from_block(
                block,
                sample_rate=config.sample_rate,
                calibration=calibration,
                tare_raw=tare,
                recorded_at=datetime.datetime.now().astimezone(),
                device=device_metadata(
                    self.info, config.gains, calibration, self.kvs.snapshot
                ),
                generator=_GENERATOR,
            )
        finally:
            await agen.aclose()

    def recording(
        self,
        units: Unit = "raw",
        path=None,
        blocksize: int = DEFAULT_BLOCKSIZE,
    ) -> "AsyncCapture":
        """An open-ended background capture, as an async context manager.

        Blocks are consumed on a task of this device's loop (the caller's
        code — driving a rig, sleeping, plotting — runs meanwhile). With
        ``path`` they stream through a :class:`CsvRecorder` to disk and
        never accumulate in memory (hours-long runs; a crash still leaves
        a valid, flushed file), and the :class:`Recording` is loaded back
        from the file on exit. Without ``path`` they accumulate in memory
        — the bounded-capture form. On exit (or ``stop()``), the partial
        data is finalized into :attr:`AsyncCapture.recording`, including
        on an exception in the body: a Ctrl+C ends the capture, it does
        not lose it. An acquisition failure mid-capture
        (:class:`ConnectionLost`, :class:`BufferOverrun`) finalizes what
        arrived and re-raises on exit. The feed is held for the capture's
        duration: ``read``/``tare``/another stream raise ``StreamActive``,
        KVS raises ``KvsBusy``.
        """
        self._require_adc()
        self.calibration.check_units(units)
        return AsyncCapture(self, units, path, blocksize)

    async def tare(self, n=None):
        """Average ``n`` raw samples per channel into ``tare_raw`` (default
        1 s of feed). Raises if any channel got no valid samples."""
        config = self._require_adc()
        if n is None:
            n = config.sample_rate
        block = await self.read(n, units="raw")
        valid = np.count_nonzero(~np.isnan(block.raw), axis=0)
        if np.any(valid == 0):
            empty = [int(i) for i in np.nonzero(valid == 0)[0]]
            raise TareError(f"tare failed: no valid samples on channel(s) {empty}")
        self.tare_raw = np.nanmean(block.raw, axis=0)
        return self.tare_raw


class AsyncCapture:
    """The async context manager returned by
    :meth:`AsyncDynamiteSampler.recording`.

    Yielded on entry; while the capture runs, :attr:`rows` counts samples
    arrived. After exit (or :meth:`stop`), :attr:`recording` holds the
    finalized :class:`Recording` (``None`` only if nothing arrived: no
    block, no ``ssn_origin``). A path-backed recording is loaded back from
    its own file — the same shape ``read_csv`` returns: ``host_time`` is
    NaN and ``data`` is the file's fixed-point conversion.
    """

    def __init__(self, dev, units, path, blocksize):
        self._dev = dev
        self._units = units
        self._path = path
        self._blocksize = blocksize
        self._blocks = []
        self._rows = 0
        self._recorder = None
        self._task = None
        self._frozen = None
        self._sample_rate = None
        self._finalized = False
        self.recording: Recording | None = None

    @property
    def rows(self) -> int:
        """Samples captured so far."""
        return self._rows

    async def __aenter__(self) -> "AsyncCapture":
        dev = self._dev
        config = dev._require_adc()
        self._sample_rate = config.sample_rate
        if self._path is not None:
            # File-first: the recorder writes every block through; the
            # conversion inputs freeze inside it (constructor re-checks
            # units against its own fresh parse).
            self._recorder = CsvRecorder(dev, self._path, units=self._units)
        else:
            calibration = dev.calibration
            self._frozen = (
                calibration,
                None if dev.tare_raw is None else np.array(dev.tare_raw),
                datetime.datetime.now().astimezone(),
                device_metadata(dev.info, config.gains, calibration, dev.kvs.snapshot),
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
        assembler = BlockAssembler(self._sample_rate, self._blocksize)
        async for block in _blocks_from_packets(self._dev.stream_packets(), assembler):
            if self._recorder is not None:
                # The recorder converts the raw block itself, through its
                # own frozen calibration; nothing is kept in memory.
                self._recorder.write(block)
            else:
                calibration, tare, _, _ = self._frozen
                self._blocks.append(
                    dataclasses.replace(
                        block,
                        data=calibration.convert(block.raw, self._units, tare),
                        units=self._units,
                    )
                )
            self._rows += len(block)

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
        elif self._blocks:
            self.recording = self._join()
        # A body exception (Ctrl+C included) wins; an acquisition failure is
        # raised here only when the body itself was clean.
        if body_exc is None and task_error is not None:
            raise task_error

    def _join(self) -> Recording:
        calibration, tare, recorded_at, device_meta = self._frozen
        return Recording(
            data=np.concatenate([block.data for block in self._blocks]),
            raw=np.concatenate([block.raw for block in self._blocks]),
            t=np.concatenate([block.t for block in self._blocks]),
            ssn0=self._blocks[0].ssn0,
            units=self._units,
            host_time=self._blocks[0].host_time,
            rows_dropped=sum(block.rows_dropped for block in self._blocks),
            sample_rate=self._sample_rate,
            calibration=calibration,
            tare_raw=tare,
            recorded_at=recorded_at,
            device=device_meta,
            generator=_GENERATOR,
        )


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

    def delete(self, key: str) -> None:
        return self._dev._run(self._ns.delete(key))

    def keys(self) -> list[str]:
        return self._dev._run(self._ns.keys())


class _SyncKvs:
    def __init__(self, dev):
        self._dev = dev
        self.factory = _SyncNamespace(dev, dev._async.kvs.factory)
        self.user = _SyncNamespace(dev, dev._async.kvs.user)
        self.settings = _SyncNamespace(dev, dev._async.kvs.settings)

    @property
    def snapshot(self):
        return self._dev._async.kvs.snapshot

    def get_device_name(self) -> str | None:
        return self._dev._run(self._dev._async.kvs.get_device_name())


class DynamiteSampler:
    """Synchronous facade over :class:`AsyncDynamiteSampler`.

    Runs a private event loop in a daemon thread; one implementation, one
    place where the math lives."""

    def __init__(self, async_dev, loop, thread):
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

    def read_tx_power_dbm(self) -> int | None:
        return self._run(self._async.read_tx_power_dbm())

    def set_tx_power(self, dbm: int) -> int:
        return self._run(self._async.set_tx_power(dbm))

    @property
    def calibration(self) -> Calibration:
        return self._async.calibration

    @property
    def tare_raw(self):
        return self._async.tare_raw

    @tare_raw.setter
    def tare_raw(self, value):
        self._async.tare_raw = value

    def assembler(self, blocksize: int = DEFAULT_BLOCKSIZE) -> BlockAssembler:
        """A :class:`BlockAssembler` for this device's sample rate."""
        return self._async.assembler(blocksize)

    def read(
        self,
        n: int | None = None,
        *,
        seconds: float | None = None,
        units: Unit = "raw",
        timeout: float = DEFAULT_READ_TIMEOUT_S,
    ) -> Recording:
        """Exactly ``n`` rows (or ``seconds`` of feed), or :class:`ReadTimeout`;
        the result is a :class:`Recording`."""
        return self._run(
            self._async.read(n, seconds=seconds, units=units, timeout=timeout)
        )

    def tare(self, n=None):
        return self._run(self._async.tare(n))

    def recording(
        self,
        units: Unit = "raw",
        path=None,
        blocksize: int = DEFAULT_BLOCKSIZE,
    ) -> "Capture":
        """An open-ended background capture, as a context manager; see
        :meth:`AsyncDynamiteSampler.recording`. The pump runs on this
        facade's private loop, so the ``with`` body owns the main thread:
        drive the rig, wait, plot — a Ctrl+C ends the capture with the
        partial data intact on :attr:`Capture.recording`."""
        return Capture(self, self._async.recording(units, path, blocksize))

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

    def stream(self, blocksize: int = DEFAULT_BLOCKSIZE, units: Unit = "raw"):
        return self._iter_async(self._async.stream(blocksize, units))

    # No sync stream_packets: one cross-thread hop per BLE notification
    # defeats the point of the packet layer (latency, link metrics). Use
    # AsyncDynamiteSampler for packets.


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
