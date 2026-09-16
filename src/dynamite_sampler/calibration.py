"""Board calibration, load cells, and the conversion pipeline.

Hardware-free and immutable: ``Calibration.from_kvs`` parses the device's raw
KVS snapshot and everything downstream (``stream``, ``read``, CSV) converts
through it. The pipeline is normative in ``docs/csv-format-v2.md``.
"""

import dataclasses
import math
from typing import Literal, Mapping

import numpy as np

from .cal_math import (
    CAL_POINT_COUNT,
    LADDER_RESISTOR_COUNT,
    ladder_setpoints_mv_per_v,
    expected_counts_per_mvv,
)
from .errors import CalibrationError, UnitUnavailable

ADC_CHANNEL_COUNT = 4

# Factory-namespace keys carrying the analog constants (all or none).
BOARD_CONSTANT_KEYS = ("adc_fsr", "exc", "afe_gain")

# Calibration-group metadata keys (the per-channel entries are added below).
_CAL_METADATA_KEYS = (
    "cal.date",
    "cal.board",
    "cal.tool",
    "cal.origin",
    "cal.temp",
    "cal.adc",
)

# User-namespace load-cell slots; the first ADC_CHANNEL_COUNT are the channels.
LOAD_CELL_SLOT_COUNT = 10

# Units and the per-mV/V factor of each force unit (1 kgf in the unit).
FORCE_FACTORS = {
    "kgf": 1.0,
    "N": 9.80665,
    "kN": 9.80665e-3,
    "lbf": 2.20462,
}
UNITS = ("raw", "mV/V", "mV", *FORCE_FACTORS)

# The keyword values accepted by Calibration.convert / stream / read.
Unit = Literal["raw", "mV/V", "mV", "kgf", "N", "kN", "lbf"]


def _cal_group_key_names(n_channels):
    keys = set(_CAL_METADATA_KEYS)
    for i in range(n_channels):
        keys.add(f"ch{i}.r")
        keys.add(f"ch{i}.raw")
    return keys


def _to_positive_float(value):
    """Parse the numeric part of a scalar ('<number>[,provenance]'); None if
    absent or not a positive finite number."""
    if value is None:
        return None
    try:
        parsed = float(value.split(",")[0].strip())
    except ValueError:
        return None
    if not np.isfinite(parsed) or parsed <= 0:
        return None
    return parsed


def _parse_number_list(value, count, key):
    """Comma-separated list of exactly ``count`` finite numbers; None if absent."""
    if value is None:
        return None
    parsed = []
    for part in value.split(","):
        try:
            parsed.append(float(part.strip()))
        except ValueError:
            raise CalibrationError(f"bad {key}: {value!r}") from None
    if len(parsed) != count or any(not np.isfinite(v) for v in parsed):
        raise CalibrationError(f"bad {key}: {value!r}")
    return parsed


def _channel_data_valid(resistors, readings):
    if resistors is None or readings is None:
        return False
    if any(not np.isfinite(r) or r <= 0 for r in resistors):
        return False
    if any(not np.isfinite(v) or v >= (1 << 23) or v < -(1 << 23) for v in readings):
        return False
    ordered = sorted(readings)
    return all(b - a >= 1000 for a, b in zip(ordered, ordered[1:]))


@dataclasses.dataclass(frozen=True)
class LoadCell:
    """A load cell as configured in a User-namespace slot."""

    name: str
    capacity_kg: float
    sensitivity_mv_v: float

    @property
    def kgf_per_mv_v(self):
        return self.capacity_kg / self.sensitivity_mv_v


@dataclasses.dataclass(frozen=True)
class BoardNominals:
    """The board's resolved analog constants and per-channel PGA gains."""

    adc_fsr_v: float
    afe_gain: float
    excitation_v: float
    pga_gains: list
    provenance: dict

    def counts_per_mvv(self, channel):
        if self.pga_gains is None:
            return None  # no ADC config (UNCONFIGURED board): nominal map unusable
        return expected_counts_per_mvv(
            self.adc_fsr_v, self.afe_gain, self.pga_gains[channel], self.excitation_v
        )


