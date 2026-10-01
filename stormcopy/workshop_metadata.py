"""Small, cached public Steam Workshop description lookup for scan results.

Only a ranked Workshop match should be looked up. This endpoint returns item
metadata, not the vehicle XML, and does not require a Steam Web API key.
"""

from collections import OrderedDict
from html import unescape
from html.parser import HTMLParser
import json
import re
import threading
import time
import urllib.error
import urllib.parse
import urllib.request

from .steam import APP_ID, DETAILS_URL


_MAX_RESPONSE_BYTES = 1024 * 1024
_MAX_DESCRIPTION_CHARS = 4000
_CACHE_CAPACITY = 256
_SUCCESS_TTL = 24 * 3600
_EMPTY_TTL = 3600
_ERROR_TTL = 120
_cache = OrderedDict()
_cache_lock = threading.Lock()


class _PlainText(HTMLParser):
    """Preserve readable paragraphs while discarding HTML formatting."""

    def __init__(self):
        super().__init__(convert_charrefs=True)
        self.parts = []
        self.hidden = 0

    def handle_starttag(self, tag, attrs):
        if tag in ("script", "style"):
            self.hidden += 1
        elif not self.hidden and tag in ("br", "p", "div", "li", "h1", "h2", "h3"):
            self.parts.append("\n")

    def handle_endtag(self, tag):
        if tag in ("script", "style"):
            self.hidden = max(0, self.hidden - 1)
        elif not self.hidden and tag in ("p", "div", "li", "h1", "h2", "h3"):
            self.parts.append("\n")

    def handle_data(self, data):
        if not self.hidden:
            self.parts.append(data)


def _clean_description(value):
    if not isinstance(value, str) or not value.strip():
        return None
    # Steam descriptions usually contain BBCode. Image markup can hold very
    # long URLs that make no sense in a plain-text result.
    value = re.sub(r"\[img\].*?\[/img\]", "", value, flags=re.I | re.S)
    value = re.sub(r"\[/?(?:[a-z][\w]*|\*)(?:=[^\]\r\n]*)?\]", "", value,
                   flags=re.I)
    parser = _PlainText()
    parser.feed(unescape(value))
    parser.close()
    value = "".join(parser.parts)
    value = re.sub(r"[\x00-\x08\x0b\x0c\x0e-\x1f\x7f]", "", value)
    value = re.sub(r"[ \t]+", " ", value)
    value = re.sub(r"[ \t]*\n[ \t]*", "\n", value)
    value = re.sub(r"\n{3,}", "\n\n", value).strip()
    if not value:
        return None
    if len(value) > _MAX_DESCRIPTION_CHARS:
        value = value[:_MAX_DESCRIPTION_CHARS].rstrip() + "…"
    return value


def _steam_id(item_id):
    """Return a canonical uint64 Workshop ID, or None for local item names."""
    if not isinstance(item_id, (str, int)) or isinstance(item_id, bool):
        return None
    value = str(item_id)
    if not re.fullmatch(r"[0-9]{1,20}", value):
        return None
    number = int(value)
    return str(number) if 0 < number <= 2**64 - 1 else None


def _remember(item_id, description, ttl):
    with _cache_lock:
        _cache[item_id] = (time.monotonic() + ttl, description)
        _cache.move_to_end(item_id)
        while len(_cache) > _CACHE_CAPACITY:
            _cache.popitem(last=False)


def get_workshop_description(item_id, *, timeout=5.0):
    """Return a public Stormworks Workshop description, or None if unavailable.

    A single result is requested over HTTPS. Responses are cached in memory
    for a day; unavailable items and transient failures have shorter TTLs.
    Call from a worker thread because the network request can block briefly.
    """
    item_id = _steam_id(item_id)
    if item_id is None:
        return None
    now = time.monotonic()
    with _cache_lock:
        cached = _cache.get(item_id)
        if cached is not None and cached[0] > now:
            _cache.move_to_end(item_id)
            return cached[1]
        if cached is not None:
            del _cache[item_id]

    payload = urllib.parse.urlencode({"itemcount": 1,
                                      "publishedfileids[0]": item_id}).encode("ascii")
    request = urllib.request.Request(
        DETAILS_URL, data=payload,
        headers={"User-Agent": "stormcopy/0.2", "Accept": "application/json",
                 "Content-Type": "application/x-www-form-urlencoded"})
    try:
        with urllib.request.urlopen(request, timeout=timeout) as response:
            body = response.read(_MAX_RESPONSE_BYTES + 1)
        if len(body) > _MAX_RESPONSE_BYTES:
            raise ValueError("Workshop metadata response exceeded size limit")
        envelope = json.loads(body)
        if not isinstance(envelope, dict) or not isinstance(envelope.get("response"), dict):
            raise ValueError("Invalid Workshop metadata envelope")
        details = envelope["response"].get("publishedfiledetails", [])
        if not isinstance(details, list):
            raise ValueError("Invalid Workshop metadata response")
        description = None
        for item in details:
            if not isinstance(item, dict):
                continue
            if (_steam_id(item.get("publishedfileid")) != item_id or
                    str(item.get("result", 1)) != "1"):
                continue
            app_id = item.get("consumer_app_id", item.get("consumer_appid"))
            if str(app_id) != str(APP_ID):
                continue
            description = _clean_description(
                item.get("description") or item.get("short_description"))
            break
        _remember(item_id, description,
                  _SUCCESS_TTL if description is not None else _EMPTY_TTL)
        return description
    except urllib.error.HTTPError as exc:
        exc.close()
        _remember(item_id, None, _ERROR_TTL)
        return None
    except (urllib.error.URLError, OSError, TimeoutError, ValueError, TypeError):
        _remember(item_id, None, _ERROR_TTL)
        return None
