"""Bounded, background maintenance of the persistent Workshop ID catalog.

``items`` is the catalog: its primary-key B-tree gives indexed ID lookups
without loading or sorting every ID in memory. Discovery only adds metadata;
XML similarity still requires downloaded and fingerprinted vehicle files.
"""

from contextlib import closing
from pathlib import Path
import sqlite3
import threading

from .credentials import get_api_key
from .index import connect
from .steam import AUTO_REFRESH_INTERVAL_SECONDS, discover_catalog


PAGE_INTERVAL_SECONDS = 60
NO_KEY_INTERVAL_SECONDS = 10 * 60
ERROR_INTERVAL_SECONDS = 15 * 60


def known_id_page(db, *, after_id="", limit=100):
    """Read a small sorted page of numeric IDs from the persistent catalog.

    The ``items`` primary key supplies the ordering and cursor lookup, so a
    large catalog never needs a Python list or full re-sort for display.
    """
    if not 1 <= limit <= 1000:
        raise ValueError("limit must be between 1 and 1000")
    rows = db.execute(
        "SELECT id,title,indexed FROM items WHERE id>? AND id!='' "
        "AND id NOT GLOB '*[^0-9]*' ORDER BY id LIMIT ?",
        (after_id, limit)).fetchall()
    return [{"id": row[0], "title": row[1], "indexed": bool(row[2])}
            for row in rows]


def refresh_catalog_once(db_path, *, api_key=None, delay=0.5,
                         force_full_scan=False):
    """Fetch at most one public Workshop page and preserve its resume cursor.

    The caller may run this repeatedly. A completed updated-items pass obeys
    Steam's six-hour refresh interval. No API key means no network request.
    """
    key = get_api_key(api_key)
    if not key:
        return {"state": "no_key", "next_check_seconds": NO_KEY_INTERVAL_SECONDS,
                "message": "Save a Steam Web API key to update known Workshop IDs."}
    with closing(connect(Path(db_path))) as db:
        discovery = discover_catalog(
            db, max_pages=1, delay=delay, api_key=key,
            refresh_interval_seconds=AUTO_REFRESH_INTERVAL_SECONDS,
            force_full_scan=force_full_scan)
        known = db.execute("SELECT COUNT(*) FROM items").fetchone()[0]
        indexed = db.execute("SELECT COUNT(DISTINCT item_id) FROM files").fetchone()[0]
    if discovery.get("skipped_recently") or (
            discovery.get("mode") == "refresh" and discovery.get("complete")):
        next_check = AUTO_REFRESH_INTERVAL_SECONDS
    else:
        next_check = PAGE_INTERVAL_SECONDS
    return {"state": "updated", "known_items": known, "indexed_items": indexed,
            "discovery": discovery, "next_check_seconds": next_check}


class CatalogRefresher:
    """Run small discovery steps without blocking a GUI or active comparison.

    ``on_update`` is called from the worker thread. GUI users must marshal it
    to their main thread before touching widgets. ``stop`` does not wait for a
    network request to finish, so closing a window stays responsive.
    """

    def __init__(self, db_path, on_update=None, *, page_interval_seconds=PAGE_INTERVAL_SECONDS,
                 manual_page_interval_seconds=2.0):
        if page_interval_seconds <= 0 or manual_page_interval_seconds <= 0:
            raise ValueError("page intervals must be positive")
        self.db_path = Path(db_path)
        self.on_update = on_update
        self.page_interval_seconds = page_interval_seconds
        self.manual_page_interval_seconds = manual_page_interval_seconds
        self._stop = threading.Event()
        self._wake = threading.Event()
        self._mode_lock = threading.Lock()
        self._manual = False
        self._manual_generation = 0
        self._manual_first_page = False
        self._manual_pages = 0
        self._manual_seen = 0
        self._thread = None

    def start(self):
        if self._thread is not None and self._thread.is_alive():
            return
        self._stop.clear()
        self._thread = threading.Thread(target=self._run, name="WorkshopCatalog",
                                        daemon=True)
        self._thread.start()

    def stop(self):
        self._stop.set()
        self._wake.set()

    @property
    def is_finding(self):
        with self._mode_lock:
            return self._manual

    def find_all(self):
        """Start or resume a public full crawl in the existing worker thread."""
        with self._mode_lock:
            if self._manual:
                return False
            self._manual = True
            self._manual_generation += 1
            self._manual_first_page = True
            self._manual_pages = 0
            self._manual_seen = 0
        if self._thread is not None and self._thread.is_alive():
            self._wake.set()
        return True

    def cancel_find(self):
        """Stop after the current page; its cursor remains saved for resume."""
        with self._mode_lock:
            if not self._manual:
                return False
            self._manual = False
            self._manual_generation += 1
        self._wake.set()
        return True

    def _run(self):
        while not self._stop.is_set():
            with self._mode_lock:
                manual = self._manual
                generation = self._manual_generation
                first_page = self._manual_first_page if manual else False
            try:
                event = refresh_catalog_once(
                    self.db_path, delay=0.5,
                    force_full_scan=first_page)
                wait_seconds = event["next_check_seconds"]
                if event["state"] == "updated" and wait_seconds == PAGE_INTERVAL_SECONDS:
                    wait_seconds = self.page_interval_seconds
            except (OSError, ValueError, RuntimeError, sqlite3.Error) as exc:
                # A denied key, network outage, or rate limit must not spin or
                # interrupt scanning. Retry only after a substantial cooldown.
                event = {"state": "error", "message": str(exc),
                         "next_check_seconds": ERROR_INTERVAL_SECONDS}
                wait_seconds = ERROR_INTERVAL_SECONDS
            if manual:
                with self._mode_lock:
                    still_finding = (self._manual and
                                     self._manual_generation == generation)
                    if still_finding:
                        if event["state"] == "updated":
                            self._manual_first_page = False
                            discovery = event["discovery"]
                            self._manual_pages += discovery.get("pages", 0)
                            self._manual_seen += discovery.get("items_seen", 0)
                            complete = bool(discovery.get("complete"))
                        else:
                            complete = False
                        pages = self._manual_pages
                        seen = self._manual_seen
                        if complete or event["state"] != "updated":
                            self._manual = False
                if still_finding:
                    event = {**event, "manual": True,
                             "manual_pages": pages,
                             "manual_items_seen": seen,
                             "manual_complete": complete}
                    if event["state"] == "updated" and not complete:
                        wait_seconds = self.manual_page_interval_seconds
                else:
                    event = {**event, "manual_cancelled": True}
            if self.on_update is not None and not self._stop.is_set():
                self.on_update(event)
            self._wake.wait(wait_seconds)
            self._wake.clear()
