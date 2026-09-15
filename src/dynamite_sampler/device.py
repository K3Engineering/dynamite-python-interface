"""The Dynamite Sampler device classes."""

import asyncio
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
    ConnectionLost,
    DynamiteError,
    ProtocolError,
    ProvisioningError,
    ReadTimeout,
)
from .gatt import DeviceInfo as DeviceInfoChar, DynamiteSamplerService
from .kvs import KVS_CHR_UUID, Kvs
from .ssn import SsnUnwrapper

ADCConfig = DynamiteSamplerService.ADCConfig

UNCONFIGURED = "UNCONFIGURED"

DISCONNECT_POLL_S = 1.0
QUEUE_SECONDS = 4
DEFAULT_BLOCKSIZE = 100
DEFAULT_READ_TIMEOUT_S = 5.0

_HEADER_BYTES = DynamiteSamplerService.ADCFeed._HEADER_BYTES
_SAMPLE_BYTES = DynamiteSamplerService.ADCFeed._SAMPLE_BYTES
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

    def __init__(self, client, info, adc_config, kvs):
        self._client = client
        self.info = info
        self._adc_config = adc_config
        self.kvs = kvs
        self._pga_gains = None if adc_config is None else list(adc_config.gains)
        self.tare_raw = None
        self._active = False
        self.calibration = Calibration.from_kvs(kvs.snapshot, self._pga_gains)
        kvs.set_on_change(self._rebuild_calibration)

    @classmethod
    async def connect(cls, address=None):
        """Connect to the one device in range (or the one at ``address``)."""
        found = await find_single(address)
        client = bleak.BleakClient(found.address)
        await client.connect()
        try:
            board_model = (
                await _read_characteristic(client, DeviceInfoChar.HardwareRevision)
                or UNCONFIGURED
            )
            firmware = await _read_characteristic(client, DeviceInfoChar.FirmwareRevision)
            manufacturer = await _read_characteristic(
                client, DeviceInfoChar.ManufacturerName
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
            return cls(client, info, adc_config, kvs)
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
        if self._client.is_connected:
            await self._client.stop_notify(KVS_CHR_UUID)
            await self._client.disconnect()

    def _rebuild_calibration(self, snapshot):
        self.calibration = Calibration.from_kvs(snapshot, self._pga_gains)

    def _require_adc(self):
        if self._adc_config is None:
            raise ProvisioningError(
                f"device is {UNCONFIGURED} (safe mode); the ADC is disabled"
            )
        return self._adc_config

    @property
    def sample_rate(self):
        return None if self._adc_config is None else self._adc_config.sample_rate

    @property
    def gains(self):
        return None if self._adc_config is None else list(self._adc_config.gains)

    async def stream(self, blocksize=DEFAULT_BLOCKSIZE, units="raw"):
        """Infinite async generator of :class:`Block`."""
        config = self._require_adc()
        self.calibration.check_units(units)
        if self._active:
            raise DynamiteError("a stream or read is already active")
        self._active = True

        rate = config.sample_rate
        num_channels = len(config.gains)
        queue = asyncio.Queue(maxsize=max(blocksize, rate * QUEUE_SECONDS))
        overrun = []

        def on_notify(_sender, data):
            try:
                queue.put_nowait(bytes(data))
            except asyncio.QueueFull:
                overrun.append(BufferOverrun("consumer slower than the feed"))

        await self._client.start_notify(
            DynamiteSamplerService.ADCFeed.UUID, on_notify
        )
        try:
            async for block in self._assemble(
                queue, overrun, blocksize, units, rate, num_channels
            ):
                yield block
        finally:
            if self._client.is_connected:
                await self._client.stop_notify(
                    DynamiteSamplerService.ADCFeed.UUID
                )
            self._active = False

    async def _assemble(self, queue, overrun, blocksize, units, rate, num_channels):
        unwrapper = SsnUnwrapper()
        origin = None
        start_index = 0
        chunks = []
        count = 0
        times = []
        while True:
            if overrun:
                raise overrun[0]
            try:
                data = await asyncio.wait_for(queue.get(), DISCONNECT_POLL_S)
            except asyncio.TimeoutError:
                if not self._client.is_connected:
                    raise ConnectionLost("device disconnected mid-stream") from None
                continue
            ssn, payload = DynamiteSamplerService.ADCFeed.split(data)
            samples = _decode_samples(payload, num_channels)
            unwrapped, missed = unwrapper.unwrap(ssn, samples.shape[0])
            if origin is None:
                origin = unwrapped
            now = time.monotonic()
            if missed:
                chunks.append(np.full((missed, num_channels), np.nan))
                times.extend([now] * missed)
                count += missed
            chunks.append(samples)
            times.extend([now] * samples.shape[0])
            count += samples.shape[0]

            while count >= blocksize:
                rows = np.concatenate(chunks)
                block_raw = rows[:blocksize]
                block_times = times[:blocksize]
                remaining = rows[blocksize:]
                chunks = [remaining] if remaining.shape[0] else []
                times = times[blocksize:]
                count = remaining.shape[0]
                yield self._make_block(
                    block_raw, block_times, start_index, origin, rate, units
                )
                start_index += blocksize

    def _make_block(self, block_raw, block_times, start_index, origin, rate, units):
        data = self.calibration.convert(block_raw, units, self.tare_raw)
        t = np.arange(start_index, start_index + block_raw.shape[0]) / rate
        return Block(
            data=data,
            raw=block_raw,
            t=t,
            ssn0=int(origin + start_index),
            units=units,
            host_time=block_times[0],
        )

    async def read(self, n, units="raw", timeout=DEFAULT_READ_TIMEOUT_S):
        """Exactly ``n`` rows, or :class:`ReadTimeout`."""
        self._require_adc()
        self.calibration.check_units(units)
        if n < 1:
            raise ValueError("n must be >= 1")
        if self._active:
            raise DynamiteError("a stream or read is already active")
        agen = self.stream(blocksize=1, units=units)
        rows = []
        first = None
        try:
            for _ in range(n):
                try:
                    block = await asyncio.wait_for(agen.__anext__(), timeout)
                except asyncio.TimeoutError:
                    raise ReadTimeout(
                        f"no rows for {timeout} s while reading {n}"
                    ) from None
                if first is None:
                    first = block
                rows.append(block.raw[0])
        finally:
            await agen.aclose()
        block_raw = np.stack(rows)
        rate = self._require_adc().sample_rate
        return self._make_block(
            block_raw, [first.host_time], 0, first.ssn0, rate, units
        )

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
            raise DynamiteError(f"tare failed: no valid samples on channel(s) {empty}")
        self.tare_raw = np.nanmean(block.raw, axis=0)
        return self.tare_raw


class _SyncNamespace:
    def __init__(self, dev, namespace):
        self._dev = dev
        self._ns = namespace

    def get(self, key):
        return self._dev._run(self._ns.get(key))

    def set(self, key, value):
        return self._dev._run(self._ns.set(key, value))

    def delete(self, key):
        return self._dev._run(self._ns.delete(key))

    def keys(self):
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
            async_dev = asyncio.run_coroutine_threadsafe(
                AsyncDynamiteSampler.connect(address), loop
            ).result()
        except BaseException:
            loop.call_soon_threadsafe(loop.stop)
            thread.join()
            loop.close()
            raise
        return cls(async_dev, loop, thread)

    def _run(self, coro):
        return asyncio.run_coroutine_threadsafe(coro, self._loop).result()

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        self.close()

    def close(self):
        try:
            self._run(self._async.close())
        finally:
            self._loop.call_soon_threadsafe(self._loop.stop)
            self._thread.join()
            self._loop.close()

    @property
    def info(self):
        return self._async.info

    @property
    def sample_rate(self):
        return self._async.sample_rate

    @property
    def gains(self):
        return self._async.gains

    @property
    def calibration(self):
        return self._async.calibration

    @property
    def tare_raw(self):
        return self._async.tare_raw

    @tare_raw.setter
    def tare_raw(self, value):
        self._async.tare_raw = value

    def read(self, n, units="raw", timeout=DEFAULT_READ_TIMEOUT_S):
        return self._run(self._async.read(n, units, timeout))

    def tare(self, n=None):
        return self._run(self._async.tare(n))

    def stream(self, blocksize=DEFAULT_BLOCKSIZE, units="raw"):
        agen = self._async.stream(blocksize, units)
        try:
            while True:
                try:
                    yield self._run(agen.__anext__())
                except StopAsyncIteration:
                    return
        finally:
            self._run(agen.aclose())
