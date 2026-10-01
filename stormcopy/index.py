"""SQLite inverted index and overlap scoring."""

from collections import Counter
import json
import math
from pathlib import Path
import re
import sqlite3
import sys
import threading
import time

from .fingerprint import fingerprint_file
from . import search_index
from .minhash import (bands as minhash_bands, estimate as minhash_estimate,
                      signature as minhash_signature)


def connect(db_path):
    path = Path(db_path)
    path.parent.mkdir(parents=True, exist_ok=True)
    db = sqlite3.connect(path)
    db.row_factory = sqlite3.Row
    db.execute("PRAGMA busy_timeout=30000")
    db.execute("PRAGMA journal_mode=WAL")
    db.executescript("""
    CREATE TABLE IF NOT EXISTS items (
      id TEXT PRIMARY KEY, title TEXT, updated INTEGER DEFAULT 0,
      downloaded INTEGER DEFAULT 0, indexed INTEGER DEFAULT 0, error TEXT,
      size_bytes INTEGER, download_attempts INTEGER DEFAULT 0,
      retry_after INTEGER DEFAULT 0, size_checked INTEGER DEFAULT 0,
      tag_checked INTEGER DEFAULT 0, tag_attempted INTEGER DEFAULT 0);
    CREATE TABLE IF NOT EXISTS item_tags (
      item_id TEXT NOT NULL, tag TEXT NOT NULL, tag_key TEXT NOT NULL,
      PRIMARY KEY(item_id,tag_key));
    CREATE INDEX IF NOT EXISTS item_tags_lookup ON item_tags(tag_key,item_id);
    CREATE TABLE IF NOT EXISTS files (
      item_id TEXT NOT NULL, path TEXT NOT NULL, mtime_ns INTEGER NOT NULL,
      size INTEGER NOT NULL, components INTEGER NOT NULL,
      PRIMARY KEY(item_id,path));
    CREATE TABLE IF NOT EXISTS features (
      item_id TEXT NOT NULL, path TEXT NOT NULL, hash TEXT NOT NULL,
      count INTEGER NOT NULL, sample TEXT NOT NULL,
      PRIMARY KEY(item_id,path,hash));
    CREATE INDEX IF NOT EXISTS features_hash ON features(hash);
    CREATE TABLE IF NOT EXISTS state (key TEXT PRIMARY KEY, value TEXT NOT NULL);
    """)
    search_index.ensure_tables(db)
    return db


def ensure_download_columns(db):
    """Add download metadata to indexes made by earlier releases."""
    columns = {row["name"] for row in db.execute("PRAGMA table_info(items)")}
    definitions = {"size_bytes": "INTEGER", "download_attempts": "INTEGER DEFAULT 0",
                   "retry_after": "INTEGER DEFAULT 0", "size_checked": "INTEGER DEFAULT 0",
                   "tag_checked": "INTEGER DEFAULT 0",
                   "tag_attempted": "INTEGER DEFAULT 0"}
    for name, definition in definitions.items():
        if name not in columns:
            db.execute(f"ALTER TABLE items ADD COLUMN {name} {definition}")
    db.execute("CREATE TABLE IF NOT EXISTS item_tags (item_id TEXT NOT NULL,tag TEXT NOT NULL,"
               "tag_key TEXT NOT NULL,PRIMARY KEY(item_id,tag_key))")
    db.execute("CREATE INDEX IF NOT EXISTS item_tags_lookup ON item_tags(tag_key,item_id)")
    db.execute("CREATE INDEX IF NOT EXISTS items_download_size_order "
               "ON items((size_bytes IS NULL),size_bytes,id)")
    db.commit()


def set_state(db, key, value):
    db.execute("INSERT INTO state VALUES (?,?) ON CONFLICT(key) DO UPDATE SET value=excluded.value",
               (key, value))
    db.commit()


def get_state(db, key, default=None):
    row = db.execute("SELECT value FROM state WHERE key=?", (key,)).fetchone()
    return row[0] if row else default


