"""Order-independent, translation-independent local geometry fingerprints."""

from collections import Counter, defaultdict
from decimal import Decimal, InvalidOperation
from functools import lru_cache
from hashlib import sha256
import json
from pathlib import Path
import re
import xml.etree.ElementTree as ET

from .winnowing import spatial_winnow
from .semantics import semantic_fingerprints

MAX_XML_BYTES = 64 * 1024 * 1024
MAX_COMPONENTS = 500_000
LEGACY_IGNORED_ATTRS = {"id", "unique_id", "name", "username", "steam_id", "data_version",
                        "bodies_id", "sc", "color", "colour", "paint", "created", "modified"}
IGNORED_ATTRS = {"id", "unique_id", "name", "username", "steam_id", "data_version",
                 "bodies_id", "sc", "bc", "ac", "color", "colour", "paint",
                 "custom_name", "description", "created", "modified", "id_counter",
                 "id_counter_node", "component_id", "built_slot_index", "transform_index"}
ATTRIBUTE = re.compile(rb'''(\s+)([^\s=/>]+)(\s*=\s*)(["'])(.*?)\4''', re.S)
NUMBER = re.compile(r"[+-]?(?:\d+(?:\.\d*)?|\.\d+)(?:[eE][+-]?\d+)?\Z")
SEPARATE_FEATURES = {"microprocessor_definition", "logic_slots", "logic_node_links",
                     "logic_node_link"}


def _digest(value):
    return sha256(value.encode("utf-8")).hexdigest()[:24]


def _tag(tag):
    return tag.rsplit("}", 1)[-1]


def _normalize_value(value, name=""):
    """Normalize alternate spellings of the same finite decimal value."""
    value = value.strip()
    if any(word in name.lower() for word in
           ("text", "name", "script", "code", "label", "description", "lua", "string")):
        return value
    parts = [part.strip() for part in value.split(",")]
    if not parts or not all(len(part) <= 40 and NUMBER.fullmatch(part) for part in parts):
        return value
    result = []
    for part in parts:
        try:
            number = Decimal(part)
        except InvalidOperation:
            return value
        if abs(number.adjusted()) > 30:
            return value
        result.append("0" if not number else format(number.normalize(), "f"))
    return ",".join(result)


@lru_cache(maxsize=4096)
def _cached_value(value, name):
    return _normalize_value(value, name)


def _value(value, name=""):
    # Common block types, orientations, and numeric settings recur thousands
    # of times. Do not retain large scripts or user text in the cache.
    return (_cached_value(value, name) if len(value) <= 128 else
            _normalize_value(value, name))


def _repair_stormworks_xml(data):
    """Make Stormworks' numeric and repeated attributes legal XML.

    The game emits matrix names such as 00= and occasionally repeats names such
    as value=. Only attribute names inside start tags are changed. Values,
    script text, comments, and component order are left intact.
    """
    out = bytearray()
    cursor = 0
    while True:
        start = data.find(b"<", cursor)
        if start < 0:
            out.extend(data[cursor:])
            break
        out.extend(data[cursor:start])
        if data.startswith(b"<!--", start):
            end = data.find(b"-->", start + 4)
            end = len(data) if end < 0 else end + 3
            out.extend(data[start:end])
            cursor = end
            continue
        if data.startswith(b"<![CDATA[", start):
            end = data.find(b"]]>", start + 9)
            end = len(data) if end < 0 else end + 3
            out.extend(data[start:end])
            cursor = end
            continue
        end = start + 1
        quote = 0
        while end < len(data):
            char = data[end]
            if quote:
                if char == quote:
                    quote = 0
            elif char in (34, 39):
                quote = char
            elif char == 62:  # > outside a quoted attribute
                end += 1
                break
            end += 1
        tag = data[start:end]
        if tag.startswith((b"</", b"<!", b"<?")):
            out.extend(tag)
        else:
            seen = set()
            previous = 0
            for match in ATTRIBUTE.finditer(tag):
                name = match.group(2)
                if name[:1].isdigit():
                    name = b"sw_" + name
                base = name
                suffix = 2
                while name in seen:
                    name = base + b"__dup" + str(suffix).encode("ascii")
                    suffix += 1
                seen.add(name)
                out.extend(tag[previous:match.start(2)])
                out.extend(name)
                previous = match.end(2)
            out.extend(tag[previous:])
        cursor = end
    return bytes(out)


