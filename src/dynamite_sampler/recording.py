"""A complete capture: a :class:`Block` plus the provenance to stand alone.

A ``Recording`` is what ``dev.read()`` returns, what ``dms.read_csv`` builds
from a file, and what ``Recording.to_csv`` writes back out: the rows, their
unit, and the frozen conversion inputs (calibration, tare, device metadata)
that make the file self-describing. ``dev.stream()`` still yields plain
``Block``s — a stream has no provenance worth freezing.
"""

import dataclasses
import datetime

import numpy as np

from .block import Block
from .calibration import Calibration, Unit

_KEEP_TARE = object()

COLUMN_NAME_STYLES = ("csv", "load_cell")


@dataclasses.dataclass(frozen=True, kw_only=True)
class Recording(Block):
    """A contiguous run of samples with its conversion inputs frozen in.

    Inherits every ``Block`` field (``data``/``raw``/``t``/``ssn0``/
    ``units``/``host_time``/``rows_dropped``) and adds:

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

    sample_rate: float
    calibration: Calibration
    tare_raw: np.ndarray | None = None
    recorded_at: datetime.datetime | None = None
    device: dict | None = None
    generator: str | None = None

    @classmethod
    def from_block(
        cls,
        block: Block,
        *,
        sample_rate: float,
        calibration: Calibration,
        tare_raw=None,
        recorded_at=None,
        device=None,
        generator=None,
    ) -> "Recording":
        """A Recording over ``block``'s rows, with the capture's frozen
        conversion inputs attached."""
        return cls(
            data=block.data,
            raw=block.raw,
            t=block.t,
            ssn0=block.ssn0,
            units=block.units,
            host_time=block.host_time,
            rows_dropped=block.rows_dropped,
            sample_rate=sample_rate,
            calibration=calibration,
            tare_raw=tare_raw,
            recorded_at=recorded_at,
            device=device,
            generator=generator,
        )

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
