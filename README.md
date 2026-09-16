# dynamite-python-interface

Python interface for the Dynamite sampler board.

## Library `dynamite_sampler`

A Bleak library for the Dynamite sampler, in two layers:

- **Blocks** (`dev.stream()`, `dev.read()`): fixed-size windows of samples on a
  continuous timeline — dropped samples arrive as NaN rows counted in
  `block.rows_dropped`. This is the data plane: record it, convert it, plot it.
- **Packets** (`dev.stream_packets()`): one item per BLE notification, with
  arrival time, payload size, and dropped-row count. This is the layer
  underneath, for per-packet latency and link metrics.

Blocks are assembled from packets: `blocks_from_packets()` / `BlockAssembler`
are public, so a script that needs both layers can iterate
`dev.stream_packets()` and feed a `BlockAssembler` itself (see `stream.py`).

Sticking to the happy path, the synchronous facade is enough:

```python
import dynamite_sampler as dms

with dms.connect() as dev:
    block = dev.read(n=1000, units="mV/V")
```

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
to divide out (one of: `adc`, `volts_adc_ir`, `volts_opamp_ir`,
`kg_with_opamp`).

Example usage:

`python stream.py --metrics --csv --socket --conversion volts_adc_ir`

## Script for OTA firmware updates `ota_update.py`

Flashes a firmware image to a board over BLE.

`python ota_update.py -f path/to/firmware.bin "device name"` — flash a local image

`python ota_update.py --check "device name"` — report installed vs channel target

`python ota_update.py --latest "device name"` — download, verify (size + SHA-256), and flash the channel target

`--channel beta` opts `--check`/`--latest` into GitHub prereleases (default: stable).
