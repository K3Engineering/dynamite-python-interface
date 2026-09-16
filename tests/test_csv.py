"""dynamite-csv read/write (csv_io), driven by the format doc's worked
example and the package's own recorder, no BLE required."""

import math

import numpy as np
import pytest
import yaml

import dynamite_sampler as dms
from dynamite_sampler.block import Block
from dynamite_sampler.calibration import Calibration
from dynamite_sampler.csv_io import MAGIC, CsvRecorder, _json_line, _yaml_lines
from dynamite_sampler.device import DeviceInfo
from dynamite_sampler.errors import (
    CsvFormatError,
    DynamiteError,
    UnitUnavailable,
)

# --- The worked example from docs/csv-format-v2.md, verbatim --------------

EXAMPLE_METADATA = {
    "format": "dynamite-csv",
    "version": 1,
    "generator": "dynamite-flutter 1.0.0",
    "recorded_at": "2026-07-29T10:05:32.184-04:00",
    "recorded_unix": 1785333932,
    "sample_rate_hz": 1000,
    "ssn_origin": 41230,
    "converted_unit": "kgf",
    "device": {
        "name": "DS A4CF1208F51E",
        "id": "A4CF1208F51E",
        "model": "Dynamite Sampler Pro Mk1",
        "hardware_rev": "v700P",
        "firmware": "v700P|v1.2.0-3-gdeadbee",
        "manufacturer": "K3 Engineering",
        "afe": {
            "adc_ref_v": 1.2,
            "front_end_gain": 101.0,
            "adc_gain": [1.0, 1.0, 1.0, 1.0],
            "excitation_v": 4.53,
        },
        "kvs": {
            "factory": {
                "adc_fsr": "1.2,nominal",
                "afe_gain": "101,nominal",
                "cal.adc": "1,1,1,1",
                "cal.board": "CB42 v1.0.3",
                "cal.date": "2026-06-14",
                "cal.origin": "factory",
                "cal.r.prov": "nominal",
                "cal.temp": "-999,24.1",
                "cal.tool": "calibrate v3.1",
                "ch0.r": "10001.2,9.98,10.01,10.02,9.99,9998.7",
                "ch0.raw": "6383553.0,3192096.0,120.0,-3191776.0,-6383313.0",
                "exc": "4.53,nominal",
            },
            "user": {
                "lc0.cap": "100",
                "lc0.name": "John Smith's 100 kg",
                "lc0.sens": "2.007",
            },
        },
    },
    "channels": [
        {
            "load_cell": {
                "name": "John Smith's 100 kg",
                "capacity_kg": 100.0,
                "sensitivity_mv_v": 2.007,
            },
            "tare_raw": -12340.5,
            "board_cal": {
                "r": [10001.2, 9.98, 10.01, 10.02, 9.99, 9998.7],
                "raw": [6383553.0, 3192096.0, 120.0, -3191776.0, -6383313.0],
                "n": {"fsr": 1.2, "afe": 101.0, "pga": 1.0, "exc": 4.53},
            },
        },
        {
            "load_cell": {
                "name": "Beam 20 kg",
                "capacity_kg": 20.0,
                "sensitivity_mv_v": 2.0,
            },
            "tare_raw": 55.0,
            "board_cal": None,
        },
        {"load_cell": None, "tare_raw": 7001.2, "board_cal": None},
        {"load_cell": None, "tare_raw": -220.0, "board_cal": None},
    ],
}

