"""Geometry previews stay bounded and accept Stormworks' XML extensions."""

from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

from stormcopy.visualizers import (
    VehicleSample,
    VehicleVisualizerPanel,
    _category,
    _project,
    match_samples,
    sample_vehicle,
)
from stormcopy.voxel_render import render_voxels


def _sample(points):
    coordinates = [point[:3] for point in points]
    bounds = (
        tuple(min(point[axis] for point in coordinates) for axis in range(3)),
        tuple(max(point[axis] for point in coordinates) for axis in range(3)),
    )
    return VehicleSample(tuple(points), len(points), bounds)


def _shape(offset=(0, 0, 0), *, changed=False):
    blocks = (
        (0, 0, 0, "structure", "hull"),
        (1, 0, 0, "structure", "hull"),
        (2, 0, 0, "mechanical", "engine"),
        (0, 1, 0, "electrical", "sensor"),
        (1, 1, 0, "control", "microcontroller"),
        (2, 1, 0, "structure", "glass"),
    )
    if changed:
        blocks = blocks[:5] + ((2, 1, 0, "mechanical", "wheel"),)
    return _sample(tuple(
        (x + offset[0], y + offset[1], z + offset[2], category, kind)
        for x, y, z, category, kind in blocks
    ))


class _CanvasSpy:
    def __init__(self):
        self.polygons = []
        self.rectangles = []
        self.images = []
        self.created_images = 0

    def delete(self, _which):
        self.polygons.clear()
        self.rectangles.clear()
        self.images.clear()

    def winfo_width(self):
        return 480

    def winfo_height(self):
        return 320

    def create_polygon(self, *coordinates, **options):
        self.polygons.append((coordinates, options))

    def create_rectangle(self, *coordinates, **options):
        self.rectangles.append((coordinates, options))

    def create_image(self, *coordinates, **options):
        self.images.append((coordinates, options))
        self.created_images += 1
        return self.created_images


