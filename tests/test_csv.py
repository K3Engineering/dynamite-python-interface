"""dynamite-csv read/write (csv_io), driven by the format doc's worked
example and the package's own recorder, no BLE required."""

import json
import math
from pathlib import Path

import numpy as np
import pytest
import yaml

import dynamite_sampler as dms
from dynamite_sampler.block import Block
from dynamite_sampler.calibration import (
    FORCE_FACTORS,
    BoardNominals,
    Calibration,
    ChannelBoard,
    LoadCell,
)
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
    assert b"\r" not in path.read_bytes()
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


# --- Regeneration: quartet 2 rebuilds from quartet 1 + metadata ------------

_CELLED_USER = {
    f"lc{i}.{key}": value
    for i in range(4)
    for key, value in (("cap", "100"), ("sens", "2.007"))
}


def _calibrated_snapshot():
    factory = dict(_NOMINAL_FACTORY)
    factory["cal.date"] = "2026-06-14"
    factory["cal.adc"] = "1,1,1,1"
    for i in range(4):
        factory[f"ch{i}.r"] = "10001.2,9.98,10.01,10.02,9.99,9998.7"
        factory[f"ch{i}.raw"] = "6383553.0,3192096.0,120.0,-3191776.0,-6383313.0"
    return {"F": factory, "U": dict(_CELLED_USER)}


def _record(tmp_path, unit, snapshot, tare_raw):
    dev = _FakeDev([1, 1, 1, 1], snapshot, tare_raw=tare_raw)
    path = tmp_path / "rec.csv"
    with CsvRecorder(dev, path, units=unit) as recorder:
        recorder.write_block(
            _block(
                [[1000, -2000, 300000, -400000], [np.nan] * 4, [5, 6, 7, 8]],
                41230,
            )
        )
        recorder.write_block(_block([[9, 10, 11, 12]], 41233))
    return path


def _calibration_from_metadata(metadata):
    """A Calibration rebuilt from a file's metadata line: channel boards
    from ``board_cal`` or the afe block (a board-less file has no boards),
    load cells per ``channels[]``."""
    afe = metadata["device"]["afe"]
    nominals = None
    if afe["adc_ref_v"] is not None:
        nominals = BoardNominals(
            adc_fsr_v=afe["adc_ref_v"],
            afe_gain=afe["front_end_gain"],
            excitation_v=afe["excitation_v"],
            pga_gains=afe["adc_gain"],
            provenance={},
        )
    boards = []
    load_cells = []
    for i, entry in enumerate(metadata["channels"]):
        board_cal = entry["board_cal"]
        if nominals is None:
            boards.append(None)
        elif board_cal is None:
            boards.append(ChannelBoard(nominals, i))
        else:
            boards.append(ChannelBoard(nominals, i, board_cal["r"], board_cal["raw"]))
        cell = entry["load_cell"]
        load_cells.append(
            None
            if cell is None
            else LoadCell(
                cell["name"] or "", cell["capacity_kg"], cell["sensitivity_mv_v"]
            )
        )
    return Calibration(boards, load_cells, nominals, None)


def _doc_scale_per_mvv(cell, unit, excitation_v):
    if unit == "mV/V":
        return 1.0
    if unit == "mV":
        return excitation_v
    return FORCE_FACTORS[unit] * cell.kgf_per_mv_v


def _doc_decimals(board, cell, unit, excitation_v):
    quantum = abs(
        _doc_scale_per_mvv(cell, unit, excitation_v) / board.counts_per_mvv_chord()
    )
    return min(10, max(0, math.ceil(1 - math.log10(quantum) - 1e-9)))


def _convertible(calibration, i, unit):
    """Per the doc's blank semantics: an all-blank converted column is
    exactly a channel the unit can't reach (no board at all, or — under a
    force unit — no load cell)."""
    if unit == "raw":
        return True
    board = calibration.board[i]
    if board is None or board.counts_per_mvv_chord() is None:
        return False
    return unit not in FORCE_FACTORS or calibration.load_cells[i] is not None