def upsert_item(db, item_id, title=None, updated=None, size_bytes=None, commit=True):
    db.execute("INSERT INTO items(id,title,updated,size_bytes) VALUES (?,?,?,?) "
               "ON CONFLICT(id) DO UPDATE SET "
               "title=COALESCE(excluded.title,items.title),"
               "updated=MAX(items.updated,excluded.updated),"
               "size_bytes=COALESCE(excluded.size_bytes,items.size_bytes),"
               "tag_checked=CASE WHEN excluded.updated>items.updated THEN 0 "
               "ELSE items.tag_checked END,"
               "tag_attempted=CASE WHEN excluded.updated>items.updated THEN 0 "
               "ELSE items.tag_attempted END",
               (str(item_id), title, updated or 0, size_bytes))
    if commit:
        db.commit()


def index_file(db, path, item_id, title=None, commit=True):
    path = Path(path).resolve()
    stat = path.stat()
    item_id = str(item_id)
    old = db.execute("SELECT mtime_ns,size FROM files WHERE item_id=? AND path=?",
                     (item_id, str(path))).fetchone()
    modern = db.execute("SELECT 1 FROM search_files WHERE item_id=? AND path=? "
                        "AND version=?",
                        (item_id, str(path), search_index.SEARCH_INDEX_VERSION)).fetchone()
    if old and tuple(old) == (stat.st_mtime_ns, stat.st_size) and modern:
        return False
    fp = fingerprint_file(path)
    def write_index():
        db.execute("INSERT INTO items(id,title) VALUES (?,?) ON CONFLICT(id) DO UPDATE SET "
                   "title=COALESCE(excluded.title,items.title)", (item_id, title))
        db.execute("DELETE FROM features WHERE item_id=? AND path=?", (item_id, str(path)))
        db.execute("DELETE FROM files WHERE item_id=? AND path=?", (item_id, str(path)))
        db.execute("INSERT INTO files VALUES (?,?,?,?,?)",
                   (item_id, str(path), stat.st_mtime_ns, stat.st_size, fp["components"]))
        search_index.save(db, item_id, str(path), fp)
        db.execute("UPDATE items SET indexed=1,error=NULL WHERE id=?", (item_id,))
    if commit:
        with db:
            write_index()
    else:
        write_index()
    return True


