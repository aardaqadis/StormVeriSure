"""Bounded value-cache behavior without changing XML normalization."""

import unittest

from stormcopy.fingerprint import _cached_value, _value


class ValueCacheTests(unittest.TestCase):
    def tearDown(self):
        _cached_value.cache_clear()

    def test_repeated_short_values_are_cached_and_normalized(self):
        _cached_value.cache_clear()
        self.assertEqual(_value("1.000, 0e0, -0.0", "r"), "1,0,0")
        self.assertEqual(_value("1.000, 0e0, -0.0", "r"), "1,0,0")
        info = _cached_value.cache_info()
        self.assertEqual((info.hits, info.misses, info.currsize), (1, 1, 1))

    def test_attribute_name_is_part_of_cache_key(self):
        _cached_value.cache_clear()
        self.assertEqual(_value("001.0", "ratio"), "1")
        self.assertEqual(_value("001.0", "script_text"), "001.0")
        self.assertEqual(_cached_value.cache_info().currsize, 2)

    def test_long_values_still_normalize_without_entering_cache(self):
        _cached_value.cache_clear()
        long_numeric = ",".join(["1.000"] * 40)
        self.assertGreater(len(long_numeric), 128)
        self.assertEqual(_value(long_numeric, "r"), ",".join(["1"] * 40))
        long_text = "user text " * 40
        self.assertEqual(_value(long_text, "script"), long_text.strip())
        self.assertEqual(_cached_value.cache_info().currsize, 0)


if __name__ == "__main__":
    unittest.main()
