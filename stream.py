#!/usr/bin/env python
"""Stream Dynamite sampler data to various locations"""
import argparse
import asyncio
import datetime
import collections
import time
import csv
import inspect
import socket
import json
import pathlib

from typing import Optional

import dynamite_sampler_api as ds
import dynamite_sampler_bleak_util as dsbu

# TODO add pretty class prints


class FeedDataCSVWriter(dsbu.NotifyCallbackFeeddatas):
    """This class writes FeedData to a CSV file"""

    def __init__(self, file_path_str: Optional[str] = None):
        if not file_path_str:
            # Use a default file path
            date_str = datetime.datetime.now().strftime("%Y%m%d_%H%M%S")
            file_path_str = f"./data/feeddata_{date_str}.csv"

        # resolve the path so .parent works properly
        self.file_path = pathlib.Path(file_path_str).resolve()

        # Make sure that the directory for the file exists, if it doesn't make it
        self.file_path.parent.mkdir(parents=True, exist_ok=True)

        # https://docs.python.org/3/library/csv.html#id4
        # for csvwriter, the newline="" has to be used for proper line ending quotes
        # Unclear if this is actually needed in this use case
        self.csv_file = open(self.file_path, "w", newline="")

    def setup(self, device_dict):
        print("#", "CSV setup:", datetime.datetime.now(), file=self.csv_file)
        print("#", device_dict, file=self.csv_file)

        fieldnames = inspect.getfullargspec(ds.FeedData.__init__).args[1:]

        self.writer = csv.DictWriter(self.csv_file, fieldnames)
        self.writer.writeheader()

    def callback(self, header: ds.FeedHeader, feeddatas: list[ds.FeedData]):
        # print("callback in csv writer")
        for data in feeddatas:
            self.writer.writerow(data.__dict__)

    def cleanup(self):
        print("Closing csv file")
        self.csv_file.close()


class TQDMPbar(dsbu.NotifyCallbackRawData):
    """Use TQDM to show packet metrics"""

    def __init__(self):
        # Import tqdm inside the class so that if the class isn't used, the import is
        # optional.
        from tqdm import tqdm

        self.tqdm = tqdm

    def setup(self, device_dict):
        self.pbar_packets = self.tqdm(
            desc="Total packets", unit="packets", position=0, smoothing=1
        )
        self.pbar_bytes = self.tqdm(
            desc="Total bytes", unit="bytes", position=1, unit_scale=True, smoothing=1
        )

    def callback(self, rawdata: bytes):
        self.pbar_packets.update(1)
        self.pbar_bytes.update(len(rawdata))

    def cleanup(self):
        self.pbar_bytes.close()
        self.pbar_packets.close()


class MetricsPrinter(dsbu.NotifyCallbackRawData):
    """Print metrics on the same line using \r"""

    def __init__(self, n_sample_avg: int = 15, print_dt: float = 0.1):
        """
        n_sample_avg: how many sample raw sample metrics to average
        print_dt:   [Seconds] The minimum time between printing the delta.
                    This makes the metrics more readable since it doesn't flicker as often.
        """
        self.queue_len = int(n_sample_avg)
        self.print_dt = float(print_dt)

    def setup(self, device_dict):
        self.prev_time = time.time()  # The previous time a callback was called
        self.prev_time_print = 0.0  # Previous time when a metric was printed
        self.total_packets: int = 0
        self.total_bytes: int = 0
        self.start_time = None  # When the first callback was called

        # Save the past N metrics for averaging. There is always one packet per call,
        # so no need to save that.
        self.q_dt = collections.deque((), self.queue_len)
        # self.q_packets = collections.deque((), queue_len)
        self.q_bytes = collections.deque((), self.queue_len)

    def callback(self, rawdata):
        ## Time calculations
        cur_time = time.time()
        if not self.start_time:
            self.start_time = cur_time

        dt = cur_time - self.prev_time  # Delta between calls
        self.prev_time = cur_time  # update for the next call

        ## Update values
        rawdata_len = len(rawdata)

        self.total_packets += 1
        self.total_bytes += rawdata_len

        self.q_dt.append(dt)
        # self.q_packets.append(1)  # its always one packet
        self.q_bytes.append(rawdata_len)

        ## Metric calculations, update less frequently to make it easier to read
        if cur_time - self.prev_time_print > self.print_dt:
            self.prev_time_print = cur_time
            elapsed_time = datetime.timedelta(seconds=cur_time - self.start_time)

            avg_dt = sum(self.q_dt) / len(self.q_dt)
            avg_packets = 1
            avg_bytes = sum(self.q_bytes) / len(self.q_bytes)

            metric_str = (
                f"[{elapsed_time}] "
                f"Avg {len(self.q_dt)} samples, "
                f"dt: {avg_dt * 1000:5.1f}ms, "
                f"{self.total_packets:10} packets, "
                f"{avg_packets / avg_dt:6.1f} packet/sec, "
                f"{avg_bytes:3} bytes/packet, "
                f"{avg_bytes / avg_dt:5.1f} bytes/sec "
            )

            print(metric_str, end="\r")

    def cleanup(self):
        print()
        print("cleaned up printer")


