#!/usr/bin/env python
"""Write real-time data to a socket EXPERIMENTAL SCRIPT"""

import asyncio
import threading

import socket

from bt import bt_setup
from replay import replay_setup


parsed_bt_queue = asyncio.Queue()
shutdown_event = threading.Event()


async def send_queue_data_to_socket(queue):

    ports = [8080, 8081, 8082, 8083]
    servers = []
    for port in ports:
        print(f"waiting socket {port}")
        s = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        s.connect(("localhost", port))
        servers.append(s)
        print(f"socket connected {port}")
    while True:
        message = await queue.get()
        for d in message:
            for val, s in zip(d["channels"], servers):
                val = int(val)

                # send as an integer
                bytes_to_send = val.to_bytes(4, "little", signed=True)
                s.send(bytes_to_send)


async def main():
    await bt_setup(parsed_bt_queue, shutdown_event)
    # await replay_setup(
    #     "./sample_data/datadump_20241212_123045.txt",
    #     parsed_bt_queue,
    #     shutdown_event,
    # )

    task = asyncio.create_task(send_queue_data_to_socket(parsed_bt_queue))
    await asyncio.gather(task, return_exceptions=True)


if __name__ == "__main__":
    asyncio.run(main())
