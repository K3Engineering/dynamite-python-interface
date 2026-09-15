"""Raw ADC feed capture to CSV.

Saves raw 24-bit counts, unwrapped sample sequence number,
and the host arrival time of its packet (note that it's not send time i.e may have jitter).
Kept in memory for immediate analysis. One minute of streaming is ~3 MB of CSV.

CSV columns: ssn, t_unix_ms, ch0, ch1, ch2, ch3
"""

import csv
import datetime
import pathlib
import time

import dynamite_sampler_bleak_util as dsbu


class FeedRecorder(dsbu.NotifyCallbackFeeddatas):
    """NotifyCallbackFeeddatas sink: records the entire feed to CSV + memory."""

    COLUMNS = ("ssn", "t_unix_ms", "ch0", "ch1", "ch2", "ch3")

    def __init__(self, file_path):
        self.file_path = pathlib.Path(file_path).resolve()
        self.file_path.parent.mkdir(parents=True, exist_ok=True)
        # Flush per packet: a crash loses at most the last partial line.
        self._file = open(self.file_path, "w", newline="")
        self._writer = None
        self._rows = []
        self._last_ssn = None
        self._missing = 0

    def setup(self, device_dict):
        stamp = datetime.datetime.now(datetime.timezone.utc).isoformat(
            timespec="seconds"
        )
        print(f"# captured: {stamp}", file=self._file)
        print(f"# device: {device_dict}", file=self._file)
        self._writer = csv.writer(self._file)
        self._writer.writerow(self.COLUMNS)

    def callback(self, header, feeddatas, missing):
        t_ms = round(time.time() * 1000)
        base = header.sample_sequence_number  # already unwrapped upstream
        for i, d in enumerate(feeddatas):
            row = (base + i, t_ms, d.ch0, d.ch1, d.ch2, d.ch3)
            self._writer.writerow(row)
            self._rows.append(row)
        if feeddatas:
            self._last_ssn = base + len(feeddatas) - 1
        self._missing += missing
        self._file.flush()

    @property
    def last_ssn(self):
        """Highest unwrapped SSN received so far, None before the first packet."""
        return self._last_ssn

    @property
    def sample_count(self):
        return len(self._rows)

    @property
    def missing_count(self):
        return self._missing

    @property
    def rows(self):
        """All captured rows as (ssn, t_unix_ms, ch0, ch1, ch2, ch3) tuples."""
        return self._rows

    def window(self, ssn_start, ssn_end):
        """Rows with ssn_start <= ssn <= ssn_end (a measurement window)."""
        return [r for r in self._rows if ssn_start <= r[0] <= ssn_end]

    def cleanup(self):
        self._file.close()