def _signature(node):
    """Canonical component content; position and cosmetic metadata are omitted."""
    tag = _tag(node.tag)
    attrs = sorted((_tag(k), _value(v, _tag(k))) for k, v in node.attrib.items()
                   if _tag(k) not in IGNORED_ATTRS and _tag(k) not in ("x", "y", "z"))
    children = sorted(_signature(c) for c in node
                      if _tag(c.tag) not in SEPARATE_FEATURES and _tag(c.tag) != "vp")
    content = " ".join((node.text or "").split())
    # Scripts can be large; their content matters but should not inflate a token.
    if len(content) > 128:
        content = _digest(content)
    return json.dumps((tag, attrs, content, children), separators=(",", ":"))


def _legacy_signature(node):
    """Preserve search compatibility while older SQLite entries are rebuilt."""
    tag = _tag(node.tag)
    attrs = sorted((k, v) for k, v in node.attrib.items()
                   if k not in LEGACY_IGNORED_ATTRS and k not in ("x", "y", "z"))
    children = sorted(_legacy_signature(c) for c in node if _tag(c.tag) != "vp")
    content = " ".join((node.text or "").split())
    if len(content) > 128:
        content = _digest(content)
    return json.dumps((tag, attrs, content, children), separators=(",", ":"))


def fingerprint_bytes(data, legacy=False):
    if len(data) > MAX_XML_BYTES:
        raise ValueError("XML exceeds 64 MiB limit")
    if re.search(rb"<!\s*(DOCTYPE|ENTITY)\b", data, re.I):
        raise ValueError("DTD and entities are not accepted")
    try:
        root = ET.fromstring(data)
    except ET.ParseError as exc:
        repaired = _repair_stormworks_xml(data)
        try:
            root = ET.fromstring(repaired)
        except ET.ParseError as second:
            raise ValueError(f"Invalid XML: {second}") from second
    if _tag(root.tag) != "vehicle":
        raise ValueError("Expected a Stormworks <vehicle> XML root")
    locations_by_body = []
    bodies = ([root] if legacy else
              [node for node in root.iter() if _tag(node.tag) == "body"] or [root])
    count = 0
    for body in bodies:
        locations = defaultdict(list)
        for comp in body.iter():
            if _tag(comp.tag) != "c":
                continue
            vp = next((n for n in comp.iter() if _tag(n.tag) == "vp"), None)
            if vp is None:
                continue
            try:
                xyz = tuple(int(vp.get(axis, "")) for axis in ("x", "y", "z"))
            except ValueError:
                continue
            locations[xyz].append(_digest(_legacy_signature(comp) if legacy else
                                          _signature(comp)))
            count += 1
            if count > MAX_COMPONENTS:
                raise ValueError("Vehicle exceeds 500,000 component limit")
        locations_by_body.append(locations)
    semantic = None if legacy else semantic_fingerprints(root)
    if count < 3 and not (semantic and
                          (semantic["micro_features"] or semantic["logic_features"])):
        raise ValueError("No usable Stormworks component geometry found")
    features = Counter()
    samples = {}
    winnow_features = Counter()
    winnow_samples = {}
    # Radius-two neighborhoods preserve geometry after XML component reorder,
    # translation and cosmetic metadata edits. Bound work to nearby grid cells.
    offsets = [(x, y, z) for x in range(-2, 3) for y in range(-2, 3)
               for z in range(-2, 3) if 0 < abs(x) + abs(y) + abs(z) <= 2]
    for locations in locations_by_body:
        if not legacy:
            winnowed, positions = spatial_winnow(locations)
            winnow_features.update(winnowed)
            for key, xyz in positions.items():
                winnow_samples.setdefault(key, xyz)
        for (x, y, z), anchors in locations.items():
            neighbors = []
            for dx, dy, dz in offsets:
                for sig in locations.get((x + dx, y + dy, z + dz), ()):
                    neighbors.append((dx, dy, dz, sig))
            if len(neighbors) < 2:
                continue
            neighbors.sort()
            for anchor in anchors:
                key = _digest(json.dumps((anchor, neighbors), separators=(",", ":")))
                features[key] += 1
                samples.setdefault(key, (x, y, z))
    if not features and not (semantic and
                             (semantic["micro_features"] or semantic["logic_features"])):
        raise ValueError("Too few adjacent components for a reliable comparison")
    if legacy:
        return {"components": count, "features": features, "samples": samples}
    return {"components": count, "features": features, "samples": samples,
            "winnow_features": winnow_features, "winnow_samples": winnow_samples,
            **semantic}


def fingerprint_file(path, legacy=False):
    path = Path(path)
    if path.stat().st_size > MAX_XML_BYTES:
        raise ValueError("XML exceeds 64 MiB limit")
    return fingerprint_bytes(path.read_bytes(), legacy=legacy)
