"""dynamite-csv 1 files (docs/csv-format-v2.md): write and read.

The format is self-contained: raw data plus everything needed to reproduce
every converted value, without the app or the device. :class:`CsvRecorder`
freezes the recording-start snapshot (device identity, KVS, calibration,
tare) and converts each incoming block's raw counts itself, so a
mid-recording KVS write or re-tare can never leak into the file.
:func:`read_csv` returns the file as a :class:`Block`, converted columns
verbatim (blank = NaN), never re-derived.
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
from .calibration import Calibration, LoadCell
from .errors import CsvFormatError, ProvisioningError

_log = logging.getLogger(__name__)

MAGIC = "# dynamite-csv 1"
VERSION = 1

try:
    _PACKAGE_VERSION = importlib.metadata.version("dynamite-sampler")
except importlib.metadata.PackageNotFoundError:  # not installed (editable src)
    _PACKAGE_VERSION = "unknown"
_GENERATOR = f"dynamite-sampler-py {_PACKAGE_VERSION}"


def _to_json(value) -> str:
    """The metadata line's machine form: compact one-line JSON. Number
    spelling is whatever the stdlib emits (csv-format-v2.md §The two
    renderings); non-finite floats raise (allow_nan=False), as JSON has
    no literal for them."""
    return json.dumps(value, ensure_ascii=False, separators=(",", ":"), allow_nan=False)


_YAML_WIDTH = 1_000_000


class _YamlDumper(yaml.SafeDumper):
    def increase_indent(self, flow=False, indentless=False):
        # Indent block sequences under their key (PyYAML's default is the
        # less readable indentless style).
        return super().increase_indent(flow, indentless=False)


def yaml_lines(metadata: dict) -> list[str]:
    """The YAML rendering of the metadata object (csv-format-v2.md §The two
    renderings): derived documentation, implementation-defined; the JSON
    line stays the only machine form, so consumers must not parse this."""
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


class CsvRecorder:
    """Record a device's feed to a dynamite-csv 1 file.

    The recording-start snapshot (identity, KVS, calibration, tare) freezes
    at construction; ``write_block`` converts each block's raw counts
    through the frozen calibration and tare (the ``Block.units`` of incoming
    blocks is ignored: only ``raw`` and ``ssn0`` are consumed). Blocks must
    arrive contiguously from a single stream; a gap between blocks raises
    rather than fabricating rows. The file is created when the first block
    arrives (``ssn_origin``), so a recorder closed with no blocks leaves no
    file.

    A force ``units`` with a cell-less channel raises ``UnitUnavailable`` at
    construction (no per-channel fallback; the converted column pattern is
    the app's export shape, not the API's).
    """

    def __init__(self, dev, path, units: str = "raw"):
        if dev.sample_rate is None or dev.gains is None:
            raise ProvisioningError(
                "device has no ADC config (UNCONFIGURED); nothing to record"
            )
        gains = list(dev.gains)
        snapshot = dev.kvs.snapshot
        # A fresh parse, not dev.calibration: identical result, but the
        # device's own instance may be None after a bad KVS write while the
        # error lives on the device. Here it raises at construction.
        self._calibration = Calibration.from_kvs(snapshot, gains)
        self._calibration.check_units(units)
        self._decimals = self._calibration.csv_decimals(units)
        tare = dev.tare_raw
        self._tare_raw = None if tare is None else list(tare)
        self._units = units
        self._n = len(gains)
        self._sample_rate = dev.sample_rate
        self._recorded = datetime.datetime.now().astimezone()
        self._device = self._device_block(dev.info, gains, self._calibration, snapshot)
        self._channels = self._channel_blocks(self._calibration, gains, self._tare_raw)
        self._path = Path(path)
        self._file = None
        self._next_ssn = None

    @staticmethod
    def _device_block(info, gains, calibration, snapshot) -> dict:
        nominals = calibration.nominals
        return {
            "name": info.name,
            "id": info.serial,
            "model": info.model_number,
            "hardware_rev": info.board_model,
            "firmware": info.firmware,
            "manufacturer": info.manufacturer,
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

    @staticmethod
    def _channel_blocks(calibration, gains, tare_raw) -> list[dict]:
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
                        "pga": float(gains[i]),
                        "exc": nominals.excitation_v,
                    },
                }
            channels.append(
                {
                    "load_cell": _load_cell_json(calibration.load_cells[i]),
                    "tare_raw": None if tare_raw is None else float(tare_raw[i]),
                    "board_cal": board_cal,
                }
            )
        return channels

    def _open(self, ssn_origin: int) -> None:
        metadata = {
            "format": "dynamite-csv",
            "version": VERSION,
            "generator": _GENERATOR,
            "recorded_at": self._recorded.isoformat(timespec="milliseconds"),
            "recorded_unix": int(self._recorded.timestamp()),
            "sample_rate_hz": self._sample_rate,
            "ssn_origin": ssn_origin,
            "converted_unit": self._units,
            "device": self._device,
            "channels": self._channels,
        }
        self._path.parent.mkdir(parents=True, exist_ok=True)
        # newline="": line endings are written explicitly ("\n" only).
        self._file = open(self._path, "w", encoding="utf-8", newline="")
        self._file.write(MAGIC + "\n")
        self._file.write("# " + _to_json(metadata) + "\n")
        for line in yaml_lines(metadata):
            self._file.write("# " + line + "\n")
        raw_cols = ",".join(f"ch{i}" for i in range(self._n))
        data_cols = ",".join(f"ch{i}_{self._units}" for i in range(self._n))
        self._file.write(f"ssn,{raw_cols},{data_cols}\n")
        self._next_ssn = ssn_origin

    def write_block(self, block: Block) -> None:
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
        lines = []
        for row in range(raw.shape[0]):
            cells = [str(self._next_ssn)]
            blanks = np.isnan(raw[row])
            for i in range(self._n):
                cells.append("" if blanks[i] else str(int(raw[row, i])))
            for i in range(self._n):
                cells.append(
                    "" if blanks[i] else f"{data[row, i]:.{self._decimals[i]}f}"
                )
            lines.append(",".join(cells))
            self._next_ssn += 1
        self._file.write("\n".join(lines) + "\n")
        self._file.flush()

    def close(self) -> None:
        if self._file is None:
            _log.info("CsvRecorder closed with no blocks; no file at %s", self._path)
        else:
            self._file.close()
            self._file = None

    def __enter__(self) -> "CsvRecorder":
        return self

    def __exit__(self, *exc):
        self.close()


def read_csv(path) -> Block:
    """A dynamite-csv 1 file as a :class:`Block`.

    ``raw`` comes from the raw columns, ``data`` from the converted columns
    verbatim (blank cells are NaN, covering both the dropped-sample row
    pattern and the unit-unavailable column pattern), ``t`` is derived from
    ``ssn`` and ``sample_rate_hz``, and ``host_time`` is NaN (a file-sourced
    block never arrived over a link). Unknown columns and unknown metadata
    fields are ignored. Container inconsistencies raise
    :class:`CsvFormatError`.
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

    return Block(
        data=data,
        raw=raw,
        t=(ssn - ssn_origin) / sample_rate,
        ssn0=ssn_origin,
        units=units,
        host_time=float("nan"),
    )


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
    if metadata.get("version") != VERSION:
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
