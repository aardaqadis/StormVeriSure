"""Explicit, fingerprint-only queries to a shared Stormworks search index.

The selected XML is parsed locally. Only structural hashes, counts, and sample
positions are sent to the search server; those still reveal some information
about a creation, so callers should make remote searching an explicit choice.
"""

import ipaddress
import json
import math
from pathlib import Path
import socket
from urllib import error, parse, request

from .fingerprint import fingerprint_file
from .search_index import SEARCH_INDEX_VERSION


MAX_REQUEST_BYTES = 64 * 1024 * 1024
MAX_RESPONSE_BYTES = 8 * 1024 * 1024
DEFAULT_TIMEOUT = 30


class RemoteSearchError(RuntimeError):
    """A remote index was unavailable or returned an unusable response."""


class _NoRedirects(request.HTTPRedirectHandler):
    def redirect_request(self, req, fp, code, msg, headers, newurl):
        # In particular, do not forward an access token or fingerprint payload
        # to an unexpected host after a redirect.
        return None


def _is_loopback(host):
    if host.lower().rstrip(".") == "localhost":
        return True
    try:
        return ipaddress.ip_address(host).is_loopback
    except ValueError:
        return False


def _api_url(endpoint, route):
    try:
        url = parse.urlsplit(str(endpoint).strip())
        # Reading .port also rejects malformed ports instead of letting the
        # transport report an opaque connection failure later.
        _ = url.port
    except ValueError as exc:
        raise RemoteSearchError(f"Invalid search server URL: {exc}") from exc
    if (url.scheme.lower() not in ("http", "https") or not url.hostname or
            url.username is not None or url.password is not None or
            url.query or url.fragment):
        raise RemoteSearchError(
            "Enter a search server URL such as https://example.com; "
            "do not include credentials, a query, or a fragment.")
    if url.scheme.lower() == "http" and not _is_loopback(url.hostname):
        raise RemoteSearchError(
            "A remote search server must use HTTPS. Plain HTTP is allowed only on this computer.")
    base_path = url.path.rstrip("/")
    for suffix in ("/v1/scan", "/v1/coverage", "/health"):
        if base_path.endswith(suffix):
            base_path = base_path[:-len(suffix)]
            break
    return parse.urlunsplit((url.scheme, url.netloc, base_path + route, "", ""))


