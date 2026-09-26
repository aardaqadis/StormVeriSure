"""Persistent candidate retrieval for whole vehicles and copied sections.

The MinHash buckets find near duplicates. A bounded, rare-feature inverted
index finds local overlap even when a copied section is too small to collide
in a whole-vehicle LSH band. The caller owns all SQLite transactions.
"""

from collections import defaultdict
from heapq import nsmallest
from hashlib import blake2b
from itertools import chain, zip_longest
import json
import math
import sqlite3
import zlib

from .minhash import bands, signature


SEARCH_INDEX_VERSION = 2
_CHANNEL_LIMITS = {"features": 512, "winnow_features": 512,
                   "logic_features": 128, "micro_features": 128}
_LARGE_FILE_POSTING_LIMIT = 4096
_CHANNEL_CODES = {"features": "g", "winnow_features": "w",
                  "logic_features": "l", "micro_features": "m"}
_CHANNEL_WEIGHTS = {"g": 1.0, "w": 1.5, "l": 2.0, "m": 3.0}
_COUNT_FIELDS = (*_CHANNEL_LIMITS, "component_types")
_SAMPLE_FIELDS = ("samples", "winnow_samples", "logic_samples", "micro_samples")
_POST_DOMAIN = b"swcopy-post-v1"
_POST_KEY_DOMAIN = b"swcopy-key-v2"
_MAX_PROBE_ROWS = 100_000
_MAX_RETRIEVAL_ROWS = 200_000


def _create_compact_tables(db: sqlite3.Connection) -> None:
    db.execute("""CREATE TABLE IF NOT EXISTS search_files (
        id INTEGER PRIMARY KEY, item_id TEXT NOT NULL, path TEXT NOT NULL,
        payload BLOB NOT NULL, version INTEGER NOT NULL,
        UNIQUE(item_id,path))""")
    db.execute("""CREATE TABLE IF NOT EXISTS search_postings (
        channel TEXT NOT NULL, hash BLOB NOT NULL, file_id INTEGER NOT NULL,
        PRIMARY KEY(channel,hash,file_id)) WITHOUT ROWID""")
    db.execute("CREATE INDEX IF NOT EXISTS search_postings_by_file "
               "ON search_postings(file_id)")
    db.execute("""CREATE TABLE IF NOT EXISTS lsh_buckets (
        bucket BLOB NOT NULL, file_id INTEGER NOT NULL,
        PRIMARY KEY(bucket,file_id)) WITHOUT ROWID""")
    db.execute("CREATE INDEX IF NOT EXISTS lsh_buckets_by_file "
               "ON lsh_buckets(file_id)")


def _decode_payload(compressed: bytes) -> dict:
    payload = json.loads(zlib.decompress(compressed).decode("utf-8"))
    payload["minhash_signature"] = tuple(payload.get("minhash_signature", ()))
    for field in _SAMPLE_FIELDS:
        payload[field] = {key: tuple(value) if isinstance(value, list) else value
                          for key, value in payload.get(field, {}).items()}
    return payload


def _migrate_text_tables(db: sqlite3.Connection) -> None:
    """Atomically rebuild the old path-heavy index from saved fingerprints."""
    db.execute("SAVEPOINT search_index_v2")
    try:
        db.execute("ALTER TABLE search_files RENAME TO search_files_v1")
        db.execute("DROP TABLE IF EXISTS search_postings")
        db.execute("DROP TABLE IF EXISTS lsh_buckets")
        _create_compact_tables(db)
        source = db.execute("SELECT item_id,path,payload FROM search_files_v1 "
                            "ORDER BY item_id,path")
        try:
            while batch := source.fetchmany(32):
                for item_id, path, compressed in batch:
                    save(db, item_id, path, _decode_payload(compressed))
                    # Old payloads cannot reflect later XML canonicalization
                    # changes. Reparse their source files during upgrade.
                    db.execute("UPDATE search_files SET version=0 "
                               "WHERE item_id=? AND path=?", (item_id, path))
        finally:
            source.close()
        db.execute("DROP TABLE search_files_v1")
        db.execute("RELEASE SAVEPOINT search_index_v2")
    except BaseException:
        db.execute("ROLLBACK TO SAVEPOINT search_index_v2")
        db.execute("RELEASE SAVEPOINT search_index_v2")
        raise


def ensure_tables(db: sqlite3.Connection) -> None:
    """Create compact storage or migrate the earlier text-path index.

    Migration uses a savepoint and 32-file read batches. It never commits a
    transaction owned by the caller; a failed migration restores old tables.
    """
    tables = {row[0] for row in db.execute(
        "SELECT name FROM sqlite_master WHERE type='table'")}
    if "search_files" not in tables:
        if "search_postings" in tables or "lsh_buckets" in tables:
            raise ValueError("Incomplete search index: search_files is missing")
        _create_compact_tables(db)
        return
    columns = {row[1] for row in db.execute("PRAGMA table_info(search_files)")}
    if "id" not in columns:
        _migrate_text_tables(db)
        return
    if "version" not in columns:
        db.execute("ALTER TABLE search_files ADD COLUMN version "
                   "INTEGER NOT NULL DEFAULT 0")
    for table in ("search_postings", "lsh_buckets"):
        if table in tables:
            fields = {row[1] for row in db.execute(f"PRAGMA table_info({table})")}
            if "file_id" not in fields:
                raise ValueError(f"Incomplete compact search index: {table}")
    _create_compact_tables(db)


