import asyncio
from matplotlib import pyplot as plt
import matplotlib.gridspec as gridspec
import seaborn as sns
import numpy as np

from queue import Queue
from datetime import datetime, timedelta

from PyQt6.QtWidgets import QApplication
import pyqtgraph as pg
from pglive.sources.data_connector import DataConnector
from pglive.sources.live_plot_widget import LivePlotWidget
from pglive.sources.live_plot import LiveLinePlot

from time import sleep

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
TARING_THRESHOLD_UV = 1.0  # uV value


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

        if to_compute_length <= 0:
            return []

        # mode='valid' outputs (signal - kernel + 1) size. We give it (signal - kernel + 1)
        new_valid_part = np.convolve(
            self.y_data[-to_compute_length - len(self.kernel) + 1 :],
            self.kernel,
            mode="valid",
        )
        self.big_convolution += list(new_valid_part)

        # TODO: remove old data from y_data to keep only necessary part for next convolutions?
        return new_valid_part


def calculate_hist(data_full):
    # Calculate histogram and Gaussian fit
    hist, bin_edges = np.histogram(data_full[-4000:], bins=100, density=True)
    bin_centers = (bin_edges[:-1] + bin_edges[1:]) / 2

    mean_ch3 = np.mean(data_full[-4000:])
    std_ch3 = np.std(data_full[-4000:])
    gaussian_fit = (1 / (std_ch3 * np.sqrt(2 * np.pi))) * np.exp(
        -0.5 * ((bin_centers - mean_ch3) / std_ch3) ** 2
    )
    return hist, gaussian_fit, bin_centers


