"""Behavior tests for spatial winnowing candidate fingerprints."""

from collections import Counter
import unittest

from stormcopy.winnowing import _selected_positions, spatial_winnow


def line(tokens, origin=(0, 0, 0), axis=0):
    out = {}
    for i, token in enumerate(tokens):
        xyz = list(origin)
        xyz[axis] += i
        out[tuple(xyz)] = [token]
    return out


class SpatialWinnowingTests(unittest.TestCase):
    def test_xml_order_and_component_order_do_not_matter(self):
        parts = line([f"part-{i}" for i in range(12)])
        parts[(3, 0, 0)].append("second-part")
        reversed_parts = {xyz: list(reversed(values))
                          for xyz, values in reversed(list(parts.items()))}
        self.assertEqual(spatial_winnow(parts), spatial_winnow(reversed_parts))

    def test_translation_preserves_hashes_and_shifts_samples(self):
        parts = line([f"part-{i}" for i in range(12)])
        shifted = {(x + 19, y - 8, z + 2): values
                   for (x, y, z), values in parts.items()}
        hashes, samples = spatial_winnow(parts)
        shifted_hashes, shifted_samples = spatial_winnow(shifted)
        self.assertTrue(hashes)
        self.assertEqual(hashes, shifted_hashes)
        self.assertEqual({h: (x + 19, y - 8, z + 2) for h, (x, y, z) in samples.items()},
                         shifted_samples)

    def test_embedded_copied_run_retains_fingerprints(self):
        copied = [f"component-{i}" for i in range(12)]
        original, _ = spatial_winnow(line(copied))
        embedded, _ = spatial_winnow(line(
            [f"before-{i}" for i in range(5)] + copied +
            [f"after-{i}" for i in range(5)]))
        self.assertGreater(sum((original & embedded).values()), 0)

    def test_changed_token_only_disrupts_nearby_grams(self):
        parts = [f"component-{i}" for i in range(30)]
        original, _ = spatial_winnow(line(parts))
        parts[15] = "replacement"
        modified, _ = spatial_winnow(line(parts))
        common = sum((original & modified).values())
        self.assertGreater(common, 0)
        self.assertLess(common, sum(original.values()))

    def test_gap_cannot_create_false_cross_gap_sequence(self):
        parts = line(["a", "b", "c"])
        parts.update(line(["d", "e", "f"], origin=(10, 0, 0)))
        self.assertEqual(spatial_winnow(parts)[0], Counter())

    def test_rightmost_minimum_breaks_ties(self):
        self.assertEqual(_selected_positions([5, 1, 1, 4], window=2), [1, 2])
        self.assertEqual(_selected_positions([2, 1, 1], window=5), [2])

    def test_invalid_parameters(self):
        with self.assertRaises(ValueError):
            spatial_winnow(line(["a"]), k=0)
        with self.assertRaises(ValueError):
            spatial_winnow(line(["a"]), window=0)


if __name__ == "__main__":
    unittest.main()
