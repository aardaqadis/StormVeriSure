"""End-to-end coverage for canonicalization and multi-stage candidate search."""

from collections import Counter
from itertools import chain
import json
import os
from pathlib import Path
import tempfile
import unittest

from stormcopy.fingerprint import fingerprint_bytes, fingerprint_file
from stormcopy.index import (_aligned_cluster, connect, index_file, scan,
                             upgrade_search_index)
from stormcopy.minhash import bands, signature
from stormcopy.search_index import candidates, delete, load


def _vehicle(bodies, cosmetic=False, reverse=False):
    """Make moderately sized, valid Stormworks-style vehicle XML."""
    body_xml = []
    for number, parts in enumerate(bodies):
        components = []
        for x, y, z, kind in parts:
            if cosmetic:
                component = (f'<c t="1e0" d="{kind}"><o sc="blue" ac="FFFFFFFF" '
                             'r="1.000,0e0,0,0,1.0,0,0,0,1" bc="EEEEEEEE">'
                             f'<vp z="{z}" y="{y}" x="{x}"/></o></c>')
            else:
                component = (f'<c d="{kind}" t="1.0"><o bc="11111111" '
                             'r="1,0,0,0,1,0,0,0,1" ac="22222222" sc="red">'
                             f'<vp x="{x}" y="{y}" z="{z}"/></o></c>')
            components.append(component)
        if reverse:
            components.reverse()
        body_xml.append(f'<body unique_id="{1000 + number}"><components>'
                        + "".join(components) + '</components></body>')
    if reverse:
        body_xml.reverse()
    metadata = ('data_version="4" name="Edited"' if cosmetic else
                'name="Original" data_version="2"')
    author = "Other" if cosmetic else "Author"
    return (f'<vehicle {metadata}><authors><author username="{author}"/></authors>'
            f'<bodies>{"".join(body_xml)}</bodies></vehicle>').encode()


def _line(count, origin, prefix):
    ox, oy, oz = origin
    return [(ox + i, oy, oz, f"{prefix}_{i:03d}") for i in range(count)]


def _micro_vehicle(prefix, surrounding=32, ids=(10, 20, 30)):
    script = "value = input.getNumber(1) + 1; output.setNumber(1, value); " * 2
    surrounding_xml = "".join(
        f'<c d="{prefix}_{i}"><o><vp x="{i}" y="0" z="0"/></o></c>'
        for i in range(surrounding))
    definition = (
        '<microprocessor_definition name="Renamable" id_counter="999">'
        '<group><components>'
        f'<c type="8"><object id="{ids[0]}"/></c>'
        f'<c type="56"><object id="{ids[1]}" script="{script}">'
        f'<in1 component_id="{ids[0]}" node_index="0"/></object></c>'
        f'<c type="6"><object id="{ids[2]}">'
        f'<in1 component_id="{ids[1]}" node_index="0"/></object></c>'
        '</components></group></microprocessor_definition>')
    controller = (f'<c d="controller"><o><vp x="1000" y="0" z="0"/>'
                  f'{definition}</o></c>')
    connectors = (('<c d="connector"><o><vp x="2000" y="0" z="0"/></o></c>'
                   '<c d="connector"><o><vp x="2004" y="0" z="0"/></o></c>')
                  if surrounding else "")
    link = (('<logic_node_link type="2"><voxel_pos_0 x="2000" y="0" z="0"/>'
             '<voxel_pos_1 x="2004" y="0" z="0"/></logic_node_link>')
            if surrounding else "")
    return (f'<vehicle><bodies><body><components>{surrounding_xml}{controller}'
            f'{connectors}</components></body></bodies><logic_node_links>{link}'
            '</logic_node_links></vehicle>').encode()


