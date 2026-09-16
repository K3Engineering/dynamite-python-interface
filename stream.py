#!/usr/bin/env python
"""Stream Dynamite sampler data to various locations.

Data sinks are recipes over blocks (blocks in, side effect out); metrics
sinks ride the packet layer beneath them, fed from the same fan-out loop.
Defaults to CSV + metrics + the TCP socket demo when nothing is selected.
"""

import argparse
import asyncio
import datetime
import socket
from typing import Protocol, TypeVar

import numpy as np

import dynamite_sampler as dms
from dynamite_sampler import gatt as ds

T = TypeVar("T")


class Sink(Protocol[T]):
    """Receives stream items one by one; ``close`` on shutdown."""

    def handle(self, item: T) -> None: ...

    def close(self) -> None: ...


class CsvSink:
    """Record the feed to a dynamite-csv 1 file (``CsvRecorder``)."""

    def __init__(self, dev, file_path_str: str = "", units: str = "raw"):
        if not file_path_str:
            date_str = datetime.datetime.now().strftime("%Y%m%d_%H%M%S")
            file_path_str = f"./data/feeddata_{date_str}.csv"
        self._recorder = dms.CsvRecorder(dev, file_path_str, units=units)

    def handle(self, block):
        self._recorder.write_block(block)

    def close(self):
        self._recorder.close()


class TqdmSink:
    """Show live sample count and rate with TQDM (elapsed time, smoothed
    samples/sec, and the single-line \\r display are all built in)."""

    def __init__(self):
        from tqdm import tqdm

        self._bar = tqdm(desc="Samples", unit="samples")

    def handle(self, block):
        self._bar.update(block.raw.shape[0])

    def close(self):
        self._bar.close()


class MetricsSink:
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

    def handle(self, packet):
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

    def close(self):
        if self._packets:
            print()


class SocketSink:
    """Stream each raw channel to a TCP localhost socket.

    Intended for waveforms & the `read_from_tcp_4_ports.js` script: the
    receiver divides by the int32 scale factor sent once per port."""

    CONVERSIONS = ("adc", "volts_adc_ir", "volts_opamp_ir", "kg_with_opamp")
    _ZERO = (0).to_bytes(4, "little", signed=True)

    def __init__(self, dev, ports=None, conversion: str = "volts_adc_ir"):
        self.ports = ports or [8090, 8091, 8092, 8093]
        if len(set(self.ports)) != 4:
            raise ValueError("There need to be 4 distinct ports")

        input("Press enter to start socket connections")
        self._servers = []
        for port in self.ports:
            print(f"waiting socket {port}")
            s = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
            s.connect(("localhost", port))
            self._servers.append(s)
            print(f"socket connected {port}")

        gains = dev.gains or [1, 1, 1, 1]
        print("Sending gains:", gains)
        for server, gain in zip(self._servers, gains):
            scale_factor = int(1 / self._conversion(conversion, gain)(1))
            print(
                "Sending scale factor:", scale_factor, "to socket", server.getsockname()
            )
            server.send(scale_factor.to_bytes(4, "little", signed=True))

    @staticmethod
    def _conversion(conversion, adc_gain):
        return {
            "adc": lambda x: x,
            "volts_adc_ir": lambda x: ds.adc_reading_to_voltage(x, adc_gain=adc_gain),
            "volts_opamp_ir": lambda x: ds.adc_reading_to_voltage(
                x, adc_gain=adc_gain, opamp_gain=26
            ),
            "kg_with_opamp": lambda x: ds.voltage_to_weight(
                ds.adc_reading_to_voltage(x, adc_gain=adc_gain, opamp_gain=26)
            ),
        }[conversion]

    def handle(self, block):
        for row in block.raw:
            if np.isnan(row[0]):
                # A NaN row is a dropped sample; zero is the receiver's
                # gap marker.
                for server in self._servers:
                    server.send(self._ZERO)
                continue
            for server, value in zip(self._servers, row):
                server.send(int(value).to_bytes(4, "little", signed=True))

    def close(self):
        print("Closing server sockets")
        for server in self._servers:
            server.close()


async def consume(dev, data_sinks, packet_sinks, blocksize: int = 100) -> None:
    """The fan-out loop: packets to packet sinks, assembled blocks to data
    sinks."""
    assembler = dms.BlockAssembler(
        dev.require_calibration(),
        dev.sample_rate,
        blocksize,
        units="raw",
        tare_raw=dev.tare_raw,
    )
    async for packet in dev.stream_packets():
        for sink in packet_sinks:
            sink.handle(packet)
        for block in assembler.push(packet):
            for sink in data_sinks:
                sink.handle(block)


async def amain(args) -> None:
    async with await dms.AsyncDynamiteSampler.connect(args.address) as dev:
        if args.txpwr is not None:
            await dev.set_tx_power(args.txpwr)

        data_sinks = []
        packet_sinks = []
        if args.csv is not None:
            data_sinks.append(CsvSink(dev, args.csv, units=args.units))
        if args.tqdm:
            data_sinks.append(TqdmSink())
        if args.socket:
            data_sinks.append(SocketSink(dev, conversion=args.conversion))
        if args.metrics:
            packet_sinks.append(MetricsSink())

        try:
            await consume(dev, data_sinks, packet_sinks)
        finally:
            for sink in data_sinks + packet_sinks:
                sink.close()


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
        choices=SocketSink.CONVERSIONS,
        default="volts_adc_ir",
        help="unit conversion the socket receiver should divide out "
        "(default: %(default)s)",
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