class SocketStream(dsbu.NotifyCallbackFeeddatas):
    """Stream each channel to a TCP localhost socket.
    Intended for to be used with waveforms & the `read_from_tcp_4_ports.js` script."""

    def __init__(
        self, ports: Optional[list[int]] = None, conversion: str = "volts_adc_ir"
    ):
        self.ports = ports
        if not self.ports:
            self.ports = [8090, 8091, 8092, 8093]
        assert len(set(self.ports)) == 4, "There needs to be 4 ports specified"

        self.conversion_str = conversion
        self.servers: list[socket.socket] = []

    def setup(self, device_dict):
        input("Press enter to start socket connections")
        for port in self.ports:
            print(f"waiting socket {port}")
            s = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
            s.connect(("localhost", port))
            self.servers.append(s)
            print(f"socket connected {port}")

        # TODO make this a bit less hacky, more versatile
        if device_dict["ADCConfig"]:
            adc_gains = device_dict["ADCConfig"].gains
        else:
            adc_gains = [1, 4, 4, 1]
        print("Sending gains:", adc_gains)

        for server, gain in zip(self.servers, adc_gains):
            conversion_funcs = {
                "adc": lambda x: x,
                "volts_adc_ir": lambda x: ds.adc_reading_to_voltage(x, adc_gain=gain),
                "volts_opamp_ir": lambda x: ds.adc_reading_to_voltage(
                    x, adc_gain=gain, opamp_gain=26
                ),
                "kg_with_opamp": lambda x: ds.voltage_to_weight(
                    ds.adc_reading_to_voltage(x, adc_gain=gain, opamp_gain=26)
                ),
            }

            self.converstion_func = conversion_funcs[self.conversion_str]

            # Send the scaling factor by which to divide the values to get the selected units.
            scale_factor = int(1 / self.converstion_func(1))
            print(
                "Sending scale factor:", scale_factor, "to socket", server.getsockname()
            )
            server.send(scale_factor.to_bytes(4, "little", signed=True))

    def callback(self, header, feeddatas):
        for data in feeddatas:
            for server, ch_val in zip(
                self.servers, (data.ch0, data.ch1, data.ch2, data.ch3)
            ):
                bytes_to_send = ch_val.to_bytes(4, "little", signed=True)
                server.send(bytes_to_send)

    def cleanup(self):
        print("Closing server sockets")
        for server in self.servers:
            server.close()


def gen_append_class_init(cls):
    class AppendClassInit(argparse.Action):
        def __call__(self, parser, namespace, values, option_string=None):
            if getattr(namespace, self.dest) is None:
                setattr(namespace, self.dest, [])

            getattr(namespace, self.dest).append(cls(**json.loads(values)))

    return AppendClassInit


if __name__ == "__main__":
    # WIP argparser. Haven't figured out the best syntax for this script.
    # This is something that works
    parser = argparse.ArgumentParser(description=__doc__)
    # TODO add help about the json input

    arg_classes = [
        ("--metrics", MetricsPrinter, "callbacks_rawdata"),
        ("--tqdm", TQDMPbar, "callbacks_rawdata"),
        ("--socket", SocketStream, "callbacks_feeddata"),
        ("--csv", FeedDataCSVWriter, "callbacks_feeddata"),
    ]

    for flag, cls, dest in arg_classes:
        # TODO add help to the arguments
        # each argument will append a class instance to the dest.
        # optionaly each flag can take in a string json that will be parsed and passed
        # into the initializer as keyword args.
        parser.add_argument(
            flag,
            action=gen_append_class_init(cls),
            dest=dest,
            nargs="?",  # 0 or 1 arguments
            const="{}",  # if 0 arguments pass in empty dict
            default=[],  # if no arguments in dest, make it an empty list
        )

    parser.add_argument(
        "--txpwr", default=None, type=int, help="Set the tx power of the board"
    )

    args = parser.parse_args()

    if args.callbacks_rawdata == [] and args.callbacks_feeddata == []:
        print("No callbacks selected; adding the following:")
        callbacks_rawdata = [MetricsPrinter()]
        callbacks_feeddata = [FeedDataCSVWriter(), SocketStream()]
        print(callbacks_rawdata)
        print(callbacks_feeddata)
    else:
        callbacks_rawdata = args.callbacks_rawdata
        callbacks_feeddata = args.callbacks_feeddata

    asyncio.run(
        dsbu.dynamite_sampler_connect_notify(
            callbacks_rawdata, callbacks_feeddata, tx_power=args.txpwr
        )
    )
