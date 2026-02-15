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
class FeedHeader:
    """Packet header prepended to each BLE ADC feed notification."""

    sample_sequence_number: int  # Running sample counter (uint16, little-endian)


@dataclasses.dataclass
class FeedData:
    """A single ADC sample. Each BLE notification contains a header followed
    by a concatenation of multiple FeedData samples."""

    ch0: int
    ch1: int
    ch2: int
    ch3: int


@dataclasses.dataclass
class FeedPacket:
    """A full BLE ADC feed notification: header + list of samples."""

    header: FeedHeader
    samples: list[FeedData]


## BLE services and characteristics sturcture
# Baseclasses and typing boiler plate stuff to make the acutal API a bit more readable.
class BLEService:
    UUID: str


class BLECharacteristic:
    UUID: str


_UnpackResultT = TypeVar("_UnpackResultT")
_PackType = TypeVar("_PackType")


class BLECharacteristicRead(BLECharacteristic, Generic[_UnpackResultT]):
    """Base class for BLE characteristics that can be read."""

    @classmethod
    def unpack(b: bytearray | bytes) -> _UnpackResultT:
        """Parses raw characteristic data into some sort of object."""
        raise NotImplementedError("Subclasses must implement the unpack method.")


class BLECharacteristicWrite(BLECharacteristic, Generic[_PackType]):
    """Base class for BLE characteristics that can be writen."""

    @classmethod
    def pack(data: _PackType) -> bytes | bytearray:
        """Pack data into bytes to send."""
        # The return type ideally would be collections.abc.Buffer, but that is new to 3.12.
        # bytes | bytearray should be good enough for now.
        raise NotImplementedError("Subclasses must implement the pack method.")


## Dynamite Sampler API classes


class DynamiteSampler(BLEService):
    """Service that sends the ADC values (the force measurements).
    This service's UUID is advertised, and can be used to filter scanning."""

    UUID = "e331016b-6618-4f8f-8997-1a2c7c9e5fa3"

    class ADCFeed(BLECharacteristicRead[FeedPacket]):
        """Characteristic that streams the ADC values. Only has BLE Notifications.

        Each notification is a packet with a 3-byte header followed by
        concatenated 12-byte ADC samples (4 channels x 3 bytes each)."""

        UUID = "beb5483e-36e1-4688-b7f5-ea07361b26a8"

        _header_bytes: ClassVar[int] = 2  # ssn (2B)
        _sample_bytes: ClassVar[int] = 12  # 4 channels x 3 bytes each

        @staticmethod
        def _unpack_header(b: bytearray | bytes) -> FeedHeader:
            """Unpack the packet header."""
            ssn = int.from_bytes(b[0:2], byteorder="little", signed=False)
            return FeedHeader(ssn)

        @staticmethod
        def _unpack_single(b: bytearray | bytes) -> FeedData:
            """Unpack a single ADC sample (4 channels, 3 bytes each, big-endian)."""
            assert len(b) == DynamiteSampler.ADCFeed._sample_bytes

            ch0 = int.from_bytes(b[0:3], byteorder="little", signed=True)
            ch1 = int.from_bytes(b[3:6], byteorder="little", signed=True)
            ch2 = int.from_bytes(b[6:9], byteorder="little", signed=True)
            ch3 = int.from_bytes(b[9:12], byteorder="little", signed=True)

            return FeedData(ch0, ch1, ch2, ch3)

        @staticmethod
        def unpack(b: bytearray | bytes) -> FeedPacket:
            """Unpack a notification packet: 2-byte header + N x 12-byte samples."""
            header_bytes = DynamiteSampler.ADCFeed._header_bytes
            sample_bytes = DynamiteSampler.ADCFeed._sample_bytes

            assert len(b) >= header_bytes
            header = DynamiteSampler.ADCFeed._unpack_header(b[:header_bytes])

            payload = b[header_bytes:]
            assert len(payload) % sample_bytes == 0

            samples = []
            for start in range(0, len(payload), sample_bytes):
                s = slice(start, start + sample_bytes)
                samples.append(DynamiteSampler.ADCFeed._unpack_single(payload[s]))

            return FeedPacket(header, samples)

    class LoadCellCalibration(BLECharacteristicRead[tuple]):
        """Characteristic that contains the calibration data. Read only.

        The firmware sends the entire calibration partition (255 bytes).
        The calibration values are stored in the first 8 bytes as two uint32."""

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
            # Firmware sends the full calibration partition (255 bytes);
            # the actual calibration data is in the first 8 bytes.
            assert len(b) >= format_len

            return struct.unpack(format, b[0:format_len])

    class ADCConfig(BLECharacteristicRead[ADCConfigData]):
        """Characteristic (Read-only) of the ADC configuration values.

        Network format (AdcConfigNetworkData, little-endian, packed):
            version: uint8   [0]
            id:      uint16  [1:3]
            status:  uint16  [3:5]
            mode:    uint16  [5:7]
            clock:   uint16  [7:9]
            pga:     uint16  [9:11]
        """

        UUID = "adcc0f19-2575-4502-9a48-0e99974eb34f"

        @staticmethod
        def unpack(b: bytearray | bytes) -> ADCConfigData:
            """Unpack the BLE raw data"""
            version = b[0]
            assert version == 1, "Can't parse this version"

            reg_id = ADS131M04Register.ID.from_buffer(bytearray(b[1:3]))
            reg_status = ADS131M04Register.Status.from_buffer(bytearray(b[3:5]))
            reg_mode = ADS131M04Register.Mode.from_buffer(bytearray(b[5:7]))
            reg_clock = ADS131M04Register.Clock.from_buffer(bytearray(b[7:9]))
            reg_gain = ADS131M04Register.Gain.from_buffer(bytearray(b[9:11]))

            # TODO: not all of the registers are exposed, so for now print all of them
            # for debug purposes.
            print(reg_id)
            print(reg_status)
            print(reg_mode)
            print(reg_clock)
            print(reg_gain)

            # num_channels is derived from the ID register's CHANCNT field
            num_ch = reg_id.CHANCNT

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
    # TODO - implement this and convert the OTA script to use this API
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


class TxPower(BLEService):
    UUID = "74788a4c-72aa-4180-a478-59e969b959c9"

    class TxPowerSet(BLECharacteristicWrite[int]):
        UUID = "7478c418-35d3-4c3d-99d9-2de090159664"

        @staticmethod
        def pack(power: int) -> bytes:
            """TX Power as a signed int8 in dBm"""
            return power.to_bytes(signed=True, length=1)


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

    class TxPowerLevel(BLECharacteristicRead[int]):
        UUID = "2A07"

        @staticmethod
        def unpack(b: bytearray | bytes) -> int:
            assert len(b) == 1, "TX power expected to be single int8 byte"
            return int.from_bytes(b, signed=True)


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