def initialize_plot():
    """Initializes the pglive plot widget and connectors with advanced features."""
    pg.setConfigOptions(antialias=True)

    layout = pg.LayoutWidget()
    layout.layout.setSpacing(0)

    def configure_histogram(
        title,
        primary_label,
        curve_configs,
        linked_y_axis_view,
    ):
        plot_widget = LivePlotWidget(title=title)
        plot_item = plot_widget.plotItem
        plot_item.setLabels(left=primary_label)

        linked_y_axis_view.setYLink(None)
        plot_item.enableAutoRange(enable=False)

        assert len(curve_configs) == 2

        def update_hist_view():
            linked_range = linked_y_axis_view.viewRange()
            # print("update_hist", linked_range)
            # plot_item.setYRange(linked_range[1][0], linked_range[1][1])
            # plot_item.setYRange(1000, 100000)

        curve_hist = LiveLinePlot(
            pen=curve_configs[0]["pen"], name=curve_configs[0]["name"]
        )
        plot_widget.addItem(curve_hist)

        curve_gaussian = LiveLinePlot(
            pen=curve_configs[1]["pen"], name=curve_configs[1]["name"]
        )
        plot_widget.addItem(curve_gaussian)

        # Link histogram Y-axis to the provided view
        linked_y_axis_view.sigYRangeChanged.connect(update_hist_view)

        return plot_widget, plot_item, [curve_hist, curve_gaussian]

    def configure_plot(
        title,
        left_label,
        curve_configs,
        secondary_axis_label,
        secondary_conversion,
        tertiary_axis_label,
        tertiary_conversion,
    ):
        """Configures a plot with primary, secondary, and tertiary Y-axes."""
        plot_widget = LivePlotWidget(title=title)
        plot_item = plot_widget.plotItem
        plot_item.setLabels(left=left_label)

        curves = [
            LiveLinePlot(pen=config["pen"], name=config["name"])
            for config in curve_configs
        ]
        for curve in curves:
            plot_widget.addItem(curve)

        # Secondary axis
        secondary_view = pg.ViewBox()
        plot_item.showAxis("right")
        plot_item.scene().addItem(secondary_view)
        plot_item.getAxis("right").linkToView(secondary_view)
        secondary_view.setXLink(plot_item)
        plot_item.getAxis("right").setLabel(secondary_axis_label)

        # Apply scaling for secondary axis
        def update_secondary_view():
            view_range = plot_item.vb.viewRange()
            secondary_view.setYRange(
                view_range[1][0] * secondary_conversion,
                view_range[1][1] * secondary_conversion,
            )

        plot_item.vb.sigYRangeChanged.connect(update_secondary_view)

        # Tertiary axis
        tertiary_view = pg.ViewBox()
        tertiary_axis = pg.AxisItem("right")
        plot_item.layout.addItem(tertiary_axis, 2, 3)
        plot_item.scene().addItem(tertiary_view)
        tertiary_axis.linkToView(tertiary_view)
        tertiary_view.setXLink(plot_item)
        tertiary_axis.setZValue(-10000)
        tertiary_axis.setLabel(tertiary_axis_label)

        # Apply scaling for tertiary axis
        def update_tertiary_view():
            view_range = plot_item.vb.viewRange()
            tertiary_view.setYRange(
                view_range[1][0] * secondary_conversion * tertiary_conversion,
                view_range[1][1] * secondary_conversion * tertiary_conversion,
            )

        plot_item.vb.sigYRangeChanged.connect(update_tertiary_view)

        return plot_widget, plot_item, curves

    # Configure raw data plot
    raw_curve_configs = [{"pen": "r", "name": "ch3"}, {"pen": "orange", "name": "ch2"}]
    raw_plot_widget, raw_plot_item, raw_curves = configure_plot(
        title="Real-time Data Plot (Raw Signals)",
        left_label="Raw ADC values",
        curve_configs=raw_curve_configs,
        secondary_axis_label="Microvolts (µV)",
        secondary_conversion=MICROVOLT_CONVERSION,
        tertiary_axis_label="Kilograms (kg)",
        tertiary_conversion=KG_CONVERSION,
    )

    # Configure filtered and tared data plot
    filtered_curve_configs = [
        {"pen": "r", "name": "ch3 (filtered, tared)"},
        {"pen": "orange", "name": "ch2 (filtered, tared)"},
    ]
    filtered_plot_widget, filtered_plot_item, filtered_curves = configure_plot(
        title="Filtered and Tared Signals",
        left_label="Filtered ADC values",
        curve_configs=filtered_curve_configs,
        secondary_axis_label="Microvolts (µV)",
        secondary_conversion=MICROVOLT_CONVERSION,
        tertiary_axis_label="Kilograms (kg)",
        tertiary_conversion=KG_CONVERSION,
    )

    # Configure histogram and Gaussian plot
    hist_curve_configs = [
        {"pen": "b", "name": "Histogram"},
        {"pen": "r", "name": "Gaussian Fit"},
    ]
    raw_plot_item.enableAutoRange(enable=False)
    hist_plot_widget, hist_plot_item, hist_curves = configure_histogram(
        title="Histogram and Gaussian Fit",
        primary_label="ADC Value",
        curve_configs=hist_curve_configs,
        linked_y_axis_view=raw_plot_item.vb,  # Link to raw plot's primary Y-axis
    )

    layout.addWidget(raw_plot_widget, row=0, col=0)
    layout.addWidget(filtered_plot_widget, row=1, col=0)
    layout.addWidget(hist_plot_widget, row=0, col=1)

    # Data connectors
    data_connectors = {
        "dc_ch3": DataConnector(raw_curves[0], max_points=4000),
        "dc_ch2": DataConnector(raw_curves[1], max_points=4000),
        "dc_ch3_filtered_tared": DataConnector(filtered_curves[0], max_points=4000),
        "dc_ch2_filtered_tared": DataConnector(filtered_curves[1], max_points=4000),
        "dc_histogram": DataConnector(hist_curves[0], max_points=100),
        "dc_gaussian": DataConnector(hist_curves[1], max_points=100),
    }

    layout.show()

    plot_classes = {
        **data_connectors,
        "pw": raw_plot_widget,
        "pw_filtered": filtered_plot_widget,
        "pw_hist": hist_plot_widget,
        "p1": raw_plot_item,
        "p4": filtered_plot_item,
        "ph": hist_plot_item,
        "layout": layout,
    }

    return plot_classes


