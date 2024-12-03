import asyncio


async def publish_messages(message_queue, parsed_bt_queue, shutdown_event):
    counter = 0
    while not shutdown_event.is_set() and counter < 100:
        try:
            message = await asyncio.wait_for(parsed_bt_queue.get(), timeout=0.5)
            await message_queue.put(message)

        except asyncio.TimeoutError:
            continue

        await asyncio.sleep(1)
        counter += 1
    print("shutting down publisher")