_EXAMPLE_JSON = (
    '{"format":"dynamite-csv","version":1,"generator":"dynamite-flutter 1.0.0"'
    ',"recorded_at":"2026-07-29T10:05:32.184-04:00","recorded_unix":1785333932'
    ',"sample_rate_hz":1000,"ssn_origin":41230,"converted_unit":"kgf","device"'
    ':{"name":"DS A4CF1208F51E","id":"A4CF1208F51E","model":"Dynamite Sampler'
    ' Pro Mk1","hardware_rev":"v700P","firmware":"v700P|v1.2.0-3-gdeadbee"'
    ',"manufacturer":"K3 Engineering","afe":{"adc_ref_v":1.2,"front_end_gain"'
    ':101.0,"adc_gain":[1.0,1.0,1.0,1.0],"excitation_v":4.53},"kvs":{"factory"'
    ':{"adc_fsr":"1.2,nominal","afe_gain":"101,nominal","cal.adc":"1,1,1,1"'
    ',"cal.board":"CB42 v1.0.3","cal.date":"2026-06-14","cal.origin":"factory"'
    ',"cal.r.prov":"nominal","cal.temp":"-999,24.1","cal.tool":"calibrate v3.1"'
    ',"ch0.r":"10001.2,9.98,10.01,10.02,9.99,9998.7","ch0.raw":"6383553.0,'
    '3192096.0,120.0,-3191776.0,-6383313.0","exc":"4.53,nominal"},"user":{'
    '"lc0.cap":"100","lc0.name":"John Smith\'s 100 kg","lc0.sens":"2.007"}}}'
    ',"channels":[{"load_cell":{"name":"John Smith\'s 100 kg","capacity_kg"'
    ':100.0,"sensitivity_mv_v":2.007},"tare_raw":-12340.5,"board_cal":{"r":['
    "10001.2,9.98,10.01,10.02,9.99,9998.7],"
    '"raw":[6383553.0,3192096.0,120.0,-3191776.0,-6383313.0],"n":{"fsr":1.2,'
    '"afe":101.0,"pga":1.0,"exc":4.53}}},{"load_cell":{"name":"Beam 20 kg",'
    '"capacity_kg":20.0,"sensitivity_mv_v":2.0},"tare_raw":55.0,"board_cal"'
    ':null},{"load_cell":null,"tare_raw":7001.2,"board_cal":null},{'
    '"load_cell":null,"tare_raw":-220.0,"board_cal":null}]}'
)

_EXAMPLE_YAML = """\
# format: 'dynamite-csv'
# version: 1
# generator: 'dynamite-flutter 1.0.0'
# recorded_at: '2026-07-29T10:05:32.184-04:00'
# recorded_unix: 1785333932
# sample_rate_hz: 1000
# ssn_origin: 41230
# converted_unit: 'kgf'
# device:
#   name: 'DS A4CF1208F51E'
#   id: 'A4CF1208F51E'
#   model: 'Dynamite Sampler Pro Mk1'
#   hardware_rev: 'v700P'
#   firmware: 'v700P|v1.2.0-3-gdeadbee'
#   manufacturer: 'K3 Engineering'
#   afe:
#     adc_ref_v: 1.2
#     front_end_gain: 101.0
#     adc_gain: [1.0, 1.0, 1.0, 1.0]
#     excitation_v: 4.53
#   kvs:
#     factory:
#       adc_fsr: '1.2,nominal'
#       afe_gain: '101,nominal'
#       cal.adc: '1,1,1,1'
#       cal.board: 'CB42 v1.0.3'
#       cal.date: '2026-06-14'
#       cal.origin: 'factory'
#       cal.r.prov: 'nominal'
#       cal.temp: '-999,24.1'
#       cal.tool: 'calibrate v3.1'
#       ch0.r: '10001.2,9.98,10.01,10.02,9.99,9998.7'
#       ch0.raw: '6383553.0,3192096.0,120.0,-3191776.0,-6383313.0'
#       exc: '4.53,nominal'
#     user:
#       lc0.cap: '100'
#       lc0.name: 'John Smith''s 100 kg'
#       lc0.sens: '2.007'
# channels:
#   - load_cell:
#       name: 'John Smith''s 100 kg'
#       capacity_kg: 100.0
#       sensitivity_mv_v: 2.007
#     tare_raw: -12340.5
#     board_cal:
#       r: [10001.2, 9.98, 10.01, 10.02, 9.99, 9998.7]
#       raw: [6383553.0, 3192096.0, 120.0, -3191776.0, -6383313.0]
#       n:
#         fsr: 1.2
#         afe: 101.0
#         pga: 1.0
#         exc: 4.53
#   - load_cell:
#       name: 'Beam 20 kg'
#       capacity_kg: 20.0
#       sensitivity_mv_v: 2.0
#     tare_raw: 55.0
#     board_cal: null
#   - load_cell: null
#     tare_raw: 7001.2
#     board_cal: null
#   - load_cell: null
#     tare_raw: -220.0
#     board_cal: null"""