async def plotter2(plot_classes, shutdown_event):
    """Handles real-time plotting using pglive."""
    x_data = []
    data_counter = 0
    kernel = gen_lowpass_filter_kernel()
    filter_delay = len(kernel) // 2

    ch3_filtered = IncrementalConvolution(kernel)
    ch2_filtered = IncrementalConvolution(kernel)

    tared = False
    tare_offset = [0, 0]

    ch3_data_full = []
    ch2_data_full = []

    i = 0

    while not shutdown_event.is_set():  # TODO asyncio queue
        if not plotting_queue.empty():
            message = plotting_queue.get_nowait()

            x_data = list(range(data_counter, data_counter + len(message)))
            data_counter += len(message)

            # Extract channel data
            ch3_data = [sample["channels"][2] for sample in message]
            ch2_data = [sample["channels"][1] for sample in message]

            ch3_data_full.extend(ch3_data)
            ch2_data_full.extend(ch2_data)

            # Apply filtering
            ch3_filtered_data = ch3_filtered.process(ch3_data)
            ch2_filtered_data = ch2_filtered.process(ch2_data)

            # Calculate taring if conditions are met
            if (
                np.std(ch2_data_full) * MICROVOLT_CONVERSION < TARING_THRESHOLD_UV
                and np.std(ch3_data_full) * MICROVOLT_CONVERSION < TARING_THRESHOLD_UV
                and data_counter > SAMPLE_RATE * 2
                and not tared
            ):
                tare_offset = [np.mean(ch2_data_full), np.mean(ch3_data_full)]
                print("Tared!")
                tared = True

            if tared:
                ch2_filtered_tared = np.array(ch2_filtered_data) - tare_offset[0]
                ch3_filtered_tared = np.array(ch3_filtered_data) - tare_offset[1]

                x_data_filtered = list(
                    range(
                        x_data[0] + filter_delay,
                        x_data[0] + filter_delay + ch2_filtered_tared.shape[0],
                    )
                )

                plot_classes["dc_ch3_filtered_tared"].cb_append_data_array(
                    ch3_filtered_tared, x_data_filtered
                )
                plot_classes["dc_ch2_filtered_tared"].cb_append_data_array(
                    ch2_filtered_tared, x_data_filtered
                )

            plot_classes["dc_ch3"].cb_append_data_array(ch3_data, x_data)
            plot_classes["dc_ch2"].cb_append_data_array(ch2_data, x_data)

            # Calculate histogram and Gaussian fit
            hist_ch3, gaussian_fit_ch3, bin_centers_ch3 = calculate_hist(ch3_data_full)

            if len(ch3_data_full) > 400 and i % 20 == 0:
                std_dev_ch2 = np.std(ch2_data_full[-4000:])
                std_dev_ch3 = np.std(ch3_data_full[-4000:])
                print("std", std_dev_ch2, std_dev_ch3)

            # Update histogram and Gaussian data
            plot_classes["dc_histogram"].cb_set_data(bin_centers_ch3, hist_ch3)
            plot_classes["dc_gaussian"].cb_set_data(bin_centers_ch3, gaussian_fit_ch3)

            i += 1

        await asyncio.sleep(0.01)


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
                uV_std_ch2 < TARING_THRESHOLD_UV
                and uV_std_ch3 < TARING_THRESHOLD_UV
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

        if tared:
            ch2_tared = np.array(ch2_filtered_section) - tare_offset[0]
            ch3_tared = np.array(ch3_filtered_section) - tare_offset[1]

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

            # ratios = np.zeros_like(ch3_tared)

            # mask = (np.abs(ch2_tared) > std_ch3 * 2) & (np.abs(ch3_tared) > std_ch3 * 2)

            # print(mask)
            # ratios[mask] = (ch3_tared[mask] - ch2_tared[mask]) / (
            #     ch3_tared[mask] + ch2_tared[mask]
            # )
            # # ratios = (ch3_tared - ch2_tared) / (ch3_tared + ch2_tared)  # TODO epsilon
            # ax_angle.clear()
            # ax_angle.plot(
            #     x_filtered,
            #     mask,
            #     label="Ratio between signals (unitless)",
            # )
            # ax_angle.legend()
            # ax_angle.set_title("Ratio Between Signals")
            # ax_angle.set_xlabel("Time (samples)")
            # ax_angle.set_ylabel("Ratio (unitless)")

        plt.pause(0.1)  # Small pause to prevent busy waiting.

    print("shutting down plotter")


def update_data(data: list):
    global next_push_time
    global plotting_queue
    global buffer

    # plotting_queue.put(data)

    buffer.extend(data)

    if datetime.now() >= next_push_time:
        # print("buffer", len(buffer))
        plotting_queue.put(buffer)

        buffer = []  # Clear the buffer
        next_push_time = datetime.now() + timedelta(milliseconds=6)
