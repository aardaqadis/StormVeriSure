"""Tests for broad, resumable downloads into an existing Steam library."""

from contextlib import redirect_stdout
from io import StringIO
import os
from pathlib import Path
import tempfile
import time
from types import SimpleNamespace
import unittest
from unittest.mock import patch

from stormcopy.bulk import adopt_existing, download_workshop
from stormcopy.console import Console
from stormcopy.index import connect, upsert_item


def workshop_root(root):
    folder = root / "library" / "steamapps" / "workshop" / "content" / "573090"
    folder.mkdir(parents=True)
    return folder


class BulkWorkshopTests(unittest.TestCase):
    def test_known_only_end_to_end_reuses_existing_and_fills_missing_folder(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            folder = workshop_root(root)
            already_here = folder / "101"
            already_here.mkdir()
            (already_here / "vehicle.xml").write_text("existing", encoding="utf-8")
            cache = root / "cache"
            cache.mkdir()
            executable = cache / "steamcmd.exe"
            executable.write_bytes(b"MZ test")
            db = connect(root / "index.sqlite")
            upsert_item(db, "101", updated=1, size_bytes=10)
            upsert_item(db, "102", updated=1, size_bytes=20)

            def fake_steamcmd(command, **_kwargs):
                item_id = command[command.index("+workshop_download_item") + 2]
                source = cache / "steamapps" / "workshop" / "content" / "573090" / item_id
                source.mkdir(parents=True)
                (source / "vehicle.xml").write_text("downloaded", encoding="utf-8")
                return SimpleNamespace(returncode=0,
                                       stdout=f"Success. Downloaded item {item_id}", stderr="")

            with patch("stormcopy.steam.subprocess.run", side_effect=fake_steamcmd) as run:
                result = download_workshop(db, folder, cache, known_only=True,
                                           workers=1, chunk_size=1,
                                           reserve_free_gb=0, download_only=True)
            self.assertEqual(run.call_count, 1)
            self.assertEqual(result["existing"]["folders"], 1)
            self.assertEqual((result["attempted"], result["downloaded"]), (1, 1))
            self.assertEqual(result["in_workshop_folder"], 2)
            self.assertEqual((already_here / "vehicle.xml").read_text(), "existing")
            self.assertEqual((folder / "102" / "vehicle.xml").read_text(), "downloaded")
            self.assertFalse((cache / "steamapps" / "workshop" / "content" /
                              "573090" / "102").exists())
            db.close()

    def test_existing_folders_are_adopted_and_only_missing_or_old_are_queued(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            folder = workshop_root(root)
            now = int(time.time())
            for item_id in ("101", "102", "103"):
                (folder / item_id).mkdir()
            os.utime(folder / "102", (now - 200, now - 200))
            (folder / "not-an-item").mkdir()
            db = connect(root / "index.sqlite")
            upsert_item(db, "101", updated=now - 100)
            upsert_item(db, "102", updated=now - 100)
            upsert_item(db, "104", updated=now - 100)
            db.execute("UPDATE items SET downloaded=? WHERE id='104'", (now,))
            db.commit()

            result = adopt_existing(db, folder)
            self.assertEqual(result["folders"], 3)
            self.assertEqual(result["stale"], 1)
            self.assertEqual(result["new_ids"], 1)
            self.assertEqual(result["cached_elsewhere"], 1)
            states = dict(db.execute("SELECT id,downloaded FROM items"))
            self.assertGreater(states["101"], 0)
            self.assertEqual(states["102"], 0)
            self.assertGreater(states["103"], 0)
            self.assertEqual(states["104"], 0)
            db.close()

    def test_bulk_command_discovers_then_downloads_in_bounded_chunks(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            folder = workshop_root(root)
            cache = root / "cache"
            cache.mkdir()
            (cache / "steamcmd.exe").write_bytes(b"MZ test")
            db = connect(root / "index.sqlite")
            for number in range(101, 108):
                upsert_item(db, str(number), updated=1, size_bytes=number)
            calls = []
            events = []
            clock = [100.0]

            def fake_download(connection, cache_path, **kwargs):
                self.assertEqual(kwargs["workshop_folder"], folder.resolve())
                self.assertTrue(kwargs["skip_attempted"])
                self.assertLessEqual(kwargs["max_items"], 3)
                rows = connection.execute(
                    "SELECT id FROM items WHERE downloaded=0 AND id NOT IN "
                    "(SELECT id FROM temp._stormcopy_bulk_attempted) "
                    "ORDER BY size_bytes LIMIT ?", (kwargs["max_items"],)).fetchall()
                ids = [row[0] for row in rows]
                calls.append(ids)
                kwargs["progress"]({"phase": "Download", "done": 0,
                                     "total": len(ids)})
                clock[0] += 10.0
                kwargs["progress"]({"phase": "Download", "done": len(ids),
                                     "total": len(ids)})
                connection.executemany("UPDATE items SET downloaded=? WHERE id=?",
                                       ((int(time.time()), item_id) for item_id in ids))
                connection.commit()
                return {"selected": len(ids), "selected_ids": ids,
                        "cached": len(ids), "failed": 0,
                        "downloaded_and_indexed": len(ids),
                        "cached_without_vehicle": 0, "batches": 1}

            with patch("stormcopy.bulk.steam.discover_catalog", return_value={
                    "complete": True, "catalog_complete": True,
                    "pages": 2, "items_seen": 7}) as discover, \
                 patch("stormcopy.bulk.steam.refresh_sizes", return_value={
                     "checked": 0, "sizes_added": 0, "unknown_sizes": 0}), \
                 patch("stormcopy.bulk.steam.download_pending", side_effect=fake_download), \
                 patch("stormcopy.bulk.time.monotonic", side_effect=lambda: clock[0]):
                result = download_workshop(
                    db, folder, cache, known_only=False, api_key="test-key",
                    max_items=5, chunk_size=3, workers=1, reserve_free_gb=0,
                    progress=events.append)
            self.assertEqual([len(group) for group in calls], [3, 2])
            download_events = [event for event in events
                               if event.get("phase") == "Workshop download"]
            self.assertEqual([event["done"] for event in download_events],
                             [0, 3, 3, 5])
            self.assertTrue(all(event["total"] == 5 for event in download_events))
            # Starting a new 50-item-style chunk keeps the overall estimate.
            self.assertGreater(download_events[1].get("eta_seconds") or 0, 0)
            self.assertGreater(download_events[2].get("eta_seconds") or 0, 0)
            self.assertEqual((result["attempted"], result["downloaded"]), (5, 5))
            self.assertEqual(result["remaining_pending"], 2)
            self.assertEqual(result["stop_reason"], "item limit reached")
            self.assertEqual(discover.call_args.kwargs["max_pages"], 0)
            self.assertFalse(discover.call_args.kwargs["restart"])
            db.close()

    def test_rate_limit_reduces_workers_between_chunks(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            folder = workshop_root(root)
            cache = root / "cache"
            cache.mkdir()
            (cache / "steamcmd.exe").write_bytes(b"MZ test")
            db = connect(root / "index.sqlite")
            for number in range(101, 107):
                upsert_item(db, str(number), updated=1, size_bytes=number)
            worker_counts = []

            def fake_download(connection, cache_path, **kwargs):
                worker_counts.append(kwargs["workers"])
                rows = connection.execute(
                    "SELECT id FROM items WHERE downloaded=0 ORDER BY size_bytes LIMIT ?",
                    (kwargs["max_items"],)).fetchall()
                ids = [row[0] for row in rows]
                connection.executemany("UPDATE items SET downloaded=? WHERE id=?",
                                       ((int(time.time()), item_id) for item_id in ids))
                connection.commit()
                return {"selected": len(ids), "selected_ids": ids,
                        "cached": len(ids), "failed": 0,
                        "downloaded_and_indexed": len(ids),
                        "cached_without_vehicle": 0, "batches": 1,
                        "rate_limited": len(worker_counts) == 1}

            with patch("stormcopy.bulk.steam.refresh_sizes", return_value={
                    "checked": 0, "sizes_added": 0, "unknown_sizes": 0}), \
                 patch("stormcopy.bulk.steam.download_pending", side_effect=fake_download), \
                 patch("stormcopy.bulk.time.sleep") as sleep:
                result = download_workshop(db, folder, cache, known_only=True,
                                           max_items=6, chunk_size=3, workers=4,
                                           reserve_free_gb=0)
            self.assertEqual(worker_counts, [4, 2])
            sleep.assert_called_once_with(30)
            self.assertEqual(result["downloaded"], 6)
            db.close()

    def test_elapsed_rate_limit_cooldown_does_not_pause_again(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            folder = workshop_root(root)
            cache = root / "cache"
            cache.mkdir()
            (cache / "steamcmd.exe").write_bytes(b"MZ test")
            db = connect(root / "index.sqlite")
            for number in range(101, 105):
                upsert_item(db, str(number), updated=1, size_bytes=number)
            worker_counts = []

            def fake_download(connection, _cache_path, **kwargs):
                worker_counts.append(kwargs["workers"])
                rows = connection.execute(
                    "SELECT id FROM items WHERE downloaded=0 ORDER BY size_bytes LIMIT ?",
                    (kwargs["max_items"],)).fetchall()
                ids = [row[0] for row in rows]
                connection.executemany("UPDATE items SET downloaded=? WHERE id=?",
                                       ((int(time.time()), item_id) for item_id in ids))
                connection.commit()
                return {"selected": len(ids), "selected_ids": ids,
                        "cached": len(ids), "failed": 0,
                        "downloaded_and_indexed": len(ids),
                        "cached_without_vehicle": 0, "batches": 1,
                        "rate_limited": len(worker_counts) == 1,
                        "cooldown_remaining_seconds": 0}

            try:
                with patch("stormcopy.bulk.steam.refresh_sizes", return_value={
                        "checked": 0, "sizes_added": 0, "unknown_sizes": 0}), \
                     patch("stormcopy.bulk.steam.download_pending", side_effect=fake_download), \
                     patch("stormcopy.bulk.time.sleep") as sleep:
                    result = download_workshop(db, folder, cache, known_only=True,
                                               max_items=4, chunk_size=2, workers=4,
                                               reserve_free_gb=0)
                self.assertEqual(worker_counts, [4, 2])
                sleep.assert_not_called()
                self.assertEqual(result["downloaded"], 4)
            finally:
                db.close()

    def test_prepare_workers_keeps_cumulative_download_progress(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            folder = workshop_root(root)
            cache = root / "cache"
            cache.mkdir()
            (cache / "steamcmd.exe").write_bytes(b"MZ test")
            db = connect(root / "index.sqlite")
            for number in range(101, 105):
                upsert_item(db, str(number), updated=1, size_bytes=number)
            events = []

            def fake_download(connection, _cache_path, **kwargs):
                rows = connection.execute(
                    "SELECT id FROM items WHERE downloaded=0 ORDER BY size_bytes LIMIT ?",
                    (kwargs["max_items"],)).fetchall()
                ids = [row[0] for row in rows]
                show = kwargs["progress"]
                show({"phase": "Prepare workers", "done": 2, "total": 2,
                      "detail": "2 SteamCMD process folders ready"})
                show({"phase": "Download", "done": 0, "total": len(ids),
                      "cached_count": 0, "failed_count": 0,
                      "worker_count": kwargs["workers"]})
                connection.executemany("UPDATE items SET downloaded=? WHERE id=?",
                                       ((int(time.time()), item_id) for item_id in ids))
                connection.commit()
                show({"phase": "Download", "done": len(ids), "total": len(ids),
                      "cached_count": len(ids), "failed_count": 0,
                      "worker_count": kwargs["workers"]})
                return {"selected": len(ids), "selected_ids": ids,
                        "cached": len(ids), "failed": 0,
                        "downloaded_and_indexed": len(ids),
                        "cached_without_vehicle": 0, "batches": 1}

            try:
                with patch("stormcopy.bulk.steam.refresh_sizes", return_value={
                        "checked": 0, "sizes_added": 0, "unknown_sizes": 0}), \
                     patch("stormcopy.bulk.steam.download_pending", side_effect=fake_download):
                    result = download_workshop(db, folder, cache, known_only=True,
                                               max_items=4, chunk_size=2, workers=2,
                                               reserve_free_gb=0, progress=events.append)
                download_events = [event for event in events
                                   if event.get("phase") == "Workshop download"]
                self.assertEqual([event["done"] for event in download_events],
                                 [0, 0, 2, 2, 2, 4])
                self.assertTrue(all(event["total"] == 4 for event in download_events))
                self.assertIn("2 cached, 0 failed", download_events[2]["detail"])
                self.assertIn("4 cached, 0 failed", download_events[-1]["detail"])
                self.assertEqual(result["downloaded"], 4)
            finally:
                db.close()

    def test_key_is_required_for_broad_discovery(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            folder = workshop_root(root)
            db = connect(root / "index.sqlite")
            with patch.dict(os.environ, {"STEAM_API_KEY": ""}):
                with self.assertRaisesRegex(ValueError, "STEAM_API_KEY"):
                    download_workshop(db, folder, root / "cache")
            self.assertFalse((root / "cache").exists())
            db.close()

    def test_human_report_contains_coverage_and_folder(self):
        output = StringIO()
        result = {"workshop_folder": r"C:\Steam\steamapps\workshop\content\573090",
                  "discovery": {"pages": 10, "items_seen": 500},
                  "discovery_complete": False,
                  "existing": {"folders": 20, "current": 19, "stale": 1,
                               "unindexed": 2},
                  "known_items": 500, "in_workshop_folder": 25,
                  "attempted": 5, "downloaded": 4, "failed": 1,
                  "indexed": 4, "remaining_pending": 475,
                  "waiting_to_retry": 1, "cached_without_vehicle": 0,
                  "stop_reason": "item limit reached", "elapsed_seconds": 2.5}
        with redirect_stdout(output):
            Console(stdout=output, stderr=StringIO()).render("download-workshop", result)
        report = output.getvalue()
        self.assertIn("Public pages checked this run: 10", report)
        self.assertIn("Ready in this folder: 25", report)
        self.assertIn("Downloaded: 4", report)
        self.assertIn("Run index-dir", report)
        self.assertNotIn("{\"", report)


if __name__ == "__main__":
    unittest.main()
