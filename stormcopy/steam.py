"""Workshop discovery, reported file sizes, and a persistent SteamCMD cache."""

from io import BytesIO
from contextlib import contextmanager, nullcontext
from concurrent.futures import FIRST_COMPLETED, ThreadPoolExecutor, wait
import errno
import hashlib
import json
import os
from pathlib import Path
import re
import shutil
import subprocess
import sys
import time
import urllib.error
import urllib.parse
import urllib.request
from zipfile import BadZipFile, ZipFile
from uuid import uuid4

from .index import ensure_download_columns, get_state, index_file, set_state, upsert_item
from .credentials import get_api_key
from . import search_index

APP_ID = 573090
MAX_DOWNLOAD_WORKERS = 16
DISCOVERY_SCOPE_VERSION = 2
REFRESH_OVERLAP_SECONDS = 7 * 86400
AUTO_REFRESH_INTERVAL_SECONDS = 6 * 3600
FULL_RESCAN_SECONDS = 30 * 86400
API_URL = "https://api.steampowered.com/IPublishedFileService/QueryFiles/v1/"
DETAILS_URL = "https://api.steampowered.com/ISteamRemoteStorage/GetPublishedFileDetails/v1/"
STEAMCMD_URL = "https://steamcdn-a.akamaihd.net/client/installer/steamcmd.zip"
_RATE_LIMIT_OUTPUT = re.compile(
    r"rate[\s_-]*limit(?:ed|ing)?|too\s+many\s+requests|"
    r"\b(?:http(?:/\d(?:\.\d)?)?(?:\s+error)?|status(?:\s+code)?|error)\s*"
    r"(?:[:=#-]\s*)?429\b", re.I)


def _steamcmd_rate_limited(output):
    """Require an actual status or rate-limit phrase, not digits in an item ID."""
    return bool(_RATE_LIMIT_OUTPUT.search(output))


def setup_steamcmd(cache, force=False):
    """Install only Valve's Windows SteamCMD bootstrap executable, on request."""
    if os.name != "nt":
        raise ValueError("Automatic SteamCMD setup is available on Windows only")
    cache = Path(cache).resolve()
    cache.mkdir(parents=True, exist_ok=True)
    with _cache_lock(cache):
        return _setup_steamcmd_locked(cache, force)


def _setup_steamcmd_locked(cache, force):
    executable = cache / "steamcmd.exe"
    if executable.is_file() and not force:
        return {"steamcmd": str(executable), "installed": False}
    request = urllib.request.Request(STEAMCMD_URL, headers={"User-Agent": "stormcopy/0.2"})
    try:
        with urllib.request.urlopen(request, timeout=30) as response:
            archive = response.read(10 * 1024 * 1024 + 1)
    except urllib.error.URLError as exc:
        raise RuntimeError(f"Could not download SteamCMD: {exc.reason}") from exc
    if len(archive) > 10 * 1024 * 1024:
        raise ValueError("SteamCMD bootstrap archive is unexpectedly large")
    try:
        with ZipFile(BytesIO(archive)) as zipped:
            info = zipped.getinfo("steamcmd.exe")
            if info.file_size > 10 * 1024 * 1024:
                raise ValueError("SteamCMD bootstrap executable is unexpectedly large")
            binary = zipped.read(info)
    except (BadZipFile, KeyError) as exc:
        raise ValueError("SteamCMD download is not a valid bootstrap archive") from exc
    if not binary.startswith(b"MZ"):
        raise ValueError("SteamCMD download is not a Windows executable")
    temporary = cache / "steamcmd.exe.tmp"
    temporary.write_bytes(binary)
    temporary.replace(executable)
    return {"steamcmd": str(executable), "installed": True}


def _size(value):
    try:
        size = int(value)
    except (TypeError, ValueError):
        return None
    return size if size > 0 else None


def _tags(values):
    if isinstance(values, str):
        values = (values,)
    result = {}
    for value in values or ():
        if not isinstance(value, str) or not value.strip():
            raise ValueError("Workshop tags must be nonempty text")
        tag = value.strip()
        result.setdefault(tag.casefold(), tag)
    return tuple(result.values())


def _check_tag_filters(required, excluded):
    required, excluded = _tags(required), _tags(excluded)
    if {tag.casefold() for tag in required} & {tag.casefold() for tag in excluded}:
        raise ValueError("The same Workshop tag cannot be both required and excluded")
    return required, excluded


def _tag_clause(required, excluded, match_all=True):
    required, excluded = _check_tag_filters(required, excluded)
    sql, values = "", []
    if required or excluded:
        sql += " AND items.tag_checked>=?"
        values.append(int(time.time()) - 7 * 86400)
    keys = [tag.casefold() for tag in required]
    if keys:
        if match_all:
            for key in keys:
                sql += (" AND EXISTS (SELECT 1 FROM item_tags t WHERE "
                        "t.item_id=items.id AND t.tag_key=?)")
                values.append(key)
        else:
            slots = ",".join("?" for _ in keys)
            sql += (" AND EXISTS (SELECT 1 FROM item_tags t WHERE "
                    f"t.item_id=items.id AND t.tag_key IN ({slots}))")
            values.extend(keys)
    keys = [tag.casefold() for tag in excluded]
    if keys:
        slots = ",".join("?" for _ in keys)
        sql += (" AND NOT EXISTS (SELECT 1 FROM item_tags t WHERE "
                f"t.item_id=items.id AND t.tag_key IN ({slots}))")
        values.extend(keys)
    return sql, tuple(values)


