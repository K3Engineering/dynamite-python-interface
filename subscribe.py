from typing import Callable
import asyncio


async def subscribe_to_messages(
    message_queue,
    shutdown_event,
    data_callback: Callable[[bytes], None] = None,
):
    while not shutdown_event.is_set():
        try:
            message = await asyncio.wait_for(message_queue.get(), timeout=0.5)
            if data_callback:
                data_callback(message)
            print(f'Received: {message.decode("utf-8")}')
        except asyncio.TimeoutError:
            continue  # timeout, check for shutdown

    print("shutting down subscriber")
