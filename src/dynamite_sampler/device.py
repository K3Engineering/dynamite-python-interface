"""The Dynamite Sampler device classes."""

import asyncio
import concurrent.futures
import dataclasses
import threading
import time

import bleak
import numpy as np

from .block import Block
from .calibration import Calibration
from .discovery import find_single
from .errors import (
    BufferOverrun,
    CalibrationError,
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
from .ssn import SsnUnwrapper

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
        self._calibration_error = None
        # The initial parse raises CalibrationError (present and wrong
        # fails connect). A later rebuild failure is deferred to stream/read.
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
            info = DeviceInfo(
                found.address, found.name, board_model, firmware, manufacturer
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
        still succeeds; the failure surfaces on the next stream/read/tare."""
        try:
            self.calibration = Calibration.from_kvs(snapshot, self._pga_gains)
            self._calibration_error = None
        except CalibrationError as exc:
            self.calibration = None
            self._calibration_error = exc

    def _require_calibration(self) -> Calibration:
        if self._calibration_error is not None:
            raise self._calibration_error
        return self.calibration

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

    async def stream(self, blocksize: int = DEFAULT_BLOCKSIZE, units: str = "raw"):
        """Infinite async generator of :class:`Block`."""
        async for block in self._stream(blocksize, units):
            yield block

    async def _stream(self, blocksize, units, inactivity_timeout=None):
        config = self._require_adc()
        calibration = self._require_calibration()
        calibration.check_units(units)
        if blocksize < 1:
            raise ValueError("blocksize must be >= 1")
        if self._active:
            raise StreamActive("a stream or read is already active")
        self._active = True

        rate = config.sample_rate
        num_channels = len(config.gains)
        queue = asyncio.Queue(maxsize=max(blocksize, rate * QUEUE_SECONDS))
        overrun = []
        disc = asyncio.ensure_future(self._disconnected.wait())

        def on_notify(_sender, data):
            try:
                queue.put_nowait(bytes(data))
            except asyncio.QueueFull:
                overrun.append(BufferOverrun("consumer slower than the feed"))

        await self._client.start_notify(DynamiteSamplerService.ADCFeed.UUID, on_notify)
        try:
            async for block in self._assemble(
                queue,
                overrun,
                disc,
                blocksize,
                units,
                rate,
                num_channels,
                calibration,
                inactivity_timeout,
            ):
                yield block
        finally:
            disc.cancel()
            if self._client.is_connected:
                await self._client.stop_notify(DynamiteSamplerService.ADCFeed.UUID)
            self._active = False

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

    async def _assemble(
        self,
        queue,
        overrun,
        disc,
        blocksize,
        units,
        rate,
        num_channels,
        calibration,
        inactivity_timeout,
    ):
        unwrapper = SsnUnwrapper()
        origin = None
        start_index = 0
        chunks = []
        chunk_times = []
        count = 0
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
            if origin is None:
                origin = unwrapped
            now = time.monotonic()
            if missed:
                chunks.append(np.full((missed, num_channels), np.nan))
                chunk_times.append(now)
                count += missed
            chunks.append(samples)
            chunk_times.append(now)
            count += samples.shape[0]

            while count >= blocksize:
                parts = []
                block_time = chunk_times[0]
                need = blocksize
                while need:
                    head = chunks[0]
                    if head.shape[0] <= need:
                        parts.append(head)
                        need -= head.shape[0]
                        chunks.pop(0)
                        chunk_times.pop(0)
                    else:
                        parts.append(head[:need])
                        chunks[0] = head[need:]
                        need = 0
                count -= blocksize
                block_raw = np.concatenate(parts)
                yield self._make_block(
                    block_raw, block_time, start_index, origin, rate, units, calibration
                )
                start_index += blocksize

    def _make_block(
        self, block_raw, host_time, start_index, origin, rate, units, calibration
    ) -> Block:
        data = calibration.convert(block_raw, units, self.tare_raw)
        t = np.arange(start_index, start_index + block_raw.shape[0]) / rate
        return Block(
            data=data,
            raw=block_raw,
            t=t,
            ssn0=int(origin + start_index),
            units=units,
            host_time=host_time,
        )

    async def read(
        self, n: int, units: str = "raw", timeout: float = DEFAULT_READ_TIMEOUT_S
    ) -> Block:
        """Exactly ``n`` rows, or :class:`ReadTimeout`."""
        if n < 1:
            raise ValueError("n must be >= 1")
        agen = self._stream(n, units, timeout)
        try:
            return await agen.__anext__()
        finally:
            await agen.aclose()

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

    def read(
        self, n: int, units: str = "raw", timeout: float = DEFAULT_READ_TIMEOUT_S
    ) -> Block:
        return self._run(self._async.read(n, units, timeout))

    def tare(self, n=None):
        return self._run(self._async.tare(n))

    def stream(self, blocksize: int = DEFAULT_BLOCKSIZE, units: str = "raw"):
        agen = self._async.stream(blocksize, units)
        try:
            while True:
                try:
                    yield self._run(agen.__anext__())
                except StopAsyncIteration:
                    return
        finally:
            if self._loop is not None:
                self._run(agen.aclose())
