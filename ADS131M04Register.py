"""Python classes defining registers for the ADS131M0, in little endian as exposed by
the dynamite sampler board. They are in a separate file to keep the dynamite_sampler_api
more streamlined, since the registers are a bunch of verbose boiler plate.

_pack_ = 1 # Ensures tight packing with no padding
The fields are defined from MSB to LSB (like the datasheet),
and then reveresed (to match what ctypes.LittleEndianStructure expects).
"""

import ctypes


## ADS131 M04 registers
# _pack_ = 1 # Ensures tight packing with no padding
# The fields are defined from MSB to LSB (like the datasheet),
# and then reveresed (to match what ctypes.LittleEndianStructure expects).


# TODO: currently the registers only parse the raw bytes, but doesn't interpret what
# the fields mean. Consider adding that maybe?


class ADCRegisterBase(ctypes.LittleEndianStructure):
    """Base class for all registers so that they have the same way of representing the
    register values.
    TODO: Not sure if this is the best way, it is just for debuging for now."""

    def __repr__(self):
        fields = []
        for name, _, bits in reversed(self._fields_):
            if name != "RESERVED":
                value = self.__getattribute__(name)
                value_bin = format(value, f"0{bits}b")
                fields.append(f"{name}=0b{value_bin}")

        init_str = ", ".join(fields)
        return f"{type(self).__name__}({init_str})"


class ID(ADCRegisterBase):
    _pack_ = 1
    _fields_ = tuple(
        reversed(
            (
                ("RESERVED", ctypes.c_uint8, 4),
                ("CHANCNT", ctypes.c_uint8, 4),
                ("RESERVED", ctypes.c_uint8, 8),
            )
        )
    )


class Status(ADCRegisterBase):
    _pack_ = 1
    _fields_ = tuple(
        reversed(
            (
                ("LOCK", ctypes.c_uint8, 1),
                ("F_RESYNC", ctypes.c_uint8, 1),
                ("REG_MAP", ctypes.c_uint8, 1),
                ("CRC_ERR", ctypes.c_uint8, 1),
                ("CRC_TYPE", ctypes.c_uint8, 1),
                ("RESET", ctypes.c_uint8, 1),
                ("WLENGTH", ctypes.c_uint8, 2),
                ("RESERVED", ctypes.c_uint8, 4),
                ("DRDY3", ctypes.c_uint8, 1),
                ("DRDY2", ctypes.c_uint8, 1),
                ("DRDY1", ctypes.c_uint8, 1),
                ("DRDY0", ctypes.c_uint8, 1),
            )
        )
    )


class Mode(ADCRegisterBase):
    _pack_ = 1
    _fields_ = tuple(
        reversed(
            (
                ("RESERVED", ctypes.c_uint8, 2),
                ("REGCRC_EN", ctypes.c_uint8, 1),
                ("RX_CRC_EN", ctypes.c_uint8, 1),
                ("CRC_TYPE", ctypes.c_uint8, 1),
                ("RESET", ctypes.c_uint8, 1),
                ("WLENGTH", ctypes.c_uint8, 2),
                ("RESERVED", ctypes.c_uint8, 3),
                ("TIMEOUT", ctypes.c_uint8, 1),
                ("DRDY_SEL", ctypes.c_uint8, 2),
                ("DRDY_HiZ", ctypes.c_uint8, 1),
                ("DRDY_FMT", ctypes.c_uint8, 1),
            )
        )
    )


class Clock(ADCRegisterBase):
    _pack_ = 1
    _fields_ = tuple(
        reversed(
            (
                ("RESERVED", ctypes.c_uint8, 4),
                ("CH3_EN", ctypes.c_uint8, 1),
                ("CH2_EN", ctypes.c_uint8, 1),
                ("CH1_EN", ctypes.c_uint8, 1),
                ("CH0_EN", ctypes.c_uint8, 1),
                ("RESERVED", ctypes.c_uint8, 2),
                ("TBM", ctypes.c_uint8, 1),
                ("OSR", ctypes.c_uint8, 3),
                ("PWR", ctypes.c_uint8, 2),
            )
        )
    )


class Gain(ADCRegisterBase):
    _pack_ = 1
    _fields_ = tuple(
        reversed(
            (
                ("RESERVED", ctypes.c_uint8, 1),
                ("PGAGAIN3", ctypes.c_uint8, 3),
                ("RESERVED", ctypes.c_uint8, 1),
                ("PGAGAIN2", ctypes.c_uint8, 3),
                ("RESERVED", ctypes.c_uint8, 1),
                ("PGAGAIN1", ctypes.c_uint8, 3),
                ("RESERVED", ctypes.c_uint8, 1),
                ("PGAGAIN0", ctypes.c_uint8, 3),
            )
        )
    )
