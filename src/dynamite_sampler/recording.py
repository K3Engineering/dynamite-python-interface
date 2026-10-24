"""A complete capture: rows plus the provenance to stand alone.

A ``Recording`` is what ``dev.read()`` returns, what ``dms.read_csv`` builds
from a file, and what ``Recording.to_csv`` writes back out: the rows, their
unit, and the frozen conversion inputs (calibration, tare, device metadata)
that make the file self-describing. ``dev.stream()`` yields plain ``Packet``s
— a stream has no provenance worth freezing.
"""

import dataclasses
import datetime

import numpy as np

from .calibration import Calibration, Unit

_KEEP_TARE = object()

COLUMN_NAME_STYLES = ("csv", "load_cell")


@dataclasses.dataclass(frozen=True, kw_only=True)
class Recording:
    """A contiguous run of samples with its conversion inputs frozen in.

    ``raw`` is absolute counts (``(n, N) float64``, exact for 24-bit) with
    NaN rows where samples were lost, so row ``i`` is sample ``ssn0 + i`` and
    ``t`` is arithmetic. ``data`` is the same rows converted to ``units``.
    ``host_time`` is NaN for a file-sourced recording: it never arrived over
    a link.

    - ``sample_rate``: samples per second (``t``'s denominator).
    - ``calibration``: the ``Calibration`` the rows convert through;
      rebuilt from the file header for a file-sourced recording
      (a metadata-less file gives an unprovisioned calibration: only
      ``raw`` converts).
    - ``tare_raw``: per-channel tare (a NaN slot means that channel was not
      tared; it reads gross), ``None`` when untared entirely.
    - ``recorded_at``: wall clock at capture start, if known.
    - ``device``: the dynamite-csv ``device`` object verbatim (identity,
      ``afe``, ``kvs``), `None` when the source file omitted it.
    - ``generator``: the program that produced the source file.
    """

    data: np.ndarray
    raw: np.ndarray
    t: np.ndarray
    ssn0: int
    units: str
    host_time: float
    sample_rate: float
    calibration: Calibration
    tare_raw: np.ndarray | None = None
    recorded_at: datetime.datetime | None = None
    device: dict | None = None
    generator: str | None = None

    @property
    def rows_dropped(self) -> int:
        """Rows lost in transit: the all-NaN rows kept in place to hold the
        timeline."""
        return int(np.count_nonzero(np.isnan(self.raw).all(axis=1)))

    def __len__(self) -> int:
        """Row count: how many samples of the timeline the recording spans."""
        return self.raw.shape[0]

    @property
    def n_channels(self) -> int:
        return self.raw.shape[1]

    def convert(self, units: Unit, tare_raw=_KEEP_TARE) -> "Recording":
        """The same rows in another unit; ``raw`` is untouched.

        Without ``tare_raw`` the recording's own tare applies. Raises
        ``UnitUnavailable`` like ``Calibration.convert`` (a cell-less
        recording can't go to force units).
        """
        if tare_raw is _KEEP_TARE:
            tare_raw = self.tare_raw
        return dataclasses.replace(
            self,
            data=self.calibration.convert(self.raw, units, tare_raw),
            units=units,
            tare_raw=tare_raw,
        )

    def column_names(self, names: str = "csv") -> list[str]:
        """Column labels: ``csv`` matches the file columns
        (``ch0_kgf``...); ``load_cell`` uses slot names where set."""
        channels = self.n_channels
        if names == "csv":
            return [f"ch{i}_{self.units}" for i in range(channels)]
        if names == "load_cell":
            cells = self.calibration.load_cells
            return [
                cells[i].name if cells[i] is not None and cells[i].name else f"ch{i}"
                for i in range(channels)
            ]
        raise ValueError(f"names must be one of {COLUMN_NAME_STYLES}: {names!r}")

    def to_dataframe(self, names: str = "csv"):
        """A pandas DataFrame: ``t`` as index (named ``t_s``), one column
        per channel (see :meth:`column_names`). pandas is an optional
        dependency (``pip install dynamite-sampler[pandas]``)."""
        try:
            import pandas
        except ImportError:
            raise ImportError(
                "to_dataframe needs pandas (pip install dynamite-sampler[pandas])"
            ) from None
        return pandas.DataFrame(
            self.data,
            index=pandas.Index(self.t, name="t_s"),
            columns=self.column_names(names),
        )

    def to_csv(self, path) -> None:
        """A self-contained dynamite-csv 1 file. What read_csv produces
        can be written back out; what to_csv writes reads back equal."""
        from .csv_io import write_recording

        write_recording(path, self)