def regenerate_check(path):
    """None when every converted cell of the file equals its regeneration
    from quartet 1 + the metadata line (compared as text — the contract is
    the written digits); a description of the first mismatch otherwise.
    Raises CsvFormatError when the metadata can't rebuild the conversion."""
    dms.read_csv(path)  # container checks (magic, header, ssn, count range)
    lines = Path(path).read_text(encoding="utf-8").splitlines()
    metadata = json.loads(lines[1][2:])
    unit = metadata["converted_unit"]
    rows = [line.split(",") for line in lines[2:] if line and not line.startswith("#")]
    n = (len(rows[0]) - 1) // 2
    try:
        calibration = _calibration_from_metadata(metadata)
        tares = [entry["tare_raw"] for entry in metadata["channels"]]
    except (KeyError, TypeError, AttributeError) as exc:
        raise CsvFormatError(
            f"{path}: metadata cannot rebuild the conversion: {exc}"
        ) from None
    if len(calibration.board) != n:
        raise CsvFormatError(
            f"{path}: {len(calibration.board)} metadata channels, {n} raw columns"
        )
    for r, row in enumerate(rows[1:]):
        for i in range(n):
            raw_text = row[1 + i]
            if raw_text == "" or not _convertible(calibration, i, unit):
                expected = ""
            else:
                board = calibration.board[i]
                tare = tares[i]
                counts = float(raw_text)
                if unit == "raw":
                    value = counts if tare is None else counts - tare
                    decimals = 1
                else:
                    cell = calibration.load_cells[i]
                    exc = calibration.nominals.excitation_v
                    tare_mvv = 0.0 if tare is None else float(board.mvv(tare))
                    net = float(board.mvv(counts)) - tare_mvv
                    value = net * _doc_scale_per_mvv(cell, unit, exc)
                    decimals = _doc_decimals(board, cell, unit, exc)
                expected = f"{value:.{decimals}f}"
            actual = row[1 + n + i]
            if actual != expected:
                return (
                    f"row {r + 1} ch{i}: converted cell {actual!r} "
                    f"!= regenerated {expected!r}"
                )
    return None


@pytest.mark.parametrize("unit", ["raw", "mV/V", "mV", "kgf", "N", "kN", "lbf"])
@pytest.mark.parametrize("calibrated", [False, True])
@pytest.mark.parametrize("tare_raw", [None, [-12340.5, 55.0, 7001.25, -220.0]])
def test_recorded_file_regenerates(tmp_path, unit, calibrated, tare_raw):
    """Self-containment on this package's own output: the recorder's files
    regenerate at every unit, on both board shapes, gross and net, gap rows
    included."""
    snapshot = (
        _calibrated_snapshot()
        if calibrated
        else {"F": dict(_NOMINAL_FACTORY), "U": dict(_CELLED_USER)}
    )
    assert regenerate_check(_record(tmp_path, unit, snapshot, tare_raw)) is None


_AFE = {
    "adc_ref_v": 1.2,
    "front_end_gain": 101.0,
    "adc_gain": [1.0, 1.0, 1.0, 1.0],
    "excitation_v": 4.53,
}

_BOARD_CAL = {
    "r": [10001.2, 9.98, 10.01, 10.02, 9.99, 9998.7],
    "raw": [6383553.0, 3192096.0, 120.0, -3191776.0, -6383313.0],
    "n": {"fsr": 1.2, "afe": 101.0, "pga": 1.0, "exc": 4.53},
}


def _app_metadata(unit, channels, afe=_AFE):
    return {
        "format": "dynamite-csv",
        "version": 1,
        "generator": "dynamite-flutter 1.0.0",
        "recorded_at": "2026-07-29T10:05:32.184-04:00",
        "recorded_unix": 1785333932,
        "sample_rate_hz": 1000,
        "ssn_origin": 41230,
        "converted_unit": unit,
        "device": {
            "name": "DS A4CF1208F51E",
            "id": "A4CF1208F51E",
            "model": "Dynamite Sampler Pro Mk1",
            "hardware_rev": "v700P",
            "firmware": "v700P|v1.2.0-3-gdeadbee",
            "manufacturer": "K3 Engineering",
            "afe": afe,
            "kvs": None,
        },
        "channels": channels,
    }


def _hand_written(tmp_path, metadata, header, rows):
    path = tmp_path / "app.csv"
    path.write_text(
        "\n".join([MAGIC, "# " + _json_line(metadata), header, *rows]) + "\n",
        encoding="utf-8",
    )
    return path


def test_regeneration_accepts_app_shaped_file(tmp_path):
    """Export shapes the Python recorder never emits: per-channel all-blank
    columns (cell-less channels under a force unit), mixed calibrated and
    nominal boards, a null tare on one channel, and a gap row. Expected
    cells come from a ``from_kvs`` parse of the same data — a construction
    path independent of ``_calibration_from_metadata``."""
    cal = Calibration.from_kvs(_calibrated_snapshot(), [1.0, 1.0, 1.0, 1.0])
    tares = [-12340.5, 55.0, None, -220.0]
    channels = [
        {
            "load_cell": {
                "name": "Beam 100 kg",
                "capacity_kg": 100.0,
                "sensitivity_mv_v": 2.007,
            },
            "tare_raw": tares[0],
            "board_cal": _BOARD_CAL,
        },
        {"load_cell": None, "tare_raw": tares[1], "board_cal": None},
        {"load_cell": None, "tare_raw": tares[2], "board_cal": _BOARD_CAL},
        {"load_cell": None, "tare_raw": tares[3], "board_cal": None},
    ]
    cell = cal.load_cells[0]
    decimals = _doc_decimals(cal.board[0], cell, "kgf", cal.nominals.excitation_v)

    def kgf_row(ssn, counts):
        board = cal.board[0]
        net = float(board.mvv(counts[0])) - float(board.mvv(tares[0]))
        value = net * cell.kgf_per_mv_v
        return f"{ssn},{','.join(str(c) for c in counts)},{value:.{decimals}f},,,"

    path = _hand_written(
        tmp_path,
        _app_metadata("kgf", channels),
        "ssn,ch0,ch1,ch2,ch3,ch0_kgf,ch1_kgf,ch2_kgf,ch3_kgf",
        [
            kgf_row(41230, [-12339, 55, 7001, -220]),
            "41231,,,,,,,,",
            kgf_row(41232, [-12350, 58, 7000, -219]),
        ],
    )
    assert regenerate_check(path) is None


