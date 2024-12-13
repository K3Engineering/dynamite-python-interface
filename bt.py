import asyncio
from collections import deque
import math
import random
from bleak import BleakScanner, BleakClient

GATT_UUID = "a659ee73-460b-45d5-8e63-ab6bf0825942"
SERVICE_UUID = "e331016b-6618-4f8f-8997-1a2c7c9e5fa3"
CHARACTERISTIC_UUID = "beb5483e-36e1-4688-b7f5-ea07361b26a8"

client = None
parsed_bt_queue = None

len_queue = deque()


class MockBleakClient:
    def __init__(self, device, timeout=10):
        self.device = device
        self.timeout = timeout
        self.is_connected = False
        self.characteristic_uuid = None
        self.notify_callback = None

    async def connect(self):
        print(f"Mock connecting to device: {self.device.name}")
        self.is_connected = True

    async def disconnect(self):
        print(f"Mock disconnecting from device: {self.device.name}")
        self.is_connected = False

    async def start_notify(self, characteristic_uuid, callback):
        print(f"Mock starting notifications for characteristic: {characteristic_uuid}")
        self.characteristic_uuid = characteristic_uuid
        self.notify_callback = callback
        asyncio.create_task(self.mock_notify_loop())  # Simulate notification in a loop

    async def stop_notify(self, characteristic_uuid):
        print(f"Mock stopping notifications for characteristic: {characteristic_uuid}")
        self.characteristic_uuid = None
        self.notify_callback = None

    async def mock_notify_loop(self):
        # Generate random data periodically
        while self.is_connected:
            if self.notify_callback:
                random_data = bytearray(
                    [random.randint(0, 255) for _ in range(67 * 3)]
                )  # Simulate 12 random bytes
                self.notify_callback(self.characteristic_uuid, random_data)
            await asyncio.sleep(1 / 14)  # Adjust frequency as needed


class MockDevice:
    def __init__(self, name):
        self.name = name


async def find_bluetooth_devices():
    devices = await BleakScanner.discover()

    mydevice = None
    print("Found the following devices:")
    for d in devices:
        print(d)
        if d.name and "DS " in d.name:
            print("Found my device")
            mydevice = d

    return mydevice


async def mock_find_bluetooth_devices():
    # Return a list of mock devices
    mock_devices = [MockDevice(name="09876543"), MockDevice(name="OtherDevice")]
    print("Mock found the following devices:")
    for d in mock_devices:
        print(f"Device name: {d.name}")
    return next((d for d in mock_devices if d.name == "09876543"), None)


def decode_packet_24bit(packet: bytearray) -> list[int]:
    # Decode 24-bit values into signed integers

    # packet is 15 bytes. 2 bytes (status) + 4 * 3 bytes (channels) + 1 byte (CRC) = 15 bytes

    assert len(packet) % 15 == 0

    decoded_packets = []
    for packet_start in range(0, len(packet), 15):
        sub_packet = packet[packet_start : packet_start + 15]
        status = (sub_packet[0] << 8) | sub_packet[1]

        channels = []

        for channel in range(4):
            base_index = 2 + channel * 3
            int_value = (
                (sub_packet[base_index + 2] << 16)
                | (sub_packet[base_index + 1] << 8)
                | sub_packet[base_index]
            )

            # Convert to signed 24-bit integer
            if int_value & (1 << 23):  # Check if the sign bit is set
                int_value -= 1 << 24

            channels.append(int_value)

        crc = sub_packet[14]

        decoded_packets.append(
            {
                "status": status,
                "channels": channels,
                "crc": crc,
            }
        )

    return decoded_packets


def simple_handle_rx(characterictic, data):
    len_queue.append(len(data))
    decoded = decode_packet_24bit(data)
    parsed_bt_queue.put_nowait(decoded)


async def print_count_stats_per_second(event, shutdown_event):
    while not shutdown_event.is_set():
        count = len(len_queue)

        min_count = math.inf
        max_count = -math.inf
        avg_count = 0
        sum_count = 0

        valuable_count = 0

        for _ in range(count):
            temp_len = len_queue.pop()

            min_count = min(min_count, temp_len)
            max_count = max(max_count, temp_len)
            avg_count += temp_len / count
            sum_count += temp_len

            temp_len -= 4
            valuable_count += temp_len / 10

        print(
            f"Received {count} BT packets, min {min_count} bytes/packet, max {max_count} bytes/packet, avg {avg_count:.2f} bytes/packet, sum {sum_count} bytes"
        )

        await event.wait()
        event.clear()


async def timer_task_f(event, shutdown_event):
    while not shutdown_event.is_set():
        await asyncio.sleep(1)
        event.set()


async def wait_for_shutdown(shutdown_event, client):
    while not shutdown_event.is_set():
        await asyncio.sleep(0.1)

    print("Disconnecting bt")
    if client.is_connected:
        await client.disconnect()


async def bt_setup(queue, shutdown_event, mock=False):
    timer_event = asyncio.Event()
    asyncio.create_task(timer_task_f(timer_event, shutdown_event))
    asyncio.create_task(print_count_stats_per_second(timer_event, shutdown_event))

    global parsed_bt_queue

    if not mock:
        mydevice = await find_bluetooth_devices()
    else:
        mydevice = await mock_find_bluetooth_devices()
    if not mydevice:
        print("No device found")
        return

    parsed_bt_queue = queue

    if not mock:
        client = BleakClient(mydevice, timeout=10)
    else:
        client = MockBleakClient(mydevice, timeout=10)
    await client.connect()
    await client.start_notify(CHARACTERISTIC_UUID, simple_handle_rx)

    asyncio.create_task(wait_for_shutdown(shutdown_event, client))
    print("finished bt setup")

    # TODO disconnect function
