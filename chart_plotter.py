from matplotlib import pyplot as plt
from queue import Queue

plotting_queue = Queue()


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
            y_data += message  # Append new Y-axis data
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
    plotting_queue.put(data)