@pytest.mark.parametrize("unit", ["mV/V", "raw"])
def test_regeneration_accepts_boardless_app_file(tmp_path, unit):
    """An unprovisioned-board export: afe all-null, every board_cal null.
    Converted units are all-blank columns; raw still converts."""
    afe = {
        "adc_ref_v": None,
        "front_end_gain": None,
        "adc_gain": [None] * 4,
        "excitation_v": None,
    }
    tares = [10.5, None, -3.25, 7.0]
    channels = [
        {"load_cell": None, "tare_raw": tare, "board_cal": None} for tare in tares
    ]
    rows = []
    for s, counts in enumerate([[100, -200, 300, 400], [101, -201, 301, 401]]):
        cells = [str(41230 + s), *(str(c) for c in counts)]
        cells.extend(
            f"{counts[i] - (tares[i] or 0.0):.1f}" if unit == "raw" else ""
            for i in range(4)
        )
        rows.append(",".join(cells))
    path = _hand_written(
        tmp_path,
        _app_metadata(unit, channels, afe),
        f"ssn,ch0,ch1,ch2,ch3,ch0_{unit},ch1_{unit},ch2_{unit},ch3_{unit}",
        rows,
    )
    assert regenerate_check(path) is None


def test_tampered_tare_fails_regeneration(tmp_path):
    path = _record(
        tmp_path,
        "mV/V",
        _calibrated_snapshot(),
        [-12340.5, 55.0, 7001.25, -220.0],
    )
    text = path.read_text(encoding="utf-8")
    tampered = text.replace('"tare_raw":-12340.5', '"tare_raw":-12340.4')
    assert tampered != text
    path.write_text(tampered, encoding="utf-8")
    assert "regenerated" in (regenerate_check(path) or "")


def test_tampered_cell_fails_regeneration(tmp_path):
    path = _record(tmp_path, "raw", {"F": dict(_NOMINAL_FACTORY), "U": {}}, None)
    lines = path.read_text(encoding="utf-8").splitlines()
    row = lines.index(
        "41230,1000,-2000,300000,-400000,1000.0,-2000.0,300000.0,-400000.0"
    )
    cells = lines[row].split(",")
    cells[5] = "1000.1"
    lines[row] = ",".join(cells)
    path.write_text("\n".join(lines) + "\n", encoding="utf-8")
    assert "regenerated" in (regenerate_check(path) or "")


def test_unexpected_blank_fails_regeneration(tmp_path):
    path = _record(tmp_path, "raw", {"F": dict(_NOMINAL_FACTORY), "U": {}}, None)
    lines = path.read_text(encoding="utf-8").splitlines()
    row = lines.index(
        "41230,1000,-2000,300000,-400000,1000.0,-2000.0,300000.0,-400000.0"
    )
    cells = lines[row].split(",")
    cells[5] = ""
    lines[row] = ",".join(cells)
    path.write_text("\n".join(lines) + "\n", encoding="utf-8")
    assert "regenerated" in (regenerate_check(path) or "")


def test_regeneration_rejects_metadata_without_conversion_inputs(tmp_path):
    """read_csv tolerates a metadata line without device/channels; the file
    is then not self-contained, and the regenerator says so."""
    path = _write(tmp_path, f"{MAGIC}\n{_META}\nssn,ch0,ch0_raw\n0,1,1.0")
    dms.read_csv(path)
    with pytest.raises(CsvFormatError, match="rebuild"):
        regenerate_check(path)


def test_regeneration_rejects_channel_count_mismatch(tmp_path):
    channels = [
        {"load_cell": None, "tare_raw": None, "board_cal": None} for _ in range(4)
    ]
    path = _hand_written(
        tmp_path,
        _app_metadata("mV/V", channels),
        "ssn,ch0,ch0_mV/V",
        ["41230,5,0.00000000"],
    )
    with pytest.raises(CsvFormatError, match="metadata channels"):
        regenerate_check(path)


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
