"""Behavior and on-disk compatibility checks for MinHash/LSH."""

import unittest

from stormcopy.minhash import NUM_BANDS, NUM_HASHES, bands, estimate, signature


class MinHashTests(unittest.TestCase):
    def test_deterministic_order_independent_signature(self):
        expected = signature(["alpha", "beta", "gamma"])
        self.assertEqual(expected, signature(["gamma", "alpha", "beta", "alpha"]))
        self.assertEqual(len(expected), NUM_HASHES)
        # This pins the versioned hash protocol across processes and upgrades.
        self.assertEqual(expected[:4], (
            1099121602564988344, 63862721552300944,
            324864684363321885, 31444543798248481,
        ))
        self.assertEqual(bands(expected)[0], "00:2f2d5fd6ea9105f7a2498d0216e323ae")

    def test_empty_sets_have_no_lsh_bucket(self):
        empty = signature([])
        nonempty = signature(["block"])
        self.assertEqual(bands(empty), ())
        self.assertEqual(estimate(empty, empty), 1.0)
        self.assertEqual(estimate(empty, nonempty), 0.0)

    def test_disjoint_and_partial_overlap(self):
        first = signature(f"a{i}" for i in range(100))
        partial = signature(f"a{i}" for i in range(50, 150))
        disjoint = signature(f"b{i}" for i in range(100))
        self.assertEqual(estimate(first, disjoint), 0.0)
        self.assertGreater(estimate(first, partial), 0.15)
        self.assertLess(estimate(first, partial), 0.5)

    def test_one_token_change_keeps_lsh_candidates(self):
        source = signature(f"part{i}" for i in range(100))
        edited = signature([*(f"part{i}" for i in range(99)), "new-part"])
        self.assertEqual(len(bands(source)), NUM_BANDS)
        self.assertEqual(bands(source), bands(signature(f"part{i}" for i in range(100))))
        self.assertTrue(set(bands(source)) & set(bands(edited)))
        self.assertGreater(estimate(source, edited), 0.9)

    def test_reject_invalid_sketch(self):
        with self.assertRaises(ValueError):
            bands((1, 2, 3))
        with self.assertRaises(ValueError):
            estimate(signature(["block"]), (1, 2, 3))


if __name__ == "__main__":
    unittest.main()
