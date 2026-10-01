"""Read-only HTTP search of an existing Workshop fingerprint index.

Clients send structural fingerprints, never their complete vehicle XML. The
service does not accept paths or download Workshop content during a request.
"""

from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from contextlib import closing
import hmac
import ipaddress
import json
import os
from pathlib import Path
import re
import socket
import sqlite3
import threading
from .fingerprint import MAX_COMPONENTS
from .index import get_state, scan_fingerprint
from .search_index import SEARCH_INDEX_VERSION


MAX_FINGERPRINT_BYTES = 64 * 1024 * 1024
MAX_FINGERPRINT_KEYS = 500_000
MAX_CONCURRENT_SCANS = 4
MAX_CONNECTION_THREADS = 16
_HASH = re.compile(r"[0-9a-f]{24}\Z")
_FEATURE_FIELDS = ("features", "winnow_features", "logic_features", "micro_features")
_SAMPLE_FIELDS = ("samples", "winnow_samples", "logic_samples", "micro_samples")
_SAMPLE_FEATURES = dict(zip(_SAMPLE_FIELDS, _FEATURE_FIELDS))


def _loopback(host):
    if host.casefold() == "localhost":
        return True
    try:
        return ipaddress.ip_address(host).is_loopback
    except ValueError:
        return False


def _read_only_db(path):
    path = Path(path).resolve()
    if not path.is_file():
        raise OSError("Workshop fingerprint index is unavailable")
    db = sqlite3.connect(path.as_uri() + "?mode=ro", uri=True, timeout=30)
    db.row_factory = sqlite3.Row
    db.execute("PRAGMA query_only=ON")
    db.execute("PRAGMA busy_timeout=30000")
    return db


def _coverage(db):
    indexed_files = db.execute("SELECT COUNT(*) FROM files").fetchone()[0]
    modern_files = db.execute(
        "SELECT COUNT(*) FROM search_files WHERE version=?",
        (SEARCH_INDEX_VERSION,)).fetchone()[0]
    return {
        "indexed_items": db.execute(
            "SELECT COUNT(DISTINCT item_id) FROM files").fetchone()[0],
        "indexed_files": indexed_files,
        "searched_files": modern_files,
        "modern_search_files": modern_files,
        "known_items": db.execute("SELECT COUNT(*) FROM items").fetchone()[0],
        "discovery_complete": get_state(db, "discovery_complete_v2_published") == "1",
    }


def _validate_fingerprint(data):
    """Reject malformed or unbounded client-provided maps before index work."""
    if not isinstance(data, dict) or type(data.get("fingerprint_version")) is not int:
        raise ValueError("Expected a versioned fingerprint JSON object")
    if data["fingerprint_version"] != SEARCH_INDEX_VERSION:
        raise ValueError("Fingerprint version does not match this search index")
    fp = data.get("fingerprint")
    if not isinstance(fp, dict):
        raise ValueError("Missing fingerprint object")
    components = fp.get("components")
    if type(components) is not int or not 0 <= components <= MAX_COMPONENTS:
        raise ValueError("Invalid component count")

    total_keys = 0
    for field in _FEATURE_FIELDS:
        values = fp.get(field)
        if not isinstance(values, dict):
            raise ValueError(f"Invalid {field} map")
        total_keys += len(values)
        if total_keys > MAX_FINGERPRINT_KEYS:
            raise ValueError("Fingerprint contains too many features")
        for key, count in values.items():
            if not isinstance(key, str) or _HASH.fullmatch(key) is None:
                raise ValueError(f"Invalid {field} hash")
            if type(count) is not int or not 1 <= count <= MAX_COMPONENTS:
                raise ValueError(f"Invalid {field} count")

    if not any(fp[field] for field in _FEATURE_FIELDS):
        raise ValueError("Fingerprint has no searchable features")

    types = fp.get("component_types")
    if not isinstance(types, dict) or len(types) > MAX_FINGERPRINT_KEYS:
        raise ValueError("Invalid component types")
    for kind, count in types.items():
        if (not isinstance(kind, str) or not 1 <= len(kind) <= 128 or
                type(count) is not int or not 1 <= count <= MAX_COMPONENTS):
            raise ValueError("Invalid component type entry")

    for field, feature_field in _SAMPLE_FEATURES.items():
        samples = fp.get(field)
        if not isinstance(samples, dict) or len(samples) > len(fp[feature_field]):
            raise ValueError(f"Invalid {field} map")
        for key, position in samples.items():
            if key not in fp[feature_field]:
                raise ValueError(f"Unknown {field} hash")
            if position is None:
                continue
            if (not isinstance(position, (list, tuple)) or len(position) != 3 or
                    any(type(value) is not int or abs(value) > 1_000_000_000
                        for value in position)):
                raise ValueError(f"Invalid {field} position")
    return fp


def _public_result(result):
    """Keep server-local source paths and raw hash keys out of the response."""
    def clean_match(match):
        if match is None:
            return None
        clean = {key: value for key, value in match.items() if key != "file"}
        clean["evidence"] = [
            {key: value for key, value in entry.items() if key != "fingerprint"}
            for entry in match.get("evidence", ())]
        return clean

    clean = dict(result)
    if "best_match" in clean:
        clean["best_match"] = clean_match(clean["best_match"])
    clean["matches"] = [clean_match(match) for match in clean.get("matches", ())]
    return clean


