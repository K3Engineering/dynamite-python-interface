from bleak import BleakScanner, BleakClient

GATT_UUID = "a659ee73-460b-45d5-8e63-ab6bf0825942"
SERVICE_UUID = "e331016b-6618-4f8f-8997-1a2c7c9e5fa3"
CHARACTERISTIC_UUID = "beb5483e-36e1-4688-b7f5-ea07361b26a8"

client = None
parsed_bt_queue = None


async def find_bluetooth_devices():
    devices = await BleakScanner.discover()

    mydevice = None
    print("Found the following devices:")
    for d in devices:
        print(d)
        if d.name == "09876543":
            print("Found my device")
            mydevice = d

    return mydevice


def decode_packet_24bit(packet: bytearray) -> list[int]:
    # Decode 24-bit values into signed integers
    int_values = []
    for i in range(0, len(packet), 3):
        if i + 2 < len(packet):
            int_value = (packet[i + 2] << 16) | (packet[i + 1] << 8) | packet[i]

            # Convert to signed 24-bit integer
            if int_value & (1 << 23):  # Check if the sign bit is set
                int_value -= 1 << 24

            int_values.append(int_value)
        else:
            print("malformed packet length")
    return int_values


def simple_handle_rx(characterictic, data):
    # len_queue.append(len(data))
    print("rx")
    decoded = decode_packet_24bit(data)
    parsed_bt_queue.put_nowait(decoded)


async def bt_setup(queue):
    global parsed_bt_queue

    mydevice = await find_bluetooth_devices()
    if not mydevice:
        print("No device found")
        return

    parsed_bt_queue = queue

    client = BleakClient(mydevice, timeout=10)
    await client.connect()
    await client.start_notify(CHARACTERISTIC_UUID, simple_handle_rx)
    print("finished bt setup")

    # TODO disconnect function
