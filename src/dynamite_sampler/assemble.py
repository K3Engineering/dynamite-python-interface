"""Assemble :class:`Block`s from :class:`Packet`s.

The device feed is a stream of :class:`Packet`s; a :class:`Block` is a
fixed-size window over the sample timeline. The assembler is public so a
script that needs both layers can drive its own fan-out loop: packets to
per-packet consumers, ``push`` results to block consumers.
"""

import numpy as np

from .block import Block
from .packet import Packet


class BlockAssembler:
    """Push packets, get fixed-size blocks.

    A block's rows come from one or more packets, and a packet straddling a
    block boundary is split. Missed samples (``Packet.rows_dropped``) become
    NaN rows counted in ``Block.rows_dropped``, so a block always spans
    exactly ``blocksize / sample_rate`` seconds of the sample timeline.

    ``push`` returns the blocks completed by that packet (usually ``[]`` or
    a single-element list; more when ``blocksize`` is smaller than a
    packet).
    """

    def __init__(
        self,
        calibration,
        sample_rate: int,
        blocksize: int,
        units: str = "raw",
        tare_raw=None,
    ):
        calibration.check_units(units)
        if blocksize < 1:
            raise ValueError("blocksize must be >= 1")
        self._calibration = calibration
        self._rate = sample_rate
        self._blocksize = blocksize
        self._units = units
        self._tare = tare_raw
        self._origin = None
        self._start_index = 0
        self._chunks = []  # (rows, is_gap, arrival_time)
        self._count = 0

    def push(self, packet: Packet) -> list[Block]:
        if self._origin is None:
            self._origin = packet.ssn
        channels = packet.raw.shape[1]
        if packet.rows_dropped:
            gap = np.full((packet.rows_dropped, channels), np.nan)
            self._chunks.append((gap, True, packet.time))
            self._count += packet.rows_dropped
        self._chunks.append((packet.raw, False, packet.time))
        self._count += packet.raw.shape[0]

        blocks = []
        while self._count >= self._blocksize:
            parts = []
            block_time = self._chunks[0][2]
            dropped = 0
            need = self._blocksize
            while need:
                head, is_gap, head_time = self._chunks[0]
                if head.shape[0] <= need:
                    take = head.shape[0]
                    parts.append(head)
                    self._chunks.pop(0)
                else:
                    take = need
                    parts.append(head[:take])
                    self._chunks[0] = (head[take:], is_gap, head_time)
                if is_gap:
                    dropped += take
                need -= take
            self._count -= self._blocksize
            blocks.append(self._make_block(np.concatenate(parts), block_time, dropped))
        return blocks

    def _make_block(self, block_raw, host_time, dropped) -> Block:
        data = self._calibration.convert(block_raw, self._units, self._tare)
        t = np.arange(self._start_index, self._start_index + block_raw.shape[0])
        t = t / self._rate
        block = Block(
            data=data,
            raw=block_raw,
            t=t,
            ssn0=int(self._origin + self._start_index),
            units=self._units,
            host_time=host_time,
            rows_dropped=dropped,
        )
        self._start_index += block_raw.shape[0]
        return block


async def blocks_from_packets(packets, assembler: BlockAssembler):
    """Async generator folding a packet stream into blocks.

    ``packets`` is e.g. :meth:`AsyncDynamiteSampler.stream_packets`; compose
    this instead of :meth:`AsyncDynamiteSampler.stream` when the packets are
    also needed themselves.
    """
    async for packet in packets:
        for block in assembler.push(packet):
            yield block