class ChannelBoard:
    """One channel's raw -> mV/V map: piecewise (calibrated) or nominal."""

    def __init__(self, nominals, channel, resistors=None, readings=None):
        self._counts_per_mvv = nominals.counts_per_mvv(channel)
        self._xs = None
        self._ys = None
        if readings is not None:
            self.setpoints = ladder_setpoints_mv_per_v(resistors)
            order = sorted(range(CAL_POINT_COUNT), key=readings.__getitem__)
            self._xs = np.array([readings[k] for k in order], dtype=np.float64)
            self._ys = np.array([self.setpoints[k] for k in order], dtype=np.float64)
            self.resistors = list(resistors)
            self.readings = list(readings)

    @property
    def is_calibrated(self):
        return self._xs is not None

    def counts_per_mvv_chord(self):
        """Counts per mV/V for the CSV precision quantum (csv-format-v2.md
        §Precision): the chord through the two outermost cal points when
        calibrated (setpoints and readings are in storage order, first point
        the most positive), else the nominal chain (``None`` when the
        runtime PGA gains are unknown)."""
        if self.is_calibrated:
            return (self.readings[0] - self.readings[-1]) / (
                self.setpoints[0] - self.setpoints[-1]
            )
        return self._counts_per_mvv

    def mvv(self, raw):
        """Absolute raw counts -> mV/V (extrapolating along the outer segments).

        ``np.interp`` clamps at the ends; this does not, so the outer segments
        extrapolate as the pipeline requires."""
        raw = np.asarray(raw, dtype=np.float64)
        if self._xs is None:
            if self._counts_per_mvv is None:
                raise UnitUnavailable(
                    "nominal conversion needs the runtime PGA gains, but this "
                    "board has no ADC config (UNCONFIGURED)"
                )
            return raw / self._counts_per_mvv
        idx = np.clip(np.searchsorted(self._xs, raw, side="left"), 1, len(self._xs) - 1)
        x0, x1 = self._xs[idx - 1], self._xs[idx]
        y0, y1 = self._ys[idx - 1], self._ys[idx]
        return y0 + (raw - x0) * (y1 - y0) / (x1 - x0)


@dataclasses.dataclass(frozen=True)
class CalGroup:
    """The parsed calibration group: metadata plus per-channel ladder data."""

    date: str
    board_id: str | None
    tool: str | None
    origin: str | None
    temps_c: tuple | None
    adc_gains: list | None
    resistors: list
    readings: list


def _resolve_nominals(factory, pga_gains):
    """The board's analog constants. ``pga_gains`` may be None (UNCONFIGURED
    board): the constants still parse; the nominal map just can't convert."""
    missing = [k for k in BOARD_CONSTANT_KEYS if k not in factory]
    if missing:
        raise CalibrationError(f"board constants: missing {', '.join(missing)}")
    values = {}
    provenance = {}
    for key in BOARD_CONSTANT_KEYS:
        raw = factory[key]
        parsed = _to_positive_float(raw)
        if parsed is None:
            raise CalibrationError(f"board constants: bad {key}: {raw!r}")
        values[key] = parsed
        parts = raw.split(",")
        if len(parts) > 1:
            provenance[key] = ",".join(parts[1:]).strip()
    return BoardNominals(
        adc_fsr_v=values["adc_fsr"],
        afe_gain=values["afe_gain"],
        excitation_v=values["exc"],
        pga_gains=None if pga_gains is None else list(pga_gains),
        provenance=provenance,
    )


def _parse_cal_group(factory, n_channels):
    """The calibration group, or None when the ``cal.date`` marker is absent."""
    date = factory.get("cal.date")
    if date is None:
        return None
    resistors = [None] * n_channels
    readings = [None] * n_channels
    saw_absent = False
    saw_present = False
    for i in range(n_channels):
        r_value = factory.get(f"ch{i}.r")
        raw_value = factory.get(f"ch{i}.raw")
        if r_value is None and raw_value is None:
            saw_absent = True
            continue
        resistors[i] = _parse_number_list(r_value, LADDER_RESISTOR_COUNT, f"ch{i}.r")
        readings[i] = _parse_number_list(raw_value, CAL_POINT_COUNT, f"ch{i}.raw")
        if not _channel_data_valid(resistors[i], readings[i]):
            raise CalibrationError(f"calibration: invalid channel data (ch{i})")
        saw_present = True
    if saw_absent or not saw_present:
        raise CalibrationError("calibration: only some channels calibrated")
    temps = _parse_number_list(factory.get("cal.temp"), 2, "cal.temp")
    return CalGroup(
        date=date,
        board_id=factory.get("cal.board"),
        tool=factory.get("cal.tool"),
        origin=factory.get("cal.origin"),
        temps_c=None if temps is None else (temps[0], temps[1]),
        adc_gains=_parse_number_list(factory.get("cal.adc"), n_channels, "cal.adc"),
        resistors=resistors,
        readings=readings,
    )


def _parse_load_cells(user, n_channels):
    """Lenient parse (the app owns these keys): a malformed slot reads empty."""
    load_cells = []
    for i in range(n_channels):
        capacity = _to_positive_float(user.get(f"lc{i}.cap"))
        sensitivity = _to_positive_float(user.get(f"lc{i}.sens"))
        if capacity is None or sensitivity is None:
            load_cells.append(None)
        else:
            load_cells.append(
                LoadCell(user.get(f"lc{i}.name") or "", capacity, sensitivity)
            )
    return load_cells


