"""dynamite-csv 1 files (docs/csv-format-v2.md): writing and reading.

:class:`CsvRecorder` records a live feed incrementally: it freezes the
recording-start snapshot (device identity, KVS, calibration, tare) and
converts each incoming block's raw counts itself, so a mid-recording KVS
write or re-tare can never leak into the file. :func:`write_recording`
is the one-shot form behind ``Recording.to_csv``. :func:`read_csv` is the
strict reader: it returns a :class:`Recording` (the same shape
``dev.read`` returns) with its ``Calibration`` rebuilt from the file's
metadata line, so loaded files re-convert without a device.
"""

import csv
import datetime
import importlib.metadata
import json
import logging
from pathlib import Path

import numpy as np
import yaml

from .block import Block
from .calibration import Calibration, LoadCell, Unit
from .errors import CsvFormatError, ProvisioningError
from .recording import Recording

_log = logging.getLogger(__name__)

MAGIC = "# dynamite-csv 1"
VERSION = 1

try:
    _PACKAGE_VERSION = importlib.metadata.version("dynamite-sampler")
except importlib.metadata.PackageNotFoundError:  # not installed (editable src)
    _PACKAGE_VERSION = "unknown"
_GENERATOR = f"dynamite-sampler-py {_PACKAGE_VERSION}"


def _json_line(metadata: dict) -> str:
    return json.dumps(
        metadata, ensure_ascii=False, separators=(",", ":"), allow_nan=False
    )


_YAML_WIDTH = 1_000_000


class _YamlDumper(yaml.SafeDumper):
    def increase_indent(self, flow=False, indentless=False):
        # Indent block sequences under their key (PyYAML's default is the
        # less readable indentless style).
        return super().increase_indent(flow, indentless=False)


def _yaml_lines(metadata: dict) -> list[str]:
    """The YAML rendering of the metadata object: derived documentation
    (csv-format-v2.md §The two renderings); consumers must not parse it."""
    text = yaml.dump(
        metadata,
        Dumper=_YamlDumper,
        sort_keys=False,
        allow_unicode=True,
        width=_YAML_WIDTH,
    )
    return text.splitlines()


def _sorted_namespace(snapshot, folder) -> dict:
    entries = snapshot.get(folder, {})
    return {key: entries[key] for key in sorted(entries)}


def _load_cell_json(cell: LoadCell | None) -> dict | None:
    if cell is None:
        return None
    return {
        "name": cell.name,
        "capacity_kg": cell.capacity_kg,
        "sensitivity_mv_v": cell.sensitivity_mv_v,
    }


def device_metadata(info, gains, calibration: Calibration, snapshot) -> dict:
    """The metadata ``device`` object: identity, analog front end, and the
    raw KVS strings. ``info`` may be None (tests; connect() always has it)."""
    nominals = calibration.nominals
    return {
        "name": None if info is None else info.name,
        "id": None if info is None else info.serial,
        "model": None if info is None else info.model_number,
        "hardware_rev": None if info is None else info.board_model,
        "firmware": None if info is None else info.firmware,
        "manufacturer": None if info is None else info.manufacturer,
        "afe": {
            "adc_ref_v": None if nominals is None else nominals.adc_fsr_v,
            "front_end_gain": None if nominals is None else nominals.afe_gain,
            "adc_gain": [float(gain) for gain in gains],
            "excitation_v": None if nominals is None else nominals.excitation_v,
        },
        "kvs": {
            "factory": _sorted_namespace(snapshot, "F"),
            "user": _sorted_namespace(snapshot, "U"),
        },
    }


def channel_metadata(calibration: Calibration, pga_gains, tare_raw) -> list[dict]:
    """One metadata entry per channel: load-cell slot, tare, board cal
    (with the nominal-input snapshot for regenerated precision)."""
    nominals = calibration.nominals
    channels = []
    for i, channel in enumerate(calibration.board):
        board_cal = None
        if channel is not None and channel.is_calibrated:
            board_cal = {
                "r": list(channel.resistors),
                "raw": list(channel.readings),
                "n": {
                    "fsr": nominals.adc_fsr_v,
                    "afe": nominals.afe_gain,
                    "pga": None if pga_gains[i] is None else float(pga_gains[i]),
                    "exc": nominals.excitation_v,
                },
            }
        channels.append(
            {
                "load_cell": _load_cell_json(calibration.load_cells[i]),
                # A NaN slot means "not tared" and serializes as null.
                "tare_raw": None
                if tare_raw is None or np.isnan(tare_raw[i])
                else float(tare_raw[i]),
                "board_cal": board_cal,
            }
        )
    return channels


