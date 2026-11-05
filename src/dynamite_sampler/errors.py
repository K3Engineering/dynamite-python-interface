"""Exception hierarchy for the Dynamite Sampler API."""


class DynamiteError(Exception):
    """Base for every error this library raises."""


class DeviceNotFound(DynamiteError):
    """No Dynamite Sampler in range (or no device with the given address)."""


class MultipleDevicesFound(DynamiteError):
    """More than one device in range and no address was given."""


class ConnectionLost(DynamiteError):
    """The BLE link dropped (mid-stream, or during a command)."""


class KvsError(DynamiteError):
    """Base for KVS command failures."""


class KvsRejected(KvsError):
    """The device answered '0': no such key, bad input, or IDX past the
    last entry."""


class KvsBusy(KvsError):
    """The device answered 'B': locked (ADC feed streaming), the request
    was not processed."""


class KvsDeviceError(KvsError):
    """The device answered 'E': a storage-layer (NVS) failure on the
    device. Never a missing key; a mid-iteration error is not
    end-of-keys."""


class KvsTimeout(KvsError, TimeoutError):
    """No reply within the command timeout. The device answers every
    request (a busy device answers 'B'), so this means the link is
    broken. Also catchable as the builtin TimeoutError."""


class OtaError(DynamiteError):
    """Base for OTA update failures: a device refusal (NAK), an unexpected
    reply, or a handshake timeout."""


class OtaRejected(OtaError):
    """The device NAK'd a handshake step: it declined to start the update,
    or rejected the image at its final integrity check."""


class OtaTimeout(OtaError, TimeoutError):
    """No reply within the handshake timeout. The device answers every
    handshake step (even with a NAK), so this means the link is broken.
    Also catchable as the builtin TimeoutError."""


class FirmwareCatalogError(DynamiteError):
    """A firmware-release catalog failure: the check/download request, the
    API payload, or the image's size/checksum verification. Surfaced as-is,
    never read as 'up to date'."""
