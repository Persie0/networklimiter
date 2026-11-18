"""Global rate parser tests, without platform/Qt dependencies."""
import unittest

from networklimiter.core import DEFAULT_KIB, MAX_KIB, RateError, to_pidnl_kbps, validate_rate


class RateTests(unittest.TestCase):
    def test_default_is_one_mebibyte_per_second(self):
        self.assertEqual(DEFAULT_KIB, 1024)

    def test_kib_to_decimal_kbps(self):
        self.assertEqual(to_pidnl_kbps(100), "819")
        self.assertEqual(to_pidnl_kbps(1024), "8389")
        self.assertEqual(to_pidnl_kbps(1), "8")

    def test_reject_non_numeric_out_of_range_and_bool(self):
        for value in (None, True, False, -10, 0, 1.1, "100", MAX_KIB + 1):
            with self.subTest(value=value), self.assertRaises(RateError):
                validate_rate(value)

    def test_bounds(self):
        self.assertEqual(validate_rate(1), 1)
        self.assertEqual(validate_rate(MAX_KIB), MAX_KIB)


if __name__ == "__main__":
    unittest.main()