def metadata_dict(
    *, sample_rate, ssn_origin, units, recorded_at, generator, device, channels
) -> dict:
    """The line-2 metadata object of a recording. Key order is format
    (csv-format-v2.md §The two renderings): do not reshuffle."""
    return {
        "format": "dynamite-csv",
        "version": VERSION,
        "generator": generator,
        "recorded_at": recorded_at.isoformat(timespec="milliseconds"),
        "recorded_unix": int(recorded_at.timestamp()),
        "sample_rate_hz": sample_rate,
        "ssn_origin": ssn_origin,
        "converted_unit": units,
        "device": device,
        "channels": channels,
    }


def _write_header(file, metadata: dict, n_channels: int, units: str) -> None:
    file.write(MAGIC + "\n")
    file.write("# " + _json_line(metadata) + "\n")
    for line in _yaml_lines(metadata):
        file.write("# " + line + "\n")
    csv_writer = csv.writer(file, lineterminator="\n")
    raw_cols = [f"ch{i}" for i in range(n_channels)]
    data_cols = [f"ch{i}_{units}" for i in range(n_channels)]
    csv_writer.writerow(["ssn", *raw_cols, *data_cols])


def _row_cells(ssn: int, raw_row, data_row, decimals) -> list[str]:
    blanks = np.isnan(raw_row)
    cells = [str(ssn)]
    for i in range(raw_row.shape[0]):
        cells.append("" if blanks[i] else str(int(raw_row[i])))
    for i in range(raw_row.shape[0]):
        cells.append("" if blanks[i] else f"{data_row[i]:.{decimals[i]}f}")
    return cells


def write_recording(path, recording: Recording) -> None:
    """Write ``recording`` as a self-contained dynamite-csv 1 file (this
    is ``Recording.to_csv``; the import is lazy there to keep the two
    modules import-order independent)."""
    calibration = recording.calibration
    nominals = calibration.nominals
    pga_gains = (
        [None] * recording.n_channels if nominals is None else nominals.pga_gains
    )
    metadata = metadata_dict(
        sample_rate=recording.sample_rate,
        ssn_origin=recording.ssn0,
        units=recording.units,
        recorded_at=recording.recorded_at or datetime.datetime.now().astimezone(),
        generator=recording.generator or _GENERATOR,
        device=recording.device or {},
        channels=channel_metadata(calibration, pga_gains, recording.tare_raw),
    )
    decimals = calibration.csv_decimals(recording.units)
    data = calibration.convert(recording.raw, recording.units, recording.tare_raw)
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    with open(path, "w", encoding="utf-8", newline="") as file:
        _write_header(file, metadata, recording.n_channels, recording.units)
        csv_writer = csv.writer(file, lineterminator="\n")
        csv_writer.writerows(
            _row_cells(recording.ssn0 + row, recording.raw[row], data[row], decimals)
            for row in range(len(recording))
        )
        file.flush()


