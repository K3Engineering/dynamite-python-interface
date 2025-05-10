# dynamite-python-interface

Python interface for the Dynamite sampler board.

## Bleak utility

A Bleak implementation that uses the `bleak` library to connect to the dynamite sampler,
and streams that data to call back classes.

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
