"""Candidate retrieval and persistence tests using only an in-memory index."""

from collections import Counter
import json
import sqlite3
import unittest
import zlib
from unittest.mock import patch

import stormcopy.search_index as search_index
from stormcopy.minhash import bands, signature
from stormcopy.search_index import candidates, delete, ensure_tables, load, save


def fingerprint(geometry=(), winnow=(), logic=(), micro=()):
    geometry = list(geometry)
    winnow = list(winnow)
    logic = list(logic)
    micro = list(micro)
    return {
        "components": len(geometry) + 3,
        "features": Counter(geometry),
        "samples": {key: (1, 2, 3) for key in geometry},
        "winnow_features": Counter(winnow),
        "winnow_samples": {key: (2, 3, 4) for key in winnow},
        "logic_features": Counter(logic),
        "logic_samples": {key: (3, 4, 5) for key in logic},
        "micro_features": Counter(micro),
        "micro_samples": {key: None for key in micro},
        "component_types": Counter({"block": len(geometry)}),
    }


class SearchIndexTests(unittest.TestCase):
    def setUp(self):
        self.db = sqlite3.connect(":memory:")
        ensure_tables(self.db)

    def tearDown(self):
        self.db.close()

    def insert_posting(self, item_id, path, channel, value):
        row = self.db.execute("SELECT id FROM search_files "
                              "WHERE item_id=? AND path=?", (item_id, path)).fetchone()
        if row is None:
            file_id = self.db.execute(
                "INSERT INTO search_files(item_id,path,payload,version) VALUES (?,?,?,?)",
                (item_id, path, zlib.compress(b"{}"),
                 search_index.SEARCH_INDEX_VERSION)).lastrowid
        else:
            file_id = row[0]
        self.db.execute("INSERT INTO search_postings(channel,hash,file_id) "
                        "VALUES (?,?,?)",
                        (channel, search_index._post_key(value), file_id))

    def test_compressed_roundtrip_and_missing_file(self):
        fp = fingerprint(["g1", "g2"], ["w1"], ["l1"], ["m1"])
        self.assertIsNone(load(self.db, "123", "source.xml"))
        save(self.db, "123", "source.xml", fp)
        payload = self.db.execute(
            "SELECT payload FROM search_files WHERE item_id='123'").fetchone()[0]
        self.assertIsInstance(payload, bytes)
        self.assertIn(b'"features"', zlib.decompress(payload))
        restored = load(self.db, "123", "source.xml")
        for field in fp:
            self.assertEqual(restored[field], fp[field])
        self.assertEqual(len(restored["minhash_signature"]), 64)
        self.assertEqual(len(self.db.execute("SELECT * FROM lsh_buckets").fetchall()), 16)
        self.assertEqual(self.db.execute("SELECT version FROM search_files").fetchone()[0],
                         search_index.SEARCH_INDEX_VERSION)
        self.assertEqual({row[1] for row in self.db.execute(
            "PRAGMA table_info(search_postings)")}, {"channel", "hash", "file_id"})
        self.assertEqual(self.db.execute(
            "SELECT typeof(hash),length(hash) FROM search_postings LIMIT 1").fetchone(),
            ("blob", 12))
        self.assertEqual(self.db.execute(
            "SELECT typeof(bucket),length(bucket) FROM lsh_buckets LIMIT 1").fetchone(),
            ("blob", 16))

    def test_exact_match_and_delete(self):
        fp = fingerprint((f"g{i}" for i in range(30)), ["rare-window"])
        save(self.db, "123", "source.xml", fp)
        save(self.db, "456", "other.xml", fingerprint(["wheel-a", "wheel-b"]))
        ranked = candidates(self.db, fp, total_files=2)
        self.assertEqual((ranked[0][0], ranked[0][1]), ("123", "source.xml"))
        self.assertGreater(ranked[0][2], 0)
        delete(self.db, "123", "source.xml")
        self.assertIsNone(load(self.db, "123", "source.xml"))
        self.assertFalse(any(item == "123" for item, _, _ in candidates(
            self.db, fp, total_files=1)))

    def test_replacement_removes_old_buckets_and_postings(self):
        before = fingerprint((f"old{i}" for i in range(60)), ["old-window"])
        after = fingerprint((f"new{i}" for i in range(60)), ["new-window"])
        old_bands = set(bands(signature(
            [*("g:" + key for key in before["features"]),
             *("w:" + key for key in before["winnow_features"])])))
        save(self.db, "123", "source.xml", before)
        save(self.db, "123", "source.xml", after)
        self.assertEqual(load(self.db, "123", "source.xml")["features"], after["features"])
        self.assertFalse(any(item == "123" for item, _, _ in candidates(
            self.db, before, total_files=1)))
        self.assertEqual(candidates(self.db, after, total_files=1)[0][:2],
                         ("123", "source.xml"))
        stored_bands = {row[0] for row in self.db.execute("SELECT bucket FROM lsh_buckets")}
        old_bands = {search_index._bucket_key(value) for value in old_bands}
        self.assertFalse(old_bands & stored_bands)
        self.assertEqual(self.db.execute("SELECT COUNT(*) FROM search_postings").fetchone()[0],
                         61)

    def test_partial_copy_retrieved_without_lsh_collision(self):
        source = fingerprint((f"part-{i}" for i in range(1000)))
        changed = fingerprint([*(f"part-{i}" for i in range(200)),
                               *(f"other-{i}" for i in range(800))])
        source_bands = set(bands(signature("g:" + key for key in source["features"])))
        query_bands = set(bands(signature("g:" + key for key in changed["features"])))
        self.assertFalse(source_bands & query_bands)
        save(self.db, "copied", "source.xml", source)
        save(self.db, "unrelated", "other.xml",
             fingerprint((f"unrelated-{i}" for i in range(1000))))
        ranked = candidates(self.db, changed, total_files=2)
        self.assertTrue(ranked)
        self.assertEqual(ranked[0][:2], ("copied", "source.xml"))
        self.assertGreater(ranked[0][2], 10)

    def test_small_section_inside_much_larger_source_has_local_postings(self):
        source = fingerprint((f"part-{i}" for i in range(20_000)))
        copied = fingerprint((f"part-{i}" for i in range(200)))
        self.assertEqual(len(search_index.sampled_hashes(source, "features")), 2000)
        self.assertFalse(set(bands(signature("g:" + key for key in source["features"]))) &
                         set(bands(signature("g:" + key for key in copied["features"]))))
        save(self.db, "large", "large.xml", source)
        self.assertEqual(candidates(self.db, copied, total_files=1)[0][:2],
                         ("large", "large.xml"))

    def test_ubiquitous_feature_is_skipped(self):
        common = fingerprint(["common"])
        for number in range(40):
            save(self.db, str(number), f"{number}.xml", common)
        self.assertEqual(candidates(self.db, common, total_files=40), [])
        self.assertEqual(candidates(self.db, common, total_files=40, limit=0), [])

    def test_rare_postings_are_read_before_common_with_tight_budget(self):
        self.insert_posting("rare-source", "rare.xml", "g", "rare")
        for i in range(6):
            self.insert_posting(f"common-{i}", f"{i}.xml", "g", "common")
        with patch.object(search_index, "_MAX_RETRIEVAL_ROWS", 8):
            ranked = candidates(self.db, fingerprint(["common", "rare"]),
                                total_files=7)
        self.assertEqual([row[0] for row in ranked], ["rare-source"])

    def test_probe_budget_reaches_winnow_amid_common_geometry(self):
        for term in range(5):
            for number in range(40):
                self.insert_posting(f"g-{term}-{number}", f"g-{term}-{number}.xml",
                                    "g", f"common-{term}")
        self.insert_posting("rare-winnow", "rare.xml", "w", "rare-window")
        query = fingerprint((f"common-{i}" for i in range(5)), ["rare-window"])
        with patch.object(search_index, "_MAX_PROBE_ROWS", 70), patch.object(
                search_index, "_MAX_RETRIEVAL_ROWS", 80):
            ranked = candidates(self.db, query, total_files=201)
        self.assertEqual([item for item, _, _ in ranked], ["rare-winnow"])

    def test_candidate_budget_and_top_limit(self):
        for i in range(20):
            self.insert_posting(f"{i:02d}", f"{i}.xml", "g", f"token-{i}")
        with patch.object(search_index, "_MAX_RETRIEVAL_ROWS", 25):
            ranked = candidates(self.db, fingerprint(
                f"token-{i}" for i in range(20)), total_files=20)
        self.assertLessEqual(len(ranked), 5)  # 20 probes + at most 5 scored rows
        self.assertTrue(ranked)
        for i in range(10):
            self.insert_posting(f"tie-{i:02d}", f"tie-{i}.xml", "g", "tie")
        tied = candidates(self.db, fingerprint(["tie"]), total_files=30, limit=3)
        self.assertEqual([row[0] for row in tied],
                         ["tie-00", "tie-01", "tie-02"])

    def test_document_frequencies_uses_compact_keys(self):
        for i in range(3):
            self.insert_posting(f"doc-{i}", f"{i}.xml", "g", "shared")
        self.assertEqual(search_index.document_frequencies(
            self.db, ["shared", "missing"], "g"), {"shared": 3})
        self.assertEqual(search_index.document_frequencies(
            self.db, ["shared"], "g", cap=1), {"shared": 2})

    def test_automatic_migration_rebuilds_compact_indexes(self):
        old = sqlite3.connect(":memory:")
        try:
            old.executescript("""
                CREATE TABLE search_files(item_id TEXT NOT NULL,path TEXT NOT NULL,
                    payload BLOB NOT NULL,PRIMARY KEY(item_id,path)) WITHOUT ROWID;
                CREATE TABLE search_postings(item_id TEXT,path TEXT,channel TEXT,hash TEXT);
                CREATE TABLE lsh_buckets(item_id TEXT,path TEXT,bucket TEXT);
            """)
            fp = fingerprint(["geom-a", "geom-b"], ["win-a"], ["logic-a"])
            payload = {field: dict(fp[field]) for field in fp if field != "components"}
            payload["components"] = fp["components"]
            payload["minhash_signature"] = signature(
                [*("g:" + key for key in fp["features"]),
                 *("w:" + key for key in fp["winnow_features"])])
            old.execute("INSERT INTO search_files VALUES (?,?,?)",
                        ("123", "old.xml", zlib.compress(json.dumps(payload).encode())))
            # Migration rebuilds from full payload, not potentially stale rows.
            old.execute("INSERT INTO search_postings VALUES (?,?,?,?)",
                        ("123", "old.xml", "g", "stale"))
            old.execute("INSERT INTO lsh_buckets VALUES (?,?,?)",
                        ("123", "old.xml", "stale"))
            ensure_tables(old)
            ensure_tables(old)
            self.assertEqual(load(old, "123", "old.xml")["features"], fp["features"])
            self.assertEqual(old.execute("SELECT version FROM search_files").fetchone()[0], 0)
            self.assertEqual(candidates(old, fp, total_files=1)[0][:2],
                             ("123", "old.xml"))
            self.assertEqual(search_index.document_frequencies(
                old, ["stale", "geom-a"], "g"), {"geom-a": 1})
            self.assertFalse(old.execute("SELECT 1 FROM sqlite_master "
                                         "WHERE name='search_files_v1'").fetchone())
        finally:
            old.close()

    def test_failed_migration_restores_old_schema(self):
        old = sqlite3.connect(":memory:")
        try:
            old.execute("CREATE TABLE search_files(item_id TEXT,path TEXT,payload BLOB,"
                        "PRIMARY KEY(item_id,path))")
            old.execute("INSERT INTO search_files VALUES (?,?,?)",
                        ("123", "old.xml", b"invalid zlib"))
            with self.assertRaises(zlib.error):
                ensure_tables(old)
            self.assertNotIn("id", {row[1] for row in old.execute(
                "PRAGMA table_info(search_files)")})
            self.assertEqual(old.execute("SELECT COUNT(*) FROM search_files").fetchone()[0], 1)
        finally:
            old.close()


if __name__ == "__main__":
    unittest.main()