_EXAMPLE_BODY = """\
ssn,ch0,ch1,ch2,ch3,ch0_kgf,ch1_kgf,ch2_kgf,ch3_kgf
41230,-12339,55,7001,-220,0.000123,0.000000,,
41231,-12338,55,7002,-221,0.000124,0.000001,,
41232,,,,,,,,
41233,,,,,,,,
41234,-12350,58,7000,-219,0.000081,0.000016,,"""


def test_worked_example_json_renders_byte_identically():
    """The writer's encoder re-renders the doc's worked example as the
    doc's machine line, verbatim."""
    assert _json_line(EXAMPLE_METADATA) == _EXAMPLE_JSON


def test_metadata_yaml_block_round_trips():
    """The YAML block is derived documentation (implementation-defined):
    reloading it yields the same metadata object."""
    block = "\n".join("# " + line for line in _yaml_lines(EXAMPLE_METADATA))
    reloaded = yaml.safe_load("\n".join(line[2:] for line in block.splitlines()))
    assert reloaded == EXAMPLE_METADATA


def test_worked_example_file_reads(tmp_path):
    """The doc's complete example file parses; converted columns come back
    verbatim, blank cells (row and column patterns) as NaN."""
    path = tmp_path / "session.csv"
    path.write_text(
        "\n".join(
            [MAGIC, "# " + _EXAMPLE_JSON, *_EXAMPLE_YAML.splitlines(), _EXAMPLE_BODY]
        )
        + "\n",
        encoding="utf-8",
    )
    block = dms.read_csv(path)
    assert block.units == "kgf"
    assert block.ssn0 == 41230
    assert block.raw.shape == (5, 4)
    assert np.isnan(block.raw[2:4]).all()  # dropped samples kept
    assert np.allclose(block.raw[0], [-12339, 55, 7001, -220])
    assert np.allclose(block.data[0, :2], [0.000123, 0.0])
    assert np.isnan(block.data[:, 2:]).all()  # cell-less channels
    assert np.isnan(block.data[2:4]).all()
    assert np.allclose(block.t, [0.0, 0.001, 0.002, 0.003, 0.004])
    assert math.isnan(block.host_time)


# --- Calibration helpers ----------------------------------------------------

_NOMINAL_FACTORY = {
    "adc_fsr": "1.2,nominal",
    "afe_gain": "101,nominal",
    "exc": "4.53,nominal",
}


def _nominal_calibration(pga=(1, 1, 1, 1), with_cells=True):
    user = {}
    if with_cells:
        for i in range(len(pga)):
            user[f"lc{i}.cap"] = "100"
            user[f"lc{i}.sens"] = "2.0"
    return Calibration.from_kvs({"F": dict(_NOMINAL_FACTORY), "U": user}, list(pga))


def test_csv_decimals_match_doc_precision_table():
    """The doc's worked precision table (100 kg / 2 mV/V cell, nominal
    chain) reproduced by csv_decimals."""
    cal = _nominal_calibration()
    assert cal.csv_decimals("kgf") == [6, 6, 6, 6]
    assert cal.csv_decimals("N") == [5, 5, 5, 5]
    assert cal.csv_decimals("lbf") == [6, 6, 6, 6]
    assert cal.csv_decimals("kN") == [8, 8, 8, 8]
    assert cal.csv_decimals("mV/V") == [8, 8, 8, 8]
    assert cal.csv_decimals("mV") == [7, 7, 7, 7]
    assert cal.csv_decimals("raw") == [1, 1, 1, 1]