def index_directory(db, root, progress=None):
    root = Path(root).resolve()
    if not root.is_dir():
        raise ValueError(f"No such directory: {root}")
    if progress is not None:
        progress({"phase": "Find XML", "done": 0, "total": None,
                  "detail": str(root)})
    # Count first, then enumerate again. A list of every Workshop XML path can
    # consume gigabytes when a large cache is indexed.
    total = 0
    last_find_report = time.monotonic()
    for path in root.rglob("*.xml"):
        if path.is_symlink():
            continue
        total += 1
        if progress is not None and time.monotonic() - last_find_report >= 1:
            progress({"phase": "Find XML", "done": total, "total": None,
                      "detail": f"{total:,} XML files found"})
            last_find_report = time.monotonic()
    if progress is not None:
        progress({"phase": "Find XML", "done": total, "total": total,
                  "detail": f"{total:,} XML files found"})
        progress({"phase": "Index XML", "done": 0, "total": total,
                  "detail": f"{total:,} XML files found"})
    else:
        print(f"Found {total} XML files. Checking cached entries and indexing vehicles; "
              "a large Workshop folder can take tens of minutes. "
              "Press Ctrl+C to stop and rerun to resume.",
              file=sys.stderr, flush=True)
    changed = examined = ignored = errors = 0
    # TEMP tables are disk backed so they scale with the Workshop cache rather
    # than retaining one Python string/set entry for every file. The stale
    # table is separate because the main files table is modified during cleanup.
    db.commit()
    db.execute("PRAGMA temp_store=FILE")
    db.executescript("""
        CREATE TEMP TABLE IF NOT EXISTS _stormcopy_seen_items (
            item_id TEXT PRIMARY KEY, had_error INTEGER NOT NULL DEFAULT 0
        ) WITHOUT ROWID;
        CREATE TEMP TABLE IF NOT EXISTS _stormcopy_valid_paths (
            item_id TEXT NOT NULL, path TEXT NOT NULL,
            PRIMARY KEY(item_id,path)
        ) WITHOUT ROWID;
        CREATE TEMP TABLE IF NOT EXISTS _stormcopy_stale_paths (
            item_id TEXT NOT NULL, path TEXT NOT NULL,
            PRIMARY KEY(item_id,path)
        ) WITHOUT ROWID;
        DELETE FROM temp._stormcopy_seen_items;
        DELETE FROM temp._stormcopy_valid_paths;
        DELETE FROM temp._stormcopy_stale_paths;
    """)

    def remember(item_id, valid_path=None, error=False):
        with db:
            db.execute("INSERT INTO temp._stormcopy_seen_items(item_id,had_error) "
                       "VALUES (?,?) ON CONFLICT(item_id) DO UPDATE SET "
                       "had_error=MAX(had_error,excluded.had_error)",
                       (item_id, int(error)))
            if valid_path is not None:
                db.execute("INSERT OR IGNORE INTO temp._stormcopy_valid_paths "
                           "VALUES (?,?)", (item_id, valid_path))

    current_progress = {"path": None, "completed": 0, "changed": 0}
    stop = threading.Event()

    def show_progress():
        while not stop.wait(10):
            current = current_progress["path"]
            if current is not None:
                if progress is not None:
                    progress({"phase": "Index XML", "done": current_progress["completed"],
                              "total": total,
                              "detail": f"{current_progress['changed']:,} added or updated; "
                                        f"{current.relative_to(root)}"})
                else:
                    print(f"Processing {current_progress['completed']}/{total}: "
                          f"{current.relative_to(root)}; "
                          f"{current_progress['changed']} added or updated",
                          file=sys.stderr, flush=True)

    reporter = threading.Thread(target=show_progress, daemon=True)
    reporter.start()
    try:
        for path in root.rglob("*.xml"):
            if path.is_symlink():
                continue
            examined += 1
            current_progress["path"] = path
            rel = path.relative_to(root)
            item_id = rel.parts[0] if len(rel.parts) > 1 else "local"
            try:
                with path.open("rb") as stream:
                    if not re.search(rb"<vehicle(?=[\s/>])", stream.read(4096)):
                        ignored += 1
                        remember(item_id)
                        continue
                changed += bool(index_file(db, path, item_id))
                remember(item_id, str(path.resolve()))
                current_progress["changed"] = changed
            except (ValueError, OSError) as exc:
                errors += 1
                remember(item_id, error=True)
                if progress is not None:
                    progress({"phase": "Index XML", "done": current_progress["completed"],
                              "total": total, "warning": f"Skipped {path}: {exc}"})
                else:
                    print(f"skip {path}: {exc}", file=sys.stderr, flush=True)
            finally:
                current_progress["completed"] = examined
        # When a second Workshop folder replaces an item, keep one current copy
        # of that item's fingerprints. Leave errored items untouched for retry.
        with db:
            db.execute("INSERT INTO temp._stormcopy_stale_paths(item_id,path) "
                       "SELECT f.item_id,f.path FROM files f "
                       "JOIN temp._stormcopy_seen_items s ON s.item_id=f.item_id "
                       "WHERE f.item_id!='local' AND s.had_error=0 AND NOT EXISTS "
                       "(SELECT 1 FROM temp._stormcopy_valid_paths v "
                       "WHERE v.item_id=f.item_id AND v.path=f.path)")
        stale = db.execute("SELECT item_id,path FROM temp._stormcopy_stale_paths")
        while rows := stale.fetchmany(500):
            with db:
                for old in rows:
                    db.execute("DELETE FROM features WHERE item_id=? AND path=?", old)
                    search_index.delete(db, old["item_id"], old["path"])
                    db.execute("DELETE FROM files WHERE item_id=? AND path=?", old)
    finally:
        stop.set()
        reporter.join(timeout=1)
        with db:
            db.execute("DROP TABLE IF EXISTS temp._stormcopy_stale_paths")
            db.execute("DROP TABLE IF EXISTS temp._stormcopy_valid_paths")
            db.execute("DROP TABLE IF EXISTS temp._stormcopy_seen_items")
        if progress is not None:
            progress({"phase": "Index XML", "done": examined, "total": total,
                      "detail": f"{changed:,} added or updated; {errors:,} errors"})
    return {"examined": examined, "indexed_or_updated": changed,
            "already_current": examined - changed - ignored - errors,
            "ignored_non_vehicle": ignored, "skipped_errors": errors,
            "skipped": errors}


