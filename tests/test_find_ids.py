"""Offline checks for the explicit, resumable Find IDs crawl."""

from contextlib import closing
from pathlib import Path
import tempfile
import threading
import time
import unittest
from unittest.mock import patch

from stormcopy.catalog import CatalogRefresher, refresh_catalog_once
from stormcopy.index import connect, get_state
from stormcopy.steam import DISCOVERY_SCOPE_VERSION, discover_catalog


class _Updates:
    def __init__(self):
        self.events = []
        self.changed = threading.Condition()

    def add(self, event):
        with self.changed:
            self.events.append(event)
            self.changed.notify_all()

    def wait_for(self, predicate, timeout=3):
        end = time.monotonic() + timeout
        with self.changed:
            while not predicate(self.events):
                remaining = end - time.monotonic()
                if remaining <= 0:
                    raise AssertionError(f"Expected catalog update not seen: {self.events!r}")
                self.changed.wait(remaining)
            return list(self.events)


def _result(*, complete=False, state="updated", mode="full"):
    return {"state": state, "known_items": 123, "indexed_items": 4,
            "discovery": {"mode": mode, "pages": 1, "complete": complete,
                          "catalog_complete": complete},
            "next_check_seconds": 60}


class FindIdsTests(unittest.TestCase):
    def test_full_scan_restarts_completed_catalog_but_resumes_partial_cursor(self):
        with tempfile.TemporaryDirectory() as folder:
            with closing(connect(Path(folder) / "known.sqlite")) as db:
                cursors = []
                pages = [
                    ("*", "baseline-next", "101"),
                    ("baseline-next", None, "102"),
                    ("*", "manual-next", "201"),
                    ("manual-next", None, "202"),
                ]

                def query(_api_key, _delay, sort, cursor, *_filters):
                    cursors.append((sort, cursor))
                    expected_cursor, next_cursor, item_id = pages.pop(0)
                    self.assertEqual(cursor, expected_cursor)
                    return ({"next_cursor": next_cursor},
                            [{"publishedfileid": item_id, "title": f"Vehicle {item_id}"}])

                with patch("stormcopy.steam._query_files", side_effect=query):
                    first = discover_catalog(db, max_pages=1, delay=0, api_key="key")
                    self.assertFalse(first["complete"])
                    # A forced click while a baseline crawl is unfinished resumes.
                    baseline = discover_catalog(db, max_pages=1, delay=0,
                                                api_key="key", force_full_scan=True)
                    self.assertTrue(baseline["complete"])
                    # A new click after completion starts another full pass.
                    manual = discover_catalog(db, max_pages=1, delay=0,
                                              api_key="key", force_full_scan=True)
                    self.assertFalse(manual["complete"])
                    self.assertEqual(get_state(
                        db, f"cursor_v{DISCOVERY_SCOPE_VERSION}_published"), "manual-next")
                    resumed = discover_catalog(db, max_pages=1, delay=0,
                                               api_key="key", force_full_scan=False)
                    self.assertTrue(resumed["complete"])
                self.assertEqual(cursors, [("published", "*"),
                                           ("published", "baseline-next"),
                                           ("published", "*"),
                                           ("published", "manual-next")])
                self.assertEqual({row[0] for row in db.execute("SELECT id FROM items")},
                                 {"101", "102", "201", "202"})

    def test_forced_page_uses_one_request_and_persists_counts(self):
        with tempfile.TemporaryDirectory() as folder:
            db_path = Path(folder) / "known.sqlite"
            report = {"mode": "full", "pages": 1, "complete": False,
                      "catalog_complete": False}
            with patch("stormcopy.catalog.get_api_key", return_value="key"), \
                 patch("stormcopy.catalog.discover_catalog", return_value=report) as crawl:
                result = refresh_catalog_once(db_path, force_full_scan=True)
        self.assertEqual(result["state"], "updated")
        self.assertEqual(result["known_items"], 0)
        self.assertEqual(crawl.call_args.kwargs["max_pages"], 1)
        self.assertTrue(crawl.call_args.kwargs["force_full_scan"])

    def test_no_key_never_queries_steam_even_when_forced(self):
        with patch("stormcopy.catalog.get_api_key", return_value=None), \
             patch("stormcopy.catalog.discover_catalog") as crawl:
            result = refresh_catalog_once("unused.sqlite", force_full_scan=True)
        self.assertEqual(result["state"], "no_key")
        crawl.assert_not_called()

    def test_manual_crawl_runs_to_completion_with_one_worker(self):
        updates = _Updates()
        started = threading.Event()
        release = threading.Event()
        calls = []

        def page(_db_path, **kwargs):
            calls.append(kwargs)
            if len(calls) == 1:
                return _result(complete=True, mode="refresh")  # initial automatic check
            if len(calls) == 2:
                started.set()
                if not release.wait(2):
                    raise AssertionError("manual page was not released")
            return _result(complete=len(calls) == 4)

        finder = CatalogRefresher("unused.sqlite", on_update=updates.add,
                                  page_interval_seconds=3600,
                                  manual_page_interval_seconds=0.01)
        with patch("stormcopy.catalog.refresh_catalog_once", side_effect=page):
            finder.start()
            try:
                updates.wait_for(lambda events: len(events) >= 1)
                self.assertTrue(finder.find_all())
                self.assertTrue(started.wait(2))
                self.assertFalse(finder.find_all(), "a second click must not spawn a crawler")
                release.set()
                events = updates.wait_for(
                    lambda rows: any(row.get("manual_complete") for row in rows))
            finally:
                release.set()
                finder.stop()
        manual = [event for event in events if event.get("manual")]
        self.assertEqual([event["manual_pages"] for event in manual], [1, 2, 3])
        self.assertTrue(manual[-1]["manual_complete"])
        self.assertEqual(len(calls), 4)
        self.assertEqual([call.get("force_full_scan", False) for call in calls[1:]],
                         [True, False, False])

    def test_cancel_then_resume_uses_checkpoint_without_parallel_request(self):
        updates = _Updates()
        calls = []
        cancel_once = [True]
        resume_gate = threading.Event()

        def page(_db_path, **kwargs):
            calls.append(kwargs)
            if len(calls) == 1:
                return _result(complete=True, mode="refresh")
            return _result(complete=len(calls) >= 4)

        def record_and_cancel(event):
            if event.get("manual") and cancel_once[0]:
                cancel_once[0] = False
                self.assertTrue(finder.cancel_find())
            updates.add(event)
            if event.get("manual") and not resume_gate.is_set():
                if not resume_gate.wait(2):
                    raise AssertionError("resume did not release the callback")

        finder = CatalogRefresher("unused.sqlite", on_update=record_and_cancel,
                                  page_interval_seconds=3600,
                                  manual_page_interval_seconds=0.5)
        with patch("stormcopy.catalog.refresh_catalog_once", side_effect=page):
            finder.start()
            try:
                updates.wait_for(lambda events: len(events) >= 1)
                self.assertTrue(finder.find_all())
                updates.wait_for(lambda events: sum(bool(row.get("manual")) for row in events) == 1)
                # The pending cursor belongs to discover_catalog and must not
                # be reset by cancelling the GUI's loop between pages.
                self.assertTrue(finder.find_all())
                resume_gate.set()
                events = updates.wait_for(
                    lambda rows: any(row.get("manual_complete") for row in rows))
            finally:
                resume_gate.set()
                finder.stop()
        self.assertEqual(sum(bool(row.get("manual")) for row in events), 3)
        self.assertEqual(len(calls), 4)

    def test_manual_error_and_missing_key_end_the_loop(self):
        for failure in ("no_key", "error"):
            with self.subTest(failure=failure):
                updates = _Updates()
                calls = []

                def page(_db_path, **kwargs):
                    calls.append(kwargs)
                    if len(calls) == 1:
                        return _result(complete=True, mode="refresh")
                    if failure == "error":
                        raise RuntimeError("rate limited")
                    return {"state": "no_key", "next_check_seconds": 600}

                finder = CatalogRefresher("unused.sqlite", on_update=updates.add,
                                          page_interval_seconds=3600,
                                          manual_page_interval_seconds=0.01)
                with patch("stormcopy.catalog.refresh_catalog_once", side_effect=page):
                    finder.start()
                    try:
                        updates.wait_for(lambda events: len(events) >= 1)
                        self.assertTrue(finder.find_all())
                        events = updates.wait_for(
                            lambda rows: any(row.get("manual") and
                                             row["state"] == failure for row in rows))
                        time.sleep(0.05)
                    finally:
                        finder.stop()
                self.assertEqual(len(calls), 2)
                self.assertFalse(any(row.get("manual_complete") for row in events))

    def test_stop_during_request_does_not_fetch_another_page(self):
        updates = _Updates()
        request_started = threading.Event()
        release = threading.Event()
        calls = []

        def page(_db_path, **kwargs):
            calls.append(kwargs)
            if len(calls) == 1:
                return _result(complete=True, mode="refresh")
            request_started.set()
            if not release.wait(2):
                raise AssertionError("test request remained blocked")
            return _result(complete=False)

        finder = CatalogRefresher("unused.sqlite", on_update=updates.add,
                                  page_interval_seconds=3600,
                                  manual_page_interval_seconds=0.01)
        with patch("stormcopy.catalog.refresh_catalog_once", side_effect=page):
            finder.start()
            try:
                updates.wait_for(lambda events: len(events) >= 1)
                self.assertTrue(finder.find_all())
                self.assertTrue(request_started.wait(2))
                finder.stop()
                release.set()
                finder._thread.join(timeout=2)
                self.assertFalse(finder._thread.is_alive())
            finally:
                release.set()
                finder.stop()
        self.assertEqual(len(calls), 2)


if __name__ == "__main__":
    unittest.main()
