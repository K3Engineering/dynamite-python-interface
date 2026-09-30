# dynamite-python-interface

Python interface for the Dynamite sampler board.

## Library `dynamite_sampler`

A Bleak library for the Dynamite sampler. Three entry points:

- **Files** (`dms.read_csv(path)`): a recorded `dynamite-csv` file (from the
  app or this package) as a `Recording` — rows, units, and the calibration
  rebuilt from the header, so it re-converts and re-tares without a device:
  `rec.convert("N")`, `rec.to_dataframe()`, `rec.to_csv("out.csv")`.
- **Captures** (`dev.read(n=..., units=...)`, `dev.recording(...)`): a
  capture returns that same `Recording`; `dev.recording()` is the open-ended
  form — a background capture that keeps the partial data when you Ctrl+C:

  ```python
  import dynamite_sampler as dms

  with dms.connect() as dev:
      rec = dev.read(seconds=10, units="kgf")
  rec.to_csv("run.csv")
  ```
- **Streams** (`dev.stream()`, `dev.stream_packets()`): live processing.
  Blocks are fixed-size windows of samples on a continuous timeline —
  dropped samples arrive as NaN rows counted in `block.rows_dropped`;
  packets are one item per BLE notification, with arrival time, payload
  size, and dropped-row count, for per-packet latency, closed-loop control,
  and link metrics.

Blocks are assembled from packets: `BlockAssembler` is public (windowing
only — it folds raw packets into raw blocks, no calibration involved), so a
script that needs both layers can iterate `dev.stream_packets()` and feed a
`dev.assembler()` itself (see `stream.py`).

The synchronous facade is enough for most scripts, with two notes: the
sync `stream()` blocks the calling thread (for background acquisition use
`dev.recording()`, for a GUI event loop use a thread + queue or the async
class), and packets are async-only — a per-notification cross-thread hop
would defeat the packet layer, so `stream_packets()` lives on
`AsyncDynamiteSampler` (`dms.aconnect()`).

## Script to stream data to various sources `stream.py`

This script implements various streaming sinks:
- `--metrics`: live link-health line (packets/sec, bytes/sec, rows/sec,
  dropped rows).
- `--tqdm`: a TQDM sample-count bar.
- `--csv [path]`: record to a dynamite-csv file (default path when no value is
  given; `--units` selects the converted column).
- `--socket`: stream to localhost sockets for plotting with Waveforms.
- `--txpwr N`: set the BLE TX power of the board before streaming.
- `--blocksize N`: rows per assembled block (default 100).

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