def upgrade_search_index(db, max_files=0, progress=None):
    """Build the new compact search data from an existing Workshop index.

    Each file uses a savepoint and batches are committed periodically, so an
    interrupted upgrade resumes after the last committed batch.
    Missing source files remain in the legacy index and remain searchable.
    """
    if max_files < 0:
        raise ValueError("max-files must be nonnegative")
    total = db.execute("SELECT COUNT(*) FROM files f LEFT JOIN search_files s "
                       "ON s.item_id=f.item_id AND s.path=f.path "
                       "WHERE s.id IS NULL OR s.version<?",
                       (search_index.SEARCH_INDEX_VERSION,)).fetchone()[0]
    if progress is not None:
        progress({"phase": "Upgrade index", "done": 0, "total": total,
                  "detail": "building MinHash and local fingerprints"})
    upgraded = missing = errors = examined = pending = 0
    last_item = last_path = ""
    try:
        while True:
            page = db.execute(
                "SELECT f.item_id,f.path FROM files f LEFT JOIN search_files s "
                "ON s.item_id=f.item_id AND s.path=f.path "
                "WHERE (s.id IS NULL OR s.version<?) "
                "AND (f.item_id>? OR (f.item_id=? AND f.path>?)) "
                "ORDER BY f.item_id,f.path LIMIT 500",
                (search_index.SEARCH_INDEX_VERSION, last_item,
                 last_item, last_path)).fetchall()
            if not page:
                break
            for row in page:
                if max_files and upgraded + errors >= max_files:
                    break
                last_item, last_path = row["item_id"], row["path"]
                examined += 1
                path = Path(last_path)
                if not path.is_file():
                    missing += 1
                    continue
                if not db.in_transaction:
                    db.execute("BEGIN")
                db.execute("SAVEPOINT upgrade_file")
                try:
                    changed = index_file(db, path, last_item, commit=False)
                except (OSError, ValueError) as exc:
                    db.execute("ROLLBACK TO upgrade_file")
                    db.execute("RELEASE upgrade_file")
                    errors += 1
                    if progress is not None:
                        progress({"phase": "Upgrade index", "done": examined, "total": total,
                                  "warning": f"Could not upgrade {path}: {exc}"})
                except BaseException:
                    db.execute("ROLLBACK TO upgrade_file")
                    db.execute("RELEASE upgrade_file")
                    raise
                else:
                    db.execute("RELEASE upgrade_file")
                    upgraded += bool(changed)
                    pending += 1
                if pending >= 20:
                    db.commit()
                    pending = 0
                if progress is not None:
                    progress({"phase": "Upgrade index", "done": examined, "total": total,
                              "detail": f"{upgraded:,} upgraded, {missing:,} files missing"})
            if max_files and upgraded + errors >= max_files:
                break
    finally:
        if db.in_transaction:
            db.commit()
    remaining = db.execute("SELECT COUNT(*) FROM files f LEFT JOIN search_files s "
                           "ON s.item_id=f.item_id AND s.path=f.path "
                           "WHERE s.id IS NULL OR s.version<?",
                           (search_index.SEARCH_INDEX_VERSION,)).fetchone()[0]
    if progress is not None:
        progress({"phase": "Upgrade index", "done": examined, "total": total,
                  "detail": f"{upgraded:,} upgraded; {remaining:,} still on old index"})
    return {"examined": examined, "upgraded": upgraded, "missing_files": missing,
            "errors": errors, "remaining_legacy_files": remaining}


