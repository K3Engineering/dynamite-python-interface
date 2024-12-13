import asyncio
import threading
import signal
from time import sleep
from replay import replay_setup
from subscribe import subscribe_to_messages
from chart_plotter import initialize_plot, plotter, plotter2, update_data
from writer import write_to_file
from bt import bt_setup

from PyQt6.QtWidgets import QApplication
import pyqtgraph as pg
from pglive.sources.data_connector import DataConnector
from pglive.sources.live_plot_widget import LivePlotWidget
from pglive.sources.live_plot import LiveLinePlot

parsed_bt_queue = asyncio.Queue()
shutdown_event = threading.Event()


async def main(data_connector_ch3, data_connector_ch2):
    replay = True

    subscribers = [update_data]

    if not replay:
        subscribers.append(write_to_file)

    subscriber_task = asyncio.create_task(
        subscribe_to_messages(parsed_bt_queue, shutdown_event, subscribers)
    )

    # plotter(shutdown_event)
    asyncio.create_task(
        plotter2(data_connector_ch3, data_connector_ch2, shutdown_event)
    )

    if not replay:
        await bt_setup(parsed_bt_queue, shutdown_event)
    else:
        await replay_setup(
            "./sample_data/datadump_20241212_123045.txt",
            parsed_bt_queue,
            shutdown_event,
        )

    await asyncio.gather(subscriber_task, return_exceptions=True)


def run_event_loop(data_connector_ch3, data_connector_ch2):
    asyncio.run(main(data_connector_ch3, data_connector_ch2))


def signal_handler(signum, frame):
    print("Signal received, shutting down...")
    shutdown_event.set()  # Set the shutdown event


if __name__ == "__main__":
    # Register signal handler for SIGINT (Ctrl+C)
    signal.signal(signal.SIGINT, signal_handler)

    app = QApplication([])

    data_connector_ch3, data_connector_ch2 = initialize_plot()

    t = threading.Thread(
        target=run_event_loop,
        args=(
            data_connector_ch3,
            data_connector_ch2,
        ),
    )
    t.start()

    app.exec()

    shutdown_event.set()

    t.join()
    sleep(0.2)
