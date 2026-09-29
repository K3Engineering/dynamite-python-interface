"""One ADC-feed packet: the on-the-wire unit beneath :class:`Block`."""

import dataclasses

import numpy as np


@dataclasses.dataclass(frozen=True)
class Packet:
    """A single BLE notification from the ADC feed, decoded and timestamped.

    ``raw`` is the packet's rows (``(rows, N) float64``, absolute counts).
    ``data`` is the same rows converted to ``units`` (defaults to ``raw``
    itself: hand-built packets are always raw). ``ssn`` is the unwrapped
    sample sequence number of the first row. ``rows_dropped`` counts
    samples lost between this packet and the previous one (0 on a clean
    link). ``time`` is the ``time.monotonic()`` arrival timestamp;
    ``payload_bytes`` is the full notification size (header + payload),
    for byte-rate metrics.
    """

    time: float
    ssn: int
    rows_dropped: int
    payload_bytes: int
    raw: np.ndarray
    data: np.ndarray | None = None
    units: str = "raw"

    def __post_init__(self):
        if self.data is None:
            object.__setattr__(self, "data", self.raw)

    @property
    def rows(self) -> int:
        return self.raw.shape[0]
