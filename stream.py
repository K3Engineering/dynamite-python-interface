#!/usr/bin/env python
"""Stream Dynamite sampler data to various locations.

The flags select sinks; a sink is a callable taking one block (or packet),
its resources held as context managers for the run of the stream. Metrics
ride the per-packet layer beneath the blocks, fed from the same fan-out
loop. Defaults to CSV + metrics + the TCP socket demo when nothing is
selected.
"""

import argparse
import asyncio
import contextlib
import datetime
import socket
from typing import Callable

import numpy as np

import dynamite_sampler as dms


def adc_reading_to_voltage(reading, adc_ref=1.2, adc_gain=4, opamp_gain=1, adc_bits=24):
    """Nominal ADC counts -> volts (demo scale factors only; the calibrated
    conversion is ``Calibration.convert``)."""
    lsb_adc_in = (adc_ref / adc_gain) / 2 ** (adc_bits - 1)
    return reading * lsb_adc_in / opamp_gain


def csv_recording_path(file_path_str: str) -> str:
    """The given path, or a timestamped default under ./data."""
    if file_path_str:
        return file_path_str
    date_str = datetime.datetime.now().strftime("%Y%m%d_%H%M%S")
    return f"./data/feeddata_{date_str}.csv"


class LinkMetrics:
    """Print link-health metrics on one \\r line, from the packet layer:
    packets/sec, bytes/sec, rows/sec, and dropped rows."""

    def __init__(self, print_dt: float = 0.5):
        self._print_dt = print_dt
        self._start = None
        self._last_print = 0.0
        self._packets = 0
        self._bytes = 0
        self._rows = 0
        self._dropped = 0

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        if self._packets:
            print()

    def update(self, packet: dms.Packet) -> None:
        if self._start is None:
            self._start = packet.time
            self._last_print = packet.time
        self._packets += 1
        self._bytes += packet.payload_bytes
        self._rows += packet.rows
        self._dropped += packet.rows_dropped
        if packet.time - self._last_print < self._print_dt:
            return
        self._last_print = packet.time
        elapsed = packet.time - self._start
        if elapsed <= 0:
            return
        print(
            f"[{datetime.timedelta(seconds=int(elapsed))}] "
            f"{self._packets / elapsed:6.1f} packets/s, "
            f"{self._bytes / elapsed:7.0f} B/s, "
            f"{self._rows / elapsed:7.1f} rows/s, "
            f"{self._dropped} dropped rows",
            end="\r",
        )


class WaveformsSocket:
    """Stream each raw channel to a TCP localhost socket.

    Intended for waveforms & the `read_from_tcp_4_ports.js` script: the
    receiver divides by the int32 scale factor sent once per port. The
    connection handshake (prompt, then four connects) runs on enter."""

    CONVERSIONS = ("adc", "volts_adc_ir", "volts_opamp_ir")
    _ZERO = (0).to_bytes(4, "little", signed=True)

    def __init__(self, dev, ports=None, conversion: str = "volts_adc_ir"):
        self._dev = dev
        self.ports = ports or [8090, 8091, 8092, 8093]
        self._conversion = conversion
        if len(set(self.ports)) != 4:
            raise ValueError("There need to be 4 distinct ports")
        self._servers = []

    def __enter__(self):
        input("Press enter to start socket connections")
        for port in self.ports:
            print(f"waiting socket {port}")
            s = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
            s.connect(("localhost", port))
            self._servers.append(s)
            print(f"socket connected {port}")

        gains = self._dev.gains or [1, 1, 1, 1]
        print("Sending gains:", gains)
        for server, gain in zip(self._servers, gains):
            scale_factor = int(1 / self._convert(self._conversion, gain)(1))
            print(
                "Sending scale factor:", scale_factor, "to socket", server.getsockname()
            )
            server.send(scale_factor.to_bytes(4, "little", signed=True))
        return self

    def __exit__(self, *exc):
        print("Closing server sockets")
        for server in self._servers:
            server.close()

    @staticmethod
    def _convert(conversion, adc_gain):
        return {
            "adc": lambda x: x,
            "volts_adc_ir": lambda x: adc_reading_to_voltage(x, adc_gain=adc_gain),
            "volts_opamp_ir": lambda x: adc_reading_to_voltage(
                x, adc_gain=adc_gain, opamp_gain=26
            ),
        }[conversion]

    def send(self, block: dms.Block) -> None:
        for row in block.raw:
            if np.isnan(row[0]):
                # A NaN row is a dropped sample; zero is the receiver's
                # gap marker.
                for server in self._servers:
                    server.send(self._ZERO)
                continue
            for server, value in zip(self._servers, row):
                server.send(int(value).to_bytes(4, "little", signed=True))


async def consume(dev, on_packet, on_block, blocksize: int = 100) -> None:
    """The fan-out loop: packets to the packet sinks, assembled blocks to
    the block sinks."""
    assembler = dev.assembler(blocksize)
    async for packet in dev.stream_packets():
        for fn in on_packet:
            fn(packet)
        for block in assembler.push(packet):
            for fn in on_block:
                fn(block)


async def amain(args) -> None:
    async with dms.aconnect(args.address) as dev:
        if args.txpwr is not None:
            await dev.set_tx_power(args.txpwr)

        on_packet: list[Callable[[dms.Packet], None]] = []
        on_block: list[Callable[[dms.Block], None]] = []
        with contextlib.ExitStack() as stack:
            if args.csv is not None:
                recorder = stack.enter_context(
                    dms.CsvRecorder(dev, csv_recording_path(args.csv), units=args.units)
                )
                on_block.append(recorder.write)
            if args.tqdm:
                from tqdm import tqdm

                bar = stack.enter_context(tqdm(desc="Samples", unit="samples"))
                on_block.append(lambda block: bar.update(len(block)))
            if args.socket:
                socket_sink = WaveformsSocket(dev, conversion=args.conversion)
                on_block.append(stack.enter_context(socket_sink).send)
            if args.metrics:
                on_packet.append(stack.enter_context(LinkMetrics()).update)

            await consume(dev, on_packet, on_block, blocksize=args.blocksize)


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--address",
        help="BLE address of the device (default: auto-detect, only one may be in range)",
    )
    parser.add_argument(
        "--csv",
        nargs="?",
        const="",
        default=None,
        help="record the feed to a dynamite-csv file "
        "(default path when no value is given)",
    )
    parser.add_argument(
        "--units",
        default="raw",
        help="converted unit for the CSV recording (raw, mV/V, mV, kgf, N, kN, lbf)",
    )
    parser.add_argument(
        "--metrics",
        action="store_true",
        help="show live link metrics (packets/sec, bytes/sec, dropped rows)",
    )
    parser.add_argument("--tqdm", action="store_true", help="show a TQDM sample bar")
    parser.add_argument(
        "--socket", action="store_true", help="stream to localhost sockets"
    )
    parser.add_argument(
        "--conversion",
        choices=WaveformsSocket.CONVERSIONS,
        default="volts_adc_ir",
        help="unit conversion the socket receiver should divide out "
        "(default: %(default)s)",
    )
    parser.add_argument(
        "--blocksize",
        type=int,
        default=100,
        help="rows per assembled block (default: %(default)s)",
    )
    parser.add_argument(
        "--txpwr", type=int, default=None, help="set the BLE TX power of the board"
    )
    args = parser.parse_args()

    if not (args.metrics or args.tqdm or args.socket or args.csv is not None):
        args.metrics = args.socket = True
        args.csv = ""

    try:
        asyncio.run(amain(args))
    except KeyboardInterrupt:
        print()


if __name__ == "__main__":
    main()
