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
SAMPLE_RATE = 1000
TARING_THRESHOLD = 1.0  # uV value


def gen_lowpass_filter_kernel():
    # Configuration.
    fL = 100  # Cutoff frequency.
    N = 259  # Filter length, must be odd.

    # Compute sinc filter.
    h = np.sinc(2 * fL / SAMPLE_RATE * (np.arange(N) - (N - 1) / 2))

    # Apply window.
    h *= np.blackman(N)

    # Normalize to get unity gain.
    h /= np.sum(h)

    return h


# Compute valid samples of a convolution based on incremental data
class IncrementalConvolution:
    def __init__(self, kernel):
        self.kernel = kernel
        self.y_data = []  # Storing the data
        self.big_convolution = []  # Storing the convoluted result

    def process(self, message: list):
        self.y_data += message

        valid_length = len(self.y_data) - len(self.kernel) + 1
        to_compute_length = valid_length - len(self.big_convolution)

        if to_compute_length > 0:
            # mode='valid' outputs (signal - kernel + 1) size. We give it (signal - kernel + 1)
            new_valid_part = np.convolve(
                self.y_data[-to_compute_length - len(self.kernel) + 1 :],
                self.kernel,
                mode="valid",
            )
            self.big_convolution += list(new_valid_part)

        # TODO: remove old data from y_data to keep only necessary part for next convolutions?
        return self.big_convolution


