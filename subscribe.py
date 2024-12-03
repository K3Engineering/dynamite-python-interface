from typing import Callable, List
import asyncio


async def subscribe_to_messages(
    message_queue,
    shutdown_event,
    data_callbacks: List[Callable] = None,
):

    if data_callbacks is None:
        data_callbacks = []

    while not shutdown_event.is_set():
        try:
            message = await asyncio.wait_for(message_queue.get(), timeout=0.5)
            for data_callback in data_callbacks:
                data_callback(message)
        except asyncio.TimeoutError:
            continue  # timeout, check for shutdown

    print("shutting down subscriber")
