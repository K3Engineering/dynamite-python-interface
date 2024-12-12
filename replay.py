import ast
import asyncio


# assumes that the input file is safe
def read_file(filepath: str):
    with open(filepath, "r") as f:
        for line in f:
            parsed = ast.literal_eval(line.strip())  # assuming the input file is safe
            # print(parsed)
            yield parsed


async def send_dict_to_queue(shutdown_event, iter, queue):
    buffer = []
    batch_size = 100
    while not shutdown_event.is_set():
        for item in iter:
            buffer.append(item)

            if len(buffer) >= batch_size:
                await queue.put(buffer.copy())
                await asyncio.sleep(
                    batch_size / 1000
                )  # note that this doesn't guarantee exactly 1khz runtime, as we do other stuff in the task as well

                buffer = []


async def replay_setup(
    filepath,
    queue,
    shutdown_event,
):
    iter = read_file(filepath)
    asyncio.create_task(send_dict_to_queue(shutdown_event, iter, queue))
