import asyncio
from datetime import datetime, timedelta


async def publish_messages(message_queue, parsed_bt_queue, shutdown_event):
    buffer = []
    next_push_time = datetime.now() + timedelta(
        milliseconds=300
    )  # this affects plotting speed

    while not shutdown_event.is_set():
        try:
            message = await asyncio.wait_for(parsed_bt_queue.get(), timeout=1)
            buffer.extend(message)

        except asyncio.TimeoutError:
            pass  # Timeout, no message received, continue loop

        # Check if it's time to push the batch to message_queue
        if datetime.now() >= next_push_time:
            if buffer:  # Only push if there are messages
                await message_queue.put(buffer)
                buffer = []  # Clear the buffer
            next_push_time = datetime.now() + timedelta(milliseconds=300)

    # Handle remaining messages in the buffer during shutdown
    if buffer:
        await message_queue.put(buffer)

    print("shutting down publisher")
