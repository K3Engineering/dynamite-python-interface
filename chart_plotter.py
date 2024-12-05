from matplotlib import pyplot as plt
import matplotlib.gridspec as gridspec
import seaborn as sns
import numpy as np

from queue import Queue
from datetime import datetime, timedelta

plotting_queue = Queue()

buffer = []
next_push_time = datetime.now() + timedelta(
    milliseconds=300
)  # this affects plotting speed

# Load cell constants
GAIN = 32
FSR = 1.2 / GAIN
RESOLUTION_BITS = 23
MICROVOLT_CONVERSION = FSR / (2**RESOLUTION_BITS) * 1000 * 1000
LC_VOLTS = 2
KG_CONVERSION = 200 * 1000 / 2 / LC_VOLTS / 1000 / 1000 / 1000
SAMPLE_RATE = 2000


def plotter(shutdown_event):
    plt.ion()

    # Use gridspec for arranging main plot and histogram
    fig = plt.figure(figsize=(8, 6))
    gs = gridspec.GridSpec(1, 2, width_ratios=[4, 1])  # Main plot larger than histogram
    ax1 = fig.add_subplot(gs[0])  # Main plot
    ax2 = ax1.twiny()
    ax3 = ax1.twinx()
    ax4 = ax1.twinx()
    ax_hist = fig.add_subplot(gs[1], sharey=ax1)  # Histogram

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
            ax1.clear()
            ax1.plot(x_data, y_data)
            ax1.set_xlabel("X")
            ax1.set_ylabel("ADC Values")
            ax1.set_title("Real-time Data Plot")

            # Secondary X-axis (Time in Seconds)
            ax2.clear()
            ax2.set_xlim(ax1.get_xlim())  # Synchronize with the sample number axis
            ax2.set_xlabel("Time (seconds)")
            ax2.xaxis.set_label_position("top")
            time_ticks = np.array(x_data[::500])  # Adjust tick density
            ax2.set_xticks(time_ticks)
            ax2.set_xticklabels(
                (time_ticks / SAMPLE_RATE).round(2)
            )  # Convert to seconds with 2 decimal places

            # Secondary Y-axis 1 (Microvolts)
            ax3.clear()
            ax3.spines["right"].set_position(
                ("outward", 60)
            )  # Offset the secondary Y-axis
            ax3.set_ylabel("Microvolts (µV)")
            ax3.yaxis.set_label_position("right")
            ax3.set_ylim(ax1.get_ylim())  # Ensure synchronization
            ax3.set_yticks(ax1.get_yticks())
            ax3.set_yticklabels((ax1.get_yticks() * MICROVOLT_CONVERSION).round(2))

            # Secondary Y-axis 2 (Kilograms)
            ax4.clear()
            ax4.spines["right"].set_position(
                ("outward", 120)
            )  # Offset the tertiary Y-axis
            ax4.set_ylabel("Kilograms (kg)")
            ax4.yaxis.set_label_position("right")
            ax4.set_ylim(ax1.get_ylim())  # Ensure synchronization
            ax4.set_yticks(ax1.get_yticks())
            ax4.set_yticklabels((ax3.get_yticks() * KG_CONVERSION).round(2))

            # Clear and update the histogram
            ax_hist.clear()
            ax_hist.hist(
                y_data,
                bins=100,
                orientation="horizontal",
                alpha=0.7,
                density=True,
            )
            ax_hist.set_ylabel("ADC Values")  # Shares the same y-axis
            ax_hist.set_xlabel("Frequency")
            ax_hist.set_title("Distribution")

            # Calculate mean and standard deviation
            mean = np.mean(y_data)
            std = np.std(y_data)
            uV_mean = mean * MICROVOLT_CONVERSION
            uV_std = std * MICROVOLT_CONVERSION
            kg_mean = uV_mean * KG_CONVERSION
            kg_std = uV_std * KG_CONVERSION

            # Generate data for Gaussian curve
            y_range = np.linspace(min(y_data), max(y_data), 100)
            gaussian = (1 / (std * np.sqrt(2 * np.pi))) * np.exp(
                -0.5 * ((y_range - mean) / std) ** 2
            )
            gaussian_scaled = gaussian

            # Plot Gaussian curve
            ax_hist.plot(gaussian_scaled, y_range, color="red", label="Gaussian Fit")
            ax_hist.text(
                0.95,
                0.95,
                f"Mean: {mean:.2f} ADC\nStd: {std:.2f} ADC\n\n"
                f"Mean: {uV_mean:.2f} uV\nStd: {uV_std:.2f} uV\n\n"
                f"Mean: {kg_mean:.2f} kg\nStd: {kg_std:.2f} kg",
                transform=ax_hist.transAxes,
                fontsize=10,
                verticalalignment="top",
                horizontalalignment="right",
                bbox=dict(facecolor="white", alpha=0.8, edgecolor="gray"),
            )

            ax_hist.legend()

            plt.tight_layout()
            plt.draw()
            # plt.pause(0.1)

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
