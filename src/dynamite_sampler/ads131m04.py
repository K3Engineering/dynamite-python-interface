"""Register layouts for the ADS131M04, as exposed by the Dynamite Sampler.

Little-endian ctypes structures: fields are declared MSB-first (like the
datasheet) and reversed for ``LittleEndianStructure``. ``_pack_ = 1`` keeps
them tight.
"""

import ctypes


class ADCRegisterBase(ctypes.LittleEndianStructure):
    """Common repr for the register structures."""

    def __repr__(self):
        fields = []
        for name, _, bits in reversed(self._fields_):
            if name != "RESERVED":
                value = self.__getattribute__(name)
                value_bin = format(value, f"0{bits}b")
                fields.append(f"{name}=0b{value_bin}")
        return f"{type(self).__name__}({', '.join(fields)})"


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