def plotter(shutdown_event):
    plt.ion()

    # Use gridspec for arranging main plot and histogram
    fig = plt.figure(figsize=(15, 10))
    gs = gridspec.GridSpec(3, 2, height_ratios=[3, 1, 1], width_ratios=[4, 1])
    ax1 = fig.add_subplot(gs[0])  # Main plot
    ax_seconds = ax1.twiny()
    ax_uV = ax1.twinx()
    ax_kg = ax1.twinx()
    ax_hist = fig.add_subplot(gs[1], sharey=ax1)  # Histogram

    ax_filtered = fig.add_subplot(gs[1, :])
    ax_filtered_seconds = ax_filtered.twiny()
    ax_filtered_uV = ax_filtered.twinx()  # µV Y-axis
    ax_filtered_kg = ax_filtered.twinx()  # kg Y-axis
    ax_angle = fig.add_subplot(gs[2, :])

    x_data, y_data_3, y_data_2 = [], [], []
    data_counter = 0  # Track the total number of data points for the X-axis
    kernel = gen_lowpass_filter_kernel()

    ch3_filtered = IncrementalConvolution(kernel)
    ch2_filtered = IncrementalConvolution(kernel)

    tared = False  # Initial state of taring
    tare_offset = [0, 0]  # in raw ADC value

    while not shutdown_event.is_set():
        if not plotting_queue.empty():
            message = plotting_queue.get_nowait()

            # Update data for the main plot
            x_data += list(
                range(data_counter, data_counter + len(message))
            )  # Adjust X-axis

            ch3_data = [sample["channels"][2] for sample in message]
            ch2_data = [sample["channels"][1] for sample in message]
            y_data_3 += ch3_data
            y_data_2 += ch2_data

            assert len(y_data_2) == len(y_data_3)

            ch3_filtered_data = ch3_filtered.process(ch3_data)
            ch2_filtered_data = ch2_filtered.process(ch2_data)

            # Limit the data for both axes
            x_data = x_data[(-SAMPLE_RATE * 8) :]
            y_data_2 = y_data_2[(-SAMPLE_RATE * 8) :]
            y_data_3 = y_data_3[(-SAMPLE_RATE * 8) :]

            ch3_filtered_section = ch3_filtered_data[x_data[0] :]
            ch2_filtered_section = ch2_filtered_data[x_data[0] :]
            x_filtered = (
                np.array(x_data[: len(ch3_filtered_section)]) + len(kernel) // 2
            )  # fix offset

            data_counter += len(message)  # Update counter for next X-axis range

            # Clear and update the main plot
            ax1.clear()
            ax1.plot(x_data, y_data_3, label="ch3")
            ax1.plot(x_data, y_data_2, label="ch2")
            ax1.plot(x_filtered, ch3_filtered_section, label="ch3 (filtered)")
            ax1.plot(x_filtered, ch2_filtered_section, label="ch2 (filtered)")
            ax1.legend()
            ax1.set_xlabel("X")
            ax1.set_ylabel("ADC Values")
            ax1.set_title("Real-time Data Plot")

            # Secondary X-axis (Time in Seconds)
            ax_seconds.clear()
            ax_seconds.set_xlim(
                ax1.get_xlim()
            )  # Synchronize with the sample number axis
            ax_seconds.set_xlabel("Time (seconds)")
            ax_seconds.xaxis.set_label_position("top")
            time_ticks = np.array(x_data[::500])  # Adjust tick density
            ax_seconds.set_xticks(time_ticks)
            ax_seconds.set_xticklabels(
                (time_ticks / SAMPLE_RATE).round(1)
            )  # Convert to seconds with 2 decimal places

            # Secondary Y-axis 1 (Microvolts)
            ax_uV.clear()
            ax_uV.spines["right"].set_position(
                ("outward", 60)
            )  # Offset the secondary Y-axis
            ax_uV.set_ylabel("Microvolts (µV)")
            ax_uV.yaxis.set_label_position("right")
            ax_uV.set_ylim(ax1.get_ylim())  # Ensure synchronization
            ax_uV.set_yticks(ax1.get_yticks())
            ax_uV.set_yticklabels((ax1.get_yticks() * MICROVOLT_CONVERSION).round(2))

            # Secondary Y-axis 2 (Kilograms)
            ax_kg.clear()
            ax_kg.spines["right"].set_position(
                ("outward", 120)
            )  # Offset the tertiary Y-axis
            ax_kg.set_ylabel("Kilograms (kg)")
            ax_kg.yaxis.set_label_position("right")
            ax_kg.set_ylim(ax1.get_ylim())  # Ensure synchronization
            ax_kg.set_yticks(ax1.get_yticks())
            ax_kg.set_yticklabels((ax_uV.get_yticks() * KG_CONVERSION).round(2))

            # Clear and update the histogram
            ax_hist.clear()
            ax_hist.hist(
                y_data_3,
                bins=100,
                orientation="horizontal",
                alpha=0.7,
                density=True,
            )
            ax_hist.hist(
                y_data_2,
                bins=100,
                orientation="horizontal",
                alpha=0.7,
                density=True,
            )
            ax_hist.set_ylabel("ADC Values")  # Shares the same y-axis
            ax_hist.set_xlabel("Frequency")
            ax_hist.set_title("Distribution")

            # Calculate mean and standard deviation
            mean_ch3 = np.mean(y_data_3)
            std_ch3 = np.std(y_data_3)
            uV_mean_ch3 = mean_ch3 * MICROVOLT_CONVERSION
            uV_std_ch3 = std_ch3 * MICROVOLT_CONVERSION
            kg_mean_ch3 = uV_mean_ch3 * KG_CONVERSION
            kg_std_ch3 = uV_std_ch3 * KG_CONVERSION

            mean_ch2 = np.mean(y_data_2)
            std_ch2 = np.std(y_data_2)
            uV_mean_ch2 = mean_ch2 * MICROVOLT_CONVERSION
            uV_std_ch2 = std_ch2 * MICROVOLT_CONVERSION
            kg_mean_ch2 = uV_mean_ch2 * KG_CONVERSION
            kg_std_ch2 = uV_std_ch2 * KG_CONVERSION

            if (
                uV_std_ch2 < TARING_THRESHOLD
                and uV_std_ch3 < TARING_THRESHOLD
                and len(x_data) > SAMPLE_RATE * 2
            ):
                if not tared:
                    tare_offset = [mean_ch2, mean_ch3]
                    tared = True

            eps = 1e-8

            # Generate data for Gaussian curve
            y_range = np.linspace(min(y_data_3), max(y_data_3), 100)
            gaussian = (1 / ((std_ch3 + eps) * np.sqrt(2 * np.pi))) * np.exp(
                -0.5 * ((y_range - mean_ch3) / std_ch3) ** 2
            )
            gaussian_scaled = gaussian

            # Plot Gaussian curve
            ax_hist.plot(gaussian_scaled, y_range, color="red", label="Gaussian Fit")
            ax_hist.text(
                0.95,
                0.95,
                f"Mean: {mean_ch3:.2f} ADC\nStd: {std_ch3:.2f} ADC\n\n"
                f"Mean: {uV_mean_ch3:.2f} uV\nStd: {uV_std_ch3:.2f} uV\n\n"
                f"Mean: {kg_mean_ch3:.2f} kg\nStd: {kg_std_ch3:.2f} kg",
                transform=ax_hist.transAxes,
                fontsize=10,
                verticalalignment="top",
                horizontalalignment="right",
                bbox=dict(facecolor="white", alpha=0.8, edgecolor="gray"),
            )

            ax_hist.legend()

            plt.tight_layout()
            plt.draw()

        if tared:  # Check if the signal is tared
            ch2_tared = [item - tare_offset[0] for item in ch2_filtered_section]
            ch3_tared = [item - tare_offset[1] for item in ch3_filtered_section]

            ax_filtered.clear()
            ax_filtered.plot(
                x_filtered,
                ch3_tared,
                label="ch3 (filtered)",
            )
            ax_filtered.plot(
                x_filtered,
                ch2_tared,
                label="ch2 (filtered)",
            )

            ax_filtered.legend()
            ax_filtered.set_title("Filtered Signals (Tared)")
            ax_filtered.set_xlabel("Time (samples)")
            ax_filtered.set_ylabel("Filtered ADC Values")

            ax_filtered_seconds.clear()
            ax_filtered_seconds.set_xlim(
                ax_filtered.get_xlim()
            )  # Synchronize with the sample number axis
            ax_filtered_seconds.set_xlabel("Time (seconds)")
            ax_filtered_seconds.xaxis.set_label_position("top")
            time_ticks = np.array(x_filtered[::500])  # Adjust tick density
            ax_filtered_seconds.set_xticks(time_ticks)
            ax_filtered_seconds.set_xticklabels(
                (time_ticks / SAMPLE_RATE).round(1)
            )  # Convert to seconds with 2 decimal places

            # Secondary Y-axis for µV
            ax_filtered_uV.clear()
            ax_filtered_uV.spines["right"].set_position(("outward", 60))
            ax_filtered_uV.set_ylabel("Microvolts (µV)")
            ax_filtered_uV.yaxis.set_label_position("right")
            ax_filtered_uV.set_ylim(ax_filtered.get_ylim())  # Synchronize Y limits
            ax_filtered_uV.set_yticks(ax_filtered.get_yticks())
            ax_filtered_uV.set_yticklabels(
                (ax_filtered.get_yticks() * MICROVOLT_CONVERSION).round(2)
            )

            # Secondary Y-axis for kg
            ax_filtered_kg.clear()
            ax_filtered_kg.spines["right"].set_position(("outward", 120))
            ax_filtered_kg.set_ylabel("Kilograms (kg)")
            ax_filtered_kg.yaxis.set_label_position("right")
            ax_filtered_kg.set_ylim(ax_filtered.get_ylim())  # Synchronize Y limits
            ax_filtered_kg.set_yticks(ax_filtered.get_yticks())
            ax_filtered_kg.set_yticklabels(
                (ax_filtered_uV.get_yticks() * KG_CONVERSION).round(2)
            )

            angles = np.degrees(np.arctan2(ch3_tared, ch2_tared))  # Angle calculation
            ax_angle.clear()
            ax_angle.plot(
                x_filtered,
                angles,
                label="Angle between signals (degrees)",
            )
            ax_angle.legend()
            ax_angle.set_title("Angle Between Signals")
            ax_angle.set_xlabel("Time (samples)")
            ax_angle.set_ylabel("Angle (degrees)")

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
