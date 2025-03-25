"""API for the dynamite sampler board.
The classes represent the different services the board exposes.

TODO: figure out how to abstract this file into separate repo.
"""

import struct


class DynamiteSampler:
    """Service that sends the ADC values (the force measurements).
    This service UUID advertised."""

    UUID = "e331016b-6618-4f8f-8997-1a2c7c9e5fa3"

    class ADCFeed:
        """Characteristic that streams the ADC values. Only has BLE Notifications."""

        UUID = "beb5483e-36e1-4688-b7f5-ea07361b26a8"

        class FeedData:
            """This is a reading sample that is transmitted. Each BLE notification is a
            concatination of multiple FeedDatas."""

            packed_bytes = 15  # FeedData is packed into 15 bytes.

            def __init__(
                self, status: int, ch0: int, ch1: int, ch2: int, ch3: int, crc: int
            ):
                self.status = status
                self.ch0 = ch0
                self.ch1 = ch1
                self.ch2 = ch2
                self.ch3 = ch3
                self.crc = crc

            @classmethod
            def _unpack_single(cls, b: bytearray | bytes):
                """Unpack a single Feed Data from bytes transmitted to its values."""
                assert len(b) == cls.packed_bytes

                status = int.from_bytes(b[0:2], byteorder="little", signed=False)
                # The ADC reading are big endian since that is what the ADC returns
                # and the firmware doesn't do any processing on it.
                ch0 = int.from_bytes(b[2:5], byteorder="big", signed=True)
                ch1 = int.from_bytes(b[5:8], byteorder="big", signed=True)
                ch2 = int.from_bytes(b[8:11], byteorder="big", signed=True)
                ch3 = int.from_bytes(b[11:14], byteorder="big", signed=True)

                crc = int.from_bytes(b[14:15], byteorder="little", signed=False)

                return cls(status, ch0, ch1, ch2, ch3, crc)

            @classmethod
            def unpack_multiple(cls, b: bytearray | bytes):
                """Unpack a notification packet which is a bunch of FeedData concatenated."""
                assert len(b) % cls.packed_bytes == 0

                feed_datas = []
                for start in range(0, len(b), cls.packed_bytes):
                    s = slice(start, start + cls.packed_bytes)
                    feed_datas.append(cls._unpack_single(b[s]))

                return feed_datas

        # class FeedData(ctypes.LittleEndianStructure):
        #     _pack_ = 1
        #     _fields_ = [
        #         ("status", ctypes.c_uint16),
        #         ("ch0", ctypes.c_uint8 * 3),
        #         ("ch1", ctypes.c_uint8 * 3),
        #         ("ch2", ctypes.c_uint8 * 3),
        #         ("ch3", ctypes.c_uint8 * 3),
        #         ("crc", ctypes.c_uint8),
        #     ]

    class LoadCellCalibration:
        """Characteristic that contains the calibration data. Read only."""

        # TODO - sync this with the calibration flashing script

        UUID = "10adce11-68a6-450b-9810-ca11b39fd283"

        _format = "II"

        @classmethod
        def generate_calibration_raw(cls, data1: int, data2: int) -> bytes:
            """Generate the raw bytes to be flashed to the calibration partition"""
            # TODO this format needs to be synced between this script and the client.
            return struct.pack(cls._format, data1, data2)

        @classmethod
        def unpack(cls, b: bytes | bytearray) -> tuple[int, int]:
            format_len = struct.calcsize(cls._format)
            assert len(b) >= format_len

            return struct.unpack(cls._format, b[0:format_len])


class OTA:
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


class DeviceInfo:
    """Read-only device info"""

    UUID = 0x180A

    class ManufacturerName:
        UUID = 0x2A29

    class FirmwareRevision:
        UUID = 0x2A26
