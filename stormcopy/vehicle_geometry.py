"""Compact, complete component positions for the local vehicle preview.

Stormworks vehicle XML may contain numeric matrix attributes that a strict XML
parser rejects.  This reads only the tags needed for placed components, with
the same file, tag, and component limits as the fingerprint/previous preview.
It retains every positioned component in XML order, not a reservoir sample.
"""

from array import array
from collections.abc import Sequence
import mmap
from pathlib import Path
import re

from .fingerprint import ATTRIBUTE, MAX_COMPONENTS, MAX_XML_BYTES


_MAX_TAG_BYTES = 2 * 1024 * 1024
_MAX_I64 = (1 << 63) - 1
_MIN_I64 = -(1 << 63)
_NAME = re.compile(rb"</?\s*([A-Za-z_][\w:.-]*)")
_FORBIDDEN_DECLARATION = re.compile(rb"<!\s*(DOCTYPE|ENTITY)\b", re.I)


def _iter_tags(source, cancelled=None):
    """Yield bounded tag slices while skipping comments and script CDATA."""
    size = len(source)
    position = 0
    tags_seen = 0
    while position < size:
        if cancelled is not None and not (tags_seen & 255) and cancelled():
            raise InterruptedError("Vehicle preview loading cancelled")
        tags_seen += 1
        start = source.find(b"<", position)
        if start < 0:
            return
        if source[start:start + 4] == b"<!--":
            end = source.find(b"-->", start + 4)
            if end < 0:
                raise ValueError("Unterminated XML comment")
            position = end + 3
            continue
        if source[start:start + 9] == b"<![CDATA[":
            end = source.find(b"]]>", start + 9)
            if end < 0:
                raise ValueError("Unterminated XML CDATA")
            position = end + 3
            continue
        quote = None
        end = start + 1
        while end < size:
            character = source[end]
            if quote is not None:
                if character == quote:
                    quote = None
            elif character in (34, 39):
                quote = character
            elif character == 62:
                break
            end += 1
            if cancelled is not None and not ((end - start) & 8191) and cancelled():
                raise InterruptedError("Vehicle preview loading cancelled")
            if end - start > _MAX_TAG_BYTES:
                raise ValueError("An XML tag is too large to preview")
        if end == size:
            raise ValueError("Unterminated XML tag")
        yield source[start:end + 1]
        position = end + 1


def _attributes(tag):
    return {match.group(2).lower(): match.group(5)
            for match in ATTRIBUTE.finditer(tag)}


def _category(kind):
    kind = kind.lower()
    if any(word in kind for word in
           ("micro", "logic", "controller", "computer", "button", "switch")):
        return "control"
    if any(word in kind for word in
           ("engine", "motor", "wheel", "prop", "pump", "piston", "rotor")):
        return "mechanical"
    if any(word in kind for word in
           ("sensor", "radar", "radio", "light", "electric", "battery")):
        return "electrical"
    return "structure"


class ComponentPoints(Sequence):
    """Read-only, lazy 5-tuples backed by packed numeric arrays.

    Tuples are materialized one at a time during iteration. Consumers should
    iterate or index rather than calling ``tuple(points)`` on a large vehicle.
    """

    __slots__ = ("_coordinates", "_kind_ids", "_kinds")

    def __init__(self, coordinates, kind_ids, kinds):
        self._coordinates = coordinates
        self._kind_ids = kind_ids
        self._kinds = kinds

    def __len__(self):
        return len(self._kind_ids)

    def __getitem__(self, index):
        if isinstance(index, slice):
            return tuple(self[i] for i in range(*index.indices(len(self))))
        count = len(self._kind_ids)
        if index < 0:
            index += count
        if not 0 <= index < count:
            raise IndexError(index)
        position = index * 3
        category, kind = self._kinds[self._kind_ids[index]]
        return (self._coordinates[position], self._coordinates[position + 1],
                self._coordinates[position + 2], category, kind)

    def __iter__(self):
        coordinates = self._coordinates
        kinds = self._kinds
        for index, kind_id in enumerate(self._kind_ids):
            position = index * 3
            category, kind = kinds[kind_id]
            yield (coordinates[position], coordinates[position + 1],
                   coordinates[position + 2], category, kind)

    def __eq__(self, other):
        if isinstance(other, ComponentPoints):
            return (self._coordinates == other._coordinates
                    and self._kind_ids == other._kind_ids
                    and self._kinds == other._kinds)
        if not isinstance(other, Sequence) or len(self) != len(other):
            return NotImplemented
        return all(left == right for left, right in zip(self, other))


class VehicleGeometry:
    """All positioned components, with interned kinds and compact coordinates."""

    __slots__ = ("coordinates", "kind_ids", "kinds", "bounds", "points")

    def __init__(self, coordinates, kind_ids, kinds, bounds):
        self.coordinates = coordinates
        self.kind_ids = kind_ids
        self.kinds = tuple(kinds)
        self.bounds = bounds
        self.points = ComponentPoints(coordinates, kind_ids, self.kinds)

    @property
    def components(self):
        return len(self.kind_ids)

    @property
    def sampled(self):
        # Compatibility with the previous preview's count field. Every
        # component is now retained, so this is the complete component count.
        return len(self.kind_ids)

    @property
    def storage_bytes(self):
        """Bytes in numeric arrays (excluding the usually small kind table)."""
        return (self.coordinates.buffer_info()[1] * self.coordinates.itemsize
                + self.kind_ids.buffer_info()[1] * self.kind_ids.itemsize)


