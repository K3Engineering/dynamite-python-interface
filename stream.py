#!/usr/bin/env python
"""Stream Dynamite sampler data to various locations.

Each sink is a recipe over ``dev.stream()``: blocks in, side effect out.
Defaults to CSV + metrics + the TCP socket demo when nothing is selected.
"""

import argparse
import datetime
import socket
import time

import dynamite_sampler as dms
from dynamite_sampler import gatt as ds


class CsvSink:
    """Record the feed to a dynamite-csv 1 file (``CsvRecorder``)."""

    def __init__(self, dev, file_path_str: str = "", units: str = "raw"):
        if not file_path_str:
            date_str = datetime.datetime.now().strftime("%Y%m%d_%H%M%S")
            file_path_str = f"./data/feeddata_{date_str}.csv"
        self._recorder = dms.CsvRecorder(dev, file_path_str, units=units)

    def block(self, block):
        self._recorder.write_block(block)

    def close(self):
        self._recorder.close()


class MetricsSink:
    """Print sample-rate metrics on one line using \\r."""

    def __init__(self, print_dt: float = 0.1):
        self.print_dt = float(print_dt)
        self._start = time.monotonic()
        self._prev_print = self._start
        self._rows_at_print = 0
        self._rows = 0

    def block(self, block):
        now = time.monotonic()
        self._rows += block.raw.shape[0]
        interval = now - self._prev_print
        if interval > self.print_dt:
            rate = (self._rows - self._rows_at_print) / interval
            print(
                f"[{datetime.timedelta(seconds=now - self._start)}] "
                f"{self._rows:10} samples, {rate:6.1f} samples/sec ",
                end="\r",
            )
            self._prev_print = now
            self._rows_at_print = self._rows

    def close(self):
        print()


class TqdmSink:
    """Show sample progress with TQDM."""

    def __init__(self):
        from tqdm import tqdm

        self._bar = tqdm(desc="Samples", unit="samples")

    def block(self, block):
        self._bar.update(block.raw.shape[0])

    def close(self):
        self._bar.close()


class SocketSink:
    """Stream each raw channel to a TCP localhost socket.

    Intended for waveforms & the `read_from_tcp_4_ports.js` script: the
    receiver divides by the int32 scale factor sent once per port."""

    CONVERSIONS = ("adc", "volts_adc_ir", "volts_opamp_ir", "kg_with_opamp")

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

    def block(self, block):
        for row in block.raw:
            for server, value in zip(self._servers, row):
                # A NaN row is a dropped sample; zero is the receiver's gap marker.
                server.send(
                    (0 if value != value else int(value)).to_bytes(
                        4, "little", signed=True
                    )
                )

    def close(self):
        print("Closing server sockets")
        for server in self._servers:
            server.close()


def consume(dev, sinks, blocksize: int = 100) -> None:
    for block in dev.stream(blocksize=blocksize, units="raw"):
        for sink in sinks:
            sink.block(block)


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
        "--metrics", action="store_true", help="print sample-rate metrics"
    )
    parser.add_argument("--tqdm", action="store_true", help="show a TQDM sample bar")
    parser.add_argument(
        "--socket", action="store_true", help="stream to localhost sockets"
    )
    parser.add_argument(
        "--txpwr", type=int, default=None, help="set the BLE TX power of the board"
    )
    args = parser.parse_args()

    if not (args.metrics or args.tqdm or args.socket or args.csv is not None):
        args.metrics = args.socket = True
        args.csv = ""

    with dms.connect(args.address) as dev:
        if args.txpwr is not None:
            dev.set_tx_power(args.txpwr)

        sinks = []
        if args.csv is not None:
            sinks.append(CsvSink(dev, args.csv, units=args.units))
        if args.tqdm:
            sinks.append(TqdmSink())
        if args.metrics:
            sinks.append(MetricsSink())
        if args.socket:
            sinks.append(SocketSink(dev))

        try:
            consume(dev, sinks)
        except KeyboardInterrupt:
            print()
        finally:
            for sink in sinks:
                sink.close()


if __name__ == "__main__":
    main()
