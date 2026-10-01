"""Compact correspondence labels for every vehicle preview component.

The labels describe aligned grid positions, not a plagiarism verdict. They
occupy one byte per component; integer records are sorted for matching.
"""

from collections import defaultdict
from collections.abc import Sequence
from dataclasses import dataclass
import math


LABELS = ("unverified", "matched", "changed", "unmatched")
_CODES = {name: index for index, name in enumerate(LABELS)}


class LabelSequence(Sequence):
    """String labels backed by one byte per vehicle component."""

    def __init__(self, codes):
        self.codes = bytes(codes)

    def __len__(self):
        return len(self.codes)

    def __getitem__(self, index):
        if isinstance(index, slice):
            return tuple(LABELS[code] for code in self.codes[index])
        return LABELS[self.codes[index]]

    def __iter__(self):
        for code in self.codes:
            yield LABELS[code]

    def __eq__(self, other):
        if isinstance(other, LabelSequence):
            return self.codes == other.codes
        if isinstance(other, (tuple, list)):
            return len(self) == len(other) and all(a == b for a, b in zip(self, other))
        return NotImplemented

    def count(self, value):
        code = _CODES.get(value)
        return self.codes.count(code) if code is not None else 0

    def __repr__(self):
        return f"LabelSequence({len(self.codes)} labels)"


@dataclass(frozen=True)
class MatchOverlay:
    input_labels: LabelSequence
    match_labels: LabelSequence
    offset: tuple[int, int, int] | None
    matched_count: int = 0


def _points(sample):
    return sample.points if sample is not None else ()


def _type(point):
    value = point[4] if len(point) > 4 and point[4] else point[3]
    return str(value).strip().casefold()


def _has_types(sample):
    kinds = getattr(sample, "kinds", None)
    if kinds is not None:
        return all(bool(kind) for _category, kind in kinds)
    return all(len(point) > 4 and bool(point[4]) for point in sample.points)


def _anchor_position(value):
    if not isinstance(value, (tuple, list)) or len(value) != 3:
        return None
    if any(isinstance(axis, bool) or not isinstance(axis, int) for axis in value):
        return None
    return tuple(value)


def _evidence_offset(evidence):
    """Find a translation backed by two distinct structural anchors."""
    anchors = defaultdict(set)
    for entry in evidence or ():
        if not isinstance(entry, dict) or entry.get("channel") not in (
                "geometry", "winnowing"):
            continue
        query = _anchor_position(entry.get("query_position"))
        workshop = _anchor_position(entry.get("workshop_position"))
        if query is None or workshop is None:
            continue
        offset = tuple(query[axis] - workshop[axis] for axis in range(3))
        anchors[offset].add((query, workshop))
    supported = []
    for offset, pairs in anchors.items():
        if (len({query for query, _ in pairs}) >= 2 and
                len({workshop for _, workshop in pairs}) >= 2):
            supported.append((len(pairs), offset))
    if not supported:
        return None
    supported.sort(reverse=True)
    if len(supported) > 1 and supported[0][0] == supported[1][0]:
        return None
    return supported[0][1]


def _geometry_offset(input_sample, workshop_sample):
    """Try one whole-shape translation when extents and type data agree."""
    if (input_sample.components < 4 or workshop_sample.components < 4 or
            not _has_types(input_sample) or not _has_types(workshop_sample)):
        return None
    try:
        input_low, input_high = input_sample.bounds
        match_low, match_high = workshop_sample.bounds
        if any(input_high[axis] - input_low[axis] !=
               match_high[axis] - match_low[axis] for axis in range(3)):
            return None
        return tuple(input_low[axis] - match_low[axis] for axis in range(3))
    except (AttributeError, IndexError, TypeError):
        return None


def _kind_codes(input_sample, workshop_sample):
    """Give component types the same small integer on both sides."""
    codes = {}
    for sample in (input_sample, workshop_sample):
        kinds = getattr(sample, "kinds", None)
        values = ((kind or category for category, kind in kinds)
                  if kinds is not None else
                  (_type(point) for point in sample.points))
        for value in values:
            value = str(value).strip().casefold()
            if value not in codes:
                codes[value] = len(codes)
    return codes


def _kind_set(sample):
    kinds = getattr(sample, "kinds", None)
    if kinds is not None:
        return {str(kind or category).strip().casefold()
                for category, kind in kinds}
    return {_type(point) for point in sample.points}