def _append_coordinate(coordinates, value):
    if value < _MIN_I64 or value > _MAX_I64:
        raise ValueError("Component coordinate is outside the 64-bit range")
    try:
        coordinates.append(value)
    except OverflowError:
        # Most vehicles fit in 32-bit grid coordinates. Upgrade once if a
        # legitimate larger coordinate is encountered.
        if coordinates.typecode != "i":
            raise ValueError("Component coordinate is outside the 64-bit range") from None
        coordinates = array("q", coordinates)
        coordinates.append(value)
    return coordinates


def load_vehicle_geometry(path, cancelled=None):
    """Load every positioned ``<c>/<vp>`` component from a vehicle XML.

    Typical 500,000-component geometry uses about 7 MiB for its numeric data,
    plus a small table of distinct component kinds. No full XML tree or list
    of Python point tuples is retained. ``cancelled`` may be a zero-argument
    callable or an event with ``is_set``; cancellation raises InterruptedError.
    """
    if cancelled is not None:
        cancelled = cancelled if callable(cancelled) else cancelled.is_set
        if cancelled():
            raise InterruptedError("Vehicle preview loading cancelled")
    path = Path(path)
    size = path.stat().st_size
    if size > MAX_XML_BYTES:
        raise ValueError("XML exceeds 64 MiB limit")
    if size == 0:
        raise ValueError("Empty vehicle XML")

    coordinates = array("i")
    kind_ids = array("H")
    kinds = []
    interned = {}
    raw_cache = {}
    component_stack = []
    root_seen = False
    count = 0
    low_x = low_y = low_z = high_x = high_y = high_z = None

    with path.open("rb") as stream, mmap.mmap(stream.fileno(), 0,
                                                access=mmap.ACCESS_READ) as source:
        if len(source) > MAX_XML_BYTES:
            raise ValueError("XML exceeds 64 MiB limit")
        for tag in _iter_tags(source, cancelled):
            if tag.startswith((b"<?", b"<!")):
                if _FORBIDDEN_DECLARATION.match(tag):
                    raise ValueError("DTD and entities are not accepted")
                continue
            name_match = _NAME.match(tag)
            if name_match is None:
                continue
            name = name_match.group(1).rsplit(b":", 1)[-1].lower()
            closing = tag.startswith(b"</")
            if not root_seen:
                if closing or name != b"vehicle":
                    raise ValueError("Expected a Stormworks <vehicle> XML root")
                root_seen = True
            if closing:
                if name == b"c" and component_stack:
                    component_stack.pop()
                continue
            if name == b"c":
                attrs = _attributes(tag)
                raw_kind = (attrs.get(b"d") or attrs.get(b"type")
                            or attrs.get(b"t") or b"")[:80]
                component_stack.append(raw_kind)
                if tag.rstrip().endswith(b"/>"):
                    component_stack.pop()
            elif name == b"vp" and component_stack:
                attrs = _attributes(tag)
                try:
                    x, y, z = (int(attrs[axis]) for axis in (b"x", b"y", b"z"))
                except (KeyError, ValueError):
                    continue
                count += 1
                if count > MAX_COMPONENTS:
                    raise ValueError("Vehicle exceeds 500,000 component limit")
                coordinates = _append_coordinate(coordinates, x)
                coordinates = _append_coordinate(coordinates, y)
                coordinates = _append_coordinate(coordinates, z)
                raw_kind = component_stack[-1]
                kind_id = raw_cache.get(raw_kind)
                if kind_id is None:
                    kind = raw_kind.decode("utf-8", "replace").strip().casefold()
                    kind_id = interned.get(kind)
                    if kind_id is None:
                        kind_id = len(kinds)
                        interned[kind] = kind_id
                        kinds.append((_category(kind), kind))
                    raw_cache[raw_kind] = kind_id
                if len(kinds) > 0xFFFF and kind_ids.typecode == "H":
                    kind_ids = array("I", kind_ids)
                kind_ids.append(kind_id)
                if low_x is None:
                    low_x = high_x = x
                    low_y = high_y = y
                    low_z = high_z = z
                else:
                    low_x = min(low_x, x)
                    low_y = min(low_y, y)
                    low_z = min(low_z, z)
                    high_x = max(high_x, x)
                    high_y = max(high_y, y)
                    high_z = max(high_z, z)

    if cancelled is not None and cancelled():
        raise InterruptedError("Vehicle preview loading cancelled")
    if not root_seen:
        raise ValueError("Expected a Stormworks <vehicle> XML root")
    if not count:
        raise ValueError("No component positions found")
    bounds = ((low_x, low_y, low_z), (high_x, high_y, high_z))
    return VehicleGeometry(coordinates, kind_ids, kinds, bounds)
