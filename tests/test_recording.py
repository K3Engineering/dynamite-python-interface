"""Recording, read_csv, read(seconds=), and the recording() capture."""

import asyncio
import datetime
import math
import threading
import time

import numpy as np
import pytest

import dynamite_sampler as dms
from dynamite_sampler.calibration import Calibration
from dynamite_sampler.csv_io import read_csv
from dynamite_sampler.errors import ConnectionLost
from dynamite_sampler.recording import Recording

from .test_csv import _CELLED_USER, _NOMINAL_FACTORY
from .test_device import (
    FakeClient,
    make_device,
    make_nominal_device,
    packet,
    wait_notify,
)

# The file columns are fixed-point (csv_decimals: mV/V at 8, kgf at 6,
# N at 5 decimals); equality here is up to the worst unit's column
# quantum. Digit-exact regeneration is checked in test_csv.py.
_CSV_TOL = 1e-5

_TARE = np.array([-12340.5, 55.0, 7001.25, -220.0])
_RAW = np.array(
    [[1000, -2000, 300000, -400000], [np.nan] * 4, [-12350, 58, 7000, -219]]
)


def _nominal_calibration():
    return Calibration.from_kvs(
        {"F": dict(_NOMINAL_FACTORY), "U": dict(_CELLED_USER)}, [1, 1, 1, 1]
    )


def _recording(units="kgf", tare_raw=_TARE):
    calibration = _nominal_calibration()
    data = calibration.convert(_RAW, units, tare_raw)
    return Recording(
        data=data,
        raw=_RAW.copy(),
        t=np.arange(len(_RAW)) / 1000.0,
        ssn0=41230,
        units=units,
        host_time=math.nan,
        sample_rate=1000,
        calibration=calibration,
        tare_raw=None if tare_raw is None else np.array(tare_raw),
        recorded_at=datetime.datetime(2026, 7, 29, 10, 5, 32).astimezone(),
        device={
            "name": "test board",
            "id": "TEST01234567",
            "model": None,
            "hardware_rev": "v700P",
            "firmware": None,
            "manufacturer": None,
            "afe": {
                "adc_ref_v": 1.2,
                "front_end_gain": 101.0,
                "adc_gain": [1.0, 1.0, 1.0, 1.0],
                "excitation_v": 4.53,
            },
            "kvs": {"factory": {}, "user": {}},
        },
        generator="dynamite-sampler-py test",
    )


def test_to_csv_read_csv_round_trip(tmp_path):
    recording = _recording()
    path = tmp_path / "run.csv"
    recording.to_csv(path)
    loaded = read_csv(path)
    assert loaded.units == "kgf"
    assert loaded.ssn0 == 41230
    assert loaded.n_channels == 4
    assert np.allclose(loaded.raw, recording.raw, equal_nan=True)
    assert np.allclose(loaded.data, recording.data, equal_nan=True, atol=_CSV_TOL)
    assert np.allclose(loaded.t, recording.t)
    assert np.allclose(loaded.tare_raw, recording.tare_raw)
    assert loaded.sample_rate == 1000
    assert loaded.device == recording.device
    # Self-contained: the loaded calibration reconverts to the same rows.
    converted = loaded.calibration.convert(loaded.raw, "kgf", loaded.tare_raw)
    assert np.allclose(converted, loaded.data, equal_nan=True, atol=_CSV_TOL)


@pytest.mark.parametrize("units", ["raw", "mV/V", "mV", "kgf", "N", "kN", "lbf"])
def test_to_csv_at_every_unit_regenerates(tmp_path, units):
    recording = _recording(units=units, tare_raw=None)
    path = tmp_path / "run.csv"
    recording.to_csv(path)
    loaded = read_csv(path)
    assert np.allclose(loaded.raw, recording.raw, equal_nan=True)
    assert np.array_equal(np.isnan(loaded.data), np.isnan(loaded.raw))
    assert np.allclose(
        loaded.data,
        _nominal_calibration().convert(_RAW, units, None),
        equal_nan=True,
        atol=_CSV_TOL,
    )


