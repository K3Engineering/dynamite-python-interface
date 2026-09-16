"""dynamite-csv 1 files (docs/csv-format-v2.md): writing.

:class:`CsvRecorder`
freezes the recording-start snapshot (device identity, KVS, calibration,
tare) and converts each incoming block's raw counts itself, so a
mid-recording KVS write or re-tare can never leak into the file. The strict
reference reader lives in the test suite (tests/test_csv.py)
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
        self._csv_writer = None
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

    def _metadata(self, ssn_origin: int) -> dict:
        """The line-2 metadata object for this recording."""
        return {
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

    def _open(self, ssn_origin: int) -> None:
        metadata = self._metadata(ssn_origin)
        self._path.parent.mkdir(parents=True, exist_ok=True)
        # newline="": line endings are written explicitly ("\n" only).
        self._file = open(self._path, "w", encoding="utf-8", newline="")
        self._file.write(MAGIC + "\n")
        self._file.write("# " + _json_line(metadata) + "\n")
        for line in _yaml_lines(metadata):
            self._file.write("# " + line + "\n")
        csv_writer = csv.writer(self._file, lineterminator="\n")
        raw_cols = [f"ch{i}" for i in range(self._n)]
        data_cols = [f"ch{i}_{self._units}" for i in range(self._n)]
        csv_writer.writerow(["ssn", *raw_cols, *data_cols])
        self._csv_writer = csv_writer
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
        rows = []
        for row in range(raw.shape[0]):
            cells = [str(self._next_ssn)]
            blanks = np.isnan(raw[row])
            for i in range(self._n):
                cells.append("" if blanks[i] else str(int(raw[row, i])))
            for i in range(self._n):
                cells.append(
                    "" if blanks[i] else f"{data[row, i]:.{self._decimals[i]}f}"
                )
            rows.append(cells)
            self._next_ssn += 1
        self._csv_writer.writerows(rows)
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
