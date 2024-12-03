from matplotlib import pyplot as plt
from queue import Queue
from datetime import datetime, timedelta

plotting_queue = Queue()

buffer = []
next_push_time = datetime.now() + timedelta(
    milliseconds=300
)  # this affects plotting speed


def plotter(shutdown_event):
    plt.ion()
    fig, ax = plt.subplots()
    x_data, y_data = [], []
    data_counter = 0  # Track the total number of data points for the X-axis

    while not shutdown_event.is_set():
        if not plotting_queue.empty():
            message = plotting_queue.get_nowait()

            x_data += list(
                range(data_counter, data_counter + len(message))
            )  # Adjust X-axis to extend
            y_data += message

            x_data = x_data[(-2000 * 4) :]
            y_data = y_data[(-2000 * 4) :]

            data_counter += len(message)  # Update counter for next X-axis range

            ax.clear()
            ax.plot(x_data, y_data)
            ax.set_xlabel("X")
            ax.set_ylabel("ADC Values")
            ax.set_title("Real-time Data Plot")
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