class PipelineTests(unittest.TestCase):
    def test_small_exact_vehicle_is_reported_with_cautious_confidence(self):
        xml = _vehicle([_line(4, (0, 0, 0), "small")])
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            source = root / "source.xml"
            query = root / "query.xml"
            source.write_bytes(xml)
            query.write_bytes(xml)
            db = connect(root / "index.sqlite")
            try:
                index_file(db, source, "1000", "Small source")
                result = scan(db, query)
                self.assertEqual(result["best_match"]["item_id"], "1000")
                self.assertTrue(result["best_match"]["listed_in_known_ids"])
                self.assertEqual(result["best_match"]["similarity_percent"], 100.0)
                self.assertEqual(result["best_match"]["confidence"], "low")
            finally:
                db.close()

    def test_partial_copy_cluster_requires_consistent_workshop_translation(self):
        hashes = [f"unique-{i}" for i in range(12)]
        query = {value: (i, 0, 0) for i, value in enumerate(hashes)}
        scattered = {value: (i * 10, 0, 0) for i, value in enumerate(hashes)}
        shifted = {value: (i + 30, 5, 0) for i, value in enumerate(hashes)}
        self.assertLess(_aligned_cluster(hashes, query, scattered), 8)
        self.assertEqual(_aligned_cluster(hashes, query, shifted), 12)

    def test_paint_attribute_order_and_decimal_spellings_keep_full_match(self):
        parts = [(x, 0, z, f"shape_{(x * 7 + z * 11) % 13}")
                 for x in range(8) for z in range(8)]
        shifted = [(x + 40, y + 2, z - 15, kind) for x, y, z, kind in parts]
        source_xml = _vehicle([parts])
        edited_xml = _vehicle([shifted], cosmetic=True, reverse=True)
        source_fp = fingerprint_bytes(source_xml)
        edited_fp = fingerprint_bytes(edited_xml)
        self.assertEqual(source_fp["features"], edited_fp["features"])
        self.assertEqual(source_fp["winnow_features"], edited_fp["winnow_features"])

        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            source = root / "source.xml"
            edited = root / "edited.xml"
            source.write_bytes(source_xml)
            edited.write_bytes(edited_xml)
            db = connect(root / "index.sqlite")
            try:
                index_file(db, source, "1001", "Source")
                result = scan(db, edited)
                self.assertEqual(result["best_match"]["item_id"], "1001")
                self.assertEqual(result["best_match"]["similarity_percent"], 100.0)
                self.assertFalse(result["best_match"]["partial_copy_evidence"])
                self.assertIn(result["suspicion_level"], ("medium", "high"))
            finally:
                db.close()

    def test_twenty_percent_copy_is_found_without_full_item_lsh_collision(self):
        copied = _line(24, (0, 0, 0), "distinct_shared")
        source_parts = copied + _line(96, (100, 0, 0), "source_only")
        query_parts = (_line(24, (300, 0, 0), "distinct_shared")
                       + _line(96, (500, 0, 0), "query_only"))
        source_xml = _vehicle([source_parts])
        query_xml = _vehicle([query_parts], reverse=True)
        source_fp = fingerprint_bytes(source_xml)
        query_fp = fingerprint_bytes(query_xml)
        self.assertEqual(source_fp["components"], query_fp["components"])
        self.assertEqual(len(copied) / query_fp["components"], 0.2)
        self.assertGreater(sum((source_fp["winnow_features"] &
                                query_fp["winnow_features"]).values()), 0)
        # Whole-file MinHash cannot be the sole candidate route in this case.
        for field in ("features", "winnow_features"):
            self.assertFalse(set(bands(signature(source_fp[field]))) &
                             set(bands(signature(query_fp[field]))), field)

        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            source = root / "source.xml"
            query = root / "query.xml"
            source.write_bytes(source_xml)
            query.write_bytes(query_xml)
            db = connect(root / "index.sqlite")
            try:
                index_file(db, source, "2002", "Copied section source")
                stored_buckets = set(bands(tuple(
                    load(db, "2002", str(source.resolve()))["minhash_signature"])))
                query_buckets = set(bands(signature(chain(
                    ("g:" + value for value in query_fp["features"]),
                    ("w:" + value for value in query_fp["winnow_features"])))))
                self.assertFalse(stored_buckets & query_buckets)
                self.assertIn("2002", [item_id for item_id, _, _ in
                                       candidates(db, query_fp, total_files=1)])
                result = scan(db, query)
                self.assertIsNotNone(result["best_match"])
                self.assertEqual(result["best_match"]["item_id"], "2002")
                self.assertIn(result["best_match"]["confidence"], ("medium", "high"))
                self.assertIn(result["suspicion_level"], ("medium", "high"))
                self.assertGreater(result["best_match"]["shared_neighborhoods"], 0)
            finally:
                db.close()

    def test_body_reorder_never_mixes_same_position_components(self):
        first = _line(8, (0, 0, 0), "first_body")
        second = _line(8, (0, 0, 0), "second_body")
        isolated_first = fingerprint_bytes(_vehicle([first]))
        isolated_second = fingerprint_bytes(_vehicle([second]))
        together = fingerprint_bytes(_vehicle([first, second]))
        reordered = fingerprint_bytes(_vehicle([second, first], reverse=True))
        self.assertEqual(together["features"],
                         isolated_first["features"] + isolated_second["features"])
        self.assertEqual(together["winnow_features"],
                         isolated_first["winnow_features"] +
                         isolated_second["winnow_features"])
        self.assertEqual(together["features"], reordered["features"])
        self.assertEqual(together["winnow_features"], reordered["winnow_features"])

    def test_reindex_replaces_old_candidates_and_bucket_memberships(self):
        old_xml = _vehicle([_line(28, (0, 0, 0), "old_distinct")])
        new_xml = _vehicle([_line(28, (0, 0, 0), "new_distinct")])
        old_fp = fingerprint_bytes(old_xml)
        new_fp = fingerprint_bytes(new_xml)
        self.assertFalse(old_fp["features"].keys() & new_fp["features"].keys())
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            indexed = root / "indexed.xml"
            old_query = root / "old.xml"
            new_query = root / "new.xml"
            indexed.write_bytes(old_xml)
            old_query.write_bytes(old_xml)
            new_query.write_bytes(new_xml)
            db = connect(root / "index.sqlite")
            try:
                self.assertTrue(index_file(db, indexed, "3003", "Changing item"))
                self.assertEqual(scan(db, old_query)["best_match"]["item_id"], "3003")
                old_buckets = {row[0] for row in db.execute(
                    "SELECT b.bucket FROM lsh_buckets b JOIN search_files s "
                    "ON s.id=b.file_id WHERE s.item_id='3003'")}
                old_postings = {(row[0], row[1]) for row in db.execute(
                    "SELECT p.channel,p.hash FROM search_postings p "
                    "JOIN search_files s ON s.id=p.file_id WHERE s.item_id='3003'")}
                self.assertTrue(old_buckets)
                self.assertTrue(old_postings)
                previous = indexed.stat().st_mtime_ns
                indexed.write_bytes(new_xml)
                os.utime(indexed, ns=(previous + 1_000_000_000,
                                      previous + 1_000_000_000))
                self.assertTrue(index_file(db, indexed, "3003", "Changing item"))
                self.assertIsNone(scan(db, old_query)["best_match"])
                self.assertEqual(scan(db, new_query)["best_match"]["item_id"], "3003")
                self.assertFalse(any(item_id == "3003" for item_id, _, _ in
                                     candidates(db, old_fp, total_files=1)))
                new_buckets = {row[0] for row in db.execute(
                    "SELECT b.bucket FROM lsh_buckets b JOIN search_files s "
                    "ON s.id=b.file_id WHERE s.item_id='3003'")}
                new_postings = {(row[0], row[1]) for row in db.execute(
                    "SELECT p.channel,p.hash FROM search_postings p "
                    "JOIN search_files s ON s.id=p.file_id WHERE s.item_id='3003'")}
                self.assertFalse(old_buckets & new_buckets)
                self.assertFalse(old_postings & new_postings)
            finally:
                db.close()

    def test_existing_database_scans_during_and_after_resumable_upgrade(self):
        xml = _vehicle([_line(30, (0, 0, 0), "old_vehicle")])
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            source = root / "source.xml"
            query = root / "query.xml"
            source.write_bytes(xml)
            query.write_bytes(xml)
            db = connect(root / "index.sqlite")
            try:
                index_file(db, source, "4004", "Legacy source")
                old = fingerprint_file(source, legacy=True)
                with db:
                    delete(db, "4004", str(source.resolve()))
                    db.executemany("INSERT INTO features VALUES (?,?,?,?,?)",
                                   (("4004", str(source.resolve()), h, n,
                                     json.dumps(old["samples"][h]))
                                    for h, n in old["features"].items()))
                before = scan(db, query)
                self.assertEqual(before["best_match"]["item_id"], "4004")
                self.assertEqual(before["coverage"]["modern_search_files"], 0)
                upgrade = upgrade_search_index(db, max_files=1)
                self.assertEqual(upgrade["upgraded"], 1)
                self.assertEqual(upgrade["remaining_legacy_files"], 0)
                self.assertEqual(db.execute("SELECT COUNT(*) FROM features").fetchone()[0], 0)
                after = scan(db, query)
                self.assertEqual(after["best_match"]["item_id"], "4004")
                self.assertEqual(after["coverage"]["modern_search_files"], 1)
                self.assertEqual(upgrade_search_index(db)["upgraded"], 0)
            finally:
                db.close()

    def test_distinctive_microprocessor_copy_is_reported_without_geometry_overlap(self):
        source_xml = _micro_vehicle("source")
        query_xml = _micro_vehicle("query", ids=(110, 120, 130))
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            source, query = root / "source.xml", root / "query.xml"
            source.write_bytes(source_xml)
            query.write_bytes(query_xml)
            db = connect(root / "index.sqlite")
            try:
                index_file(db, source, "5005", "Shared controller")
                result = scan(db, query)
                best = result["best_match"]
                self.assertEqual(best["item_id"], "5005")
                self.assertEqual(best["similarity_percent"], 0.0)
                self.assertEqual(best["channels"]["microcontrollers"]["shared"], 2)
                self.assertEqual(result["status"], "possible shared controller")
                self.assertEqual(result["suspicion_level"], "medium")
            finally:
                db.close()

    def test_one_block_with_substantial_controller_can_be_indexed(self):
        source_xml = _micro_vehicle("source", surrounding=0)
        query_xml = _micro_vehicle("query", surrounding=0, ids=(110, 120, 130))
        self.assertFalse(fingerprint_bytes(source_xml)["features"])
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            source, query = root / "source.xml", root / "query.xml"
            source.write_bytes(source_xml)
            query.write_bytes(query_xml)
            db = connect(root / "index.sqlite")
            try:
                index_file(db, source, "6006", "Controller only")
                result = scan(db, query)
                self.assertEqual(result["best_match"]["item_id"], "6006")
                self.assertEqual(result["status"], "possible shared controller")
                self.assertIsNone(result["best_match"]["minhash_jaccard_estimate_percent"])
                other = root / "legacy.xml"
                other.write_bytes(_vehicle([_line(12, (0, 0, 0), "legacy")]))
                index_file(db, other, "7007", "Legacy geometry")
                old = fingerprint_file(other, legacy=True)
                with db:
                    delete(db, "7007", str(other.resolve()))
                    db.executemany("INSERT INTO features VALUES (?,?,?,?,?)",
                                   (("7007", str(other.resolve()), h, n,
                                     json.dumps(old["samples"][h]))
                                    for h, n in old["features"].items()))
                self.assertEqual(scan(db, query)["best_match"]["item_id"], "6006")
            finally:
                db.close()


if __name__ == "__main__":
    unittest.main()
