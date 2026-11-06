import pytest

from dynamite_sampler.api import (
    ADCConfigData,
    DeviceInformation,
    DynamiteSamplerService,
    TxPower,
    TxPowerLevel,
)
from dynamite_sampler.errors import ProtocolError


def test_software_revision_unpacks_esp_idf_version():
    assert DeviceInformation.SoftwareRevision.UUID == "2A28"
    assert DeviceInformation.SoftwareRevision.unpack(b"ESP-IDF v6.1") == "ESP-IDF v6.1"


def test_adc_feed_split():
    payload = bytes(range(12))
    assert DynamiteSamplerService.ADCFeed.split(b"\x34\x12" + payload) == (
        0x1234,
        payload,
    )


@pytest.mark.parametrize("frame", [b"", b"\x00", b"\x00\x00" + bytes(11)])
def test_adc_feed_rejects_incomplete_frames(frame):
    with pytest.raises(ProtocolError):
        DynamiteSamplerService.ADCFeed.split(frame)


def test_adc_config_unpacks_register_fields():
    reg_id = 4 << 8
    reg_clock = 2 | (3 << 2)
    reg_gain = 0x3210
    frame = b"".join(
        (
            b"\x01",
            reg_id.to_bytes(2, "little"),
            bytes(4),
            reg_clock.to_bytes(2, "little"),
            reg_gain.to_bytes(2, "little"),
        )
    )
    assert DynamiteSamplerService.ADCConfig.unpack(frame) == ADCConfigData(
        num_channels=4,
        power_mode="HIGH_RESOLUTION",
        sample_rate=4000,
        gains=[1, 2, 4, 8],
    )


@pytest.mark.parametrize(
    "frame",
    [bytes(10), b"\x02" + bytes(10)],
    ids=["short", "unsupported_version"],
)
def test_adc_config_rejects_invalid_frames(frame):
    with pytest.raises(ProtocolError):
        DynamiteSamplerService.ADCConfig.unpack(frame)


@pytest.mark.parametrize("value", [-128, -9, 0, 127])
def test_tx_power_round_trips_int8(value):
    packed = TxPower.TxPowerSet.pack(value)
    assert TxPowerLevel.Level.unpack(packed) == value


@pytest.mark.parametrize("frame", [b"", b"\x00\x00"])
def test_tx_power_rejects_invalid_length(frame):
    with pytest.raises(ProtocolError):
        TxPowerLevel.Level.unpack(frame)
