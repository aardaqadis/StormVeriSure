"""Resumable broad Workshop discovery and bounded item downloads."""

from collections import Counter
import os
from pathlib import Path
import shutil
import time

from . import steam
from .credentials import get_api_key
from .index import ensure_download_columns


def existing_workshop_folder(db, value=None):
    """Find the user's installed Steam content/573090 directory."""
    if value is not None:
        return steam._workshop_destination(Path.cwd(), value)

    candidates = []
    if os.name == "nt":
        for variable, fallback in (("PROGRAMFILES(X86)", r"C:\Program Files (x86)"),
                                   ("PROGRAMFILES", r"C:\Program Files")):
            candidates.append(Path(os.environ.get(variable, fallback)) / "Steam" /
                              "steamapps" / "workshop" / "content" / str(steam.APP_ID))
    else:
        candidates.extend((Path.home() / ".local/share/Steam/steamapps/workshop/content" /
                           str(steam.APP_ID),
                           Path.home() / ".steam/steam/steamapps/workshop/content" /
                           str(steam.APP_ID)))
    for candidate in candidates:
        if candidate.is_dir():
            return steam._workshop_destination(Path.cwd(), candidate)

    # Other Steam libraries can live on any drive. Reuse paths already indexed
    # from a conventional Workshop content tree, preferring the most common.
    roots = Counter()
    for row in db.execute("SELECT path FROM files LIMIT 10000"):
        for parent in Path(row[0]).parents:
            if (parent.name == str(steam.APP_ID) and
                    parent.parent.name.lower() == "content" and
                    parent.parent.parent.name.lower() == "workshop" and
                    parent.parent.parent.parent.name.lower() == "steamapps"):
                roots[parent] += 1
                break
    for candidate, _ in roots.most_common():
        if candidate.is_dir() and ".stormcopy-workers" not in candidate.parts:
            return steam._workshop_destination(Path.cwd(), candidate)
    raise ValueError("Existing Steam Workshop folder not found. Pass "
                     "--workshop-folder PATH ending in steamapps/workshop/content/573090")


