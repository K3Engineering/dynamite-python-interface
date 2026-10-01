"""Ladder setpoints and the nominal analog chain.

Pure functions shared by the calibration layer and the factory tools. The
ladder is the calibration board's resistor chain, powered by the DUT's own
excitation (ratiometric, so only resistor ratios matter):

    EXC+ --[R0=10k]--t1--[R1=10R]--...--t5--[R5=10k]--GND

Storage order of the five measured configs is (t1,t5), (t2,t4), (t3,t3),
(t4,t2), (t5,t1).
"""

ADC_COUNTS_PER_POLARITY = 1 << 23  # 24-bit bipolar

NOMINAL_LADDER_RESISTORS = (10000.0, 10.0, 10.0, 10.0, 10.0, 10000.0)

CAL_POINT_COUNT = 5
LADDER_RESISTOR_COUNT = 6


def ladder_setpoints_mv_per_v(resistors=NOMINAL_LADDER_RESISTORS):
    """Differential setpoints (mV/V of excitation) per config, storage order."""
    if len(resistors) != LADDER_RESISTOR_COUNT:
        raise ValueError(f"need {LADDER_RESISTOR_COUNT} ladder resistors")
    if any(r <= 0 for r in resistors):
        raise ValueError("ladder resistors must be positive")
    below = [0.0] * CAL_POINT_COUNT
    acc = 0.0
    for i in range(LADDER_RESISTOR_COUNT - 1, 0, -1):
        acc += resistors[i]
        below[i - 1] = acc
    total = acc + resistors[0]
    return [
        1000.0 * (below[k] - below[CAL_POINT_COUNT - 1 - k]) / total
        for k in range(CAL_POINT_COUNT)
    ]


def expected_counts_per_mvv(adc_fsr_v, afe_gain, pga_gain, exc_v):
    """Nominal analog chain: ADC counts per mV/V of load-cell output."""
    return ADC_COUNTS_PER_POLARITY * afe_gain * pga_gain / (adc_fsr_v * 1000.0) * exc_v
