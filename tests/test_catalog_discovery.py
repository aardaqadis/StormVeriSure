"""Offline behavior tests for the resumable Workshop catalog crawler."""

from contextlib import redirect_stdout
from io import StringIO
import json
from pathlib import Path
import sys
import tempfile
import unittest
import urllib.parse
from unittest.mock import patch

from stormcopy import steam
from stormcopy.__main__ import main as cli_main
from stormcopy.bulk import download_workshop
from stormcopy.console import Console
from stormcopy.index import connect, get_state, set_state


def page(items=(), next_cursor=None):
    return {"response": {"result": 1, "publishedfiledetails": list(items),
                         "next_cursor": next_cursor}}


def item(item_id, updated=None, title=None):
    details = {"publishedfileid": str(item_id), "title": title or f"Vehicle {item_id}",
               "file_size": "123"}
    if updated is not None:
        details["time_updated"] = updated
    return details


def request_payload(call):
    query = urllib.parse.parse_qs(urllib.parse.urlsplit(call.args[0]).query)
    return json.loads(query["input_json"][0])


class CatalogDiscoveryTests(unittest.TestCase):
    def setUp(self):
        self.folder = tempfile.TemporaryDirectory()
        self.addCleanup(self.folder.cleanup)
        self.db = connect(Path(self.folder.name) / "catalog.sqlite")
        self.addCleanup(self.db.close)

    def test_full_crawl_checkpoints_cursor_then_switches_to_updated_refresh(self):
        clock = [2_000_000]
        responses = [page([item(101, clock[0] - 10)], "second"),
                     page([item(102, clock[0] - 5)]),
                     page([item(102, clock[0] - 5)])]
        with patch("stormcopy.steam.time.time", side_effect=lambda: clock[0]), \
             patch("stormcopy.steam._request_json", side_effect=responses) as request:
            first = steam.discover_catalog(self.db, max_pages=1, delay=0, api_key="key")
            self.assertFalse(first["complete"])
            resumed = steam.discover_catalog(self.db, max_pages=1, delay=0, api_key="key")
            self.assertTrue(resumed["complete"])
            clock[0] += 3600
            refreshed = steam.discover_catalog(self.db, max_pages=1, delay=0,
                                                api_key="key")
            self.assertTrue(refreshed["complete"])
        payloads = [request_payload(call) for call in request.call_args_list]
        self.assertEqual([(p["query_type"], p["cursor"]) for p in payloads],
                         [(1, "*"), (1, "second"), (21, "*")])
        self.assertEqual({row[0] for row in self.db.execute("SELECT id FROM items")},
                         {"101", "102"})

    def test_refresh_resumes_with_frozen_overlap_and_keeps_cutoff_ties(self):
        clock = [2_000_000]
        cutoff = clock[0] - 7 * 86400
        responses = [page([item(100, clock[0] - 1)]),
                     page([item(101, clock[0] + 10)], "refresh-2"),
                     page([item(102, cutoff), item(103)], "refresh-3"),
                     page([item(104, cutoff)], "refresh-4"),
                     page([item(105, cutoff - 1)], "unused")]
        with patch("stormcopy.steam.time.time", side_effect=lambda: clock[0]), \
             patch("stormcopy.steam._request_json", side_effect=responses) as request:
            steam.discover_catalog(self.db, max_pages=1, delay=0, api_key="key")
            clock[0] += 3600
            first = steam.discover_catalog(self.db, max_pages=1, delay=0, api_key="key")
            self.assertFalse(first["complete"])
            clock[0] += 2 * 86400
            for expected_complete in (False, False, True):
                result = steam.discover_catalog(self.db, max_pages=1, delay=0,
                                                api_key="key")
                self.assertEqual(result["complete"], expected_complete)
        payloads = [request_payload(call) for call in request.call_args_list]
        self.assertEqual([(p["query_type"], p["cursor"]) for p in payloads],
                         [(1, "*"), (21, "*"), (21, "refresh-2"),
                          (21, "refresh-3"), (21, "refresh-4")])
        self.assertIn("104", {row[0] for row in self.db.execute("SELECT id FROM items")})

    def test_refresh_requeues_new_and_changed_items_only(self):
        now = 2_000_000
        initial = page([item(101, now - 10), item(102, now - 10)])
        newer = page([item(101, now - 10, "Renamed only"), item(102, now + 5),
                      item(103, now + 5)])
        with patch("stormcopy.steam.time.time", return_value=now), \
             patch("stormcopy.steam._request_json", side_effect=[initial, newer]):
            steam.discover_catalog(self.db, max_pages=0, delay=0, api_key="key")
            self.db.execute("UPDATE items SET downloaded=updated WHERE id IN ('101','102')")
            self.db.commit()
            steam.discover_catalog(self.db, max_pages=0, delay=0, api_key="key")
        queued = {row[0] for row in self.db.execute(
            "SELECT id FROM items WHERE downloaded=0 OR updated>downloaded")}
        self.assertEqual(queued, {"102", "103"})
        self.assertEqual(self.db.execute(
            "SELECT title FROM items WHERE id='101'").fetchone()[0], "Renamed only")

    def test_repeated_refresh_cursor_keeps_checkpoint_and_watermark(self):
        clock = [2_000_000]
        initial = page([item(100, clock[0])])
        first = page([item(101, clock[0] + 100)], "next")
        repeated = page([item(102, clock[0] + 50)], "next")
        finishing = page([item(103, clock[0] - 8 * 86400)], "later")
        with patch("stormcopy.steam.time.time", side_effect=lambda: clock[0]), \
             patch("stormcopy.steam._request_json", side_effect=[
                 initial, first, repeated, finishing]) as request:
            steam.discover_catalog(self.db, max_pages=1, delay=0, api_key="key")
            original_watermark = get_state(self.db, "refresh_watermark_v1")
            clock[0] += 3600
            steam.discover_catalog(self.db, max_pages=1, delay=0, api_key="key")
            with self.assertRaisesRegex(RuntimeError, "same refresh cursor"):
                steam.discover_catalog(self.db, max_pages=1, delay=0, api_key="key")
            self.assertEqual(get_state(self.db, "refresh_watermark_v1"),
                             original_watermark)
            self.assertEqual(get_state(self.db, "refresh_cursor_v1"), "next")
            completed = steam.discover_catalog(self.db, max_pages=1, delay=0,
                                                api_key="key")
        self.assertTrue(completed["complete"])
        self.assertEqual([request_payload(call)["cursor"]
                          for call in request.call_args_list],
                         ["*", "*", "next", "next"])
        self.assertIsNone(self.db.execute(
            "SELECT id FROM items WHERE id='102'").fetchone())

    def test_refresh_watermark_uses_sweep_start_for_later_changes(self):
        clock = [2_000_000]
        full = page([item(100, clock[0])])
        refresh_first = page([item(101, clock[0] + 3600)], "next")
        refresh_last = page([item(102, clock[0] - 8 * 86400)], "later")
        next_refresh = page([item(777, clock[0] + 3700)])
        with patch("stormcopy.steam.time.time", side_effect=lambda: clock[0]), \
             patch("stormcopy.steam._request_json", side_effect=[
                 full, refresh_first, refresh_last, next_refresh]) as request:
            steam.discover_catalog(self.db, max_pages=1, delay=0, api_key="key")
            clock[0] += 3600
            sweep_start = clock[0]
            first = steam.discover_catalog(self.db, max_pages=1, delay=0,
                                           api_key="key")
            self.assertFalse(first["complete"])
            clock[0] += 8 * 86400
            finished = steam.discover_catalog(self.db, max_pages=1, delay=0,
                                              api_key="key")
            self.assertTrue(finished["complete"])
            self.assertEqual(int(get_state(self.db, "refresh_watermark_v1")),
                             sweep_start)
            clock[0] += 1
            steam.discover_catalog(self.db, max_pages=1, delay=0, api_key="key")
        self.assertEqual([request_payload(call)["query_type"]
                          for call in request.call_args_list], [1, 21, 21, 21])
        self.assertEqual(self.db.execute(
            "SELECT updated,downloaded FROM items WHERE id='777'").fetchone()[:],
                         (2_003_700, 0))

    def test_completed_older_database_gets_initial_updated_sweep(self):
        set_state(self.db, "discovery_complete_v2_published", "1")
        responses = [page([item(101, 1)], "later"), page([item(102, 2)])]
        with patch("stormcopy.steam.time.time", return_value=2_000_000), \
             patch("stormcopy.steam._request_json", side_effect=responses) as request:
            first = steam.discover_catalog(self.db, max_pages=1, delay=0, api_key="key")
            second = steam.discover_catalog(self.db, max_pages=1, delay=0, api_key="key")
        self.assertFalse(first["complete"])
        self.assertTrue(second["complete"])
        self.assertEqual([(request_payload(call)["query_type"],
                           request_payload(call)["cursor"])
                          for call in request.call_args_list],
                         [(21, "*"), (21, "later")])

    def test_periodic_full_recrawl_and_tag_scopes(self):
        clock = [2_000_000]
        responses = [page([item(101, clock[0])]),
                     page([item(201, clock[0])]),
                     page([item(101, clock[0])]),
                     page([item(202, clock[0])])]
        with patch("stormcopy.steam.time.time", side_effect=lambda: clock[0]), \
             patch("stormcopy.steam._request_json", side_effect=responses) as request:
            steam.discover_catalog(self.db, max_pages=0, delay=0, api_key="key")
            steam.discover_catalog(self.db, max_pages=0, delay=0, api_key="key",
                                   tags=("Vehicle",))
            clock[0] += 31 * 86400
            steam.discover_catalog(self.db, max_pages=0, delay=0, api_key="key")
            steam.discover_catalog(self.db, max_pages=0, delay=0, api_key="key",
                                   tags=("Vehicle",))
        payloads = [request_payload(call) for call in request.call_args_list]
        self.assertEqual([p["query_type"] for p in payloads], [1, 1, 1, 1])
        self.assertEqual([p["cursor"] for p in payloads], ["*"] * 4)
        self.assertNotIn("requiredtags", payloads[0])
        self.assertEqual(payloads[1]["requiredtags"], ["Vehicle"])
        self.assertNotIn("requiredtags", payloads[2])
        self.assertEqual(payloads[3]["requiredtags"], ["Vehicle"])

    def test_automatic_refresh_cooldown_skips_only_completed_recent_pass(self):
        clock = [2_000_000]
        responses = [page([item(100, clock[0])]),
                     page([item(101, clock[0] + 10)]),
                     page([item(102, clock[0] + 20)], "resume-me"),
                     page([item(103, clock[0] + 30)])]
        interval = 6 * 3600
        with patch("stormcopy.steam.time.time", side_effect=lambda: clock[0]), \
             patch("stormcopy.steam._request_json", side_effect=responses) as request:
            steam.discover_catalog(self.db, max_pages=1, delay=0, api_key="key")
            clock[0] += 3600
            steam.discover_catalog(self.db, max_pages=1, delay=0, api_key="key",
                                   refresh_interval_seconds=interval)
            clock[0] += 60
            skipped = steam.discover_catalog(self.db, max_pages=1, delay=0,
                                             api_key="key",
                                             refresh_interval_seconds=interval)
            self.assertTrue(skipped["skipped_recently"])
            self.assertEqual(skipped["pages"], 0)
            self.assertEqual(request.call_count, 2)
            # An explicit CLI-style run has no cooldown and can begin a pass.
            explicit = steam.discover_catalog(self.db, max_pages=1, delay=0,
                                              api_key="key")
            self.assertFalse(explicit["complete"])
            clock[0] += 60
            resumed = steam.discover_catalog(self.db, max_pages=1, delay=0,
                                              api_key="key",
                                              refresh_interval_seconds=interval)
            self.assertTrue(resumed["complete"])
            self.assertFalse(resumed.get("skipped_recently", False))
        self.assertEqual([request_payload(call)["cursor"]
                          for call in request.call_args_list],
                         ["*", "*", "*", "resume-me"])

    def test_bulk_uses_catalog_discovery_and_reports_full_coverage(self):
        root = Path(self.folder.name)
        folder = root / "library" / "steamapps" / "workshop" / "content" / "573090"
        folder.mkdir(parents=True)
        cache = root / "cache"
        cache.mkdir()
        (cache / "steamcmd.exe").write_bytes(b"MZ test")
        discovery = {"pages": 1, "items_seen": 0, "complete": False,
                     "catalog_complete": True, "mode": "updated"}
        empty_download = {"selected": 0, "selected_ids": [], "cached": 0,
                          "failed": 0, "downloaded_and_indexed": 0,
                          "cached_without_vehicle": 0, "batches": 0}
        with patch("stormcopy.bulk.steam.discover_catalog", return_value=discovery) as crawl, \
             patch("stormcopy.bulk.steam.discover", side_effect=AssertionError(
                 "bulk should use catalog discovery")), \
             patch("stormcopy.bulk.steam.refresh_sizes", return_value={
                 "checked": 0, "sizes_added": 0, "unknown_sizes": 0}), \
             patch("stormcopy.bulk.steam.download_pending", return_value=empty_download):
            result = download_workshop(self.db, folder, cache, api_key="key", workers=1,
                                       metadata_delay=0, delay=0, reserve_free_gb=0)
        self.assertEqual(crawl.call_count, 1)
        self.assertEqual(crawl.call_args.kwargs["refresh_interval_seconds"], 6 * 3600)
        self.assertTrue(result["discovery_complete"])

    def test_cli_routes_default_discover_to_catalog_and_reports_refresh(self):
        report = {"mode": "refresh", "pages": 2, "items_seen": 12,
                  "complete": False, "catalog_complete": True,
                  "tags": (), "excluded_tags": ()}
        output = StringIO()
        argv = ["stormcopy", "--db", str(Path(self.folder.name) / "cli.sqlite"),
                "discover", "--pages", "2", "--restart", "--json"]
        with patch.object(sys, "argv", argv), \
             patch("stormcopy.__main__.discover_catalog", return_value=report) as crawl, \
             redirect_stdout(output):
            cli_main()
        self.assertEqual(crawl.call_args.kwargs["max_pages"], 2)
        self.assertTrue(crawl.call_args.kwargs["restart"])
        self.assertEqual(json.loads(output.getvalue())["mode"], "refresh")

        readable = StringIO()
        Console(stdout=readable, stderr=StringIO(), color=False).render("discover", report)
        self.assertIn("Recent updates refresh", readable.getvalue())
        self.assertIn("Current pass complete: no", readable.getvalue())
        self.assertIn("Full public catalog crawl complete: yes", readable.getvalue())

    def test_tag_scoped_discovery_report_does_not_claim_full_public_coverage(self):
        filtered = {"mode": "full", "pages": 1, "items_seen": 3,
                    "complete": True, "catalog_complete": True,
                    "tags": ("Vehicle",), "excluded_tags": ("WIP",)}
        output = StringIO()
        Console(stdout=output, stderr=StringIO(), color=False).render(
            "discover", filtered)
        report = output.getvalue()
        self.assertRegex(report, r"Filtered .* crawl complete: yes")
        self.assertNotIn("Full public catalog crawl complete", report)


if __name__ == "__main__":
    unittest.main()
