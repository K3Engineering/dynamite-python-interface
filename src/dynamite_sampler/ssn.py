"""Sample-sequence-number unwrapping for the 16-bit ADC feed counter."""


class SsnUnwrapper:
    """Unwrap the feed's 16-bit sample sequence number to a linear counter and
    count missed samples, handling the 16-bit rollover (e.g. expected 65535,
    got 0).

    The modular gap is exact only while the silence between packets is under
    one rollover period (65536 / sample_rate: 65 s at 1 ksps). BLE's
    supervision timeout caps that well below the period, so a longer dead
    interval is a dropped link, not an ambiguous count."""

    UINT16_MODULO = 2**16

    def __init__(self):
        self._expected = None

    def unwrap(self, ssn, sample_count):
        """(unwrapped_ssn, missed_samples) for a packet with `sample_count`
        samples starting at wire `ssn`."""
        if self._expected is None:
            self._expected = ssn
        missed = (ssn - self._expected) % self.UINT16_MODULO
        unwrapped = self._expected + missed
        self._expected = unwrapped + sample_count
        return unwrapped, missed