def _decode_response(response):
    raw = response.read(MAX_RESPONSE_BYTES + 1)
    if len(raw) > MAX_RESPONSE_BYTES:
        raise RemoteSearchError("Search server returned an oversized response.")
    try:
        result = json.loads(raw.decode("utf-8"))
    except (UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise RemoteSearchError("Search server returned invalid JSON.") from exc
    if not isinstance(result, dict):
        raise RemoteSearchError("Search server returned an invalid result.")
    return result


def _request_json(url, *, data=None, token=None, timeout=DEFAULT_TIMEOUT):
    if not isinstance(timeout, (int, float)) or timeout <= 0:
        raise RemoteSearchError("Search timeout must be greater than zero.")
    if token is not None and (not isinstance(token, str) or "\r" in token or
                              "\n" in token or not token.strip()):
        raise RemoteSearchError("Search access token is invalid.")
    headers = {"Accept": "application/json"}
    if data is not None:
        headers["Content-Type"] = "application/json"
    if token is not None:
        headers["Authorization"] = "Bearer " + token.strip()
    req = request.Request(url, data=data, headers=headers,
                          method="POST" if data is not None else "GET")
    opener = request.build_opener(_NoRedirects())
    try:
        with opener.open(req, timeout=timeout) as response:
            return _decode_response(response)
    except error.HTTPError as exc:
        try:
            details = _decode_response(exc)
            message = details.get("error")
        except RemoteSearchError:
            message = None
        finally:
            exc.close()
        if not isinstance(message, str) or not message.strip():
            message = "Search server rejected the request."
        raise RemoteSearchError(f"{message} (HTTP {exc.code})") from exc
    except (error.URLError, socket.timeout, TimeoutError) as exc:
        reason = getattr(exc, "reason", exc)
        if isinstance(reason, (socket.timeout, TimeoutError)):
            raise RemoteSearchError(f"Search server timed out after {timeout:g} seconds.") from exc
        raise RemoteSearchError(f"Could not reach search server: {reason}") from exc


def _validate_coverage(coverage):
    if not isinstance(coverage, dict):
        raise RemoteSearchError("Search server response has no coverage information.")
    for key in ("indexed_items", "indexed_files", "known_items"):
        value = coverage.get(key)
        if isinstance(value, bool) or not isinstance(value, int) or value < 0:
            raise RemoteSearchError(f"Search server response has invalid {key} coverage.")
    for key in ("modern_search_files", "searched_files"):
        if key in coverage:
            value = coverage[key]
            if isinstance(value, bool) or not isinstance(value, int) or value < 0:
                raise RemoteSearchError(f"Search server response has invalid {key} coverage.")


def _nonnegative_integer(value):
    return type(value) is int and value >= 0


def _percentage(value):
    # Check the range before converting to float; a valid JSON integer can be
    # large enough for math.isfinite() to raise OverflowError.
    return (type(value) in (int, float) and 0 <= value <= 100 and
            math.isfinite(value))


def _validate_match(match):
    """Validate fields the GUI renders or opens from an untrusted service."""
    if not isinstance(match, dict):
        raise RemoteSearchError("Search server returned an invalid match.")
    item_id = match.get("item_id")
    if not isinstance(item_id, str) or not item_id or len(item_id) > 256:
        raise RemoteSearchError("Search server returned an invalid Workshop item ID.")
    if match.get("title") is not None and not isinstance(match["title"], str):
        raise RemoteSearchError("Search server returned an invalid Workshop title.")
    for field in ("similarity_percent", "workshop_coverage_percent",
                  "combined_similarity_percent"):
        if not _percentage(match.get(field)):
            raise RemoteSearchError(f"Search server returned invalid {field}.")
    estimate = match.get("minhash_jaccard_estimate_percent")
    if estimate is not None and not _percentage(estimate):
        raise RemoteSearchError("Search server returned an invalid MinHash estimate.")
    if match.get("confidence") not in ("low", "medium", "high"):
        raise RemoteSearchError("Search server returned invalid match confidence.")
    for field in ("shared_neighborhoods", "rare_shared_neighborhoods"):
        if not _nonnegative_integer(match.get(field)):
            raise RemoteSearchError(f"Search server returned invalid {field}.")
    for field in ("partial_copy_evidence", "semantic_copy_evidence"):
        if field in match and type(match[field]) is not bool:
            raise RemoteSearchError(f"Search server returned invalid {field}.")

    channels = match.get("channels", {})
    if not isinstance(channels, dict):
        raise RemoteSearchError("Search server returned invalid matched data channels.")
    for name, channel in channels.items():
        if (not isinstance(name, str) or not isinstance(channel, dict) or
                not _nonnegative_integer(channel.get("shared")) or
                not _percentage(channel.get("query_coverage_percent"))):
            raise RemoteSearchError("Search server returned invalid matched data channels.")

    url = match.get("url")
    expected_url = (f"https://steamcommunity.com/sharedfiles/filedetails/?id={item_id}"
                    if item_id.isdigit() else None)
    if url is not None and (not isinstance(url, str) or url != expected_url):
        raise RemoteSearchError("Search server returned an invalid Workshop URL.")

    evidence = match.get("evidence")
    if not isinstance(evidence, list):
        raise RemoteSearchError("Search server returned invalid match evidence.")
    for entry in evidence:
        if not isinstance(entry, dict):
            raise RemoteSearchError("Search server returned invalid match evidence.")
        if not isinstance(entry.get("channel"), str) or not entry["channel"]:
            raise RemoteSearchError("Search server returned invalid evidence channel.")
        if not _nonnegative_integer(entry.get("matching_components")):
            raise RemoteSearchError("Search server returned invalid evidence count.")
        for field in ("query_position", "workshop_position"):
            position = entry.get(field)
            if position is not None and (not isinstance(position, list) or
                    len(position) != 3 or any(type(value) is not int for value in position)):
                raise RemoteSearchError("Search server returned invalid evidence position.")


def _validate_scan_result(result):
    if (not isinstance(result.get("status"), str) or not result["status"] or
            not isinstance(result.get("suspicion_level"), str) or
            not result["suspicion_level"] or
            not isinstance(result.get("matches"), list)):
        raise RemoteSearchError("Search server returned an incomplete comparison result.")
    _validate_coverage(result.get("coverage"))
    best = result.get("best_match")
    if best is not None:
        _validate_match(best)
    for match in result["matches"]:
        _validate_match(match)
    if best is not None and (not result["matches"] or
                             best != result["matches"][0]):
        raise RemoteSearchError("Search server returned inconsistent best-match information.")
    if best is None and result["matches"]:
        raise RemoteSearchError("Search server returned matches without a best match.")


def scan_remote(path, endpoint, token=None, timeout=DEFAULT_TIMEOUT):
    """Compare a local XML against a shared index without uploading the XML.

    The returned dictionary follows :func:`stormcopy.index.scan`. Raises
    ``ValueError`` for an invalid vehicle and ``RemoteSearchError`` for an
    invalid endpoint, failed request, or malformed service response.
    """
    url = _api_url(endpoint, "/v1/scan")
    fp = fingerprint_file(Path(path))
    payload = {"fingerprint_version": SEARCH_INDEX_VERSION,
               "fingerprint": fp}
    encoded = json.dumps(payload, ensure_ascii=False, allow_nan=False,
                         separators=(",", ":")).encode("utf-8")
    if len(encoded) > MAX_REQUEST_BYTES:
        raise RemoteSearchError("This vehicle's fingerprints exceed the server's 64 MiB request limit.")
    result = _request_json(url, data=encoded, token=token, timeout=timeout)
    _validate_scan_result(result)
    return result


def remote_coverage(endpoint, token=None, timeout=DEFAULT_TIMEOUT):
    """Read how much of a shared index is currently searchable."""
    url = _api_url(endpoint, "/v1/coverage")
    result = _request_json(url, token=token, timeout=timeout)
    if result.get("status") != "ok":
        raise RemoteSearchError("Search server returned an invalid coverage status.")
    _validate_coverage(result.get("coverage"))
    return result["coverage"]
