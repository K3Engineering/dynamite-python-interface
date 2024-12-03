import random

import asyncio


async def publish_messages(message_queue, shutdown_event):
    counter = 0
    while not shutdown_event.is_set() and counter < 100:
        random_number = random.randint(0, 100)
        message = f"{random_number}".encode("utf-8")
        await message_queue.put(message)
        print(f"Published: {message}")
        await asyncio.sleep(1)
        counter += 1
    print("shutting down publisher")