def _save_item_tags(db, item):
    """Replace tags only when Steam actually included a tags field."""
    if "tags" not in item or not item.get("publishedfileid"):
        return
    item_id = str(item["publishedfileid"])
    raw_tags = item.get("tags") or []
    if not isinstance(raw_tags, list):
        raw_tags = []
    tags = _tags(entry["tag"] for entry in raw_tags
                 if isinstance(entry, dict) and isinstance(entry.get("tag"), str)
                 and entry["tag"].strip())
    db.execute("DELETE FROM item_tags WHERE item_id=?", (item_id,))
    db.executemany("INSERT INTO item_tags(item_id,tag,tag_key) VALUES (?,?,?)",
                   ((item_id, tag, tag.casefold()) for tag in tags))
    db.execute("UPDATE items SET tag_checked=? WHERE id=?", (int(time.time()), item_id))


def _item_matches_tags(item, required, excluded, match_all):
    """Check returned tags too, in case a remote filter is ignored."""
    if not required and not excluded:
        return True
    if "tags" not in item:
        return True  # Refresh details before a local filtered download.
    raw = item.get("tags") or []
    names = {entry.get("tag", "").casefold() for entry in raw
             if isinstance(entry, dict) and isinstance(entry.get("tag"), str)}
    wanted = {tag.casefold() for tag in required}
    blocked = {tag.casefold() for tag in excluded}
    return ((not wanted or (wanted <= names if match_all else bool(wanted & names)))
            and not (blocked & names))


def _request_json(url, delay, data=None):
    for attempt in range(5):
        try:
            headers = {"User-Agent": "stormcopy/0.2"}
            if data is not None:
                headers["Content-Type"] = "application/x-www-form-urlencoded"
            req = urllib.request.Request(url, data=data, headers=headers)
            with urllib.request.urlopen(req, timeout=30) as response:
                # Some Workshop titles/descriptions contain invalid UTF-8.
                # Preserve the JSON structure and replace only those bytes.
                result = json.loads(response.read().decode("utf-8", "replace"))
            time.sleep(delay)
            return result
        except urllib.error.HTTPError as exc:
            if exc.code not in (429, 500, 502, 503, 504) or attempt == 4:
                raise RuntimeError(f"Steam API HTTP {exc.code}") from exc
            wait = min(120, max(delay, 2 ** attempt * 5))
            if exc.headers.get("Retry-After", "").isdigit():
                wait = min(120, max(wait, int(exc.headers["Retry-After"])))
            time.sleep(wait)
        except urllib.error.URLError as exc:
            if attempt == 4:
                raise RuntimeError(f"Steam API connection failed: {exc.reason}") from exc
            time.sleep(min(120, max(delay, 2 ** attempt * 5)))
    raise RuntimeError("Steam API retries exhausted")


def _discovery_suffix(tags, excluded_tags, match_all):
    """Keep separate cursors and refresh watermarks for each tag query."""
    if not tags and not excluded_tags:
        return ""
    signature = json.dumps({"tags": sorted(tags),
                            "exclude": sorted(excluded_tags),
                            "all": bool(match_all)}, sort_keys=True, separators=(",", ":"))
    return "_" + hashlib.sha256(signature.encode()).hexdigest()[:16]


def _put_state(db, key, value):
    """Write state inside the caller's transaction."""
    db.execute("INSERT INTO state VALUES (?,?) ON CONFLICT(key) "
               "DO UPDATE SET value=excluded.value", (key, str(value)))


def _query_files(api_key, delay, sort, cursor, tags, excluded_tags, match_all):
    payload = {"query_type": 1 if sort == "published" else 21,
               "appid": APP_ID, "filetype": 0, "cursor": cursor,
               "numperpage": 100, "return_tags": True,
               "return_short_description": True}
    if tags:
        payload["requiredtags"] = list(tags)
        payload["match_all_tags"] = bool(match_all)
    if excluded_tags:
        payload["excludedtags"] = list(excluded_tags)
    url = API_URL + "?" + urllib.parse.urlencode({"key": api_key,
                                                   "input_json": json.dumps(payload, separators=(",", ":"))})
    try:
        envelope = _request_json(url, delay)
    except RuntimeError as exc:
        if str(exc) == "Steam API HTTP 403":
            raise RuntimeError(
                "Steam denied Workshop discovery (HTTP 403). Check the Steam "
                "Web API key at https://steamcommunity.com/dev/apikey and "
                "replace the saved value with `py -3 -m stormcopy set-api-key "
                "--prompt`. A STEAM_API_KEY set in this Command Prompt "
                "overrides the saved value."
            ) from exc
        raise
    response = envelope.get("response")
    if not isinstance(response, dict) or response.get("result", 1) != 1:
        raise RuntimeError(f"Steam QueryFiles rejected the request: {response}")
    files = response.get("publishedfiledetails", [])
    if not isinstance(files, list):
        raise RuntimeError("Steam QueryFiles returned an invalid item list")
    return response, files