class CsvRecorder:
    """Record a device's feed to a dynamite-csv 1 file.

    The recording-start snapshot (identity, KVS, calibration, tare) freezes
    at construction; ``write`` converts each block's raw counts through the
    frozen calibration and tare (the ``Block.units`` of incoming blocks is
    ignored: only ``raw`` and ``ssn0`` are consumed). Blocks must arrive
    contiguously from a single stream; a gap between blocks raises rather
    than fabricating rows. The file is created when the first block
    arrives (``ssn_origin``), so a recorder closed with no blocks leaves no
    file.

    A force ``units`` with a cell-less channel raises ``UnitUnavailable`` at
    construction (no per-channel fallback; the converted column pattern is
    the app's export shape, not the API's).
    """

    def __init__(self, dev, path, units: Unit = "raw"):
        if dev.sample_rate is None or dev.gains is None:
            raise ProvisioningError(
                "device has no ADC config (UNCONFIGURED); nothing to record"
            )
        gains = list(dev.gains)
        snapshot = dev.kvs.snapshot
        # A fresh parse, not dev.calibration: after a calibration-breaking
        # KVS write the device keeps its last valid Calibration, while a
        # new recording must fail here, at construction.
        self._calibration = Calibration.from_kvs(snapshot, gains)
        self._calibration.check_units(units)
        self._decimals = self._calibration.csv_decimals(units)
        tare = dev.tare_raw
        self._tare_raw = None if tare is None else list(tare)
        self._units = units
        self._n = len(gains)
        self._sample_rate = dev.sample_rate
        self._recorded_at = datetime.datetime.now().astimezone()
        self._device = device_metadata(dev.info, gains, self._calibration, snapshot)
        self._channels = channel_metadata(self._calibration, gains, self._tare_raw)
        self._path = Path(path)
        self._file = None
        self._csv_writer = None
        self._next_ssn = None

    def _open(self, ssn_origin: int) -> None:
        metadata = metadata_dict(
            sample_rate=self._sample_rate,
            ssn_origin=ssn_origin,
            units=self._units,
            recorded_at=self._recorded_at,
            generator=_GENERATOR,
            device=self._device,
            channels=self._channels,
        )
        self._path.parent.mkdir(parents=True, exist_ok=True)
        # newline="": line endings are written explicitly ("\n" only).
        self._file = open(self._path, "w", encoding="utf-8", newline="")
        _write_header(self._file, metadata, self._n, self._units)
        self._csv_writer = csv.writer(self._file, lineterminator="\n")
        self._next_ssn = ssn_origin

    def write(self, block: Block) -> None:
        """Append one block (converted per the frozen snapshot)."""
        raw = block.raw
        if raw.shape[1] != self._n:
            raise CsvFormatError(
                f"block has {raw.shape[1]} channels; the recording is {self._n}"
            )
        if self._file is None:
            self._open(block.ssn0)
        elif block.ssn0 != self._next_ssn:
            raise CsvFormatError(
                f"non-contiguous block: ssn0 {block.ssn0}, expected {self._next_ssn}"
            )
        data = self._calibration.convert(raw, self._units, self._tare_raw)
        self._csv_writer.writerows(
            _row_cells(self._next_ssn + row, raw[row], data[row], self._decimals)
            for row in range(len(block))
        )
        self._next_ssn += len(block)
        self._file.flush()

    def close(self) -> None:
        if self._file is None:
            _log.info("CsvRecorder closed with no blocks; no file at %s", self._path)
        else:
            self._file.close()
            self._file = None
            self._csv_writer = None

    def __enter__(self) -> "CsvRecorder":
        return self

    def __exit__(self, *exc):
        self.close()


def _parse_metadata(lines: list[str], path) -> dict:
    """The metadata line (line 2 is the only metadata; every comment line
    after it is documentation and is ignored here)."""
    if not lines or lines[0] != MAGIC:
        raise CsvFormatError(f"{path}: not a dynamite-csv 1 file")
    if len(lines) < 2 or not lines[1].startswith("# {"):
        raise CsvFormatError(f"{path}: missing metadata line")
    try:
        metadata = json.loads(lines[1][2:])
    except json.JSONDecodeError as exc:
        raise CsvFormatError(f"{path}: bad metadata JSON: {exc}") from None
    if metadata.get("format") != "dynamite-csv":
        raise CsvFormatError(f"{path}: metadata format must be 'dynamite-csv'")
    if metadata.get("version") != 1:
        raise CsvFormatError(
            f"{path}: unsupported dynamite-csv version {metadata.get('version')!r}"
        )
    return metadata


def _parse_header(header: list[str], metadata: dict, path):
    """Column layout from the header row: N raw + N converted (extra columns
    after them are ignored, per the format's extensibility rule). Returns
    ``(raw_col_indices, data_col_indices, units)``."""
    if not header or header[0] != "ssn":
        raise CsvFormatError(f"{path}: header must start with 'ssn'")
    n = 0
    while 1 + n < len(header) and header[1 + n] == f"ch{n}":
        n += 1
    if n == 0:
        raise CsvFormatError(f"{path}: no raw channel columns in header")
    converted = header[1 + n : 1 + 2 * n]
    if len(converted) < n:
        raise CsvFormatError(f"{path}: header has raw columns but no converted ones")
    suffixes = set()
    for i, name in enumerate(converted):
        prefix, _, suffix = name.partition(f"ch{i}_")
        if prefix or not suffix:
            raise CsvFormatError(f"{path}: unexpected column {name!r}")
        suffixes.add(suffix)
    if len(suffixes) != 1:
        raise CsvFormatError(f"{path}: mixed converted units in header")
    units = suffixes.pop()
    if units != metadata.get("converted_unit"):
        raise CsvFormatError(
            f"{path}: header unit {units!r} != metadata converted_unit "
            f"{metadata.get('converted_unit')!r}"
        )
    raw_cols = list(range(1, 1 + n))
    data_cols = list(range(1 + n, 1 + 2 * n))
    return raw_cols, data_cols, units