def test_worked_example_calibration_parses_and_maps():
    """From_kvs on the example's embedded KVS (trimmed to one channel, the
    example only carries ch0 factory data) reproduces its own ladder map:
    each cal reading maps to the setpoint derived from the resistors."""
    factory = dict(EXAMPLE_METADATA["device"]["kvs"]["factory"])
    factory["cal.adc"] = "1"  # the example's 4-channel count, trimmed like ch0
    cal = Calibration.from_kvs({"F": factory, "U": {}}, [1.0])
    board = cal.board[0]
    assert board.is_calibrated
    readings = [6383553.0, 3192096.0, 120.0, -3191776.0, -6383313.0]
    for reading, setpoint in zip(readings, board.setpoints):
        assert board.mvv(reading) == setpoint
    assert board.mvv(readings[2]) == 0.0
    # The chord quantum used for file precision: doc-consistent 6 decimals.
    chord = board.counts_per_mvv_chord()
    quantum = abs((100.0 / 2.007) / chord)
    assert math.ceil(1 - math.log10(quantum) - 1e-9) == 6


# --- Recorder / reader round trip -------------------------------------------


class _FakeKvs:
    def __init__(self, snapshot):
        self.snapshot = snapshot


class _FakeDev:
    """The recorder's device surface, no BLE."""

    def __init__(self, gains, snapshot, tare_raw=None):
        self.info = DeviceInfo(
            "AA:BB:CC:DD:EE:FF",
            "DS AABBCCDDEEFF",
            "v700P",
            "v700P|v1.0.0",
            "K3 Engineering",
            "Dynamite Sampler Pro Mk1",
            "AABBCCDDEEFF",
        )
        self.sample_rate = 1000
        self.gains = gains
        self.kvs = _FakeKvs(snapshot)
        self.tare_raw = tare_raw


def _block(rows, ssn0):
    raw = np.asarray(rows, dtype=np.float64)
    return Block(
        data=raw.copy(),
        raw=raw,
        t=np.arange(raw.shape[0]) / 1000.0,
        ssn0=ssn0,
        units="raw",
        host_time=0.0,
    )


def test_recorder_round_trip(tmp_path):
    snapshot = {"F": dict(_NOMINAL_FACTORY), "U": {}}
    dev = _FakeDev([1, 1, 1, 1], snapshot)
    cal = Calibration.from_kvs(snapshot, [1, 1, 1, 1])
    path = tmp_path / "rec.csv"
    recorder = CsvRecorder(dev, path, units="mV/V")
    rows_a = [[1000, -2000, 300000, -400000], [np.nan] * 4, [5, 6, 7, 8]]
    rows_b = [[9, 10, 11, 12]]
    recorder.write_block(_block(rows_a, 100))
    recorder.write_block(_block(rows_b, 103))
    recorder.close()

    block = dms.read_csv(path)
    expected_raw = np.array(rows_a + rows_b, dtype=np.float64)
    assert block.units == "mV/V"
    assert block.ssn0 == 100
    assert np.allclose(block.raw, expected_raw, equal_nan=True)
    expected_data = cal.convert(expected_raw, "mV/V")
    assert np.allclose(block.data, expected_data, equal_nan=True)
    assert np.isnan(block.raw[1]).all() and np.isnan(block.data[1]).all()
    assert np.allclose(block.t, [0.0, 0.001, 0.002, 0.003])
    text = path.read_text(encoding="utf-8")
    assert text.startswith(MAGIC + "\n# {")
    assert "101,,,,,,,," in text  # the gap row keeps its ssn, blank cells


def test_recorder_rejects_noncontiguous_block(tmp_path):
    dev = _FakeDev([1, 1, 1, 1], {"F": dict(_NOMINAL_FACTORY), "U": {}})
    recorder = CsvRecorder(dev, tmp_path / "rec.csv")
    recorder.write_block(_block([[1, 2, 3, 4]], 50))
    with pytest.raises(CsvFormatError, match="non-contiguous"):
        recorder.write_block(_block([[1, 2, 3, 4]], 60))
    recorder.close()


def test_recorder_force_units_need_load_cells(tmp_path):
    dev = _FakeDev([1, 1, 1, 1], {"F": dict(_NOMINAL_FACTORY), "U": {}})
    with pytest.raises(UnitUnavailable):
        CsvRecorder(dev, tmp_path / "rec.csv", units="kgf")


