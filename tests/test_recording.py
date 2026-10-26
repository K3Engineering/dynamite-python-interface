"""Recording and read_csv round-trip tests."""

import datetime
import math

import numpy as np
import pytest

import dynamite_sampler as dms
from dynamite_sampler.calibration import Calibration
from dynamite_sampler.csv_io import read_csv
from dynamite_sampler.recording import Recording

from .test_csv import _CELLED_USER, _NOMINAL_FACTORY

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


def test_read_csv_counts_dropped_rows(tmp_path):
    recording = _recording()  # _RAW's middle row is a dropped (all-NaN) row
    recording.to_csv(tmp_path / "run.csv")
    assert read_csv(tmp_path / "run.csv").rows_dropped == 1


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