def _sketch(fp: dict) -> tuple[int, ...]:
    return signature(chain(
        ("g:" + value for value in fp.get("features", ())),
        ("w:" + value for value in fp.get("winnow_features", ())),
    ))


def _bottom_k(values, size: int) -> list[str]:
    """Choose the same bounded sample in every copy regardless of XML order."""
    def rank(value: str) -> tuple[bytes, str]:
        digest = blake2b(value.encode("utf-8"), digest_size=8,
                         person=_POST_DOMAIN).digest()
        return digest, value

    return nsmallest(size, set(values), key=rank)


def sampled_hashes(fp: dict, field: str) -> list[str]:
    """Return the exact deterministic sample stored in a channel's postings."""
    if field not in _CHANNEL_LIMITS:
        raise ValueError(f"Unknown fingerprint channel: {field}")
    values = fp.get(field, ())
    limit = _CHANNEL_LIMITS[field]
    if field in ("features", "winnow_features"):
        # A fixed 512-token sample is too thin for a copied section inside a
        # very large creation. Grow the local sample to roughly 10% of unique
        # structures while retaining a finite per-file storage cap.
        limit = min(_LARGE_FILE_POSTING_LIMIT, max(limit, math.ceil(len(values) * 0.10)))
    return _bottom_k(values, limit)


def _postings(fp: dict):
    for field in _CHANNEL_LIMITS:
        code = _CHANNEL_CODES[field]
        for value in sampled_hashes(fp, field):
            yield code, value


def _query_postings(fp: dict):
    """Give each evidence channel a chance before a probe budget runs out."""
    channels = [[(code, value) for value in sampled_hashes(fp, field)]
                for field, code in _CHANNEL_CODES.items()]
    for group in zip_longest(*channels):
        for posting in group:
            if posting is not None:
                yield posting


def _post_key(value: str) -> bytes:
    return blake2b(value.encode("utf-8"), digest_size=12,
                   person=_POST_KEY_DOMAIN).digest()


def _bucket_key(value: str) -> bytes:
    # minhash.bands already includes the band number in its 16-byte digest.
    return bytes.fromhex(value.partition(":")[2])


def delete(db: sqlite3.Connection, item_id: str, path: str) -> None:
    """Remove a file and its candidate indexes without committing."""
    row = db.execute("SELECT id FROM search_files WHERE item_id=? AND path=?",
                     (str(item_id), str(path))).fetchone()
    if row is None:
        return
    file_id = row[0]
    db.execute("DELETE FROM search_postings WHERE file_id=?", (file_id,))
    db.execute("DELETE FROM lsh_buckets WHERE file_id=?", (file_id,))
    db.execute("DELETE FROM search_files WHERE id=?", (file_id,))


def save(db: sqlite3.Connection, item_id: str, path: str, fp: dict) -> None:
    """Replace one file's compressed evidence, MinHash bands, and postings."""
    item_id, path = str(item_id), str(path)
    sketch = _sketch(fp)
    payload = {field: dict(fp.get(field) or {})
               for field in (*_COUNT_FIELDS, *_SAMPLE_FIELDS)}
    payload["components"] = int(fp.get("components", 0))
    payload["minhash_signature"] = sketch
    compressed = zlib.compress(json.dumps(
        payload, ensure_ascii=False, sort_keys=True,
        separators=(",", ":")).encode("utf-8"), level=6)

    row = db.execute("SELECT id FROM search_files WHERE item_id=? AND path=?",
                     (item_id, path)).fetchone()
    if row is None:
        file_id = db.execute(
            "INSERT INTO search_files(item_id,path,payload,version) "
            "VALUES (?,?,?,?)",
            (item_id, path, compressed, SEARCH_INDEX_VERSION)).lastrowid
    else:
        file_id = row[0]
        db.execute("DELETE FROM search_postings WHERE file_id=?", (file_id,))
        db.execute("DELETE FROM lsh_buckets WHERE file_id=?", (file_id,))
        db.execute("UPDATE search_files SET payload=?,version=? WHERE id=?",
                   (compressed, SEARCH_INDEX_VERSION, file_id))
    db.executemany("INSERT INTO search_postings(channel,hash,file_id) "
                   "VALUES (?,?,?)",
                   ((code, _post_key(value), file_id)
                    for code, value in _postings(fp)))
    db.executemany("INSERT INTO lsh_buckets(bucket,file_id) VALUES (?,?)",
                   ((_bucket_key(bucket), file_id) for bucket in bands(sketch)))