def discover(db, max_pages=1, delay=2.0, api_key=None, resume=True, sort="published",
             tags=(), excluded_tags=(), match_all=True, progress=None):
    ensure_download_columns(db)
    if max_pages < 0 or delay < 0:
        raise ValueError("pages and delay must be nonnegative")
    tags, excluded_tags = _check_tag_filters(tags, excluded_tags)
    api_key = get_api_key(api_key)
    if not api_key:
        raise ValueError("Set STEAM_API_KEY to use Workshop discovery")
    if sort not in ("published", "updated"):
        raise ValueError("sort must be published or updated")
    suffix = _discovery_suffix(tags, excluded_tags, match_all)
    # Earlier cursors were scoped to creations made by the game's account.
    # They cannot resume an all-creators traversal without skipping items.
    cursor_key = f"cursor_v{DISCOVERY_SCOPE_VERSION}_{sort}{suffix}"
    complete_key = f"discovery_complete_v{DISCOVERY_SCOPE_VERSION}_{sort}{suffix}"
    started_key = f"full_started_v1{suffix}"
    completed_key = f"full_completed_v1{suffix}"
    watermark_key = f"refresh_watermark_v1{suffix}"
    if resume and get_state(db, complete_key) == "1":
        return {"pages": 0, "items_seen": 0, "sort": sort, "complete": True,
                "tags": tags, "excluded_tags": excluded_tags}
    cursor = get_state(db, cursor_key, "*") if resume else "*"
    if not resume:
        with db:
            _put_state(db, cursor_key, "*")
            _put_state(db, complete_key, "0")
            if sort == "published":
                _put_state(db, started_key, int(time.time()))
                db.execute("DELETE FROM state WHERE key IN (?,?,?)",
                           (f"refresh_cursor_v1{suffix}",
                            f"refresh_started_v1{suffix}",
                            f"refresh_cutoff_v1{suffix}"))
    elif sort == "published" and cursor == "*" and get_state(db, started_key) is None:
        # Old partial crawls lack a trustworthy start time. Leave them without
        # a seed so their first later refresh scans the full updated listing.
        set_state(db, started_key, int(time.time()))
    total = pages = 0
    if progress is not None:
        progress({"phase": "Discover", "done": 0, "total": max_pages or None,
                  "detail": "contacting Steam Workshop"})
    while max_pages == 0 or pages < max_pages:
        response, files = _query_files(api_key, delay, sort, cursor, tags,
                                       excluded_tags, match_all)
        next_cursor = response.get("next_cursor")
        if next_cursor and next_cursor == cursor:
            raise RuntimeError("Steam returned the same discovery cursor twice; rerun to retry")
        complete = not next_cursor
        with db:
            for item in files:
                if not isinstance(item, dict) or item.get("result", 1) != 1 or not item.get("publishedfileid"):
                    continue
                if not _item_matches_tags(item, tags, excluded_tags, match_all):
                    continue
                upsert_item(db, item["publishedfileid"], item.get("title"),
                            item.get("time_updated"), _size(item.get("file_size")), commit=False)
                _save_item_tags(db, item)
                total += 1
            _put_state(db, cursor_key, "*" if complete else next_cursor)
            _put_state(db, complete_key, "1" if complete else "0")
            if complete and sort == "published":
                _put_state(db, completed_key, int(time.time()))
                started = get_state(db, started_key)
                if started is not None:
                    _put_state(db, watermark_key, started)
        pages += 1
        if progress is not None:
            # A resumed cursor starts its page count at zero. Steam's reported
            # overall result count cannot be used as this run's progress total.
            progress({"phase": "Discover", "done": pages,
                      "total": max_pages or None,
                      "detail": f"{total:,} matching items found"})
        if complete:
            break
        cursor = next_cursor
    set_state(db, "last_discovery", str(int(time.time())))
    return {"pages": pages, "items_seen": total, "sort": sort,
            "complete": get_state(db, complete_key) == "1",
            "tags": tags, "excluded_tags": excluded_tags}


def refresh_recent(db, max_pages=1, delay=2.0, api_key=None, tags=(),
                   excluded_tags=(), match_all=True, progress=None,
                   overlap_seconds=REFRESH_OVERLAP_SECONDS):
    """Resume one last-updated sweep; advance its watermark only on completion."""
    ensure_download_columns(db)
    if max_pages < 0 or delay < 0 or overlap_seconds < 0:
        raise ValueError("pages, delay, and overlap must be nonnegative")
    tags, excluded_tags = _check_tag_filters(tags, excluded_tags)
    api_key = get_api_key(api_key)
    if not api_key:
        raise ValueError("Set STEAM_API_KEY to use Workshop discovery")
    suffix = _discovery_suffix(tags, excluded_tags, match_all)
    cursor_key = f"refresh_cursor_v1{suffix}"
    started_key = f"refresh_started_v1{suffix}"
    cutoff_key = f"refresh_cutoff_v1{suffix}"
    watermark_key = f"refresh_watermark_v1{suffix}"
    completed_key = f"refresh_completed_v1{suffix}"
    started = get_state(db, started_key)
    if started is None:
        # The pass start, rather than the newest item observed, is the next
        # watermark: updates arriving while pages are read remain eligible on
        # the following pass.
        started = int(time.time())
        previous = int(get_state(db, watermark_key, "0"))
        cutoff = max(0, previous - overlap_seconds)
        cursor = "*"
        with db:
            _put_state(db, started_key, started)
            _put_state(db, cutoff_key, cutoff)
            _put_state(db, cursor_key, cursor)
    else:
        started = int(started)
        cutoff = int(get_state(db, cutoff_key, "0"))
        cursor = get_state(db, cursor_key, "*")

    pages = total = 0
    complete = False
    if progress is not None:
        progress({"phase": "Refresh", "done": 0, "total": max_pages or None,
                  "detail": "checking recently updated Workshop items"})
    while max_pages == 0 or pages < max_pages:
        response, files = _query_files(api_key, delay, "updated", cursor,
                                       tags, excluded_tags, match_all)
        next_cursor = response.get("next_cursor")
        if next_cursor and next_cursor == cursor:
            raise RuntimeError("Steam returned the same refresh cursor twice; rerun to retry")
        # Query type 21 is newest first. Cross the cutoff strictly so all
        # items sharing its timestamp are included; unknown times require a
        # deeper walk rather than an unsafe early stop.
        timestamps = []
        for item in files:
            if not isinstance(item, dict):
                timestamps.append(None)
                continue
            try:
                timestamp = int(item.get("time_updated"))
            except (TypeError, ValueError):
                timestamp = 0
            timestamps.append(timestamp if timestamp > 0 else None)
        past_cutoff = bool(cutoff and timestamps and
                           all(timestamp is not None for timestamp in timestamps) and
                           any(timestamp < cutoff for timestamp in timestamps))
        complete = past_cutoff or not next_cursor
        with db:
            for item in files:
                if (not isinstance(item, dict) or item.get("result", 1) != 1 or
                        not item.get("publishedfileid")):
                    continue
                if not _item_matches_tags(item, tags, excluded_tags, match_all):
                    continue
                upsert_item(db, item["publishedfileid"], item.get("title"),
                            item.get("time_updated"), _size(item.get("file_size")), commit=False)
                _save_item_tags(db, item)
                total += 1
            if complete:
                _put_state(db, watermark_key, started)
                _put_state(db, completed_key, int(time.time()))
                db.execute("DELETE FROM state WHERE key IN (?,?,?)",
                           (cursor_key, started_key, cutoff_key))
            else:
                _put_state(db, cursor_key, next_cursor)
        pages += 1
        if progress is not None:
            progress({"phase": "Refresh", "done": pages,
                      "total": max_pages or None,
                      "detail": f"{total:,} items checked this pass"})
        if complete:
            break
        cursor = next_cursor
    set_state(db, "last_discovery", str(int(time.time())))
    return {"pages": pages, "items_seen": total, "sort": "updated",
            "mode": "refresh", "complete": complete,
            "catalog_complete": get_state(
                db, f"discovery_complete_v{DISCOVERY_SCOPE_VERSION}_published{suffix}") == "1",
            "tags": tags, "excluded_tags": excluded_tags}


