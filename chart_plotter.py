from matplotlib import pyplot as plt
import matplotlib.gridspec as gridspec
import seaborn as sns

from queue import Queue
from datetime import datetime, timedelta

plotting_queue = Queue()

buffer = []
next_push_time = datetime.now() + timedelta(
    milliseconds=300
)  # this affects plotting speed


def plotter(shutdown_event):
    plt.ion()

    # Use gridspec for arranging main plot and histogram
    fig = plt.figure(figsize=(8, 6))
    gs = gridspec.GridSpec(1, 2, width_ratios=[4, 1])  # Main plot larger than histogram
    ax = fig.add_subplot(gs[0])  # Main plot
    ax_hist = fig.add_subplot(gs[1], sharey=ax)  # Histogram

    x_data, y_data = [], []
    data_counter = 0  # Track the total number of data points for the X-axis

    while not shutdown_event.is_set():
        if not plotting_queue.empty():
            message = plotting_queue.get_nowait()

            # Update data for the main plot
            x_data += list(
                range(data_counter, data_counter + len(message))
            )  # Adjust X-axis
            y_data += message

            # Limit the data for both axes
            x_data = x_data[(-2000 * 4) :]
            y_data = y_data[(-2000 * 4) :]

            data_counter += len(message)  # Update counter for next X-axis range

            # Clear and update the main plot
            ax.clear()
            ax.plot(x_data, y_data)
            ax.set_xlabel("X")
            ax.set_ylabel("ADC Values")
            ax.set_title("Real-time Data Plot")

            # Clear and update the histogram
            ax_hist.clear()
            ax_hist.hist(
                y_data, bins=30, orientation="horizontal", color="gray", alpha=0.7
            )
            ax_hist.set_ylabel("ADC Values")  # Shares the same y-axis
            ax_hist.set_xlabel("Frequency")
            ax_hist.set_title("Distribution")

            plt.tight_layout()
            plt.draw()
            plt.pause(0.1)

        plt.pause(0.1)  # Small pause to prevent busy waiting.

    print("shutting down plotter")


def update_data(data: list):
    global next_push_time
    global plotting_queue
    global buffer

    # plotting_queue.put(data)

    buffer.extend(data)

    if datetime.now() >= next_push_time:
        plotting_queue.put(buffer)

        buffer = []  # Clear the buffer
        next_push_time = datetime.now() + timedelta(milliseconds=300)
