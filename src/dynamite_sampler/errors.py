"""Exception hierarchy for the Dynamite Sampler API."""


class DynamiteError(Exception):
    """Base for every error this library raises."""


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
