#!/usr/bin/env python
"""Stream Dynamite sampler data to various locations"""
import asyncio
import datetime
import collections
import time
import csv
import inspect
import socket

# aint this confusing
import dynamite_sampler_api as ds
import dynamite_sampler_streamer as dss


class FeedDataCSVWriter(dss.NotifyCallbackFeeddatas):
    """This class writes FeedData to a CSV file"""

    def __init__(self):
        pass  # TODO

    def setup(self, device_dict):
        # print("init csv writer", device_dict)

        start_time = datetime.datetime.now()
        date = start_time.strftime("%Y%m%d_%H%M%S")
        name = f"./data/datadump_{date}.txt"
        self.csv_file = open(name, "w", newline="")

        print("#", date, file=self.csv_file)
        print("#", device_dict, file=self.csv_file)

        fieldnames = inspect.getfullargspec(
            ds.DynamiteSampler.ADCFeed.FeedData.__init__
        ).args[1:]

        self.writer = csv.DictWriter(self.csv_file, fieldnames)
        self.writer.writeheader()

    def callback(self, feeddatas: list[ds.DynamiteSampler.ADCFeed.FeedData]):
        # print("callback in csv writer")
        for data in feeddatas:
            self.writer.writerow(data.__dict__)

    def cleanup(self):
        print("Closing csv file")
        self.csv_file.close()


class TQDMPbar(dss.NotifyCallbackRawData):
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


class MetricsPrinter(dss.NotifyCallbackRawData):
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
                f"dt: {avg_dt*1000:5.1f}ms, "
                f"{self.total_packets:10} packets, "
                f"{avg_packets/avg_dt:6.1f} packet/dt, "
                f"{avg_bytes:3} bytes/packet, "
                f"{avg_bytes/avg_dt:5.1f} bytes/dt "
            )

            print(metric_str, end="\r")

    def cleanup(self):
        print()
        print("cleaned up printer")


class SocketStream(dss.NotifyCallbackFeeddatas):
    """Stream each channel to a TCP localhost socket.
    Intended for to be used with waveforms & the `read_from_tcp_4_ports.js` script."""

    def __init__(self, ports=None):
        self.ports = ports
        if not self.ports:
            self.ports = [8080, 8081, 8082, 8083]

    def setup(self, device_dict):
        self.servers: list[socket.socket] = []
        input("Press enter to start socket connections")
        for port in self.ports:
            print(f"waiting socket {port}")
            s = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
            s.connect(("localhost", port))
            self.servers.append(s)
            print(f"socket connected {port}")

    def callback(self, feeddatas):
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


# TODO make a argparse interface
# callbacks_raw = [TQDMPbar()]
# callbacks_feeddata = [FeedDataCSVWriter, SocketStream]
callbacks_raw = [MetricsPrinter()]
callbacks_feeddata = [FeedDataCSVWriter()]
asyncio.run(dss.dynamite_sampler_connect_notify(callbacks_raw, callbacks_feeddata))
