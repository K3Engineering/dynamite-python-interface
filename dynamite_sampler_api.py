"""API for the dynamite sampler board.
The classes represent the different services the board exposes.

TODO: figure out how to abstract this file into separate repo.
"""

import struct
from typing import Generic, TypeVar, ClassVar
import dataclasses


import ADS131M04Register


## Data classes that hold the parsed information from the characteristics
# Moved outside of the service characteristic classes to make it less nested.
# Unclear if thats the best way, TODO: decide where to put these. TODO: is dataclass a good idea?
@dataclasses.dataclass
class ADCConfigData:
    num_channels: int
    power_mode: str
    sample_rate: int
    gains: list[int]


@dataclasses.dataclass
class FeedData:
    """This is a reading sample that is transmitted. Each BLE notification is a
    concatination of multiple FeedDatas."""

    status: int
    ch0: int
    ch1: int
    ch2: int
    ch3: int
    crc: int


## BLE services and characteristics sturcture
class BLEService:
    UUID: str


class BLECharacteristic:
    UUID: str


_UnpackResultT = TypeVar("_UnpackResultT")


class BLECharacteristicRead(BLECharacteristic, Generic[_UnpackResultT]):
    """Base class for BLE characteristics that can be read."""

    @classmethod
    def unpack(b: bytearray | bytes) -> _UnpackResultT:
        """Parses raw characteristic data into some sort of object."""
        raise NotImplementedError("Subclasses must implement the unpack method.")


class DynamiteSampler(BLEService):
    """Service that sends the ADC values (the force measurements).
    This service's UUID is advertised, and can be used to filter scanning."""

    UUID = "e331016b-6618-4f8f-8997-1a2c7c9e5fa3"

    class ADCFeed(BLECharacteristicRead[FeedData]):
        """Characteristic that streams the ADC values. Only has BLE Notifications."""

        UUID = "beb5483e-36e1-4688-b7f5-ea07361b26a8"

        _packed_bytes: ClassVar[int] = 15  # FeedData is packed into 15 bytes.

        @staticmethod
        def _unpack_single(b: bytearray | bytes) -> FeedData:
            """Unpack a single Feed Data from bytes transmitted to its values."""
            assert len(b) == DynamiteSampler.ADCFeed._packed_bytes

            status = int.from_bytes(b[0:2], byteorder="little", signed=False)
            # The ADC reading are big endian since that is what the ADC returns
            # and the firmware doesn't do any processing on it.
            ch0 = int.from_bytes(b[2:5], byteorder="big", signed=True)
            ch1 = int.from_bytes(b[5:8], byteorder="big", signed=True)
            ch2 = int.from_bytes(b[8:11], byteorder="big", signed=True)
            ch3 = int.from_bytes(b[11:14], byteorder="big", signed=True)

            crc = int.from_bytes(b[14:15], byteorder="little", signed=False)

            return FeedData(status, ch0, ch1, ch2, ch3, crc)

        @staticmethod
        def unpack(b: bytearray | bytes) -> list[FeedData]:
            """Unpack a notification packet which is a bunch of FeedData concatenated."""
            packed_bytes = DynamiteSampler.ADCFeed._packed_bytes
            assert len(b) % packed_bytes == 0

            feed_datas = []
            for start in range(0, len(b), packed_bytes):
                s = slice(start, start + packed_bytes)
                feed_datas.append(DynamiteSampler.ADCFeed._unpack_single(b[s]))

            return feed_datas

    class LoadCellCalibration(BLECharacteristicRead[tuple]):
        """Characteristic that contains the calibration data. Read only."""

        # TODO - sync this with the calibration flashing script

        UUID = "10adce11-68a6-450b-9810-ca11b39fd283"

        _format = "II"

        @staticmethod
        def pack(data1: int, data2: int) -> bytes:
            """Generate the raw bytes to be flashed to the calibration partition"""
            # TODO this format needs to be synced between this script and the client.
            format = DynamiteSampler.LoadCellCalibration._format
            return struct.pack(format, int(data1), int(data2))

        @staticmethod
        def unpack(b: bytes | bytearray) -> tuple[int, int]:
            format = DynamiteSampler.LoadCellCalibration._format
            format_len = struct.calcsize(format)
            assert len(b) >= format_len

            return struct.unpack(format, b[0:format_len])

    class ADCConfig(BLECharacteristicRead[ADCConfigData]):
        """Characteristic (Read-only) of the ADC configuration values"""

        UUID = "adcc0f19-2575-4502-9a48-0e99974eb34f"

        @staticmethod
        def unpack(b: bytearray | bytes) -> ADCConfigData:
            """Unpack the BLE raw data"""
            version = b[0]
            assert version == 1, "Can't parse this version"

            num_ch = b[1]

            reg_id = ADS131M04Register.ID.from_buffer(b[2:4])
            reg_status = ADS131M04Register.Status.from_buffer(b[4:6])
            reg_mode = ADS131M04Register.Mode.from_buffer(b[6:8])
            reg_clock = ADS131M04Register.Clock.from_buffer(b[8:10])
            reg_gain = ADS131M04Register.Gain.from_buffer(b[10:12])

            # TODO: not all of the registers are exposed, so for now print all of them
            # for debug purposes.
            print(reg_id)
            print(reg_status)
            print(reg_mode)
            print(reg_clock)
            print(reg_gain)

            pow_mode_dict = {0: "VERY_LOW_POWER", 1: "LOW_POWER", 2: "HIGH_RESOLUTION"}
            pow_mode = pow_mode_dict[reg_clock.PWR]
            rate = 32000 // 2**reg_clock.OSR
            gains = [
                2**reg_gain.PGAGAIN0,
                2**reg_gain.PGAGAIN1,
                2**reg_gain.PGAGAIN2,
                2**reg_gain.PGAGAIN3,
            ]

            return ADCConfigData(num_ch, pow_mode, rate, gains)


