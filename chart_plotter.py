from matplotlib import pyplot as plt
from queue import Queue

plotting_queue = Queue()


def plotter(shutdown_event):
    plt.ion()
    fig, ax = plt.subplots()
    x_data, y_data = [], []

    while not shutdown_event.is_set():
        if not plotting_queue.empty():
            message_int = plotting_queue.get()
            x_data.append(len(x_data))
            y_data.append(int(message_int))
            ax.clear()
            ax.plot(x_data, y_data)
            plt.draw()
            plt.pause(0.1)
        plt.pause(0.1)  # Small pause to prevent busy waiting.
    print("shutting down plotter")


def update_data(data: bytes):
    plotting_queue.put(int(data.decode("utf-8")))
