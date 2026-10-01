"""A homogeneous block of ADC feed samples."""

import dataclasses

import numpy as np


@dataclasses.dataclass(frozen=True)
class Block:
    """``n`` rows of ``N`` channels, in one unit, with time and identity.

    ``raw`` is absolute counts (``(n, N) float64``, exact for 24-bit) and
    ``data`` is the same rows converted to ``units``. Dropped samples are NaN
    rows (``rows_dropped`` of them), so a block always spans exactly
    ``n / sample_rate`` seconds. ``host_time`` is NaN for a file-sourced
    block: it never arrived over a link.
    """

    data: np.ndarray
    raw: np.ndarray
    t: np.ndarray
    ssn0: int
    units: str
    host_time: float
    rows_dropped: int = 0

    def __len__(self) -> int:
        """Row count: how many samples of the timeline the block spans."""
        return self.raw.shape[0]

    @property
    def n_channels(self) -> int:
        return self.raw.shape[1]
