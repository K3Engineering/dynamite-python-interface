"""Hardware-free tests for Calibration parsing and the conversion pipeline.

Fixtures follow the worked example in docs/csv-format-v2.md.
"""

import numpy as np
import pytest

from dynamite_sampler.cal_math import ladder_setpoints_mv_per_v
from dynamite_sampler.calibration import Calibration
from dynamite_sampler.errors import CalibrationError, UnitUnavailable

RESISTORS = "10001.2,9.98,10.01,10.02,9.99,9998.7"
READINGS = "6383553.0,3192096.0,120.0,-3191776.0,-6383313.0"
RESISTOR_VALUES = [float(v) for v in RESISTORS.split(",")]
READING_VALUES = [float(v) for v in READINGS.split(",")]

FACTORY = {
    "board_model": "v700P",
    "kvs_ver": "1",
    "adc_fsr": "1.2,nominal",
    "afe_gain": "101,nominal",
    "exc": "4.53,nominal",
    "cal.adc": "1,1,1,1",
    "cal.board": "CB42 v1.0.3",
    "cal.date": "2026-06-14",
    "cal.origin": "factory",
    "cal.r.prov": "nominal",
    "cal.temp": "-999,24.1",
    "cal.tool": "calibrate v3.1",
}
for _i in range(4):
    FACTORY[f"ch{_i}.r"] = RESISTORS
    FACTORY[f"ch{_i}.raw"] = READINGS

USER = {
    "lc0.cap": "100",
    "lc0.name": "John Smith's 100 kg",
    "lc0.sens": "2.007",
    "lc1.cap": "20",
    "lc1.sens": "2.0",
    "lc2.cap": "50",
    "lc2.sens": "2.0",
    "lc3.cap": "50",
    "lc3.sens": "2.0",
}

SNAPSHOT = {"F": FACTORY, "U": USER}
PGA = [1, 1, 1, 1]


def calibrated():
    return Calibration.from_kvs(SNAPSHOT, PGA)


def test_calibrated_state_and_metadata():
    cal = calibrated()
    assert cal.is_calibrated
    assert cal.group.date == "2026-06-14"
    assert cal.group.board_id == "CB42 v1.0.3"
    assert all(ch.is_calibrated for ch in cal.board)
    assert cal.load_cells[0].capacity_kg == 100.0
    assert cal.load_cells[0].sensitivity_mv_v == 2.007


def test_setpoints_from_resistors():
    cal = calibrated()
    expected = ladder_setpoints_mv_per_v(RESISTOR_VALUES)
    for ch in cal.board:
        assert ch.setpoints == pytest.approx(expected)


def test_mvv_interpolates_through_cal_points():
    cal = calibrated()
    channel = cal.board[0]
    expected = ladder_setpoints_mv_per_v(RESISTOR_VALUES)
    for raw, sp in zip(READING_VALUES, expected):
        assert channel.mvv(raw) == pytest.approx(sp, rel=1e-9)


def test_mvv_extrapolates_beyond_cal_range():
    cal = calibrated()
    channel = cal.board[0]
    expected = ladder_setpoints_mv_per_v(RESISTOR_VALUES)
    slope = (expected[0] - expected[1]) / (READING_VALUES[0] - READING_VALUES[1])
    raw = READING_VALUES[0] + 1000.0
    assert channel.mvv(raw) == pytest.approx(expected[0] + 1000.0 * slope, rel=1e-9)


def test_tare_zero_invariant():
    cal = calibrated()
    raw = np.array([READING_VALUES[0]] * 4)
    for units in ("mV/V", "mV", "kgf", "N"):
        out = cal.convert(raw, units, tare_raw=raw)
        assert np.allclose(out, 0.0, atol=1e-12)


def test_net_in_mvv_then_scale():
    cal = calibrated()
    raw = np.array([READING_VALUES[0]] * 4)
    tare = np.array([READING_VALUES[4]] * 4)
    sp = ladder_setpoints_mv_per_v(RESISTOR_VALUES)
    net = sp[0] - sp[4]

    mv_v = cal.convert(raw, "mV/V", tare)
    assert mv_v[0] == pytest.approx(net, rel=1e-9)

    kgf = cal.convert(raw, "kgf", tare)
    assert kgf[0] == pytest.approx(net * 100.0 / 2.007, rel=1e-9)

    newtons = cal.convert(raw, "N", tare)
    assert newtons[0] == pytest.approx(net * 100.0 / 2.007 * 9.80665, rel=1e-9)


