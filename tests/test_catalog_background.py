"""Offline checks for the bounded background Workshop ID refresh."""

from contextlib import closing
from pathlib import Path
import tempfile
import threading
import unittest
from unittest.mock import patch

from stormcopy.catalog import (AUTO_REFRESH_INTERVAL_SECONDS,
                               NO_KEY_INTERVAL_SECONDS, PAGE_INTERVAL_SECONDS,
                               CatalogRefresher, known_id_page,
                               refresh_catalog_once)
from stormcopy.index import connect, upsert_item


class BackgroundCatalogTests(unittest.TestCase):
    def test_known_id_pages_use_sorted_index_cursor(self):
        with tempfile.TemporaryDirectory() as tmp:
            with closing(connect(Path(tmp) / "catalog.sqlite")) as db:
                for item_id in ("202", "101", "local", "303"):
                    upsert_item(db, item_id, f"Item {item_id}")
                first = known_id_page(db, limit=2)
                second = known_id_page(db, after_id=first[-1]["id"], limit=2)
        self.assertEqual([entry["id"] for entry in first + second],
                         ["101", "202", "303"])

    def test_no_api_key_makes_no_network_request(self):
        with patch("stormcopy.catalog.get_api_key", return_value=None), \
             patch("stormcopy.catalog.discover_catalog") as discover:
            result = refresh_catalog_once("unused.sqlite")
        self.assertEqual(result["state"], "no_key")
        self.assertEqual(result["next_check_seconds"], NO_KEY_INTERVAL_SECONDS)
        discover.assert_not_called()

    def test_refresh_is_one_page_and_counts_persistent_ids(self):
        with tempfile.TemporaryDirectory() as tmp:
            db_path = Path(tmp) / "catalog.sqlite"
            with closing(connect(db_path)) as db:
                upsert_item(db, "123", "Stored vehicle")
            discovery = {"mode": "full", "pages": 1, "complete": False}
            with patch("stormcopy.catalog.get_api_key", return_value="key"), \
                 patch("stormcopy.catalog.discover_catalog", return_value=discovery) as crawl:
                result = refresh_catalog_once(db_path)
            self.assertEqual(result["known_items"], 1)
            self.assertEqual(result["indexed_items"], 0)
            self.assertEqual(result["next_check_seconds"], PAGE_INTERVAL_SECONDS)
            self.assertEqual(crawl.call_args.kwargs["max_pages"], 1)
            self.assertEqual(crawl.call_args.kwargs["refresh_interval_seconds"],
                             AUTO_REFRESH_INTERVAL_SECONDS)

    def test_completed_recent_sweep_waits_six_hours(self):
        with tempfile.TemporaryDirectory() as tmp:
            discovery = {"mode": "refresh", "pages": 1, "complete": True}
            with patch("stormcopy.catalog.get_api_key", return_value="key"), \
                 patch("stormcopy.catalog.discover_catalog", return_value=discovery):
                result = refresh_catalog_once(Path(tmp) / "catalog.sqlite")
        self.assertEqual(result["next_check_seconds"], AUTO_REFRESH_INTERVAL_SECONDS)

    def test_refresher_repeats_small_steps_until_stopped(self):
        seen = []
        done = threading.Event()
        refresher = CatalogRefresher("unused.sqlite", page_interval_seconds=0.01)

        def update(event):
            seen.append(event)
            if len(seen) == 2:
                refresher.stop()
                done.set()

        refresher.on_update = update
        result = {"state": "updated", "next_check_seconds": PAGE_INTERVAL_SECONDS,
                  "discovery": {"mode": "full", "complete": False}}
        with patch("stormcopy.catalog.refresh_catalog_once", return_value=result) as refresh:
            refresher.start()
            self.assertTrue(done.wait(2), "background refresh did not repeat")
        self.assertEqual(refresh.call_count, 2)


if __name__ == "__main__":
    unittest.main()
