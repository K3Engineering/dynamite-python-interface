import asyncio


async def publish_messages(message_queue, parsed_bt_queue, shutdown_event):
    while not shutdown_event.is_set():
        try:
            message = await asyncio.wait_for(parsed_bt_queue.get(), timeout=1)
            await message_queue.put(message)

        except asyncio.TimeoutError:
            continue

    print("shutting down publisher")