def make_server(db_path, host="127.0.0.1", port=8766, token=None):
    """Create a server for CLI use or tests. Nonlocal binding requires a token."""
    if token is None:
        token = os.environ.get("STORMCOPY_SEARCH_TOKEN", "")
    if not isinstance(token, str):
        raise ValueError("Search token must be text")
    if not _loopback(host) and not token:
        raise ValueError("Set STORMCOPY_SEARCH_TOKEN before binding outside localhost")
    if not 0 <= port <= 65535:
        raise ValueError("Port must be from 0 to 65535")
    db_path = Path(db_path)
    with closing(_read_only_db(db_path)) as db:
        _coverage(db)
    scan_slots = threading.BoundedSemaphore(MAX_CONCURRENT_SCANS)

    class Handler(BaseHTTPRequestHandler):
        protocol_version = "HTTP/1.1"

        def _json(self, status, payload):
            encoded = json.dumps(payload, separators=(",", ":")).encode("utf-8")
            self.send_response(status)
            self.send_header("Content-Type", "application/json; charset=utf-8")
            self.send_header("Content-Length", str(len(encoded)))
            self.send_header("Cache-Control", "no-store")
            self.send_header("X-Content-Type-Options", "nosniff")
            self.send_header("Connection", "close")
            self.end_headers()
            self.wfile.write(encoded)
            self.close_connection = True

        def _authorized(self):
            if not token:
                return True
            supplied = self.headers.get("Authorization", "")
            return hmac.compare_digest(supplied, "Bearer " + token)

        def do_GET(self):
            if not self._authorized():
                self._json(401, {"error": "Authentication required"})
                return
            if self.path not in ("/", "/health", "/v1/coverage"):
                self._json(404, {"error": "Unknown endpoint"})
                return
            try:
                with closing(_read_only_db(db_path)) as db:
                    coverage = _coverage(db)
            except (OSError, sqlite3.Error):
                self._json(503, {"error": "Workshop fingerprint index is unavailable"})
                return
            self._json(200, {"status": "ok", "coverage": coverage,
                             "fingerprint_version": SEARCH_INDEX_VERSION})

        def do_POST(self):
            if not self._authorized():
                self._json(401, {"error": "Authentication required"})
                return
            if self.path != "/v1/scan":
                self._json(404, {"error": "Unknown endpoint"})
                return
            if self.headers.get("Content-Type", "").split(";", 1)[0].strip().lower() != "application/json":
                self._json(415, {"error": "Send fingerprint JSON as application/json"})
                return
            raw_size = self.headers.get("Content-Length")
            if raw_size is None:
                self._json(411, {"error": "Content-Length is required"})
                return
            try:
                size = int(raw_size)
            except ValueError:
                self._json(400, {"error": "Invalid Content-Length"})
                return
            if size <= 0:
                self._json(400, {"error": "Empty fingerprint request"})
                return
            if size > MAX_FINGERPRINT_BYTES:
                self._json(413, {"error": "Fingerprint exceeds 64 MiB limit"})
                return
            if not scan_slots.acquire(blocking=False):
                self._json(429, {"error": "Search is busy; retry shortly"})
                return
            try:
                self.connection.settimeout(30)
                raw = self.rfile.read(size)
                if len(raw) != size:
                    self._json(400, {"error": "Incomplete request body"})
                    return
                try:
                    data = json.loads(raw)
                    fp = _validate_fingerprint(data)
                except (UnicodeDecodeError, json.JSONDecodeError, ValueError) as exc:
                    self._json(422, {"error": str(exc)})
                    return
                try:
                    with closing(_read_only_db(db_path)) as db:
                        result = scan_fingerprint(db, fp)
                except (OSError, sqlite3.Error):
                    self._json(503, {"error": "Workshop fingerprint index is unavailable"})
                    return
                self._json(200, _public_result(result))
            except socket.timeout:
                self._json(408, {"error": "Fingerprint request timed out"})
            finally:
                scan_slots.release()

    class SearchServer(ThreadingHTTPServer):
        daemon_threads = True

        def __init__(self, *args, **kwargs):
            self._connection_slots = threading.BoundedSemaphore(MAX_CONNECTION_THREADS)
            super().__init__(*args, **kwargs)

        def get_request(self):
            connection, address = super().get_request()
            # Bound the time an accepted connection may spend sending headers.
            connection.settimeout(15)
            return connection, address

        def process_request(self, request, client_address):
            if not self._connection_slots.acquire(blocking=False):
                self.shutdown_request(request)
                return
            try:
                super().process_request(request, client_address)
            except BaseException:
                self._connection_slots.release()
                raise

        def process_request_thread(self, request, client_address):
            try:
                super().process_request_thread(request, client_address)
            finally:
                self._connection_slots.release()

    return SearchServer((host, port), Handler)


def serve_search(db_path, host="127.0.0.1", port=8766):
    with make_server(db_path, host, port) as server:
        address = server.server_address
        print(f"Stormworks fingerprint service ready at http://{address[0]}:{address[1]}/")
        server.serve_forever()
