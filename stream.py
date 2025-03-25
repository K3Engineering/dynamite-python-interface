#!/usr/bin/env python
"""Stream Dynamite sampler data to various locations"""
import asyncio
from datetime import datetime
import csv
import inspect

from tqdm import tqdm

# aint this confusing
import dynamite_sampler_api as ds
import dynamite_sampler_streamer as dss


class FeedDataCSVWriter(dss.NotifyCallbackFeeddatas):
    """This class writes FeedData to a CSV file"""

    def __init__(self, device_dict):
        # print("init csv writer", device_dict)

        start_time = datetime.now()
        date = start_time.strftime("%Y%m%d_%H%M%S")
        name = f"./data/datadump_{date}.txt"
        csv_file = open(name, "w", newline="")

        print("#", date, file=csv_file)
        print("#", device_dict, file=csv_file)

        fieldnames = inspect.getfullargspec(
            ds.DynamiteSampler.ADCFeed.FeedData.__init__
        ).args[1:]

        self.writer = csv.DictWriter(csv_file, fieldnames)
        self.writer.writeheader()

    def callback(self, feeddatas: list[ds.DynamiteSampler.ADCFeed.FeedData]):
        # print("callback in csv writer")
        for data in feeddatas:
            self.writer.writerow(data.__dict__)


class TQDMPbar(dss.NotifyCallbackRawData):
    """Use TQDM to show packet metrics"""

    def __init__(self, device_dict):
        self.pbar_packets = tqdm(
            desc="Total packets:", unit="packets", position=0, smoothing=1
        )
        self.pbar_bytes = tqdm(
            desc="Total bytes", unit="bytes", position=1, unit_scale=True, smoothing=1
        )

    def callback(self, rawdata: bytes):
        self.pbar_packets.update(1)
        self.pbar_bytes.update(len(rawdata))


asyncio.run(dss.dynamite_sampler_connect_notify([TQDMPbar], [FeedDataCSVWriter]))
