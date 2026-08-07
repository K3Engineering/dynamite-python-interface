# Run it like so: `python -m tests.test_kvs`

import unittest

from dynamite_sampler_kvs import MAX_KEY_LEN, MAX_VAL_LEN, KvsClient


class CheckKeyValTest(unittest.TestCase):
    def test_valid(self):
        KvsClient._check_key_val("exc", "4.53,nominal")
        KvsClient._check_key_val("ch0.raw")  # GET-style, no value

    def test_empty_and_long_keys(self):
        with self.assertRaises(ValueError):
            KvsClient._check_key_val("")
        with self.assertRaises(ValueError):
            KvsClient._check_key_val("k" * (MAX_KEY_LEN + 1))

    def test_equals_sign_rejected(self):
        # The firmware splits SET data at the first '=', so a key containing
        # one would silently write under a truncated key.
        with self.assertRaises(ValueError):
            KvsClient._check_key_val("a=b", "1")

    def test_value_length(self):
        with self.assertRaises(ValueError):
            KvsClient._check_key_val("k", "")
        with self.assertRaises(ValueError):
            KvsClient._check_key_val("k", "v" * (MAX_VAL_LEN + 1))


if __name__ == "__main__":
    unittest.main()