def load(db: sqlite3.Connection, item_id: str, path: str) -> dict | None:
    """Load all comparison evidence, including the stored MinHash signature."""
    row = db.execute("SELECT payload FROM search_files WHERE item_id=? AND path=?",
                     (str(item_id), str(path))).fetchone()
    if row is None:
        return None
    return _decode_payload(row[0])


def _frequency_cutoff(total_files: int) -> int:
    # Small indexes must retain all ordinary overlaps; at Workshop scale the
    # cap prevents generic construction patterns from flooding a query.
    return max(32, min(2000, math.ceil(max(0, total_files) * 0.02)))


def document_frequencies(db: sqlite3.Connection, hashes, channel: str,
                         cap: int | None = None) -> dict[str, int]:
    """Count indexed documents by original fingerprint string.

    With ``cap``, counts above it are returned as ``cap + 1``. This avoids
    walking all postings of a ubiquitous fingerprint when exact high counts
    are unnecessary for evidence ranking.
    """
    if channel not in _CHANNEL_WEIGHTS:
        raise ValueError(f"Unknown fingerprint channel: {channel}")
    if cap is not None and cap < 0:
        raise ValueError("cap must be nonnegative")
    counts = {}
    for value in dict.fromkeys(hashes):
        key = _post_key(value)
        if cap is None:
            number = db.execute(
                "SELECT COUNT(*) FROM search_postings WHERE channel=? AND hash=?",
                (channel, key)).fetchone()[0]
        else:
            number = len(db.execute(
                "SELECT 1 FROM search_postings WHERE channel=? AND hash=? LIMIT ?",
                (channel, key, cap + 1)).fetchall())
        if number:
            counts[value] = number
    return counts


def candidates(db: sqlite3.Connection, fp: dict, total_files: int,
               limit: int = 200) -> list[tuple[str, str, float]]:
    """Rank a bounded union of LSH hits and rare local-feature hits.

    Votes are retrieval scores only. The caller must rerank returned files
    using their full stored structural evidence before reporting similarity.
    """
    if limit <= 0:
        return []
    cutoff = _frequency_cutoff(total_files)
    votes = defaultdict(float)
    probe_rows = 0
    scored_rows = 0

    # Fetch at most cutoff+1 rows so a popular LSH bucket is rejected without
    # scanning all its members.
    for bucket in bands(_sketch(fp)):
        rows = db.execute("SELECT file_id FROM lsh_buckets WHERE bucket=? "
                          "LIMIT ?", (_bucket_key(bucket), cutoff + 1)).fetchall()
        probe_rows += len(rows)
        if len(rows) > cutoff:
            continue
        for (file_id,) in rows:
            votes[file_id] += 8.0

    # Probe every term at low document frequency before trying larger probes.
    # This finds rare copied sections without a COUNT(*) walk over common
    # postings. A second fetch is made only for selected, useful terms.
    unresolved = list(_query_postings(fp))
    selected = []
    for threshold in sorted({min(cutoff, level) for level in (32, 128, 512, cutoff)}):
        remaining = []
        for code, value in unresolved:
            budget = _MAX_PROBE_ROWS - probe_rows
            if budget < 2:
                break
            fetch_limit = min(threshold + 1, budget)
            seen = db.execute("SELECT 1 FROM search_postings "
                              "WHERE channel=? AND hash=? LIMIT ?",
                              (code, _post_key(value), fetch_limit)).fetchall()
            probe_rows += len(seen)
            if len(seen) < fetch_limit:
                if seen:
                    selected.append((len(seen), code, value))
            else:
                remaining.append((code, value))
        unresolved = remaining
        if not unresolved or probe_rows >= _MAX_PROBE_ROWS:
            break

    # Each candidate row occupies one budget unit; the probe budget also
    # counts toward the total. Processing rare terms first maximizes useful
    # coverage if the limit is reached.
    for frequency, code, value in sorted(selected):
        if frequency > _MAX_RETRIEVAL_ROWS - probe_rows - scored_rows:
            break
        rarity = 1.0 + max(0.0, math.log((max(0, total_files) + 1) /
                                           (frequency + 1)))
        weight = _CHANNEL_WEIGHTS[code] * rarity
        for (file_id,) in db.execute(
                "SELECT file_id FROM search_postings "
                "WHERE channel=? AND hash=? LIMIT ?",
                (code, _post_key(value), frequency)):
            votes[file_id] += weight
            scored_rows += 1

    ranked = nsmallest(limit, votes.items(),
                       key=lambda entry: (-entry[1], entry[0]))
    if not ranked:
        return []
    results = []
    # Resolve long paths only for the top results, never for every posting.
    for file_id, score in ranked:
        row = db.execute("SELECT item_id,path FROM search_files WHERE id=?",
                         (file_id,)).fetchone()
        if row is not None:
            results.append((row[0], row[1], score))
    return results