def read_csv(path) -> Recording:
    """A dynamite-csv 1 file as a :class:`Recording`, strict.

    ``raw`` comes from the raw columns, ``data`` from the converted columns
    verbatim (blank cells are NaN, covering both the dropped-sample row
    pattern and the unit-unavailable column pattern), ``t`` is derived from
    ``ssn`` and ``sample_rate_hz``, and ``host_time`` is NaN (a file-sourced
    block never arrived over a link). The calibration is rebuilt from the
    metadata line, so ``Recording.convert`` works without a device; a file
    without metadata inputs gets an unprovisioned calibration (only ``raw``
    converts). Unknown columns and unknown metadata fields are ignored.
    Container inconsistencies raise :class:`CsvFormatError`.
    """
    with open(path, encoding="utf-8") as file:
        lines = file.read().splitlines()
    metadata = _parse_metadata(lines, path)
    rows = [line for line in lines[2:] if line and not line.startswith("#")]
    if not rows:
        raise CsvFormatError(f"{path}: no column header")
    parsed = list(csv.reader(rows))
    header, body = parsed[0], parsed[1:]
    raw_cols, data_cols, units = _parse_header(header, metadata, path)
    n = len(raw_cols)

    n_rows = len(body)
    ssn_origin = metadata.get("ssn_origin")
    if not isinstance(ssn_origin, int) or isinstance(ssn_origin, bool):
        raise CsvFormatError(f"{path}: metadata ssn_origin must be an integer")
    sample_rate = metadata.get("sample_rate_hz")
    if not isinstance(sample_rate, (int, float)) or isinstance(sample_rate, bool):
        raise CsvFormatError(f"{path}: metadata sample_rate_hz must be a number")

    ssn = np.empty(n_rows, dtype=np.int64)
    raw = np.full((n_rows, n), np.nan)
    data = np.full((n_rows, n), np.nan)
    for r, row in enumerate(body):
        try:
            ssn[r] = int(row[0])
            for i in range(n):
                if row[raw_cols[i]] != "":
                    counts = int(row[raw_cols[i]])
                    if not -(1 << 23) <= counts < (1 << 23):
                        raise ValueError("count outside the 24-bit range")
                    raw[r, i] = counts
                if row[data_cols[i]] != "":
                    data[r, i] = float(row[data_cols[i]])
        except (ValueError, IndexError) as exc:
            raise CsvFormatError(f"{path}: bad data row {r + 1}: {exc}") from None

    if n_rows and ssn[0] != ssn_origin:
        raise CsvFormatError(
            f"{path}: ssn of row 0 ({ssn[0]}) != metadata ssn_origin ({ssn_origin})"
        )
    if np.any(np.diff(ssn) != 1):
        raise CsvFormatError(f"{path}: non-contiguous ssn (rows lost in transit)")

    entries = metadata.get("channels") or []
    tares = [entries[i].get("tare_raw") if i < len(entries) else None for i in range(n)]
    tare_raw = None
    if any(tare is not None for tare in tares):
        # A null slot tares nothing: NaN marks it so convert() runs it gross.
        tare_raw = np.array([np.nan if tare is None else float(tare) for tare in tares])
    recorded_at = metadata.get("recorded_at")
    if isinstance(recorded_at, str):
        try:
            recorded_at = datetime.datetime.fromisoformat(recorded_at)
        except ValueError:
            recorded_at = None
    else:
        recorded_at = None

    return Recording(
        data=data,
        raw=raw,
        t=(ssn - ssn_origin) / sample_rate,
        ssn0=ssn_origin,
        units=units,
        host_time=float("nan"),
        sample_rate=sample_rate,
        calibration=Calibration.from_metadata(metadata, n_channels=n),
        tare_raw=tare_raw,
        recorded_at=recorded_at,
        device=metadata.get("device"),
        generator=metadata.get("generator"),
    )