class Calibration:
    """A device's calibration: per-channel board map, load cells, provenance.

    ``board`` is the per-channel raw -> mV/V map, ``None`` when the board has
    no analog constants (``raw`` is the only convertible unit).
    """

    def __init__(self, channels, load_cells, nominals, group):
        self._channels = channels
        self.load_cells = load_cells
        self.nominals = nominals
        self._group = group

    @classmethod
    def from_kvs(
        cls, snapshot: Mapping[str, Mapping[str, str]], pga_gains: list[int] | None
    ) -> "Calibration":
        """Parse a raw KVS snapshot (``{"F": {...}, "U": {...}}``).

        ``pga_gains`` may be None (UNCONFIGURED board with no ADC config);
        the nominal map then can't convert, failing at ``check_units``/
        ``convert`` instead of here. Raises :class:`CalibrationError` only
        on data that is present and wrong."""
        factory = dict(snapshot.get("F", {}))
        user = dict(snapshot.get("U", {}))
        n_channels = len(pga_gains) if pga_gains else ADC_CHANNEL_COUNT

        constants_present = any(k in factory for k in BOARD_CONSTANT_KEYS)
        cal_keys_present = any(k in factory for k in _cal_group_key_names(n_channels))

        if not constants_present:
            if cal_keys_present:
                raise CalibrationError(
                    "calibration keys without the analog constants "
                    "(run provisioning first)"
                )
            channels = [None] * n_channels
            group = None
            nominals = None
        else:
            nominals = _resolve_nominals(factory, pga_gains)
            group = _parse_cal_group(factory, n_channels)
            if group is None:
                if cal_keys_present:
                    raise CalibrationError(
                        "calibration keys without the cal.date marker (torn write)"
                    )
                channels = [ChannelBoard(nominals, i) for i in range(n_channels)]
            else:
                if (
                    pga_gains is not None
                    and group.adc_gains is not None
                    and group.adc_gains != list(pga_gains)
                ):
                    raise CalibrationError(
                        f"stale calibration: cal.adc {group.adc_gains} != "
                        f"runtime PGA {list(pga_gains)}"
                    )
                channels = [
                    ChannelBoard(nominals, i, group.resistors[i], group.readings[i])
                    if group.readings[i] is not None
                    else ChannelBoard(nominals, i)
                    for i in range(n_channels)
                ]

        return cls(
            channels,
            _parse_load_cells(user, n_channels),
            nominals,
            group,
        )

    @property
    def board(self):
        """Per-channel raw -> mV/V map (``ChannelBoard | None``)."""
        return self._channels

    @property
    def group(self) -> "CalGroup | None":
        """The parsed calibration group (metadata, resistors, readings), or
        ``None`` when the board carries only nominal constants."""
        return self._group

    @property
    def is_calibrated(self):
        return self._group is not None

    def check_units(self, units: str) -> None:
        """Raise :class:`UnitUnavailable` if ``units`` can't convert everywhere."""
        if units not in UNITS:
            raise ValueError(f"unknown unit {units!r}; choose one of {UNITS}")
        if units == "raw":
            return
        if self.nominals is None:
            raise UnitUnavailable(
                f"unit {units!r} needs the board's analog constants; only 'raw' "
                "converts on this device"
            )
        if not self.is_calibrated and self.nominals.pga_gains is None:
            raise UnitUnavailable(
                f"unit {units!r} needs the runtime PGA gains, but this board has "
                "no ADC config (UNCONFIGURED); only 'raw' converts on this device"
            )
        if units in FORCE_FACTORS:
            for i, load_cell in enumerate(self.load_cells):
                if load_cell is None:
                    raise UnitUnavailable(
                        f"unit {units!r} needs a load cell in slot {i} (ch{i})"
                    )

    def csv_decimals(self, units: str) -> list[int]:
        """Fixed-point decimals per channel for a dynamite-csv column in
        ``units`` (csv-format-v2.md §Precision): one guard digit beyond the
        value of 1 ADC count in that unit."""
        self.check_units(units)
        if units == "raw":
            return [1] * len(self._channels)
        decimals = []
        for i, channel in enumerate(self._channels):
            quantum = abs(
                self._scale_per_mvv(i, units) / channel.counts_per_mvv_chord()
            )
            decimals.append(min(10, max(0, math.ceil(1 - math.log10(quantum) - 1e-9))))
        return decimals

    def _scale_per_mvv(self, channel, units):
        if units == "mV/V":
            return 1.0
        if units == "mV":
            return self.nominals.excitation_v
        return FORCE_FACTORS[units] * self.load_cells[channel].kgf_per_mv_v

    def convert(self, raw, units: str, tare_raw=None) -> np.ndarray:
        """Convert absolute raw counts to ``units``, net of ``tare_raw``.

        ``raw`` has channels on its last axis. With ``units='raw'`` the result
        is the net counts (``raw - tare_raw``); otherwise the map is applied
        to both the reading and the tare and differenced in mV/V space.
        """
        self.check_units(units)
        raw = np.asarray(raw, dtype=np.float64)
        if units == "raw":
            if tare_raw is None:
                return raw.copy()
            return raw - np.asarray(tare_raw, dtype=np.float64)
        out = np.empty_like(raw)
        for i, channel in enumerate(self._channels):
            tare = None if tare_raw is None else np.asarray(tare_raw)[i]
            tare_mvv = 0.0 if tare is None else channel.mvv(tare)
            out[..., i] = (channel.mvv(raw[..., i]) - tare_mvv) * self._scale_per_mvv(
                i, units
            )
        return out
