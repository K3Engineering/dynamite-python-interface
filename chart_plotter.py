from matplotlib import pyplot as plt
from queue import Queue

plotting_queue = Queue()


def plotter(shutdown_event):
    plt.ion()
    fig, ax = plt.subplots()
    x_data, y_data = [], []

    while not shutdown_event.is_set():
        if not plotting_queue.empty():
            message = plotting_queue.get_nowait()

            print(f"rxd message {message}")
            x_data += list(range(len(message)))
            y_data += message
            ax.clear()
            ax.plot(x_data, y_data)
            plt.draw()
            plt.pause(0.1)
        plt.pause(0.1)  # Small pause to prevent busy waiting.
    print("shutting down plotter")


def update_data(data: list):
    plotting_queue.put(data)
