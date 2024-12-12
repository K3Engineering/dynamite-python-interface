import asyncio
import threading
import signal
from time import sleep
from replay import replay_setup
from subscribe import subscribe_to_messages
from chart_plotter import plotter, update_data
from writer import write_to_file
from bt import bt_setup

parsed_bt_queue = asyncio.Queue()
shutdown_event = threading.Event()


async def main():
    replay = True

    subscribers = [update_data]

    if not replay:
        subscribers.append(write_to_file)

    subscriber_task = asyncio.create_task(
        subscribe_to_messages(parsed_bt_queue, shutdown_event, subscribers)
    )

    if not replay:
        await bt_setup(parsed_bt_queue, shutdown_event)
    else:
        await replay_setup(
            "./data/datadump_20241212_123045.txt", parsed_bt_queue, shutdown_event
        )

    await asyncio.gather(subscriber_task, return_exceptions=True)


def run_event_loop(loop):
    asyncio.set_event_loop(loop)
    loop.run_until_complete(main())


def signal_handler(signum, frame):
    print("Signal received, shutting down...")
    shutdown_event.set()  # Set the shutdown event


if __name__ == "__main__":
    # Register signal handler for SIGINT (Ctrl+C)
    signal.signal(signal.SIGINT, signal_handler)

    new_loop = asyncio.new_event_loop()
    t = threading.Thread(target=run_event_loop, args=(new_loop,))
    t.start()
    plotter(shutdown_event)
    t.join()
    sleep(0.2)
