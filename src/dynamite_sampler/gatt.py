"""GATT schema for the Dynamite Sampler.

Service and characteristic UUIDs, and the pack/unpack codecs for the few
characteristics that carry structured data. The ADC feed frame is a 2-byte
sample counter followed by concatenated 12-byte samples (4 channels x 3
bytes, signed little-endian).
"""

import dataclasses
from typing import ClassVar

from .errors import ProtocolError


@dataclasses.dataclass
class ADCConfigData:
    num_channels: int
    power_mode: str
    sample_rate: int
    gains: list[int]


class BLEService:
    UUID: str


class BLECharacteristic:
    UUID: str


class DynamiteSamplerService(BLEService):
    """Service that sends the ADC values (the force measurements).
    Its UUID is advertised and used to filter scanning."""

    UUID = "e331016b-6618-4f8f-8997-1a2c7c9e5fa3"

    class ADCFeed(BLECharacteristic):
        """The ADC feed. Notifications only.

        Header: 2-byte sample counter. Payload: N x 12-byte samples
        (4 channels x 3 bytes, signed little-endian).
        """

        UUID = "beb5483e-36e1-4688-b7f5-ea07361b26a8"

        HEADER_BYTES: ClassVar[int] = 2
        SAMPLE_BYTES: ClassVar[int] = 12

        @classmethod
        def split(cls, b: bytearray | bytes) -> tuple[int, bytes]:
            """(ssn, sample payload). The payload is a whole number of samples."""
            if len(b) < cls.HEADER_BYTES:
                raise ProtocolError(
                    f"ADC feed frame shorter than its header: {len(b)} B"
                )
            payload = bytes(b[cls.HEADER_BYTES :])
            if len(payload) % cls.SAMPLE_BYTES != 0:
                raise ProtocolError(
                    f"ADC feed payload {len(payload)} B is not a whole number of "
                    f"{cls.SAMPLE_BYTES} B samples"
                )
            ssn = int.from_bytes(b[0 : cls.HEADER_BYTES], "little")
            return ssn, payload

    class ADCConfig(BLECharacteristic):
        """ADC configuration (read-only).

        Network format (little-endian, packed):
            version: uint8   [0]
            id:      uint16  [1:3]
            status:  uint16  [3:5]
            mode:    uint16  [5:7]
            clock:   uint16  [7:9]
            pga:     uint16  [9:11]

        Only four ADS131M04 register fields are used (datasheet bit
        ranges): id.CHANCNT [11:8]; clock.PWR [1:0] and clock.OSR [4:2];
        pga.PGAGAIN of channel i at bits [4i+2:4i].
        """

        UUID = "adcc0f19-2575-4502-9a48-0e99974eb34f"

        @classmethod
        def unpack(cls, b: bytearray | bytes) -> ADCConfigData:
            if len(b) < 11:
                raise ProtocolError(f"ADC config frame too short: {len(b)} B")
            version = b[0]
            if version != 1:
                raise ProtocolError(f"Unsupported ADC config version: {version}")
            reg_id = int.from_bytes(b[1:3], "little")
            reg_clock = int.from_bytes(b[7:9], "little")
            reg_gain = int.from_bytes(b[9:11], "little")

            power_mode = {0: "VERY_LOW_POWER", 1: "LOW_POWER", 2: "HIGH_RESOLUTION"}[
                reg_clock & 0b11
            ]
            rate = 32000 // 2 ** ((reg_clock >> 2) & 0b111)
            gains = [2 ** ((reg_gain >> (4 * i)) & 0b111) for i in range(4)]
            return ADCConfigData((reg_id >> 8) & 0xF, power_mode, rate, gains)


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

    class TxPowerSet(BLECharacteristic):
        UUID = "7478c418-35d3-4c3d-99d9-2de090159664"

        @staticmethod
        def pack(power: int) -> bytes:
            """TX power as a signed int8 in dBm."""
            return power.to_bytes(signed=True, length=1)


class DeviceInformation(BLEService):
    """Read-only device info. The UUIDs are 16 bit hex."""

    UUID = "180A"

    class ManufacturerName(BLECharacteristic):
        UUID = "2A29"

        @staticmethod
        def unpack(b: bytearray | bytes) -> str:
            return str(b, "utf-8")

    class ModelNumber(BLECharacteristic):
        """Marketing name, from the flashed board identity."""

        UUID = "2A24"

        @staticmethod
        def unpack(b: bytearray | bytes) -> str:
            return bytes(b).rstrip(b"\x00").decode("utf-8")

    class SerialNumber(BLECharacteristic):
        """Serial number string (the eFuse MAC hex)."""

        UUID = "2A25"

        @staticmethod
        def unpack(b: bytearray | bytes) -> str:
            return bytes(b).rstrip(b"\x00").decode("utf-8")

    class FirmwareRevision(BLECharacteristic):
        UUID = "2A26"

        @staticmethod
        def unpack(b: bytearray | bytes) -> str:
            return str(b, "utf-8")

    class HardwareRevision(BLECharacteristic):
        """Board model, e.g. 'v700P'."""

        UUID = "2A27"

        @staticmethod
        def unpack(b: bytearray | bytes) -> str:
            return bytes(b).rstrip(b"\x00").decode("utf-8")

    class TxPowerLevel(BLECharacteristic):
        UUID = "2A07"

        @staticmethod
        def unpack(b: bytearray | bytes) -> int:
            if len(b) != 1:
                raise ProtocolError("TX power must be a single int8 byte")
            return int.from_bytes(b, signed=True)
