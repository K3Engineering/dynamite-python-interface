# Run it like so: `python -m tests.test_convolution`

import unittest

import numpy as np
from chart_plotter import IncrementalConvolution


class TestConv(unittest.TestCase):
    def test1(self):
        myConv = IncrementalConvolution(kernel=[1, -1, 0, 0, 0])
        messages = [[1, 0, 0, 0], [1, 0, 0, 0], [1, 0, 0, 0]]
        for message in messages:
            result1 = myConv.process(message)

        m2 = [x for xs in messages for x in xs]
        myConv = IncrementalConvolution(kernel=[1, -1, 0, 0, 0])
        result2 = myConv.process(m2)

        result3 = np.convolve(m2, [1, -1, 0, 0, 0], mode="valid").tolist()
        print(result1, result2)
        self.assertTrue(result1 == result2)
        self.assertTrue(result2 == result3)


if __name__ == "__main__":
    unittest.main()