def test_recorder_without_blocks_writes_no_file(tmp_path):
    dev = _FakeDev([1, 1, 1, 1], {"F": dict(_NOMINAL_FACTORY), "U": {}})
    path = tmp_path / "rec.csv"
    CsvRecorder(dev, path).close()
    assert not path.exists()


def test_two_channel_round_trip(tmp_path):
    dev = _FakeDev([1, 1], {"F": dict(_NOMINAL_FACTORY), "U": {}})
    path = tmp_path / "rec.csv"
    with CsvRecorder(dev, path) as recorder:
        recorder.write_block(_block([[1, 2], [3, 4]], 7))
    block = dms.read_csv(path)
    assert block.raw.shape == (2, 2)
    assert block.data.shape == (2, 2)
    assert block.ssn0 == 7


# --- Reader rejects ----------------------------------------------------------


def _write(tmp_path, text):
    path = tmp_path / "bad.csv"
    path.write_text(text, encoding="utf-8")
    return path


_META = (
    '# {"format":"dynamite-csv","version":1,"sample_rate_hz":1000,'
    '"ssn_origin":0,"converted_unit":"raw"}'
)


def test_read_rejects_bad_magic(tmp_path):
    path = _write(tmp_path, "# something else\nssn,ch0,ch0_raw\n0,1,1.0")
    with pytest.raises(CsvFormatError, match="not a dynamite-csv"):
        dms.read_csv(path)


def test_read_rejects_missing_metadata_line(tmp_path):
    path = _write(tmp_path, MAGIC + "\nssn,ch0,ch0_raw\n0,1,1.0")
    with pytest.raises(CsvFormatError, match="metadata"):
        dms.read_csv(path)


def test_read_rejects_unknown_version(tmp_path):
    meta = _META.replace('"version":1', '"version":2')
    path = _write(tmp_path, f"{MAGIC}\n{meta}\nssn,ch0,ch0_raw\n0,1,1.0")
    with pytest.raises(CsvFormatError, match="version"):
        dms.read_csv(path)


def test_read_rejects_non_contiguous_ssn(tmp_path):
    path = _write(
        tmp_path,
        f"{MAGIC}\n{_META}\nssn,ch0,ch0_raw\n0,1,1.0\n2,2,2.0",
    )
    with pytest.raises(CsvFormatError, match="contiguous"):
        dms.read_csv(path)


def test_read_rejects_out_of_range_counts(tmp_path):
    path = _write(
        tmp_path,
        f"{MAGIC}\n{_META}\nssn,ch0,ch0_raw\n0,{1 << 23},1.0",
    )
    with pytest.raises(CsvFormatError, match="row"):
        dms.read_csv(path)


def test_read_rejects_unit_header_mismatch(tmp_path):
    path = _write(tmp_path, f"{MAGIC}\n{_META}\nssn,ch0,ch0_kgf\n0,1,1.0")
    with pytest.raises(CsvFormatError, match="converted_unit"):
        dms.read_csv(path)


def test_read_rejects_ssn_origin_mismatch(tmp_path):
    path = _write(tmp_path, f"{MAGIC}\n{_META}\nssn,ch0,ch0_raw\n9,1,1.0")
    with pytest.raises(CsvFormatError, match="ssn_origin"):
        dms.read_csv(path)


def test_read_ignores_later_comment_lines_and_unknown_columns(tmp_path):
    text = (
        f"{MAGIC}\n{_META}\n"
        "# this: comment could be stale YAML\n"
        "# whatever else\n"
        "ssn,ch0,ch0_raw,future_col\n"
        "0,1,2.0,x\n"
        "1,3,4.0,y"
    )
    path = _write(tmp_path, text)
    block = dms.read_csv(path)
    assert np.allclose(block.raw, [[1], [3]])
    assert np.allclose(block.data, [[2.0], [4.0]])


def test_csv_errors_are_dynamite_errors():
    assert issubclass(CsvFormatError, DynamiteError)