def test_raw_unit_is_net_counts():
    cal = calibrated()
    raw = np.array([100.0, 200.0, 300.0, 400.0])
    tare = np.array([1.0, 2.0, 3.0, 4.0])
    assert np.array_equal(cal.convert(raw, "raw", tare), raw - tare)
    assert np.array_equal(cal.convert(raw, "raw"), raw)


def test_force_without_load_cell_raises_naming_channel():
    user = {k: v for k, v in USER.items() if not k.startswith("lc2.")}
    cal = Calibration.from_kvs({"F": FACTORY, "U": user}, PGA)
    raw = np.zeros((1, 4))
    with pytest.raises(UnitUnavailable, match="slot 2"):
        cal.convert(raw, "kgf")


def test_unprovisioned_board_raw_only():
    cal = Calibration.from_kvs({"F": {}, "U": {}}, None)
    assert not cal.is_calibrated
    assert cal.board == [None, None, None, None]
    raw = np.arange(8, dtype=float).reshape(2, 4)
    assert np.array_equal(cal.convert(raw, "raw"), raw)
    with pytest.raises(UnitUnavailable):
        cal.check_units("mV/V")


def test_nominal_chain_conversion():
    factory = {k: v for k, v in FACTORY.items() if not k.startswith(("ch", "cal."))}
    cal = Calibration.from_kvs({"F": factory, "U": {}}, PGA)
    assert not cal.is_calibrated
    raw = np.zeros((1, 4))
    raw[0, 0] = cal.nominals.counts_per_mvv(0)
    assert cal.convert(raw, "mV/V")[0, 0] == pytest.approx(1.0, rel=1e-9)


def test_stale_calibration_raises():
    bad = dict(FACTORY)
    bad["cal.adc"] = "1,1,1,2"
    with pytest.raises(CalibrationError, match="stale"):
        Calibration.from_kvs({"F": bad, "U": {}}, PGA)


def test_torn_group_without_date_raises():
    bad = dict(FACTORY)
    del bad["cal.date"]
    with pytest.raises(CalibrationError):
        Calibration.from_kvs({"F": bad, "U": {}}, PGA)


def test_constants_without_adc_config_parse_raw_only():
    # An UNCONFIGURED board mid-provisioning: constants are present but the
    # ADC config is unreadable. That is a state, not corrupt data.
    factory = {k: v for k, v in FACTORY.items() if not k.startswith(("ch", "cal."))}
    cal = Calibration.from_kvs({"F": factory, "U": {}}, None)
    assert not cal.is_calibrated
    raw = np.arange(8, dtype=float).reshape(2, 4)
    assert np.array_equal(cal.convert(raw, "raw"), raw)
    with pytest.raises(UnitUnavailable, match="PGA"):
        cal.check_units("mV/V")


def test_calibrated_without_adc_config_converts_mvv():
    # The piecewise map is not a conversion input on PGA gains, so a
    # calibrated board converts without the ADC config; the cal.adc
    # staleness check is skipped (nothing to compare against).
    cal = Calibration.from_kvs(SNAPSHOT, None)
    assert cal.is_calibrated
    sp = ladder_setpoints_mv_per_v(RESISTOR_VALUES)
    out = cal.convert(np.array([READING_VALUES[0]] * 4), "mV/V")
    assert out[0] == pytest.approx(sp[0], rel=1e-9)


def test_partial_constants_raise():
    bad = dict(FACTORY)
    del bad["exc"]
    with pytest.raises(CalibrationError, match="missing"):
        Calibration.from_kvs({"F": bad, "U": {}}, PGA)


def test_calibration_keys_without_constants_raise():
    factory = {
        k: v for k, v in FACTORY.items() if k not in ("adc_fsr", "exc", "afe_gain")
    }
    with pytest.raises(CalibrationError):
        Calibration.from_kvs({"F": factory, "U": {}}, PGA)


def test_invalid_channel_data_raises():
    bad = dict(FACTORY)
    bad["ch2.raw"] = "1,2,3,4,5"  # sub-thousand spacing
    with pytest.raises(CalibrationError, match="ch2"):
        Calibration.from_kvs({"F": bad, "U": {}}, PGA)


def test_malformed_load_cell_slot_reads_empty():
    cal = Calibration.from_kvs({"F": FACTORY, "U": {"lc0.cap": "100"}}, PGA)
    assert cal.load_cells[0] is None