class VehicleVisualizerTests(unittest.TestCase):
    def test_retains_every_component_of_large_vehicle(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "vehicle.xml"
            components = "".join(
                f'<c d="01_block"><o><vp x="{i}" y="{i % 3}" z="0"/></o></c>'
                for i in range(2000))
            path.write_text(
                '<vehicle><body><initial_local_transform 00="1" 00="2"/>'
                f'<components>{components}</components></body></vehicle>',
                encoding="utf-8")
            first = sample_vehicle(path)
            second = sample_vehicle(path)
            self.assertEqual(first.components, 2000)
            self.assertEqual(first.sampled, 2000)
            self.assertEqual(first.bounds, ((0, 0, 0), (1999, 2, 0)))
            self.assertLessEqual(first.storage_bytes, 2000 * 16)
            self.assertNotIsInstance(first.points, tuple)
            self.assertEqual(first.points, second.points)
            self.assertEqual(first.points[0][:3], (0, 0, 0))
            self.assertEqual(first.points[-1][:3], (1999, 1, 0))

    def test_stale_vehicle_load_can_stop_during_parsing(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "vehicle.xml"
            path.write_text(
                "<vehicle>" +
                '<c d="block"><vp x="1" y="2" z="3"/></c>' * 5000 +
                "</vehicle>", encoding="utf-8")
            calls = 0

            def cancelled():
                nonlocal calls
                calls += 1
                return calls >= 3

            with self.assertRaises(InterruptedError):
                sample_vehicle(path, cancelled=cancelled)
            self.assertGreaterEqual(calls, 3)

    def test_skips_comment_and_cdata_and_categorizes_positions(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "vehicle.xml"
            path.write_text(
                '<vehicle><!-- <c><vp x="90" y="0" z="0"/></c> -->'
                '<![CDATA[<c><vp x="91" y="0" z="0"/></c>]]>'
                '<c d="engine"><o><vp x="1" y="2" z="3"/></o></c>'
                '<c d="microcontroller"><o><vp x="2" y="2" z="3"/></o></c>'
                '</vehicle>', encoding="utf-8")
            sample = sample_vehicle(path)
            self.assertEqual(sample.components, 2)
            self.assertEqual([point[3] for point in sample.points],
                             ["mechanical", "control"])
            self.assertEqual([point[4] for point in sample.points],
                             ["engine", "microcontroller"])

    def test_translated_shape_has_matching_cubes_on_both_sides(self):
        input_sample = _shape()
        workshop_sample = _shape((17, -9, 4))
        overlay = match_samples(input_sample, workshop_sample)
        self.assertEqual(overlay.input_labels, ("matched",) * 6)
        self.assertEqual(overlay.match_labels, ("matched",) * 6)
        self.assertEqual(overlay.offset, (-17, 9, -4))

    def test_aligned_changed_block_is_not_colored_as_a_match(self):
        overlay = match_samples(_shape(), _shape((7, 0, -12), changed=True))
        self.assertEqual(overlay.input_labels, ("matched",) * 5 + ("changed",))
        self.assertEqual(overlay.match_labels, ("matched",) * 5 + ("changed",))

    def test_unrelated_geometry_is_not_reported_as_matched(self):
        other = _sample(tuple(
            (10 + x * 7, 20 + x * 11, x * 13, "structure", f"other_{x}")
            for x in range(6)
        ))
        overlay = match_samples(_shape(), other)
        self.assertEqual(overlay.input_labels, ("unverified",) * 6)
        self.assertEqual(overlay.match_labels, ("unverified",) * 6)
        self.assertIsNone(overlay.offset)

    def test_missing_workshop_xml_keeps_input_unverified(self):
        overlay = match_samples(_shape(), None)
        self.assertEqual(overlay.input_labels, ("unverified",) * 6)
        self.assertEqual(overlay.match_labels, ())
        self.assertIsNone(overlay.offset)

    def test_two_coherent_evidence_anchors_reveal_a_small_copied_section(self):
        input_sample = _sample((
            (0, 0, 0, "structure", "hull"),
            (1, 0, 0, "mechanical", "engine"),
            (20, 0, 0, "structure", "input_only_a"),
            (21, 0, 0, "structure", "input_only_b"),
        ))
        workshop_sample = _sample((
            (9, -3, 2, "structure", "hull"),
            (10, -3, 2, "mechanical", "engine"),
            (40, 0, 0, "structure", "workshop_only_a"),
            (41, 0, 0, "structure", "workshop_only_b"),
        ))
        evidence = (
            {"channel": "geometry", "query_position": (0, 0, 0),
             "workshop_position": (9, -3, 2)},
            {"channel": "winnowing", "query_position": (1, 0, 0),
             "workshop_position": (10, -3, 2)},
        )
        overlay = match_samples(input_sample, workshop_sample, evidence)
        self.assertEqual(overlay.offset, (-9, 3, -2))
        self.assertEqual(overlay.input_labels,
                         ("matched", "matched", "unmatched", "unmatched"))
        self.assertEqual(overlay.match_labels,
                         ("matched", "matched", "unmatched", "unmatched"))

    def test_match_colours_include_changed_and_missing_tail_of_large_vehicle(self):
        count = 2400
        input_sample = _sample(tuple(
            (x, 0, 0, "structure", "hull") for x in range(count)
        ))
        workshop_sample = _sample(tuple(
            (x, 0, 0, "structure", "wheel" if x == count - 2 else "hull")
            for x in range(count - 1)
        ))
        evidence = (
            {"channel": "geometry", "query_position": (0, 0, 0),
             "workshop_position": (0, 0, 0)},
            {"channel": "winnowing", "query_position": (1, 0, 0),
             "workshop_position": (1, 0, 0)},
        )
        overlay = match_samples(input_sample, workshop_sample, evidence)
        self.assertEqual(len(overlay.input_labels), count)
        self.assertEqual(len(overlay.match_labels), count - 1)
        self.assertEqual(overlay.input_labels[:count - 2], ("matched",) * (count - 2))
        self.assertEqual(overlay.input_labels[-2:], ("changed", "unmatched"))
        self.assertEqual(overlay.match_labels[-1], "changed")
        self.assertEqual(overlay.matched_count, count - 2)

    def test_one_common_block_and_one_anchor_are_insufficient(self):
        input_sample = _sample((
            (0, 0, 0, "structure", "hull"),
            (3, 2, 1, "control", "input_controller"),
            (4, 2, 1, "mechanical", "input_engine"),
            (5, 2, 1, "structure", "input_window"),
        ))
        workshop_sample = _sample((
            (30, 0, 0, "structure", "hull"),
            (100, 2, 1, "control", "workshop_controller"),
            (101, 2, 1, "mechanical", "workshop_engine"),
            (102, 2, 1, "structure", "workshop_window"),
        ))
        evidence = ({"channel": "geometry", "query_position": (0, 0, 0),
                     "workshop_position": (30, 0, 0)},)
        overlay = match_samples(input_sample, workshop_sample, evidence)
        self.assertEqual(overlay.input_labels, ("unverified",) * 4)
        self.assertEqual(overlay.match_labels, ("unverified",) * 4)
        self.assertIsNone(overlay.offset)

    def test_anchors_without_any_matching_sampled_type_stay_unverified(self):
        input_sample = _shape()
        other = _sample(tuple((x + 9, y - 2, z + 1, category, "different_" + kind)
                              for x, y, z, category, kind in input_sample.points))
        anchors = (
            {"channel": "geometry", "query_position": (0, 0, 0),
             "workshop_position": (9, -2, 1)},
            {"channel": "winnowing", "query_position": (1, 0, 0),
             "workshop_position": (10, -2, 1)},
        )
        overlay = match_samples(input_sample, other, anchors)
        self.assertIsNone(overlay.offset)
        self.assertEqual(overlay.input_labels, ("unverified",) * 6)

    def test_disjoint_component_types_skip_large_alignment_sort(self):
        left = _sample(((0, 0, 0, "structure", "hull"),) * 100)
        right = _sample(((0, 0, 0, "structure", "wheel"),) * 100)
        with patch("stormcopy.visual_match._classify",
                   side_effect=AssertionError("unneeded sort")):
            overlay = match_samples(left, right)
        self.assertIsNone(overlay.offset)
        self.assertEqual(overlay.input_labels.count("unverified"), 100)

    def test_every_view_uses_one_bounded_raster_item_on_repeated_draw(self):
        panel = VehicleVisualizerPanel.__new__(VehicleVisualizerPanel)
        panel._yaw = .68
        panel._pitch = .52
        panel._zoom = 1.0
        sample = _sample(tuple(
            (x % 48, 0, x // 48, "structure", "hull") for x in range(2400)
        ))
        labels = ("matched",) * 2397 + ("changed", "unmatched", "unverified")
        with patch("stormcopy.visualizers.tk.PhotoImage") as photo_image:
            photo_image.return_value = object()
            for view in ("3D", "Top", "Side", "Front"):
                canvas = _CanvasSpy()
                panel._draw(canvas, sample, view, labels)
                self.assertEqual(len(canvas.images), 1, view)
                self.assertFalse(canvas.polygons, view)
                self.assertFalse(canvas.rectangles, view)
                frame = photo_image.call_args.kwargs["data"]
                self.assertTrue(frame.startswith(b"P6"), view)
                self.assertLessEqual(len(frame), 480 * 320 * 3 + 128, view)
                if view == "Top":
                    # Keep identical camera bounds. Only the final cube differs.
                    # Its pixels must be present even though it is beyond the
                    # old 1,200-component preview cap.
                    shortened = VehicleSample(sample.points[:-1], 2399,
                                              sample.bounds)
                    panel._draw(canvas, shortened, view, labels[:-1])
                    self.assertNotEqual(frame, photo_image.call_args.kwargs["data"])
                panel._draw(canvas, sample, view, labels)
                self.assertEqual(len(canvas.images), 1, view)
                self.assertEqual(canvas.created_images,
                                 3 if view == "Top" else 2, view)

    def test_stale_raster_aborts_and_duplicate_grid_keeps_last_colour(self):
        bounds = ((0, 0, 0), (1, 0, 0))
        point = (0, 0, 0, "structure", "hull")
        many = (point,) * 2000
        calls = 0

        def cancelled():
            nonlocal calls
            calls += 1
            return calls >= 3

        with self.assertRaises(InterruptedError):
            render_voxels(many, (), bounds, "3D", .68, .52, 1,
                          480, 320, cancelled=cancelled)
        frame = render_voxels(many, ("matched",) * 1999 + ("unmatched",),
                              bounds, "3D", .68, .52, 1, 480, 320)
        last_only = render_voxels((point,), ("unmatched",), bounds, "3D",
                                  .68, .52, 1, 480, 320)
        self.assertEqual(frame, last_only)

    def test_dense_top_view_includes_final_component_colour(self):
        bounds = ((0, 0, 0), (1, 0, 0))
        point = (0, 0, 0, "structure", "hull")
        many = (point,) * 25000
        earlier = render_voxels(many[:-1], ("matched",) * 24999,
                                bounds, "Top", .68, .52, 1, 480, 320)
        with_tail = render_voxels(many, ("matched",) * 24999 + ("unmatched",),
                                  bounds, "Top", .68, .52, 1, 480, 320)
        self.assertNotEqual(with_tail, earlier)

    def test_rejects_non_vehicle_or_entity(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "vehicle.xml"
            for xml in ('<other><c><vp x="1" y="2" z="3"/></c></other>',
                        '<!DOCTYPE vehicle><vehicle/>'):
                path.write_text(xml, encoding="utf-8")
                with self.assertRaises(ValueError):
                    sample_vehicle(path)

    def test_projections_are_distinct_and_finite(self):
        point = (3, 7, 11)
        views = {_project(point, view, 0.68, 0.52)[:2]
                 for view in ("3D", "Top", "Side", "Front")}
        self.assertEqual(len(views), 4)
        self.assertEqual(_category("01_block"), "structure")


if __name__ == "__main__":
    unittest.main()