def adopt_existing(db, workshop_folder, progress=None):
    """Count already installed items and queue only missing or older folders.

    A temporary SQLite table bounds Python memory even for very large local
    Workshop directories. A folder is considered current only when its mtime
    is at least the latest published update time known to the index.
    """
    ensure_download_columns(db)
    folder = Path(workshop_folder)
    db.execute("CREATE TEMP TABLE IF NOT EXISTS _stormcopy_bulk_existing "
               "(id TEXT PRIMARY KEY, mtime INTEGER NOT NULL) WITHOUT ROWID")
    db.execute("DELETE FROM temp._stormcopy_bulk_existing")
    examined = 0
    batch = []
    now = int(time.time())
    last_report = time.monotonic()
    try:
        for child in folder.iterdir():
            if (not child.name.isdigit() or not child.is_dir() or child.is_symlink() or
                    (hasattr(child, "is_junction") and child.is_junction())):
                continue
            mtime = max(1, min(int(child.stat().st_mtime), now))
            batch.append((child.name, mtime))
            examined += 1
            if len(batch) >= 1000:
                db.executemany("INSERT OR REPLACE INTO temp._stormcopy_bulk_existing "
                               "VALUES (?,?)", batch)
                db.commit()
                batch.clear()
            if progress is not None and time.monotonic() - last_report >= 2:
                progress({"phase": "Existing Workshop", "done": examined,
                          "total": None, "detail": "checking installed item folders"})
                last_report = time.monotonic()
        if batch:
            db.executemany("INSERT OR REPLACE INTO temp._stormcopy_bulk_existing "
                           "VALUES (?,?)", batch)
            db.commit()

        unknown = db.execute(
            "SELECT COUNT(*) FROM temp._stormcopy_bulk_existing e "
            "LEFT JOIN items i ON i.id=e.id WHERE i.id IS NULL").fetchone()[0]
        stale = db.execute(
            "SELECT COUNT(*) FROM temp._stormcopy_bulk_existing e "
            "JOIN items i ON i.id=e.id WHERE i.updated>e.mtime").fetchone()[0]
        absent = db.execute(
            "SELECT COUNT(*) FROM items i WHERE i.downloaded>0 "
            "AND i.id!='' AND i.id NOT GLOB '*[^0-9]*' "
            "AND NOT EXISTS (SELECT 1 FROM temp._stormcopy_bulk_existing e "
            "WHERE e.id=i.id)").fetchone()[0]
        with db:
            db.execute("INSERT INTO items(id,downloaded) "
                       "SELECT e.id,e.mtime FROM temp._stormcopy_bulk_existing e "
                       "WHERE NOT EXISTS (SELECT 1 FROM items i WHERE i.id=e.id)")
            db.execute(
                "UPDATE items SET downloaded=CASE "
                "WHEN updated=0 OR updated<=(SELECT e.mtime FROM "
                "temp._stormcopy_bulk_existing e WHERE e.id=items.id) "
                "THEN MAX(downloaded,(SELECT e.mtime FROM "
                "temp._stormcopy_bulk_existing e WHERE e.id=items.id)) "
                "ELSE 0 END WHERE id IN "
                "(SELECT id FROM temp._stormcopy_bulk_existing)")
            # `downloaded` describes the selected destination in this command.
            # An item cached only elsewhere must still be copied here.
            db.execute(
                "UPDATE items SET downloaded=0 WHERE downloaded>0 "
                "AND id!='' AND id NOT GLOB '*[^0-9]*' "
                "AND NOT EXISTS (SELECT 1 FROM temp._stormcopy_bulk_existing e "
                "WHERE e.id=items.id)")
        unindexed = db.execute(
            "SELECT COUNT(*) FROM temp._stormcopy_bulk_existing e "
            "JOIN items i ON i.id=e.id WHERE i.indexed=0").fetchone()[0]
        if progress is not None:
            progress({"phase": "Existing Workshop", "done": examined,
                      "total": examined, "detail": f"{examined - stale:,} current folders"})
        return {"folders": examined, "current": examined - stale,
                "stale": stale, "new_ids": unknown,
                "cached_elsewhere": absent, "unindexed": unindexed}
    finally:
        db.execute("DROP TABLE IF EXISTS temp._stormcopy_bulk_existing")
        db.commit()


def _pending_counts(db):
    now = int(time.time())
    base = ("FROM items WHERE id!='' AND id NOT GLOB '*[^0-9]*' "
            "AND (downloaded=0 OR updated>downloaded)")
    total = db.execute("SELECT COUNT(*) " + base).fetchone()[0]
    eligible = db.execute("SELECT COUNT(*) " + base + " AND retry_after<=?",
                          (now,)).fetchone()[0]
    return total, eligible


def _check_steamcmd(cache, steamcmd, workers):
    if steamcmd == "steamcmd" and (cache / "steamcmd.exe").is_file():
        resolved = cache / "steamcmd.exe"
    else:
        supplied = Path(steamcmd).expanduser()
        found = shutil.which(steamcmd)
        resolved = supplied if supplied.is_file() else Path(found) if found else None
    if resolved is None:
        raise ValueError("SteamCMD is required. Run `py -3 -m stormcopy setup-steamcmd "
                         "--cache steam-cache` first, or pass --steamcmd PATH")
    resolved = resolved.resolve()
    if workers > 1 and resolved.parent != cache:
        raise ValueError("Parallel downloads need SteamCMD inside --cache; "
                         "use setup-steamcmd --cache PATH or --workers 1")