def discover_catalog(db, max_pages=1, delay=2.0, api_key=None, restart=False,
                     tags=(), excluded_tags=(), match_all=True, progress=None,
                     refresh_interval_seconds=0, force_full_scan=False):
    """Finish the full crawl, then perform overlapping recent-update sweeps."""
    if max_pages < 0 or delay < 0 or refresh_interval_seconds < 0:
        raise ValueError("pages, delay, and refresh interval must be nonnegative")
    tags, excluded_tags = _check_tag_filters(tags, excluded_tags)
    api_key = get_api_key(api_key)
    if not api_key:
        raise ValueError("Set STEAM_API_KEY to use Workshop discovery")
    ensure_download_columns(db)
    suffix = _discovery_suffix(tags, excluded_tags, match_all)
    full_complete_key = f"discovery_complete_v{DISCOVERY_SCOPE_VERSION}_published{suffix}"
    full_completed_key = f"full_completed_v1{suffix}"
    refresh_started_key = f"refresh_started_v1{suffix}"
    refresh_completed_key = f"refresh_completed_v1{suffix}"
    refresh_watermark_key = f"refresh_watermark_v1{suffix}"
    full_complete = get_state(db, full_complete_key) == "1"
    # A user-requested full pass restarts only a completed catalog. An
    # interrupted pass keeps its saved cursor and continues where it stopped.
    if force_full_scan and full_complete:
        restart = True
    if full_complete and get_state(db, full_completed_key) is None:
        # Existing databases know their crawl finished but not when. Begin a
        # fresh 30-day schedule; no refresh watermark means one full updated
        # sweep before the bounded overlapping refreshes can be trusted.
        set_state(db, full_completed_key, int(time.time()))
    last_full = int(get_state(db, full_completed_key, "0"))
    refresh_active = get_state(db, refresh_started_key) is not None
    due_full = full_complete and not refresh_active and (
        int(time.time()) - last_full >= FULL_RESCAN_SECONDS)
    if restart or not full_complete or due_full:
        result = discover(db, max_pages=max_pages, delay=delay, api_key=api_key,
                          resume=not (restart or due_full), sort="published",
                          tags=tags, excluded_tags=excluded_tags,
                          match_all=match_all, progress=progress)
        return {**result, "mode": "full", "catalog_complete": result["complete"]}
    last_refresh = int(get_state(db, refresh_completed_key, "0"))
    refresh_age = int(time.time()) - last_refresh
    if (not refresh_active and refresh_interval_seconds and last_refresh and
            get_state(db, refresh_watermark_key) is not None and
            0 <= refresh_age < refresh_interval_seconds):
        return {"pages": 0, "items_seen": 0, "sort": "updated",
                "mode": "refresh", "complete": True, "catalog_complete": True,
                "skipped_recently": True, "tags": tags,
                "excluded_tags": excluded_tags}
    return refresh_recent(db, max_pages=max_pages, delay=delay, api_key=api_key,
                          tags=tags, excluded_tags=excluded_tags,
                          match_all=match_all, progress=progress)


def refresh_tags(db, max_items=0, delay=0.5, force=False, progress=None):
    """Load tags for known Workshop IDs through public, batched details calls."""
    ensure_download_columns(db)
    if max_items < 0 or delay < 0:
        raise ValueError("max-items and delay must be nonnegative")
    sql = ("SELECT id FROM items WHERE id != '' AND id NOT GLOB '*[^0-9]*' ")
    params = ()
    if not force:
        sql += ("AND ((tag_checked=0 AND tag_attempted < ?) "
                "OR (tag_checked>0 AND tag_checked < ?)) ")
        now = int(time.time())
        params = (now - 86400, now - 7 * 86400)
    sql += "ORDER BY id"
    rows = db.execute(sql + (" LIMIT ?" if max_items else ""),
                      params + ((max_items,) if max_items else ())).fetchall()
    checked = tagged = 0
    if progress is not None:
        progress({"phase": "Workshop tags", "done": 0, "total": len(rows),
                  "detail": "checking public item details"})
    for start in range(0, len(rows), 50):
        ids = [row["id"] for row in rows[start:start + 50]]
        params = {"itemcount": len(ids)}
        params.update({f"publishedfileids[{n}]": item_id for n, item_id in enumerate(ids)})
        envelope = _request_json(DETAILS_URL, delay, urllib.parse.urlencode(params).encode())
        details = envelope.get("response", {}).get("publishedfiledetails", [])
        with db:
            # A missing or inaccessible response must not count as verified tags.
            checked_at = int(time.time())
            db.executemany("UPDATE items SET tag_checked=0,tag_attempted=? WHERE id=?",
                           ((checked_at, item_id) for item_id in ids))
            for item in details:
                if item.get("result", 1) != 1 or not item.get("publishedfileid"):
                    continue
                app = item.get("consumer_app_id", item.get("consumer_appid"))
                if app is not None and str(app) != str(APP_ID):
                    continue
                upsert_item(db, item["publishedfileid"], item.get("title"),
                            item.get("time_updated"), _size(item.get("file_size")), commit=False)
                _save_item_tags(db, item)
                tagged += bool(item.get("tags"))
        checked += len(ids)
        if progress is not None:
            progress({"phase": "Workshop tags", "done": checked, "total": len(rows),
                      "detail": f"{tagged:,} tagged items"})
    return {"checked": checked, "tagged_items": tagged,
            "known_tagged_items": db.execute(
                "SELECT COUNT(DISTINCT item_id) FROM item_tags").fetchone()[0]}