def test_convert_retargets_units_and_keeps_raw():
    recording = _recording()
    newtons = recording.convert("N")
    assert newtons.units == "N"
    assert newtons.raw is recording.raw
    assert np.allclose(
        newtons.data,
        _nominal_calibration().convert(_RAW, "N", _TARE),
        equal_nan=True,
    )
    gross = recording.convert("mV/V", tare_raw=None)
    assert gross.tare_raw is None
    assert np.allclose(
        gross.data,
        _nominal_calibration().convert(_RAW, "mV/V", None),
        equal_nan=True,
    )


def test_partial_tare_round_trips_gross_slots(tmp_path):
    tare = np.array([-12340.5, np.nan, 7001.25, np.nan])
    recording = _recording(units="mV/V", tare_raw=tare)
    recording.to_csv(tmp_path / "run.csv")
    loaded = read_csv(tmp_path / "run.csv")
    assert loaded.tare_raw[0] == -12340.5
    assert math.isnan(loaded.tare_raw[1])
    converted = loaded.calibration.convert(loaded.raw, "mV/V", loaded.tare_raw)
    assert np.allclose(converted, loaded.data, equal_nan=True, atol=_CSV_TOL)


def test_metadata_less_file_reads_with_raw_only_calibration(tmp_path):
    path = tmp_path / "bare.csv"
    path.write_text(
        "# dynamite-csv 1\n"
        '# {"format":"dynamite-csv","version":1,"sample_rate_hz":1000,'
        '"ssn_origin":0,"converted_unit":"raw"}\n'
        "ssn,ch0,ch0_raw\n0,1,1.0\n1,3,3.0\n",
        encoding="utf-8",
    )
    loaded = read_csv(path)
    assert loaded.device is None
    assert loaded.tare_raw is None
    with pytest.raises(dms.UnitUnavailable):
        loaded.convert("mV/V")


def test_column_names_and_dataframe():
    recording = _recording(units="mV")
    assert recording.column_names() == ["ch0_mV", "ch1_mV", "ch2_mV", "ch3_mV"]
    assert recording.column_names("load_cell")[0] == "ch0"  # unnamed cell falls back
    pandas = pytest.importorskip("pandas")
    frame = recording.to_dataframe()
    assert isinstance(frame, pandas.DataFrame)
    assert list(frame.columns) == ["ch0_mV", "ch1_mV", "ch2_mV", "ch3_mV"]
    assert frame.index.name == "t_s"
    assert len(frame) == len(recording)


async def test_read_seconds_of_feed():
    client = FakeClient()
    device = make_device(client)
    pending = asyncio.ensure_future(device.read(seconds=0.01, units="raw"))
    await wait_notify(client)
    client.notify(None, packet(0, [[i] * 4 for i in range(6)]))
    client.notify(None, packet(6, [[i] * 4 for i in range(6, 10)]))
    recording = await pending
    assert isinstance(recording, Recording)
    assert len(recording) == 10
    assert recording.sample_rate == 1000
    assert recording.units == "raw"
    assert recording.device is not None
    assert recording.calibration is not None


async def test_read_requires_exactly_one_length():
    client = FakeClient()
    device = make_device(client)
    with pytest.raises(ValueError, match="exactly one"):
        await device.read()
    with pytest.raises(ValueError, match="exactly one"):
        await device.read(10, seconds=0.01)


async def test_stream_packets_convert_to_units():
    client = FakeClient()
    device = make_nominal_device(client)
    packets = device.stream_packets(units="mV/V")
    pending = asyncio.ensure_future(packets.__anext__())
    await wait_notify(client)
    client.notify(None, packet(0, [[1000, 0, 0, 0]]))
    pkt = await pending
    await packets.aclose()
    counts_per_mvv = (1 << 23) * 15.6 / (1.2 * 1000.0) * 4.5
    assert pkt.units == "mV/V"
    assert np.allclose(pkt.data[0, 0], 1000 / counts_per_mvv)
    assert pkt.raw[0, 0] == 1000


def test_hand_built_packet_defaults_to_raw_data():
    raw = np.array([[1.0, 2.0]])
    pkt = dms.Packet(time=0.0, ssn=0, rows_dropped=0, payload_bytes=0, raw=raw)
    assert pkt.data is raw
    assert pkt.units == "raw"


