# dynamite-python-interface

Python interface for the Dynamite sampler board.

## Bleak utility

A Bleak implementation that uses the `bleak` library to connect to the dynamite sampler,
and streams that data to call back classes.

## Asynchronous device acquisition

`aconnect()` connects to the only device in range, or to a supplied address.
The device streams raw `Packet` values and reads fixed-size windows as
self-describing `Recording` values:

```python
import asyncio
import dynamite_sampler as dms

async def acquire():
    async with dms.aconnect() as dev:
        calibration = await dev.read_calibration()
        tare = await dev.tare()
        recording = await dev.read(
            seconds=10,
            units="kgf",
            calibration=calibration,
            tare_raw=tare,
        )
    recording.to_csv("run.csv")

asyncio.run(acquire())
```

Calibration and tare are explicit values rather than cached measurement
state. `dev.stream()` yields raw counts; `dev.read()` lays dropped samples
into the returned timeline as NaN rows. `dms.read_csv(path)` reads recordings
without a device.

### Open-ended asynchronous capture

`dev.recording()` captures in a background task until its context exits or
`stop()` is called. Received data is finalized even when the context body
raises. With a path, packets stream directly to CSV and the final recording
is read back from that file; without one, packets accumulate in memory:

```python
async with dev.recording(units="raw", path="session.csv") as capture:
    await operate_rig()
recording = capture.recording
```

An acquisition error such as connection loss is re-raised after finalizing
the partial recording.

## Script to stream data to various sources `stream.py`

This script implements various streaming sinks:
- printing metrics to screen.
- saving data to a `.csv` file.
- sending it to a socket for to plotted by Waveforms.

This script is still in flux and the arguments parsing might change.

### Waveforms plotting

Waveforms can be used for real time plotting of the data.

- Launch python script
- Wait for the connection to a Dynamite Sampler
- The python script will then pause.
- Launch the waveforms script `read_from_tcp_4_ports.js`
- Press enter on the python script
- Data will be streamed to Waveforms reference channels.

#### Changing units

Currently the script accepts a `JSON` dictionary to pass into the streaming class
initializer. This allows the user to configure the streaming class to send a scaling
factor so that the waveforms script can convert the data to other units.

Options are:

- "adc": raw ADC reading
- "volts_adc_ir": Voltage on the ADC input referenced
- "volts_opamp_ir": Voltage on the op-amp input referenced
- "kg_with_opamp": Weight in Kg on the loadcell

Example usage:

`python stream.py --metrics --csv --socket '{"conversion":"volts_adc_ir"}'`

## Local OTA firmware updates

`ota_update.py` flashes a local firmware image to a board over BLE:

```powershell
python ota_update.py -f path/to/firmware.bin "device name"
```

The protocol client accepts an already-connected Bleak client; the script owns
device lookup, connection lifetime, and progress reporting.

## Firmware release catalog

The release catalog selects the newest qualifying GitHub release for the
`stable` or `beta` channel and verifies both image size and SHA-256 before
flashing:

```powershell
python ota_update.py --check "device name"
python ota_update.py --latest "device name"
python ota_update.py --channel beta --latest "device name"
```

`--check` reports the installed and target versions without flashing.
`--latest` downloads, verifies, and flashes the channel target.