def refresh_sizes(db, max_items=0, delay=0.5, pending_only=False, force=False,
                  tags=(), excluded_tags=(), match_all=True, progress=None):
    """Fetch size metadata for known IDs; no API key is needed for public items."""
    ensure_download_columns(db)
    if max_items < 0 or delay < 0:
        raise ValueError("max-items and delay must be nonnegative")
    sql = ("SELECT id FROM items WHERE id != '' AND id NOT GLOB '*[^0-9]*' "
           "AND size_bytes IS NULL ")
    params = ()
    if not force:
        sql += "AND (size_checked=0 OR size_checked < ?) "
        params = (int(time.time()) - 86400,)
    if pending_only:
        sql += "AND (downloaded=0 OR updated>downloaded) "
    filter_sql, filter_params = _tag_clause(tags, excluded_tags, match_all)
    sql += filter_sql + " "
    params += filter_params
    sql += "ORDER BY id"
    rows = db.execute(sql + (" LIMIT ?" if max_items else ""),
                      params + ((max_items,) if max_items else ())).fetchall()
    added = 0
    if progress is not None:
        progress({"phase": "File sizes", "done": 0, "total": len(rows),
                  "detail": "checking public item details"})
    for start in range(0, len(rows), 50):
        ids = [row["id"] for row in rows[start:start + 50]]
        params = {"itemcount": len(ids)}
        params.update({f"publishedfileids[{n}]": item_id for n, item_id in enumerate(ids)})
        envelope = _request_json(DETAILS_URL, delay, urllib.parse.urlencode(params).encode())
        details = envelope.get("response", {}).get("publishedfiledetails", [])
        with db:
            checked_at = int(time.time())
            db.executemany("UPDATE items SET size_checked=? WHERE id=?",
                           ((checked_at, item_id) for item_id in ids))
            for item in details:
                if item.get("result", 1) != 1 or not item.get("publishedfileid"):
                    continue
                app = item.get("consumer_app_id", item.get("consumer_appid"))
                if app is not None and str(app) != str(APP_ID):
                    continue
                size = _size(item.get("file_size"))
                upsert_item(db, item["publishedfileid"], item.get("title"),
                            item.get("time_updated"), size, commit=False)
                _save_item_tags(db, item)
                added += size is not None
        if progress is not None:
            progress({"phase": "File sizes", "done": min(start + 50, len(rows)),
                      "total": len(rows), "detail": f"{added:,} sizes found"})
    unknown_sql = ("SELECT COUNT(*) FROM items WHERE id != '' "
                   "AND id NOT GLOB '*[^0-9]*' AND size_bytes IS NULL " + filter_sql)
    unknown = db.execute(unknown_sql, filter_params).fetchone()[0]
    return {"checked": len(rows), "sizes_added": added, "unknown_sizes": unknown}


def _index_cached_item(db, folder, item_id, title):
    usable = 0
    valid_paths = set()
    for xml in folder.rglob("*.xml"):
        if xml.is_symlink():
            continue
        try:
            index_file(db, xml, item_id, title)
            usable += 1
            valid_paths.add(str(xml.resolve()))
        except ValueError:
            pass  # Addons can contain microcontrollers and unrelated XML.
    for old in db.execute("SELECT path FROM files WHERE item_id=?", (item_id,)).fetchall():
        if old["path"] not in valid_paths:
            db.execute("DELETE FROM features WHERE item_id=? AND path=?", (item_id, old["path"]))
            search_index.delete(db, item_id, old["path"])
            db.execute("DELETE FROM files WHERE item_id=? AND path=?", (item_id, old["path"]))
    return usable


