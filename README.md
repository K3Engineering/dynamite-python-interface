# dynamite-python-interface

Python interface for the Dynamite sampler board.

## Library `dynamite_sampler`

A Bleak library for the Dynamite sampler. The device object is a thin,
stateless handle on the hardware; the measurements and their conversion
inputs are values you hold and pass.

Three entry points:

- **Files** (`dms.read_csv(path)`): a recorded `dynamite-csv` file (from the
  app or this package) as a `Recording` — rows, units, and the calibration
  rebuilt from the header, so it re-converts and re-tares without a device:
  `rec.convert("N")`, `rec.to_dataframe()`, `rec.to_csv("out.csv")`.
- **Captures** (`dev.read(n=..., units=...)`, `dev.recording(...)`): a
  capture returns that same `Recording`; `dev.recording()` is the open-ended
  form — a background capture that keeps the partial data when you Ctrl+C.
  With a `path` it streams every packet to disk and never accumulates in
  memory (hours-long runs), loading the `Recording` back from the file on
  exit:

  ```python
  import dynamite_sampler as dms

  with dms.connect() as dev:
      calibration = dev.read_calibration()
      tare = dev.tare()
      rec = dev.read(
          seconds=10, units="kgf", calibration=calibration, tare_raw=tare
      )
  rec.to_csv("run.csv")
  ```
- **Streams** (`dev.stream()`): one `Packet` per BLE notification — rows of
  raw counts, the unwrapped sample sequence number of the first row, the
  number of samples lost since the previous packet, arrival time, payload
  size, and nothing else. That is the layer for per-packet latency,
  closed-loop control, and link metrics; convert through a `Calibration` you
  read once up front.

A fixed window of N samples is what `read()` is for: it collects packets
until it has the rows you asked for and lays lost samples in as NaN rows, so
row `i` is always sample `ssn0 + i`.

### State is explicit

`connect()` reads the identity and ADC characteristics and subscribes to the
KVS — it does not read the KVS, so a board with unprovisioned, half-written,
or stale calibration keys still connects, which is what repairing one needs.

Nothing measurement-related is cached on the device:

| To get | Call |
| --- | --- |
| the calibration | `dev.read_calibration()` (a KVS dump, parsed) |
| a tare | `dev.tare()` → an ndarray you keep |
| converted rows | `dev.read(units=..., calibration=..., tare_raw=...)` |
| raw counts, live | `dev.stream()` |

`dev.kvs` is a plain command interface (`read_all()` for a dump,
`set`/`set_many`/`set_group` for verified writes); a write rebuilds nothing,
because the library parses KVS strings into a `Calibration` only when asked.

The synchronous facade is enough for most scripts, with two notes: the sync
`stream()` blocks the calling thread (for background acquisition use
`dev.recording()`, for a GUI event loop use a thread + queue or the async
class).

## Script to stream data to various sources `stream.py`

This script implements various streaming sinks:
- `--metrics`: live link-health line (packets/sec, bytes/sec, rows/sec,
  dropped rows).
- `--tqdm`: a TQDM sample-count bar.
- `--csv [path]`: record to a dynamite-csv file (default path when no value is
  given; `--units` selects the converted column).
- `--socket`: stream to localhost sockets for plotting with Waveforms.
- `--txpwr N`: set the BLE TX power of the board before streaming.

With no flags it runs metrics + socket + CSV recording.

### Waveforms plotting

Waveforms can be used for real time plotting of the data.

- Launch python script
- Wait for the connection to a Dynamite Sampler
- The python script will then pause.
- Launch the waveforms script `read_from_tcp_4_ports.js`
- Press enter on the python script
- Data will be streamed to Waveforms reference channels.

#### Changing units

`--conversion` selects the unit conversion the socket sink tells the receiver
to divide out (one of: `adc`, `volts_adc_ir`, `volts_opamp_ir`).

Example usage:

`python stream.py --metrics --csv --socket --conversion volts_adc_ir`

## Script for OTA firmware updates `ota_update.py`

Flashes a firmware image to a board over BLE.

`python ota_update.py -f path/to/firmware.bin "device name"` — flash a local image

`python ota_update.py --check "device name"` — report installed vs channel target

`python ota_update.py --latest "device name"` — download, verify (size + SHA-256), and flash the channel target

`--channel beta` opts `--check`/`--latest` into GitHub prereleases (default: stable).