def _weight(total, df):
    return 1 + math.log((total + 1) / (df + 1))


def _document_frequency(db, table, hashes, channel=None):
    """Count candidate documents for evidence without hitting SQLite's bind limit."""
    result = {}
    for start in range(0, len(hashes), 400):
        group = hashes[start:start + 400]
        if not group:
            continue
        slots = ",".join("?" for _ in group)
        if channel is None:
            sql = f"SELECT hash,COUNT(*) AS n FROM {table} WHERE hash IN ({slots}) GROUP BY hash"
            params = group
        else:
            sql = (f"SELECT hash,COUNT(*) AS n FROM {table} "
                   f"WHERE channel=? AND hash IN ({slots}) GROUP BY hash")
            params = (channel, *group)
        for row in db.execute(sql, params):
            result[row["hash"]] = row["n"]
    return result


def _legacy_candidates(db, fp, total_files):
    hashes = list(fp["features"])
    df = _document_frequency(db, "features", hashes)
    ranked = sorted(hashes, key=lambda h: (df.get(h, 0), h))
    selected = [h for h in ranked if 0 < df.get(h, 0) <= max(20, total_files // 10)][:300]
    if not selected:
        selected = [h for h in ranked if df.get(h, 0)][:100]
    votes = Counter()
    for start in range(0, len(selected), 400):
        group = selected[start:start + 400]
        slots = ",".join("?" for _ in group)
        for row in db.execute(f"SELECT item_id,path,hash FROM features WHERE hash IN ({slots})", group):
            votes[(row["item_id"], row["path"])] += _weight(total_files, df[row["hash"]])
    return votes.most_common(80), df


def _overlap(ours, theirs):
    shared = {h: min(n, theirs[h]) for h, n in ours.items() if h in theirs}
    count = sum(shared.values())
    own_total = sum(ours.values())
    their_total = sum(theirs.values())
    return shared, count, (100 * count / own_total if own_total else 0.0), (
        100 * count / their_total if their_total else 0.0)


def _largest_cluster(positions):
    """Count nearby shared anchors; disjoint generic hits are weaker evidence."""
    points = {tuple(position) for position in positions if position is not None}
    largest = 0
    while points:
        stack = [points.pop()]
        size = 0
        while stack:
            x, y, z = stack.pop()
            size += 1
            for dx in range(-2, 3):
                for dy in range(-2, 3):
                    for dz in range(-2, 3):
                        if abs(dx) + abs(dy) + abs(dz) > 3:
                            continue
                        neighbor = (x + dx, y + dy, z + dz)
                        if neighbor in points:
                            points.remove(neighbor)
                            stack.append(neighbor)
        largest = max(largest, size)
    return largest


def _aligned_cluster(hashes, query_samples, other_samples):
    """Require nearby rare anchors with one consistent relative translation."""
    by_shift = {}
    for value in hashes:
        query = query_samples.get(value)
        other = other_samples.get(value)
        if not query or not other or len(query) != 3 or len(other) != 3:
            continue
        shift = tuple(int(b) - int(a) for a, b in zip(query, other))
        by_shift.setdefault(shift, []).append(query)
    return max((_largest_cluster(points) for points in by_shift.values()), default=0)


def _legacy_fingerprint(db, item_id, path):
    rows = db.execute("SELECT hash,count,sample FROM features WHERE item_id=? AND path=?",
                      (item_id, path)).fetchall()
    if not rows:
        return None
    file_row = db.execute("SELECT components FROM files WHERE item_id=? AND path=?",
                          (item_id, path)).fetchone()
    return {"components": file_row[0] if file_row else 0,
            "features": Counter({row["hash"]: row["count"] for row in rows}),
            "samples": {row["hash"]: json.loads(row["sample"]) for row in rows},
            "winnow_features": Counter(), "winnow_samples": {},
            "logic_features": Counter(), "logic_samples": {},
            "micro_features": Counter(), "micro_samples": {},
            "component_types": Counter()}


def _rank_match(db, query, theirs, item_id, candidate_path, total_files, df, legacy=False,
                query_signature=None):
    geometry, shared_n, query_pct, other_pct = _overlap(query["features"], theirs["features"])
    winnow, winnow_n, winnow_pct, _ = _overlap(
        query.get("winnow_features", {}), theirs.get("winnow_features", {}))
    logic, logic_n, logic_pct, _ = _overlap(
        query.get("logic_features", {}), theirs.get("logic_features", {}))
    micro, micro_n, micro_pct, _ = _overlap(
        query.get("micro_features", {}), theirs.get("micro_features", {}))
    _, type_n, type_pct, _ = _overlap(
        query.get("component_types", {}), theirs.get("component_types", {}))
    small_exact = (bool(query["features"]) and
                   query["features"] == theirs["features"] and
                   query["components"] == theirs["components"])
    if shared_n < 5 and winnow_n < 3 and logic_n < 3 and micro_n < 2 and not small_exact:
        return None
    rare_limit = max(2, total_files // 100)
    rare_hashes = [h for h in geometry if 0 < df.get(h, 0) <= rare_limit]
    rare_winnow = [h for h in winnow if 0 < df.get("w:" + h, 0) <= rare_limit]
    rare_micro = [h for h in micro if 0 < df.get("m:" + h, 0) <= rare_limit]
    rare_logic = [h for h in logic if 0 < df.get("l:" + h, 0) <= rare_limit]
    rare_count = sum(geometry[h] for h in rare_hashes)
    cluster = _aligned_cluster(rare_hashes, query.get("samples", {}),
                               theirs.get("samples", {}))
    partial = (12 <= query_pct < 65 and shared_n >= 20 and len(rare_hashes) >= 10 and
               cluster >= 8 and (winnow_n >= 3 or len(rare_hashes) >= 25))
    high = shared_n >= 40 and query_pct >= 65 and len(rare_hashes) >= 25
    medium = (shared_n >= 20 and query_pct >= 35 and len(rare_hashes) >= 10) or partial
    if not medium and winnow_n >= 8 and len(rare_winnow) >= 4 and shared_n >= 10:
        medium = True
    semantic_copy = (len(rare_micro) >= 2 and micro_n >= 2 and
                     (len(rare_logic) >= 1 or shared_n >= 5 or
                      not query["features"]))
    if semantic_copy:
        medium = True
    confidence = "high" if high else "medium" if medium else "low"
    channels = {}
    for label, count, coverage in (("geometry", shared_n, query_pct),
                                   ("winnowing", winnow_n, winnow_pct),
                                   ("logic", logic_n, logic_pct),
                                   ("microcontrollers", micro_n, micro_pct),
                                   ("component_types", type_n, type_pct)):
        channels[label] = {"shared": count, "query_coverage_percent": round(coverage, 1)}
    # Scores are directional containment, not a probability of plagiarism.
    weights = (("features", query_pct, 0.55),
               ("winnow_features", winnow_pct, 0.20),
               ("component_types", type_pct, 0.10),
               ("logic_features", logic_pct, 0.07),
               ("micro_features", micro_pct, 0.08))
    applicable = [(score, weight) for key, score, weight in weights if query.get(key)]
    combined = sum(score * weight for score, weight in applicable) / sum(
        weight for _, weight in applicable) if applicable else 0.0
    evidence = []
    for h in sorted(geometry, key=lambda h: (df.get(h, total_files + 1), -geometry[h]))[:5]:
        evidence.append({"channel": "geometry", "fingerprint": h,
                         "matching_components": geometry[h],
                         "indexed_occurrences": df.get(h),
                         "occurrence_scope": "full" if legacy else "sampled",
                         "query_position": query["samples"].get(h),
                         "workshop_position": theirs["samples"].get(h)})
    for h in sorted(winnow, key=lambda h: (df.get("w:" + h, total_files + 1), -winnow[h]))[:2]:
        evidence.append({"channel": "winnowing", "fingerprint": h,
                         "matching_components": winnow[h],
                         "indexed_occurrences": df.get("w:" + h),
                         "occurrence_scope": "sampled",
                         "query_position": query["winnow_samples"].get(h),
                         "workshop_position": theirs["winnow_samples"].get(h)})
    for label, shared, key, prefix in (("logic", logic, "logic_samples", "l:"),
                                       ("microcontroller", micro, "micro_samples", "m:")):
        for h in sorted(shared, key=lambda h: (df.get(prefix + h, total_files + 1),
                                               -shared[h]))[:2]:
            evidence.append({"channel": label, "fingerprint": h,
                             "matching_components": shared[h],
                             "indexed_occurrences": df.get(prefix + h),
                             "occurrence_scope": "sampled",
                             "query_position": query[key].get(h),
                             "workshop_position": theirs[key].get(h)})
    estimate_pct = None
    if (not legacy and query_signature is not None and minhash_bands(query_signature)
            and theirs.get("minhash_signature") and
            minhash_bands(tuple(theirs["minhash_signature"]))):
        estimate_pct = round(100 * minhash_estimate(query_signature,
                                                    tuple(theirs["minhash_signature"])), 1)
    # The items primary-key B-tree checks a candidate ID in logarithmic time.
    # A local catalog entry does not verify publication or establish copying.
    item = db.execute("SELECT title FROM items WHERE id=?", (item_id,)).fetchone()
    return {"item_id": item_id, "title": item["title"] if item else None,
            "listed_in_known_ids": bool(item is not None and item_id.isdigit()),
            "url": (f"https://steamcommunity.com/sharedfiles/filedetails/?id={item_id}"
                    if item_id.isdigit() else None),
            "file": candidate_path, "similarity_percent": round(query_pct, 1),
            "workshop_coverage_percent": round(other_pct, 1),
            "combined_similarity_percent": round(combined, 1),
            "minhash_jaccard_estimate_percent": estimate_pct,
            "shared_neighborhoods": shared_n, "distinct_shared_neighborhoods": len(geometry),
            "rare_shared_neighborhoods": rare_count,
            "rare_distinct_neighborhoods": len(rare_hashes),
            "largest_matching_cluster": cluster, "partial_copy_evidence": partial,
            "semantic_copy_evidence": semantic_copy,
            "channels": channels, "confidence": confidence, "evidence": evidence}


def scan_fingerprint(db, fp, limit=5, legacy_fp=None):
    """Compare a precomputed structural fingerprint with the indexed corpus.

    ``legacy_fp`` enables searches of pre-upgrade rows. Remote callers omit it
    because their requests contain no XML from which to build that older form.
    """
    if limit < 1:
        raise ValueError("limit must be positive")
    total_files = db.execute("SELECT COUNT(*) FROM files").fetchone()[0]
    indexed_items = db.execute("SELECT COUNT(DISTINCT item_id) FROM files").fetchone()[0]
    known_items = db.execute("SELECT COUNT(*) FROM items").fetchone()[0]
    search_files = db.execute("SELECT COUNT(*) FROM search_files").fetchone()[0]
    modern_files = db.execute("SELECT COUNT(*) FROM search_files WHERE version=?",
                              (search_index.SEARCH_INDEX_VERSION,)).fetchone()[0]
    # Older search rows may contain hashes from a previous canonicalization.
    # Count only current-version rows as reliably covered by a remote query.
    searched_files = total_files if legacy_fp is not None else modern_files
    if not total_files:
        return {"status": "no index", "suspicion_level": "unknown",
                "message": "Index Workshop XML first.",
                "coverage": {"indexed_items": 0, "known_items": known_items,
                             "indexed_files": 0, "modern_search_files": 0,
                             "searched_files": 0},
                "matches": []}
    query_signature = minhash_signature(
        ["g:" + h for h in fp["features"]] +
        ["w:" + h for h in fp["winnow_features"]])
    modern = search_index.candidates(db, fp, total_files, limit=150)
    legacy = []
    legacy_df = {}
    if legacy_fp is not None and search_files < total_files:
        legacy, legacy_df = _legacy_candidates(db, legacy_fp, total_files)
    selected = {}
    for item_id, candidate_path, votes in modern:
        selected[(item_id, candidate_path)] = (False, votes)
    for (item_id, candidate_path), votes in legacy:
        selected.setdefault((item_id, candidate_path), (True, votes))
    # Exact reranking uses full compressed fingerprints. Count only sampled
    # hashes overlapping each candidate, and reuse frequencies seen earlier.
    # Keeping one decoded candidate at a time avoids both duplicate loads and
    # a large peak allocation when the bounded candidate list is full.
    sampled = {field: set(search_index.sampled_hashes(fp, field))
               for field in ("features", "winnow_features",
                             "logic_features", "micro_features")}
    checked = {field: set() for field in sampled}
    cap = max(2, total_files // 100)
    df = {}
    results = []
    for (item_id, candidate_path), (is_legacy, _) in selected.items():
        theirs = (_legacy_fingerprint(db, item_id, candidate_path) if is_legacy else
                  search_index.load(db, item_id, candidate_path))
        if theirs is None:
            continue
        if not is_legacy:
            for field, code in (("features", "g"), ("winnow_features", "w"),
                                ("logic_features", "l"), ("micro_features", "m")):
                unseen = sampled[field].intersection(theirs[field]) - checked[field]
                if unseen:
                    checked[field].update(unseen)
                    counts = search_index.document_frequencies(db, unseen, code, cap=cap)
                    prefix = "" if code == "g" else code + ":"
                    df.update({prefix + h: n for h, n in counts.items()})
        result = _rank_match(db, legacy_fp if is_legacy else fp, theirs,
                             item_id, candidate_path, total_files,
                             legacy_df if is_legacy else df,
                             legacy=is_legacy, query_signature=query_signature)
        if result is not None:
            results.append(result)
    results.sort(key=lambda r: ({"high": 2, "medium": 1, "low": 0}[r["confidence"]],
                                r["combined_similarity_percent"],
                                r["similarity_percent"], r["shared_neighborhoods"]),
                 reverse=True)
    best = results[0] if results else None
    status = ("strong overlap" if best and best["confidence"] == "high" else
              "possible shared controller" if best and best["semantic_copy_evidence"] else
              "possible shared structure" if best and best["confidence"] == "medium" else
              "no strong match in indexed set")
    suspicion = ("high" if status == "strong overlap" else
                 "medium" if status.startswith("possible shared") else
                 "low within indexed set")
    return {"status": status, "suspicion_level": suspicion,
            "query_components": fp["components"],
            "query_neighborhoods": sum(fp["features"].values()),
            "coverage": {"indexed_items": indexed_items, "known_items": known_items,
                         "indexed_files": total_files, "modern_search_files": modern_files,
                         "searched_files": searched_files,
                         "discovery_complete": get_state(db, "discovery_complete_v2_published") == "1",
                         "last_discovery": get_state(db, "last_discovery")},
            "best_match": best, "matches": results[:limit],
            "note": "Similarity measures directional structural overlap; confidence is heuristic, "
                    "not proof of copying. Only indexed public/downloadable files were checked."
                    + (f" Fingerprint search covered {searched_files:,} of "
                       f"{total_files:,} indexed files; upgrade the index to cover the rest."
                       if searched_files < total_files else "")}


def scan(db, path, limit=5):
    """Compare a local XML file, including legacy index rows where available."""
    if limit < 1:
        raise ValueError("limit must be positive")
    fp = fingerprint_file(path)
    total_files = db.execute("SELECT COUNT(*) FROM files").fetchone()[0]
    search_files = db.execute("SELECT COUNT(*) FROM search_files").fetchone()[0]
    legacy_fp = None
    if search_files < total_files:
        try:
            legacy_fp = fingerprint_file(path, legacy=True)
        except ValueError:
            # A controller-only vehicle may have no legacy geometry at all.
            pass
    return scan_fingerprint(db, fp, limit=limit, legacy_fp=legacy_fp)