async def test_capture_finalizes_contiguous_recording(tmp_path):
    client = FakeClient()
    device = make_device(client)
    async with device.recording(
        units="raw", path=tmp_path / "cap.csv", blocksize=2
    ) as cap:
        await wait_notify(client)
        client.notify(None, packet(0, [[1, 2, 3, 4], [5, 6, 7, 8]]))
        client.notify(None, packet(2, [[9, 10, 11, 12]]))
        for _ in range(1000):
            if cap.rows >= 2:
                break
            await asyncio.sleep(0)
        assert cap.rows == 2
    recording = cap.recording
    assert recording is not None  # one full block; the last row is partial
    assert len(recording) == 2
    assert np.array_equal(recording.raw, [[1, 2, 3, 4], [5, 6, 7, 8]])
    assert not device._active
    from_file = read_csv(tmp_path / "cap.csv")
    assert np.array_equal(from_file.raw, recording.raw)
    assert from_file.ssn0 == recording.ssn0


async def test_capture_stop_is_idempotent():
    client = FakeClient()
    device = make_device(client)
    async with device.recording(blocksize=2) as cap:
        await wait_notify(client)
        client.notify(None, packet(0, [[1, 2, 3, 4], [5, 6, 7, 8]]))
        for _ in range(1000):
            if cap.rows:
                break
            await asyncio.sleep(0)
        await cap.stop()
        assert cap.recording is not None
    assert cap.recording is not None
    assert not device._active


async def test_capture_surfaces_link_loss_but_keeps_partial_data():
    client = FakeClient()
    disconnected = asyncio.Event()
    device = make_device(client, disconnected=disconnected)
    with pytest.raises(ConnectionLost):
        async with device.recording(blocksize=2) as cap:
            await wait_notify(client)
            client.notify(None, packet(0, [[1, 2, 3, 4], [5, 6, 7, 8]]))
            for _ in range(1000):
                if cap.rows:
                    break
                await asyncio.sleep(0)
            disconnected.set()
    assert cap.recording is not None
    assert len(cap.recording) == 2
    assert not device._active


async def test_capture_body_exception_keeps_data_and_frees_feed():
    client = FakeClient()
    device = make_device(client)
    with pytest.raises(RuntimeError, match="boom"):
        async with device.recording(blocksize=2) as cap:
            await wait_notify(client)
            client.notify(None, packet(0, [[1, 2, 3, 4], [5, 6, 7, 8]]))
            for _ in range(1000):
                if cap.rows:
                    break
                await asyncio.sleep(0)
            raise RuntimeError("boom")
    assert cap.recording is not None
    assert not device._active


async def test_capture_with_no_blocks_yields_no_recording():
    client = FakeClient()
    device = make_device(client)
    async with device.recording(blocksize=2) as cap:
        await wait_notify(client)
    assert cap.recording is None
    assert not device._active


class _StubKvs:
    """The facade constructor wants namespace handles; the capture tests
    don't touch them."""

    snapshot = {"F": {}, "U": {}}
    factory = user = settings = None

    def set_on_change(self, callback):
        pass


def test_sync_capture_runs_pump_on_the_facade_loop():
    from dynamite_sampler.device import AsyncDynamiteSampler, DynamiteSampler
    from dynamite_sampler.gatt import ADCConfigData

    client = FakeClient()
    loop = asyncio.new_event_loop()
    thread = threading.Thread(target=loop.run_forever, daemon=True)
    thread.start()
    try:
        adc = ADCConfigData(4, "HIGH_RESOLUTION", 1000, [1, 1, 1, 1])
        dev = DynamiteSampler(
            AsyncDynamiteSampler(client, None, adc, _StubKvs()), loop, thread
        )
        with dev.recording(units="raw", blocksize=2) as cap:
            # The user's thread is free here; post notifications onto the
            # facade loop the way bleak would.
            for _ in range(200):
                if client.notify is not None:
                    break
                time.sleep(0.001)
            loop.call_soon_threadsafe(
                client.notify, None, packet(0, [[1, 2, 3, 4], [5, 6, 7, 8]])
            )
            for _ in range(200):
                if cap.rows:
                    break
                time.sleep(0.001)
            assert cap.rows == 2
            time.sleep(0)  # the with-body owned the main thread throughout
        assert cap.recording is not None
        assert np.array_equal(cap.recording.raw, [[1, 2, 3, 4], [5, 6, 7, 8]])
        assert not dev._async._active
    finally:
        loop.call_soon_threadsafe(loop.stop)
        thread.join()
        loop.close()
