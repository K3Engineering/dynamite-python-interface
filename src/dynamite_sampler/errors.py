"""Exception hierarchy for the Dynamite Sampler API."""


class DynamiteError(Exception):
    """Base for every error this library raises."""


class DeviceNotFound(DynamiteError):
    """No Dynamite Sampler in range (or no device with the given address)."""


class MultipleDevicesFound(DynamiteError):
    """More than one device in range and no address was given."""


class ConnectionLost(DynamiteError):
    """The BLE link dropped (mid-stream, or during a command)."""


class ReadTimeout(DynamiteError, TimeoutError):
    """A bounded read received no rows within its inactivity timeout.

    Also catchable as the builtin TimeoutError."""


class BufferOverrun(DynamiteError):
    """The consumer is slower than the feed and the internal queue filled."""


class StreamActive(DynamiteError):
    """A ``stream()``/``read()``/``tare()`` is already active on this device.

    Notifications are started on entry and stopped on exit, so only one feed
    consumer may run at a time."""


class TareError(DynamiteError):
    """Tare failed: a channel had no valid (non-NaN) samples in the window.

    ``tare_raw`` is left untouched when this is raised."""


class UnitUnavailable(DynamiteError):
    """The requested unit cannot be produced on every channel.

    `mV`/`mV/V`/force need the board's analog constants; force additionally
    needs a load cell in every channel's slot. `raw` always converts."""


class ProtocolError(DynamiteError):
    """Wire-level break: a malformed frame or an unparseable ADC config."""


class ProvisioningError(DynamiteError):
    """The device is in safe mode (``UNCONFIGURED``): the ADC is disabled.

    Raised by ``stream()``/``read()``/``tare()``; KVS access still works,
    which is what provisioning a fresh board needs."""


class CalibrationError(DynamiteError):
    """Board calibration data is present and unusable.

    A torn calibration group, mixed/partial channels, invalid ladder data, a
    stale ``cal.adc``, or orphaned calibration keys without their constants.
    Raised by ``connect()``."""


class CsvFormatError(DynamiteError):
    """A dynamite-csv file is malformed (read) or the recording's inputs are
    inconsistent (write: non-contiguous blocks, wrong channel count)."""


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
