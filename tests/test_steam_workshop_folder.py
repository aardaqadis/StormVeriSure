"""Broad Workshop discovery and promotion into an existing Steam library."""

import errno
import json
from pathlib import Path
from types import SimpleNamespace
import tempfile
import unittest
from unittest.mock import patch
import urllib.parse

from stormcopy.index import connect, ensure_download_columns, upsert_item
from stormcopy.steam import (_promote_worker_item, _remove_backup, discover,
                             download_pending)


def steam_folder(root):
    folder = root / "library" / "steamapps" / "workshop" / "content" / "573090"
    folder.mkdir(parents=True)
    return folder


class SteamWorkshopFolderTests(unittest.TestCase):
    def test_discovery_includes_all_creators_and_ignores_old_narrow_cursor(self):
        with tempfile.TemporaryDirectory() as tmp:
            db = connect(Path(tmp) / "index.sqlite")
            db.execute("INSERT INTO state VALUES (?,?)", ("cursor_published", "old-narrow"))
            db.execute("INSERT INTO state VALUES (?,?)",
                       ("discovery_complete_published", "1"))
            db.commit()
            responses = [
                {"response": {"result": 1, "publishedfiledetails": [
                    {"publishedfileid": "101", "title": "User-made vehicle", "file_size": "10"}],
                    "next_cursor": "next-wide"}},
                {"response": {"result": 1, "publishedfiledetails": [
                    {"publishedfileid": "102", "title": "Another creator", "file_size": "20"}],
                    "next_cursor": None}},
            ]
            with patch("stormcopy.steam._request_json", side_effect=responses) as request:
                first = discover(db, max_pages=1, api_key="test-key", delay=0)
                second = discover(db, max_pages=1, api_key="test-key", delay=0)
                self.assertEqual(discover(db, api_key="test-key", delay=0)["pages"], 0)
            self.assertFalse(first["complete"])
            self.assertTrue(second["complete"])
            payloads = [json.loads(urllib.parse.parse_qs(
                urllib.parse.urlsplit(call.args[0]).query)["input_json"][0])
                for call in request.call_args_list]
            self.assertEqual([p["cursor"] for p in payloads], ["*", "next-wide"])
            self.assertTrue(all(p["appid"] == 573090 and p["filetype"] == 0
                                and "creator_appid" not in p for p in payloads))
            self.assertEqual(db.execute(
                "SELECT value FROM state WHERE key='discovery_complete_v2_published'"
            ).fetchone()[0], "1")
            self.assertEqual(db.execute("SELECT COUNT(*) FROM items").fetchone()[0], 2)
            db.close()

    def test_unbounded_resumed_discovery_does_not_claim_a_page_total(self):
        with tempfile.TemporaryDirectory() as tmp:
            db = connect(Path(tmp) / "index.sqlite")
            db.execute("INSERT INTO state VALUES (?,?)",
                       ("cursor_v2_published", "next-wide"))
            db.commit()
            events = []
            response = {"response": {"result": 1, "total": 100000,
                                     "publishedfiledetails": [], "next_cursor": None}}
            with patch("stormcopy.steam._request_json", return_value=response):
                discover(db, max_pages=0, api_key="test-key", delay=0,
                         progress=events.append)
            self.assertEqual([event["done"] for event in events], [0, 1])
            self.assertTrue(all(event["total"] is None for event in events))
            db.close()

    def test_single_worker_promotes_into_existing_workshop_folder(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            cache = root / "cache"
            destination = steam_folder(root)
            db = connect(root / "index.sqlite")
            upsert_item(db, "101", updated=1, size_bytes=10)

            def steamcmd(command, **_kwargs):
                item_id = command[command.index("+workshop_download_item") + 2]
                source = cache / "steamapps" / "workshop" / "content" / "573090" / item_id
                source.mkdir(parents=True)
                (source / "vehicle.xml").write_text("new vehicle")
                return SimpleNamespace(returncode=0,
                                       stdout=f"Success. Downloaded item {item_id}", stderr="")

            with patch("stormcopy.steam.subprocess.run", side_effect=steamcmd):
                result = download_pending(db, cache, max_items=1, delay=0,
                                          cache_only=True, workshop_folder=destination)
            self.assertEqual(result["cached"], 1)
            self.assertFalse(result["rate_limited"])
            self.assertEqual(result["workshop_folder"], str(destination.resolve()))
            self.assertEqual((destination / "101" / "vehicle.xml").read_text(), "new vehicle")
            self.assertFalse((cache / "steamapps" / "workshop" / "content" /
                              "573090" / "101").exists())
            self.assertEqual(db.execute(
                "SELECT downloaded FROM items WHERE id='101'").fetchone()[0] > 0, True)
            db.close()

    def test_parallel_workers_promote_each_item_into_existing_workshop_folder(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            cache = root / "cache"
            cache.mkdir()
            executable = cache / "steamcmd.exe"
            executable.write_bytes(b"MZ test")
            destination = steam_folder(root)
            db = connect(root / "index.sqlite")
            for item_id in ("101", "102"):
                upsert_item(db, item_id, updated=1, size_bytes=int(item_id))

            def steamcmd(command, **_kwargs):
                home = Path(command[2])
                item_id = command[command.index("+workshop_download_item") + 2]
                source = home / "steamapps" / "workshop" / "content" / "573090" / item_id
                source.mkdir(parents=True)
                (source / "vehicle.xml").write_text(item_id)
                return SimpleNamespace(returncode=0,
                                       stdout=f"Success. Downloaded item {item_id}", stderr="")

            with patch("stormcopy.steam.subprocess.run", side_effect=steamcmd):
                result = download_pending(db, cache, executable, max_items=2, delay=0,
                                          batch_size=1, workers=2, cache_only=True,
                                          workshop_folder=destination)
            self.assertEqual((result["cached"], result["workers_used"]), (2, 2))
            for item_id in ("101", "102"):
                self.assertEqual((destination / item_id / "vehicle.xml").read_text(), item_id)
                self.assertFalse(any((home / "steamapps" / "workshop" / "content" /
                                      "573090" / item_id).exists()
                                     for home in (cache / ".stormcopy-workers").iterdir()))
            db.close()

    def test_cross_volume_copy_and_index_failure_restore_previous_item(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            cache = root / "cache"
            destination = steam_folder(root)
            old_item = destination / "101"
            old_item.mkdir()
            (old_item / "vehicle.xml").write_text("old vehicle")
            db = connect(root / "index.sqlite")
            upsert_item(db, "101", updated=2, size_bytes=10)

            def steamcmd(command, **_kwargs):
                source = cache / "steamapps" / "workshop" / "content" / "573090" / "101"
                source.mkdir(parents=True)
                (source / "vehicle.xml").write_text("new vehicle")
                return SimpleNamespace(returncode=0,
                                       stdout="Success. Downloaded item 101", stderr="")

            actual_rename = Path.rename

            def cross_volume(source, target):
                if source.name == "101" and str(target).find(".stormcopy-stage-") >= 0:
                    raise OSError(errno.EXDEV, "different volume")
                return actual_rename(source, target)

            with patch("stormcopy.steam.subprocess.run", side_effect=steamcmd), \
                 patch("pathlib.Path.rename", new=cross_volume), \
                 patch("stormcopy.steam._index_cached_item", side_effect=OSError("index failed")):
                result = download_pending(db, cache, max_items=1, delay=0,
                                          workshop_folder=destination)
            self.assertEqual((result["cached"], result["failed"]), (0, 1))
            self.assertEqual((destination / "101" / "vehicle.xml").read_text(), "old vehicle")
            self.assertEqual((cache / "steamapps" / "workshop" / "content" /
                              "573090" / "101" / "vehicle.xml").read_text(), "new vehicle")
            self.assertFalse(any(destination.glob(".stormcopy-stage-*")))
            self.assertFalse(any(destination.glob(".stormcopy-backup-*")))
            db.close()

    def test_cross_volume_success_cleans_cache_staging_copy(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            cache = root / "cache"
            destination = steam_folder(root)
            db = connect(root / "index.sqlite")
            upsert_item(db, "101", updated=1, size_bytes=10)

            def steamcmd(command, **_kwargs):
                source = cache / "steamapps" / "workshop" / "content" / "573090" / "101"
                source.mkdir(parents=True)
                (source / "vehicle.xml").write_text("new vehicle")
                return SimpleNamespace(returncode=0,
                                       stdout="Success. Downloaded item 101", stderr="")

            actual_rename = Path.rename

            def cross_volume(source, target):
                if source.name == "101" and str(target).find(".stormcopy-stage-") >= 0:
                    raise OSError(errno.EXDEV, "different volume")
                return actual_rename(source, target)

            with patch("stormcopy.steam.subprocess.run", side_effect=steamcmd), \
                 patch("pathlib.Path.rename", new=cross_volume):
                result = download_pending(db, cache, max_items=1, delay=0,
                                          cache_only=True, workshop_folder=destination)
            self.assertEqual(result["cached"], 1)
            self.assertEqual((destination / "101" / "vehicle.xml").read_text(), "new vehicle")
            self.assertFalse((cache / "steamapps" / "workshop" / "content" /
                              "573090" / "101").exists())
            db.close()

    def test_explicit_workshop_folder_must_exist_and_point_to_game_content(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            cache = root / "cache"
            db = connect(root / "index.sqlite")
            with self.assertRaisesRegex(ValueError, "not found"):
                download_pending(db, cache, workshop_folder=root / "missing")
            wrong = root / "workshop"
            wrong.mkdir()
            with self.assertRaisesRegex(ValueError, "steamapps/workshop/content/573090"):
                download_pending(db, cache, workshop_folder=wrong)
            db.close()

    def test_linked_source_is_rejected_and_cleanup_error_is_warning(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            cache = root / "cache"
            source = cache / "steamapps" / "workshop" / "content" / "573090" / "101"
            source.mkdir(parents=True)
            destination = steam_folder(root)
            real_is_symlink = Path.is_symlink

            def linked_source(path):
                return path == source or real_is_symlink(path)

            with patch("pathlib.Path.is_symlink", new=linked_source):
                with self.assertRaisesRegex(RuntimeError, "source.*link"):
                    _promote_worker_item(cache, destination, "101")
            self.assertTrue(source.is_dir())
            self.assertFalse((destination / "101").exists())

            warnings = []
            unsafe_backup = root / "elsewhere"
            unsafe_backup.mkdir()
            _remove_backup(destination, unsafe_backup, warnings.append)
            self.assertEqual(len(warnings), 1)
            self.assertIn("outside", warnings[0])
            self.assertTrue(unsafe_backup.is_dir())

    def test_bulk_chunk_skips_failed_item_even_after_cooldown_expires(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            cache = root / "cache"
            db = connect(root / "index.sqlite")
            for item_id in ("1", "2", "3"):
                upsert_item(db, item_id, updated=1, size_bytes=int(item_id))

            def steamcmd(command, **_kwargs):
                item_id = command[command.index("+workshop_download_item") + 2]
                if item_id == "1":
                    return SimpleNamespace(returncode=0,
                                           stdout="ERROR! Download item 1 failed (Failure)",
                                           stderr="")
                source = cache / "steamapps" / "workshop" / "content" / "573090" / item_id
                source.mkdir(parents=True)
                (source / "vehicle.xml").write_text(item_id)
                return SimpleNamespace(returncode=0,
                                       stdout=f"Success. Downloaded item {item_id}", stderr="")

            with patch("stormcopy.steam.subprocess.run", side_effect=steamcmd):
                first = download_pending(db, cache, max_items=1, delay=0,
                                         cache_only=True, skip_attempted=True)
                self.assertEqual((first["selected_ids"], first["failed"]), (["1"], 1))
                db.execute("INSERT INTO temp._stormcopy_bulk_attempted(id) VALUES (?)",
                           (first["selected_ids"][0],))
                db.execute("UPDATE items SET retry_after=0 WHERE id='1'")
                db.commit()
                second = download_pending(db, cache, max_items=1, delay=0,
                                          cache_only=True, skip_attempted=True)
            self.assertEqual((second["selected_ids"], second["cached"]), (["2"], 1))
            self.assertEqual(db.execute("SELECT COUNT(*) FROM temp._stormcopy_bulk_attempted"
                                        ).fetchone()[0], 1)
            db.close()

    def test_rate_limit_report_survives_end_of_chunk(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            db = connect(root / "index.sqlite")
            upsert_item(db, "101", updated=1, size_bytes=10)
            response = SimpleNamespace(returncode=0,
                                       stdout="ERROR! Download item 101 failed (429 rate limit)",
                                       stderr="")
            with patch("stormcopy.steam.subprocess.run", return_value=response):
                result = download_pending(db, root / "cache", max_items=1,
                                          delay=0, cache_only=True)
            self.assertEqual((result["selected"], result["failed"]), (1, 1))
            self.assertTrue(result["rate_limited"])
            db.close()

    def test_successful_item_id_containing_429_does_not_trigger_rate_limit(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            cache = root / "cache"
            item_id = "1234294567"
            db = connect(root / "index.sqlite")
            upsert_item(db, item_id, updated=1, size_bytes=10)

            def steamcmd(_command, **_kwargs):
                folder = (cache / "steamapps" / "workshop" / "content" /
                          "573090" / item_id)
                folder.mkdir(parents=True)
                (folder / "vehicle.xml").write_text("vehicle", encoding="utf-8")
                return SimpleNamespace(returncode=0,
                                       stdout=f"Success. Downloaded item {item_id}",
                                       stderr="")

            try:
                with patch("stormcopy.steam.subprocess.run", side_effect=steamcmd):
                    result = download_pending(db, cache, max_items=1, delay=0,
                                              cache_only=True)
                self.assertEqual((result["cached"], result["failed"]), (1, 0))
                self.assertFalse(result["rate_limited"])
            finally:
                db.close()

    def test_explicit_steamcmd_rate_limit_errors_are_detected(self):
        for message in ("HTTP 429", "Rate Limit Exceeded", "Too Many Requests"):
            with self.subTest(message=message), tempfile.TemporaryDirectory() as tmp:
                root = Path(tmp)
                db = connect(root / "index.sqlite")
                upsert_item(db, "101", updated=1, size_bytes=10)
                response = SimpleNamespace(
                    returncode=0,
                    stdout=f"ERROR! Download item 101 failed ({message})",
                    stderr="")
                try:
                    with patch("stormcopy.steam.subprocess.run", return_value=response):
                        result = download_pending(db, root / "cache", max_items=1,
                                                  delay=0, cache_only=True)
                    self.assertEqual((result["selected"], result["failed"]), (1, 1))
                    self.assertTrue(result["rate_limited"])
                finally:
                    db.close()

    def test_size_order_index_is_used_by_bounded_download_query(self):
        with tempfile.TemporaryDirectory() as tmp:
            db = connect(Path(tmp) / "index.sqlite")
            ensure_download_columns(db)
            db.executemany("INSERT INTO items(id,size_bytes) VALUES (?,?)",
                           ((str(n), n) for n in range(1, 1001)))
            db.commit()
            self.assertIsNotNone(db.execute(
                "SELECT 1 FROM sqlite_master WHERE type='index' "
                "AND name='items_download_size_order'").fetchone())
            db.execute("CREATE TEMP TABLE _stormcopy_bulk_attempted(id TEXT PRIMARY KEY)")
            plan = " ".join(row["detail"] for row in db.execute(
                "EXPLAIN QUERY PLAN SELECT id FROM items WHERE id != '' "
                "AND id NOT GLOB '*[^0-9]*' AND retry_after <= ? "
                "AND (downloaded=0 OR updated>downloaded) "
                "AND NOT EXISTS (SELECT 1 FROM temp._stormcopy_bulk_attempted a "
                "WHERE a.id=items.id) "
                "ORDER BY (size_bytes IS NULL),size_bytes ASC,id ASC LIMIT ?",
                (1, 1)))
            self.assertIn("items_download_size_order", plan)
            self.assertNotIn("TEMP B-TREE", plan)
            db.close()


if __name__ == "__main__":
    unittest.main()
