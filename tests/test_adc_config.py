"""ADC configuration frame decoding."""

import pytest

from dynamite_sampler.api import ADCConfigData, DynamiteSamplerService

ADCConfig = DynamiteSamplerService.ADCConfig


def _frame():
    return b"".join(
        (
            b"\x01",
            (4 << 8).to_bytes(2, "little"),
            bytes(4),
            (2 | (3 << 2)).to_bytes(2, "little"),
            (0x3210).to_bytes(2, "little"),
        )
    )


@pytest.mark.parametrize("length", range(11))
def test_adc_config_rejects_short_frames(length):
    with pytest.raises(Exception, match="too short"):
        ADCConfig.unpack(b"\x01" * length)


@pytest.mark.parametrize(
    "trailing", [b"", b"\xde\xad\xbe\xef"], ids=["exact", "trailing_bytes"]
)
def test_adc_config_unpacks_register_fields(trailing):
    assert ADCConfig.unpack(_frame() + trailing) == ADCConfigData(
        num_channels=4,
        power_mode="HIGH_RESOLUTION",
        sample_rate=4000,
        gains=[1, 2, 4, 8],
    )