def download_workshop(db, workshop_folder=None, cache="steam-cache", steamcmd="steamcmd",
                      *, known_only=False, pages=0, restart_discovery=False,
                      max_items=0, chunk_size=100, workers=4, batch_size=10,
                      delay=0.5, metadata_delay=0.5, login="anonymous",
                      reserve_free_gb=20.0, download_only=False, progress=None,
                      api_key=None):
    """Discover broad public coverage, then fill the installed Workshop folder.

    Each download call handles at most ``chunk_size`` IDs, so a full cursor
    pass does not turn into an unbounded in-memory SteamCMD queue. The SQLite
    discovery cursor and item states are committed throughout the run.
    """
    if not 1 <= workers <= steam.MAX_DOWNLOAD_WORKERS:
        raise ValueError(f"processes must be between 1 and {steam.MAX_DOWNLOAD_WORKERS}")
    if (pages < 0 or max_items < 0 or not 1 <= chunk_size <= 500 or
            not 1 <= batch_size <= 50 or
            delay < 0 or metadata_delay < 0 or reserve_free_gb < 0):
        raise ValueError("Invalid pages, item, chunk, batch, delay, or disk-reserve value")
    if known_only and restart_discovery:
        raise ValueError("--known-only cannot be combined with --restart-discovery")
    api_key = get_api_key(api_key)
    if not known_only and not api_key:
        raise ValueError("Set STEAM_API_KEY to discover most public Workshop items, "
                         "or use --known-only for items already in the index")
    ensure_download_columns(db)
    destination = existing_workshop_folder(db, workshop_folder)
    cache = Path(cache).expanduser().resolve()
    if (cache == destination or cache.is_relative_to(destination) or
            destination.is_relative_to(cache)):
        raise ValueError("SteamCMD staging cache must be separate from the Steam installation")
    cache.mkdir(parents=True, exist_ok=True)
    _check_steamcmd(cache, steamcmd, workers)

    started = time.monotonic()
    discovery = None
    if not known_only:
        discovery = steam.discover_catalog(db, max_pages=pages, delay=metadata_delay,
                                           api_key=api_key, restart=restart_discovery,
                                           progress=progress,
                                           refresh_interval_seconds=
                                               steam.AUTO_REFRESH_INTERVAL_SECONDS)
    existing = adopt_existing(db, destination, progress=progress)
    # QueryFiles normally supplies size metadata. Bound the public details
    # refresh for older locally known IDs so bulk downloading can start.
    sizes = steam.refresh_sizes(db, max_items=1000, delay=metadata_delay,
                                pending_only=True, progress=progress)
    initial_pending, initial_eligible = _pending_counts(db)
    target = min(initial_eligible, max_items) if max_items else initial_eligible
    download_started = time.monotonic()
    attempted = downloaded = failed = indexed = no_vehicle = 0
    batches = 0
    active_workers = workers
    peak_workers_used = 0
    unthrottled_chunks = 0
    stop_reason = "queue empty"
    reserve_bytes = int(reserve_free_gb * 1024 ** 3)

    def show(event):
        if progress is None:
            return
        if event.get("phase") == "Prepare workers":
            event = {**event, "phase": "Workshop download", "done": attempted,
                     "total": target}
        elif event.get("phase") == "Download":
            aggregate_done = attempted + (event.get("done") or 0)
            event = {**event, "phase": "Workshop download",
                     "done": aggregate_done,
                     "total": target}
            if "cached_count" in event:
                cached_count = downloaded + event.pop("cached_count")
                failed_count = failed + event.pop("failed_count")
                worker_count = event.pop("worker_count")
                event["detail"] = (f"{cached_count:,} cached, {failed_count:,} failed; "
                                   f"{worker_count} process(es)")
        if event.get("phase") == "Workshop download":
            aggregate_done = event.get("done") or 0
            elapsed = time.monotonic() - download_started
            if event.get("warning"):
                event["eta_paused"] = True
            elif target > aggregate_done >= 2 and elapsed >= 2:
                event["eta_seconds"] = elapsed * (target - aggregate_done) / aggregate_done
        progress(event)

    # A failed small item can become eligible again during a very long run.
    # Keep one run's attempted IDs in SQLite so it still advances to larger
    # items without holding a huge set in Python or retrying indefinitely.
    db.execute("CREATE TEMP TABLE IF NOT EXISTS _stormcopy_bulk_attempted "
               "(id TEXT PRIMARY KEY) WITHOUT ROWID")
    db.execute("DELETE FROM temp._stormcopy_bulk_attempted")
    db.commit()
    try:
        while not max_items or attempted < max_items:
            if (shutil.disk_usage(destination).free <= reserve_bytes or
                    shutil.disk_usage(cache).free <= reserve_bytes):
                stop_reason = "disk reserve reached"
                break
            remaining = max_items - attempted if max_items else chunk_size
            size = min(chunk_size, remaining)
            result = steam.download_pending(
                db, cache, steamcmd=steamcmd, max_items=size, delay=delay,
                login=login, batch_size=batch_size, cache_only=download_only,
                workers=active_workers, progress=show, workshop_folder=destination,
                skip_attempted=True)
            if result["selected"] == 0:
                stop_reason = "queue empty or items waiting to retry"
                break
            db.executemany("INSERT OR IGNORE INTO temp._stormcopy_bulk_attempted VALUES (?)",
                           ((item_id,) for item_id in result["selected_ids"]))
            db.commit()
            attempted += result["selected"]
            downloaded += result["cached"]
            failed += result["failed"]
            indexed += result["downloaded_and_indexed"]
            no_vehicle += result["cached_without_vehicle"]
            batches += result["batches"]
            peak_workers_used = max(peak_workers_used, result.get("workers_used", 0))
            if max_items and attempted >= max_items:
                stop_reason = "item limit reached"
                break
            if result.get("rate_limited"):
                active_workers = max(1, active_workers // 2)
                unthrottled_chunks = 0
                cooldown = max(0.0, result.get("cooldown_remaining_seconds", 30.0))
                if progress is not None:
                    progress({"phase": "Workshop download", "done": attempted,
                              "total": target,
                              "eta_paused": cooldown > 0,
                              "warning": ("Steam asked us to slow down; waiting before the next group"
                                          if cooldown > 0 else
                                          "Steam asked us to slow down; continuing with fewer workers")})
                if cooldown:
                    time.sleep(cooldown)
            else:
                unthrottled_chunks += 1
                if unthrottled_chunks >= 2 and active_workers < workers:
                    active_workers += 1
                    unthrottled_chunks = 0
    finally:
        db.execute("DROP TABLE IF EXISTS temp._stormcopy_bulk_attempted")
        db.commit()

    pending, eligible = _pending_counts(db)
    known = db.execute("SELECT COUNT(*) FROM items WHERE id!='' "
                       "AND id NOT GLOB '*[^0-9]*'").fetchone()[0]
    in_folder = db.execute("SELECT COUNT(*) FROM items WHERE downloaded>0 AND id!='' "
                           "AND id NOT GLOB '*[^0-9]*'").fetchone()[0]
    return {"workshop_folder": str(destination), "staging_cache": str(cache),
            "discovery": discovery, "discovery_complete":
                discovery.get("catalog_complete", discovery["complete"]) if discovery else None,
            "existing": existing, "size_refresh": sizes,
            "known_items": known, "in_workshop_folder": in_folder,
            "initial_pending": initial_pending, "attempted": attempted,
            "downloaded": downloaded, "indexed": indexed,
            "cached_without_vehicle": no_vehicle, "failed": failed,
            "remaining_pending": pending, "remaining_eligible": eligible,
            "waiting_to_retry": pending - eligible,
            "batches": batches, "workers_requested": workers,
            "peak_workers_used": peak_workers_used,
            "stop_reason": stop_reason,
            "elapsed_seconds": round(time.monotonic() - started, 1)}
