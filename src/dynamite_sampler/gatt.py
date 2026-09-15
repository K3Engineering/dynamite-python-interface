"""GATT schema for the Dynamite Sampler.

Service and characteristic UUIDs, and the pack/unpack codecs for the few
characteristics that carry structured data. The ADC feed frame is a 2-byte
sample counter followed by concatenated 12-byte samples (4 channels x 3
bytes, signed little-endian).
"""

import dataclasses
import struct
from typing import ClassVar, Generic, TypeVar

from . import ads131m04
from .errors import ProtocolError


@dataclasses.dataclass
class ADCConfigData:
    num_channels: int
    power_mode: str
    sample_rate: int
    gains: list[int]


@dataclasses.dataclass
class FeedHeader:
    """Packet header prepended to each BLE ADC feed notification."""

    sample_sequence_number: int  # uint16, little-endian


@dataclasses.dataclass
class FeedData:
    """A single ADC sample."""

    ch0: int
    ch1: int
    ch2: int
    ch3: int


@dataclasses.dataclass
class FeedPacket:
    """A full BLE ADC feed notification: header + list of samples."""

    header: FeedHeader
    samples: list[FeedData]


class BLEService:
    UUID: str


class BLECharacteristic:
    UUID: str


_UnpackResultT = TypeVar("_UnpackResultT")
_PackType = TypeVar("_PackType")


class BLECharacteristicRead(BLECharacteristic, Generic[_UnpackResultT]):
    """Base class for BLE characteristics that can be read."""

    @classmethod
    def unpack(cls, b: bytearray | bytes) -> _UnpackResultT:
        raise NotImplementedError("Subclasses must implement the unpack method.")


class BLECharacteristicWrite(BLECharacteristic, Generic[_PackType]):
    """Base class for BLE characteristics that can be written."""

    @classmethod
    def pack(cls, data: _PackType) -> bytes | bytearray:
        raise NotImplementedError("Subclasses must implement the pack method.")


class DynamiteSamplerService(BLEService):
    """Service that sends the ADC values (the force measurements).
    Its UUID is advertised and used to filter scanning."""

    UUID = "e331016b-6618-4f8f-8997-1a2c7c9e5fa3"

    class ADCFeed(BLECharacteristicRead[FeedPacket]):
        """The ADC feed. Notifications only.

        Header: 2-byte sample counter. Payload: N x 12-byte samples
        (4 channels x 3 bytes, signed little-endian).
        """

        UUID = "beb5483e-36e1-4688-b7f5-ea07361b26a8"

        _HEADER_BYTES: ClassVar[int] = 2
        _SAMPLE_BYTES: ClassVar[int] = 12

        @classmethod
        def split(cls, b: bytearray | bytes) -> tuple[int, bytes]:
            """(ssn, sample payload). The payload is a whole number of samples."""
            if len(b) < cls._HEADER_BYTES:
                raise ProtocolError(f"ADC feed frame shorter than its header: {len(b)} B")
            payload = bytes(b[cls._HEADER_BYTES :])
            if len(payload) % cls._SAMPLE_BYTES != 0:
                raise ProtocolError(
                    f"ADC feed payload {len(payload)} B is not a whole number of "
                    f"{cls._SAMPLE_BYTES} B samples"
                )
            ssn = int.from_bytes(b[0 : cls._HEADER_BYTES], "little")
            return ssn, payload

        @classmethod
        def unpack(cls, b: bytearray | bytes) -> FeedPacket:
            """Per-sample dataclass parse (slow path; the device decodes arrays)."""
            ssn, payload = cls.split(b)
            samples = []
            for start in range(0, len(payload), cls._SAMPLE_BYTES):
                chunk = payload[start : start + cls._SAMPLE_BYTES]
                ch0, ch1, ch2, ch3 = struct.unpack("<iii", chunk[:12])
                # 3-byte fields: unpack as 4 bytes then shift would misread;
                # decode each explicitly.
                samples.append(
                    FeedData(
                        *(
                            int.from_bytes(chunk[i : i + 3], "little", signed=True)
                            for i in (0, 3, 6, 9)
                        )
                    )
                )
            return FeedPacket(FeedHeader(ssn), samples)

    class ADCConfig(BLECharacteristicRead[ADCConfigData]):
        """ADC configuration (read-only).

        Network format (little-endian, packed):
            version: uint8   [0]
            id:      uint16  [1:3]
            status:  uint16  [3:5]
            mode:    uint16  [5:7]
            clock:   uint16  [7:9]
            pga:     uint16  [9:11]
        """

        UUID = "adcc0f19-2575-4502-9a48-0e99974eb34f"

        @classmethod
        def unpack(cls, b: bytearray | bytes) -> ADCConfigData:
            if len(b) < 11:
                raise ProtocolError(f"ADC config frame too short: {len(b)} B")
            version = b[0]
            if version != 1:
                raise ProtocolError(f"Unsupported ADC config version: {version}")
            reg_id = ads131m04.ID.from_buffer(bytearray(b[1:3]))
            reg_clock = ads131m04.Clock.from_buffer(bytearray(b[7:9]))
            reg_gain = ads131m04.Gain.from_buffer(bytearray(b[9:11]))

            power_mode = {0: "VERY_LOW_POWER", 1: "LOW_POWER", 2: "HIGH_RESOLUTION"}[
                reg_clock.PWR
            ]
            rate = 32000 // 2**reg_clock.OSR
            gains = [
                2**reg_gain.PGAGAIN0,
                2**reg_gain.PGAGAIN1,
                2**reg_gain.PGAGAIN2,
                2**reg_gain.PGAGAIN3,
            ]
            return ADCConfigData(reg_id.CHANCNT, power_mode, rate, gains)


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


class TxPower(BLEService):
    UUID = "74788a4c-72aa-4180-a478-59e969b959c9"

    class TxPowerSet(BLECharacteristicWrite[int]):
        UUID = "7478c418-35d3-4c3d-99d9-2de090159664"

        @staticmethod
        def pack(power: int) -> bytes:
            """TX power as a signed int8 in dBm."""
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

    class HardwareRevision(BLECharacteristicRead[str]):
        """Board model, e.g. 'v700P'."""

        UUID = "2A27"

        @staticmethod
        def unpack(b: bytearray | bytes) -> str:
            return bytes(b).rstrip(b"\x00").decode("utf-8")

    class TxPowerLevel(BLECharacteristicRead[int]):
        UUID = "2A07"

        @staticmethod
        def unpack(b: bytearray | bytes) -> int:
            if len(b) != 1:
                raise ProtocolError("TX power must be a single int8 byte")
            return int.from_bytes(b, signed=True)


def adc_reading_to_voltage(
    reading: int,
    adc_ref: float = 1.2,
    adc_gain: int = 4,
    opamp_gain: int = 1,
    adc_bits: int = 24,
) -> float:
    """Legacy nominal ADC-to-voltage helper (stream.py's socket demo).

    Not the calibrated conversion; use ``Calibration.convert`` instead."""
    fsr_adc_in = adc_ref / adc_gain
    lsb_adc_in = fsr_adc_in / 2 ** (adc_bits - 1)
    voltage_adc_in = reading * lsb_adc_in
    return voltage_adc_in / opamp_gain


def voltage_to_weight(
    value: float,
    loadcell_ratio: float = 2.0,
    fullscale: float = 200,
    voltage_in: float = 4,
) -> float:
    """Legacy nominal voltage-to-weight helper (stream.py's socket demo)."""
    return value * fullscale / (loadcell_ratio / 1000 * voltage_in)