class OTA(BLEService):
    UUID = "d6f1d96d-594c-4c53-b1c6-144a1dfde6d8"

    class Control:
        UUID = "7ad671aa-21c0-46a4-b722-270e3ae3d830"

        NOP = bytearray.fromhex("00")

        REQUEST = bytearray.fromhex("01")
        REQUEST_ACK = bytearray.fromhex("02")
        REQUEST_NAK = bytearray.fromhex("03")

        DONE = bytearray.fromhex("04")
        DONE_ACK = bytearray.fromhex("05")
        DONE_NAK = bytearray.fromhex("06")

    class Data:

        UUID = "23408888-1f40-4cd8-9b89-ca8d45f8a5b0"


class DeviceInfo(BLEService):
    """Read-only device info. The UUIDs are 16 bit hex."""

    UUID = "180A"

    class ManufacturerName(BLECharacteristicRead[str]):
        UUID = "2A29"

        @staticmethod
        def unpack(b: bytearray | bytes) -> str:
            return str(b, "utf-8")

    class FirmwareRevision(BLECharacteristicRead[str]):
        UUID = "2A26"

        @staticmethod
        def unpack(b: bytearray | bytes) -> str:
            return str(b, "utf-8")


## ADC converstion utility functions
# TODO: Have the function just return the scale factor and not do the converting.
def adc_reading_to_voltage(
    reading: int,
    adc_ref: float = 1.2,
    adc_gain: int = 4,
    opamp_gain: int = 1,
    adc_bits: int = 24,
) -> float:
    """Convert ADC reading to a voltage (in volts)"""
    fsr_adc_in = adc_ref / adc_gain  # volts
    lsb_adc_in = fsr_adc_in / 2 ** (adc_bits - 1)  # remember one is for sign
    voltage_adc_in = reading * lsb_adc_in
    voltage_op_amp_in = voltage_adc_in / opamp_gain

    return voltage_op_amp_in


def voltage_to_weight(
    value: float,
    loadcell_ratio: float = 2.0,
    fullscale: float = 200,
    voltage_in: float = 4,
) -> float:
    """Convert the mv reading from the ADC into a weight value on the loadcell.
    value: voltage in volts
    loadcell_ratio: mV/V value that is specified for loadcells
    fullscale: Loadcell's rated fullscale output
    """
    return value * fullscale / (loadcell_ratio / 1000 * voltage_in)