def _classify(input_sample, workshop_sample, offset, cancelled=None):
    """Sort integer keys, pair duplicate types, then mark changed positions."""
    input_points = _points(input_sample)
    match_points = _points(workshop_sample)
    count_input, count_match = len(input_points), len(match_points)
    input_codes = bytearray([3]) * count_input
    match_codes = bytearray([3]) * count_match
    kind_codes = _kind_codes(input_sample, workshop_sample)
    kind_count = max(1, len(kind_codes))
    lows = tuple(min(input_sample.bounds[0][axis],
                     workshop_sample.bounds[0][axis] + offset[axis])
                 for axis in range(3))
    highs = tuple(max(input_sample.bounds[1][axis],
                      workshop_sample.bounds[1][axis] + offset[axis])
                  for axis in range(3))
    span_y = highs[1] - lows[1] + 1
    span_z = highs[2] - lows[2] + 1
    index_bits = max(1, max(count_input, count_match).bit_length())
    mask = (1 << index_bits) - 1

    def records(points, shift):
        result = []
        append = result.append
        sx, sy, sz = shift
        for index, point in enumerate(points):
            if cancelled is not None and index % 4096 == 0 and cancelled():
                raise InterruptedError("Preview comparison cancelled")
            x, y, z = point[:3]
            position = (((x + sx - lows[0]) * span_y + y + sy - lows[1]) *
                        span_z + z + sz - lows[2])
            kind = kind_codes[_type(point)]
            append(((position * kind_count + kind) << index_bits) | index)
        result.sort()
        return result

    input_records = records(input_points, (0, 0, 0))
    match_records = records(match_points, offset)
    i = j = matched = 0
    while i < count_input and j < count_match:
        if cancelled is not None and (i + j) % 8192 == 0 and cancelled():
            raise InterruptedError("Preview comparison cancelled")
        left = input_records[i] >> index_bits
        right = match_records[j] >> index_bits
        if left == right:
            input_codes[input_records[i] & mask] = 1
            match_codes[match_records[j] & mask] = 1
            matched += 1
            i += 1
            j += 1
        elif left < right:
            i += 1
        else:
            j += 1

    remaining_input = sorted((((record >> index_bits) // kind_count) << index_bits) |
                             (record & mask) for record in input_records
                             if input_codes[record & mask] != 1)
    remaining_match = sorted((((record >> index_bits) // kind_count) << index_bits) |
                             (record & mask) for record in match_records
                             if match_codes[record & mask] != 1)
    i = j = 0
    while i < len(remaining_input) and j < len(remaining_match):
        if cancelled is not None and (i + j) % 8192 == 0 and cancelled():
            raise InterruptedError("Preview comparison cancelled")
        left = remaining_input[i] >> index_bits
        right = remaining_match[j] >> index_bits
        if left == right:
            input_codes[remaining_input[i] & mask] = 2
            match_codes[remaining_match[j] & mask] = 2
            i += 1
            j += 1
        elif left < right:
            i += 1
        else:
            j += 1
    return LabelSequence(input_codes), LabelSequence(match_codes), matched


def match_samples(input_sample, workshop_sample, evidence=(), cancelled=None):
    """Colour all components using verified spatial alignment when available."""
    count_input = len(_points(input_sample))
    count_match = len(_points(workshop_sample))
    unknown = MatchOverlay(LabelSequence(bytes(count_input)),
                           LabelSequence(bytes(count_match)), None)
    if not count_input or not count_match:
        return unknown
    if cancelled is not None and cancelled():
        raise InterruptedError("Preview comparison cancelled")
    if not _kind_set(input_sample).intersection(_kind_set(workshop_sample)):
        return unknown
    evidence_offset = _evidence_offset(evidence)
    offset = evidence_offset
    if offset is None:
        offset = _geometry_offset(input_sample, workshop_sample)
    if offset is None:
        return unknown
    input_labels, match_labels, matched = _classify(
        input_sample, workshop_sample, offset, cancelled)
    if not matched:
        return unknown
    if evidence_offset is None and (
            matched < 4 or
            matched < math.ceil(.8 * count_input) or
            matched < math.ceil(.8 * count_match)):
        return unknown
    return MatchOverlay(input_labels, match_labels, offset, matched)