@contextmanager
def _cache_lock(cache):
    """Keep two downloader invocations from changing one cache at once."""
    with (cache / ".stormcopy-download.lock").open("a+b") as handle:
        handle.seek(0, os.SEEK_END)
        if handle.tell() == 0:
            handle.write(b"\0")
            handle.flush()
        handle.seek(0)
        try:
            if os.name == "nt":
                import msvcrt
                msvcrt.locking(handle.fileno(), msvcrt.LK_NBLCK, 1)
            else:
                import fcntl
                fcntl.flock(handle.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
        except OSError as exc:
            raise RuntimeError(f"Workshop cache is already in use: {cache}") from exc
        try:
            yield
        finally:
            handle.seek(0)
            if os.name == "nt":
                msvcrt.locking(handle.fileno(), msvcrt.LK_UNLCK, 1)
            else:
                fcntl.flock(handle.fileno(), fcntl.LOCK_UN)


def _worker_homes(cache, steamcmd, workers, progress=None):
    """Give concurrent SteamCMD processes separate binaries and manifests."""
    source = Path(steamcmd)
    if not source.is_file():
        found = shutil.which(steamcmd)
        source = Path(found) if found else source
    if not source.is_file():
        raise ValueError(f"SteamCMD not found: {steamcmd}. Install it or pass --steamcmd PATH")
    source = source.resolve()
    if source.parent != cache:
        raise ValueError("Parallel downloads need SteamCMD installed inside --cache. "
                         "Run setup-steamcmd --cache PATH, or use --workers 1")
    root = cache / ".stormcopy-workers"
    root.mkdir(exist_ok=True)
    homes = []
    if progress is not None:
        progress({"phase": "Prepare workers", "done": 0, "total": workers,
                  "detail": "setting up separate SteamCMD process folders"})
    for number in range(1, workers + 1):
        home = root / f"worker-{number}"
        home.mkdir(exist_ok=True)
        marker = home / ".stormcopy-source-version"
        version = f"{source.stat().st_size}:{source.stat().st_mtime_ns}"
        if not (home / source.name).is_file() or not marker.is_file() or marker.read_text() != version:
            for path in cache.iterdir():
                if path.is_file() and (path.suffix.lower() in (".exe", ".dll", ".so", ".sh", ".vdf")
                                       or path.name in ("steamcmd",)):
                    shutil.copy2(path, home / path.name)
                elif path.is_dir() and path.name in (
                        "appcache", "bin", "config", "package", "public", "siteserverui",
                        "userdata", "linux32", "linux64"):
                    shutil.copytree(path, home / path.name, dirs_exist_ok=True)
            marker.write_text(version)
        elif (cache / "config").is_dir():
            # SteamCMD login state can change without updating steamcmd.exe.
            shutil.copytree(cache / "config", home / "config", dirs_exist_ok=True)
        homes.append((home, str(home / source.name)))
        if progress is not None:
            progress({"phase": "Prepare workers", "done": number, "total": workers,
                      "detail": f"{number:,} SteamCMD process folders ready"})
    return homes


def _run_batch(command, home, count):
    try:
        result = subprocess.run(command, capture_output=True, text=True, errors="replace",
                                timeout=max(300, 180 * count), check=False, cwd=home)
        return result, result.stdout + "\n" + result.stderr
    except FileNotFoundError as exc:
        raise ValueError(f"SteamCMD not found: {command[0]}. Install it or pass --steamcmd PATH") from exc
    except (OSError, subprocess.TimeoutExpired) as exc:
        return None, str(exc)


def _workshop_destination(cache, workshop_folder):
    """Resolve the item-content root without creating a mistaken Steam path."""
    if workshop_folder is None:
        return cache / "steamapps" / "workshop" / "content" / str(APP_ID)
    requested = Path(workshop_folder).expanduser()
    if not requested.is_dir():
        raise ValueError(f"Existing Stormworks Workshop folder not found: {requested}")
    destination = requested.resolve()
    if (destination.name != str(APP_ID) or destination.parent.name.lower() != "content"
            or destination.parent.parent.name.lower() != "workshop"):
        raise ValueError("Workshop folder must be the existing "
                         "steamapps/workshop/content/573090 directory")
    return destination


def _remove_tree_within(root, path):
    """Remove only a real item or staging directory below its expected root."""
    root = Path(root).resolve()
    path = Path(path)
    if path.is_symlink() or (hasattr(path, "is_junction") and path.is_junction()):
        raise RuntimeError(f"Refusing to remove linked Workshop directory: {path}")
    resolved = path.resolve()
    if resolved == root or not resolved.is_relative_to(root):
        raise RuntimeError(f"Refusing to remove a Workshop directory outside {root}: {path}")
    shutil.rmtree(path)


def _promote_worker_item(worker_home, destination_root, item_id):
    """Stage a finished item in the destination volume, then replace atomically."""
    source = worker_home / "steamapps" / "workshop" / "content" / str(APP_ID) / item_id
    destination = destination_root / item_id
    if not source.is_dir():
        raise RuntimeError(f"SteamCMD reported success but item {item_id} has no files")
    if source.is_symlink() or (hasattr(source, "is_junction") and source.is_junction()):
        raise RuntimeError(f"Workshop source for item {item_id} is a link")
    if destination.is_symlink() or (hasattr(destination, "is_junction") and destination.is_junction()):
        raise RuntimeError(f"Workshop destination for item {item_id} is a link")
    destination.parent.mkdir(parents=True, exist_ok=True)
    stage = destination_root / f".stormcopy-stage-{item_id}-{uuid4().hex}"
    copied = False
    try:
        source.rename(stage)
    except OSError as exc:
        if exc.errno != errno.EXDEV and getattr(exc, "winerror", None) != 17:
            raise
        try:
            shutil.copytree(source, stage, symlinks=True)
        except OSError:
            if stage.exists():
                _remove_tree_within(destination_root, stage)
            raise
        copied = True
    backup = None
    try:
        if destination.exists():
            backup = destination.parent / f".stormcopy-backup-{item_id}-{uuid4().hex}"
            destination.rename(backup)
        stage.rename(destination)
    except OSError:
        if backup is not None and not destination.exists():
            backup.rename(destination)
        if stage.exists():
            if copied:
                _remove_tree_within(destination_root, stage)
            else:
                stage.rename(source)
        raise
    return destination, backup, copied


def _remove_backup(cache, backup, warn=None):
    if backup is not None:
        try:
            _remove_tree_within(cache, backup)
        except (OSError, RuntimeError) as exc:
            message = f"Could not remove old cache backup {backup}: {exc}"
            if warn is not None:
                warn(message)
            else:
                print(message, file=sys.stderr, flush=True)


def _rollback_promoted_item(home, destination_root, item_id, destination, backup, copied):
    source = home / "steamapps" / "workshop" / "content" / str(APP_ID) / item_id
    if destination.exists():
        if copied:
            _remove_tree_within(destination_root, destination)
        else:
            destination.rename(source)
    if backup is not None and backup.exists():
        backup.rename(destination)
    return backup is not None and destination.is_dir()


def download_pending(db, cache, steamcmd="steamcmd", max_items=50, delay=0.5,
                     login="anonymous", batch_size=10, force=False, item_id=None,
                     cache_only=False, workers=1, tags=(), excluded_tags=(),
                     match_all=True, progress=None, workshop_folder=None,
                     skip_attempted=False):
    if (max_items < 0 or delay < 0 or batch_size < 1 or batch_size > 50 or
            not 1 <= workers <= MAX_DOWNLOAD_WORKERS):
        raise ValueError("max-items and delay must be nonnegative; batch-size 1-50; "
                         f"workers 1-{MAX_DOWNLOAD_WORKERS}")
    if skip_attempted and max_items == 0:
        raise ValueError("skip_attempted requires a positive max-items chunk size")
    cache = Path(cache).resolve()
    cache.mkdir(parents=True, exist_ok=True)
    destination_root = _workshop_destination(cache, workshop_folder)
    if steamcmd == "steamcmd" and (cache / "steamcmd.exe").is_file():
        steamcmd = str(cache / "steamcmd.exe")
    executable = Path(steamcmd).expanduser()
    if executable.is_file():
        steamcmd = str(executable.resolve())
    if not login.replace("_", "").isalnum():
        raise ValueError("Steam login must be a plain account name or anonymous")
    if item_id is not None and not str(item_id).isdigit():
        raise ValueError("item-id must be numeric")
    tags, excluded_tags = _check_tag_filters(tags, excluded_tags)
    with _cache_lock(cache):
        # The destination lock also protects runs using different SteamCMD caches.
        destination_lock = (_cache_lock(destination_root) if workshop_folder is not None
                            else nullcontext())
        with destination_lock:
            ensure_download_columns(db)
            if skip_attempted:
                db.execute("CREATE TEMP TABLE IF NOT EXISTS _stormcopy_bulk_attempted "
                           "(id TEXT PRIMARY KEY)")
            return _download_pending_locked(db, cache, steamcmd, max_items, delay,
                                            login, batch_size, force, item_id, cache_only, workers,
                                            tags, excluded_tags, match_all, progress,
                                            destination_root, skip_attempted)


def _download_pending_locked(db, cache, steamcmd, max_items, delay, login, batch_size,
                             force, item_id, cache_only, workers, tags, excluded_tags,
                             match_all, progress, destination_root, skip_attempted):
    started = time.monotonic()
    now = int(time.time())
    sql = ("SELECT id,title,updated,downloaded,size_bytes,download_attempts "
           "FROM items WHERE id != '' AND id NOT GLOB '*[^0-9]*' ")
    params = ()
    if item_id is not None:
        sql += "AND id=? "
        params = (str(item_id),)
    if not force:
        sql += "AND retry_after <= ? AND (downloaded=0 OR updated>downloaded) "
        params += (now,)
    if skip_attempted:
        sql += ("AND NOT EXISTS (SELECT 1 FROM temp._stormcopy_bulk_attempted a "
                "WHERE a.id=items.id) ")
    filter_sql, filter_params = _tag_clause(tags, excluded_tags, match_all)
    sql += filter_sql + " "
    params += filter_params
    sql += "ORDER BY (size_bytes IS NULL),size_bytes ASC,id ASC"
    if max_items:
        sql += " LIMIT ?"
    rows = db.execute(sql, params + ((max_items,) if max_items else ())).fetchall()
    done = failed = no_vehicle = unindexed = 0
    rate_limited = False
    throttle_until = 0.0
    known_sizes = sum(row["size_bytes"] is not None for row in rows)
    requested_workers = workers
    # Small queues split into ten-item batches may not occupy every requested
    # process. Smaller batches expose enough work to the isolated workers.
    effective_batch_size = (min(batch_size, max(1, len(rows) // workers))
                            if workers > 1 and rows else batch_size)
    batches = [(start, rows[start:start + effective_batch_size])
               for start in range(0, len(rows), effective_batch_size)]
    workers = min(workers, len(batches)) if batches else 0
    homes = (_worker_homes(cache, steamcmd, workers, progress) if workers > 1
             else [(cache, steamcmd)])
    active = {}
    if progress is not None:
        progress({"phase": "Download", "done": 0, "total": len(rows),
                  "detail": "smallest reported files first"})

    def command_for(batch, home, executable):
        command = [executable, "+force_install_dir", str(home), "+login", login]
        for row in batch:
            command += ["+workshop_download_item", str(APP_ID), row["id"]]
        return command + ["+quit"]

    def record_batch(start, batch, home, result, output):
        nonlocal done, failed, no_vehicle, unindexed, rate_limited, throttle_until

        def warn(message):
            if progress is not None:
                progress({"phase": "Download", "done": done + failed,
                          "total": len(rows), "warning": message})
            else:
                print(message, file=sys.stderr, flush=True)

        bad_ids = set(re.findall(r"Download item\s+(\d+)\s+failed", output, re.I))
        good_ids = set(re.findall(r"Success\.\s+Downloaded item\s+(\d+)", output, re.I))
        for row in batch:
            item_id = row["id"]
            folder = home / "steamapps" / "workshop" / "content" / str(APP_ID) / item_id
            updated_self = (result is not None and result.returncode == 7 and
                            "Update complete, launching" in output)
            success = (result is not None and folder.is_dir() and item_id not in bad_ids
                       and item_id in good_ids)
            backup = None
            copied = promoted = False
            try:
                if not success:
                    detail = ("SteamCMD updated itself; rerun after its child process exits. "
                              if updated_self else "")
                    raise RuntimeError(f"{detail}SteamCMD could not cache item {item_id}: {output[-350:]}")
                if folder.parent != destination_root:
                    folder, backup, copied = _promote_worker_item(home, destination_root, item_id)
                    promoted = True
                usable = 0 if cache_only else _index_cached_item(db, folder, item_id, row["title"])
                if cache_only:
                    # Previous fingerprints may describe an older version of this item.
                    db.execute("DELETE FROM features WHERE item_id=?", (item_id,))
                    for indexed in db.execute("SELECT path FROM search_files WHERE item_id=?",
                                              (item_id,)).fetchall():
                        search_index.delete(db, item_id, indexed["path"])
                    db.execute("DELETE FROM files WHERE item_id=?", (item_id,))
                db.execute("UPDATE items SET downloaded=?,download_attempts=0,retry_after=0,"
                           "error=?,indexed=? WHERE id=?",
                           (max(row["updated"], int(time.time())),
                            None if usable or cache_only else "Cached item has no vehicle XML",
                            int(usable > 0), item_id))
                db.commit()
                _remove_backup(destination_root, backup, warn)
                if copied:
                    original = home / "steamapps" / "workshop" / "content" / str(APP_ID) / item_id
                    try:
                        _remove_tree_within(cache, original)
                    except (OSError, RuntimeError) as exc:
                        warn(f"Could not remove downloaded staging copy for {item_id}: {exc}")
                done += 1
                no_vehicle += usable == 0 and not cache_only
                unindexed += cache_only
            except (OSError, RuntimeError) as exc:
                if promoted:
                    try:
                        restored = _rollback_promoted_item(
                            home, destination_root, item_id, folder, backup, copied)
                    except (OSError, RuntimeError) as restore_error:
                        warn(f"Could not restore previous cache for {item_id}: {restore_error}")
                    else:
                        if restored and not cache_only:
                            try:
                                _index_cached_item(db, folder, item_id, row["title"])
                            except OSError as restore_error:
                                warn(f"Restored cache for {item_id}, but could not restore its index: "
                                     f"{restore_error}")
                attempts = (row["download_attempts"] or 0) + 1
                retry_after = int(time.time()) + min(3600, 30 * 2 ** min(attempts - 1, 7))
                db.execute("UPDATE items SET error=?,download_attempts=?,retry_after=? WHERE id=?",
                           (str(exc)[:500], attempts, retry_after, item_id))
                db.commit()
                failed += 1
        if progress is not None:
            progress({"phase": "Download", "done": done + failed, "total": len(rows),
                      "active": len(active),
                      "cached_count": done, "failed_count": failed,
                      "worker_count": workers,
                      "detail": f"{done:,} cached, {failed:,} failed; {workers} process(es)"})
        throttled = _steamcmd_rate_limited(output)
        rate_limited = rate_limited or throttled
        if throttled:
            throttle_until = max(throttle_until, time.monotonic() + 30.0)
        return throttled

    if batches and workers == 1:
        for batch_number, (start, batch) in enumerate(batches):
            home, executable = homes[0]
            if progress is None:
                print(f"Downloading {start + 1}-{start + len(batch)}/{len(rows)} "
                      f"(smallest reported files first)", file=sys.stderr, flush=True)
            result, output = _run_batch(command_for(batch, home, executable), home, len(batch))
            record_batch(start, batch, home, result, output)
            if batch_number + 1 < len(batches):
                wait_for = max(delay, throttle_until - time.monotonic())
                if wait_for > 0:
                    time.sleep(wait_for)
    elif batches:
        next_batch = 0
        next_launch = 0.0
        allowed_workers = workers
        def launch(pool, number):
            nonlocal next_batch, next_launch
            start, batch = batches[next_batch]
            next_batch += 1
            home, executable = homes[number]
            wait_for = max(next_launch, throttle_until) - time.monotonic()
            if wait_for > 0:
                time.sleep(wait_for)
            if progress is None:
                print(f"Downloading {start + 1}-{start + len(batch)}/{len(rows)} "
                      f"(worker {number + 1}/{workers}; smallest reported files first)",
                      file=sys.stderr, flush=True)
            future = pool.submit(_run_batch, command_for(batch, home, executable), home, len(batch))
            active[future] = (number, start, batch, home)
            next_launch = time.monotonic() + delay
            if progress is not None:
                progress({"phase": "Download", "done": done + failed,
                          "total": len(rows), "active": len(active),
                          "detail": f"{workers} SteamCMD process slots"})

        with ThreadPoolExecutor(max_workers=workers) as pool:
            for number in range(workers):
                launch(pool, number)
            while active:
                completed, _ = wait(active, return_when=FIRST_COMPLETED)
                for future in completed:
                    number, start, batch, home = active.pop(future)
                    result, output = future.result()
                    if record_batch(start, batch, home, result, output):
                        allowed_workers = max(1, allowed_workers // 2)
                        warning = ("Steam reported a rate limit; pausing new batches and "
                                   f"reducing to {allowed_workers} worker(s)")
                        if progress is not None:
                            progress({"phase": "Download", "done": done + failed,
                                      "total": len(rows), "warning": warning})
                        else:
                            print(warning, file=sys.stderr, flush=True)
                    if next_batch < len(batches) and len(active) < allowed_workers:
                        launch(pool, number)
    result = {"selected": len(rows), "cached": done, "cached_without_vehicle": no_vehicle,
            "cached_unindexed": unindexed, "downloaded_and_indexed": done - no_vehicle - unindexed,
            "failed": failed, "reported_sizes_known": known_sizes,
            "reported_sizes_unknown": len(rows) - known_sizes,
            "batches": len(batches), "workers_used": workers,
            "workers_requested": requested_workers,
            "batch_size_used": effective_batch_size,
            "rate_limited": rate_limited,
            "cooldown_remaining_seconds": max(0.0, throttle_until - time.monotonic()),
            "elapsed_seconds": round(time.monotonic() - started, 1),
            "tags": tags, "excluded_tags": excluded_tags,
            "workshop_folder": str(destination_root)}
    if skip_attempted:
        result["selected_ids"] = [row["id"] for row in rows]
    return result
