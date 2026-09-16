"""One ADC-feed packet: the on-the-wire unit beneath :class:`Block`."""

import dataclasses

import numpy as np


@dataclasses.dataclass(frozen=True)
class Packet:
    """A single BLE notification from the ADC feed, decoded and timestamped.

    ``raw`` is the packet's rows (``(rows, N) float64``, absolute counts).
    ``ssn`` is the unwrapped sample sequence number of the first row.
    ``rows_dropped`` counts samples lost between this packet and the
    previous one (0 on a clean link). ``time`` is the ``time.monotonic()``
    arrival timestamp; ``payload_bytes`` is the full notification size
    (header + payload), for byte-rate metrics.
    """

    time: float
    ssn: int
    rows_dropped: int
    payload_bytes: int
    raw: np.ndarray

    @property
    def rows(self) -> int:
        return self.raw.shape[0]
