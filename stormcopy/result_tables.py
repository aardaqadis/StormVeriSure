"""Presentation rows for local and shared Workshop comparison results.

The detector's percentages describe overlap with the *input* vehicle. They are
not a probability that a creation was copied. This module keeps that distinction
visible while giving the GUI a single, testable report model.
"""

from datetime import datetime, timezone
import math
from pathlib import Path


_CHANNELS = (
    ("geometry", "Structure"),
    ("winnowing", "Matching sections"),
    ("microcontrollers", "Microcontrollers"),
    ("logic", "Logic connections"),
    ("component_types", "Component types"),
)
_EVIDENCE_CHANNELS = {
    "geometry": "Structure",
    "winnowing": "Matching section",
    "microcontroller": "Microcontroller",
    "logic": "Logic connection",
}


def _number(value):
    return f"{value:,}" if type(value) is int and value >= 0 else "Unavailable"


def _percent(value):
    if type(value) not in (int, float) or not 0 <= value <= 100 or not math.isfinite(value):
        return "Unavailable"
    return f"{value:.1f}%"


def _seconds(value):
    if type(value) not in (int, float) or value < 0:
        return "Unavailable"
    try:
        finite = math.isfinite(value)
    except OverflowError:
        finite = False
    if not finite:
        return "Unavailable"
    return f"{value:.2f} s" if value < 1 else f"{value:.1f} s"


def _memory(value):
    if type(value) is not int or value < 0:
        return "Unavailable"
    return f"{value / (1024 * 1024):.1f} MiB (process working set)"


def _timestamp(value):
    if value is None:
        return "Unavailable"
    try:
        seconds = int(value)
        if 0 <= seconds <= 253402300799:
            return datetime.fromtimestamp(seconds, timezone.utc).astimezone().strftime(
                "%Y-%m-%d %H:%M:%S %Z")
    except (ValueError, TypeError, OverflowError, OSError):
        pass
    return _text(value)


def _text(value, fallback="Unavailable"):
    if not isinstance(value, str):
        return fallback
    compact = " ".join(value.split())
    return compact or fallback


def _position(value):
    if (not isinstance(value, (list, tuple)) or len(value) != 3 or
            any(type(part) not in (int, float) for part in value)):
        return "Unavailable"
    return "(" + ", ".join(str(part) for part in value) + ")"


def _channels(match):
    value = match.get("channels")
    return value if isinstance(value, dict) else {}


def certainty_for(result, match=None):
    """Return one of the six user-facing certainty labels.

    A controller-only result is labelled separately so its zero geometry
    coverage is not mistaken for an absence of shared data. The remaining
    labels reflect the detector's heuristic confidence, not an accusation.
    """
    if match is None:
        match = result.get("best_match")
    if not match:
        if result.get("status") == "no index":
            return "Uncertain"
        coverage = result.get("coverage") or {}
        searched = coverage.get("searched_files", coverage.get("indexed_files", 0))
        indexed = coverage.get("indexed_files", 0)
        return "Uncertain" if not searched or searched < indexed else "No match found"
    channels = _channels(match)
    micro_data = channels.get("microcontrollers")
    geometry_data = channels.get("geometry")
    micro = (micro_data if isinstance(micro_data, dict) else {}).get("shared", 0)
    geometry = (geometry_data if isinstance(geometry_data, dict) else {}).get(
        "shared", match.get("shared_neighborhoods", 0))
    if micro and not geometry:
        return "Holds some matched microcontrollers"
    return {"high": "High", "medium": "Medium", "low": "Low"}.get(
        match.get("confidence"), "Uncertain")


def _matched_data(match):
    if not match:
        return "No reportable shared structure or controller patterns"
    channels = _channels(match)
    parts = []
    for key, label in _CHANNELS:
        data = channels.get(key)
        data = data if isinstance(data, dict) else {}
        shared = data.get("shared", 0)
        if type(shared) is int and shared > 0:
            parts.append(f"{label}: {shared:,} ({_percent(data.get('query_coverage_percent'))} of input)")
    if parts:
        return "; ".join(parts)
    shared = match.get("shared_neighborhoods", 0)
    if type(shared) is int and shared > 0:
        return f"Structure: {shared:,} shared neighborhoods"
    return "No detailed channel counts available"


def _source_label(source, match):
    location = "shared index" if source == "shared" else "local index"
    if not match:
        return f"No matched source ({location})"
    if source == "other" or match.get("source") == "other":
        return f"Other indexed source ({location})"
    url = match.get("url") or ""
    item_id = match.get("item_id") or ""
    if match and not (str(url).startswith("https://steamcommunity.com/sharedfiles/") or
                      str(item_id).isdigit() or match.get("source") == "steam"):
        return f"Other indexed source ({location})"
    return f"Steam Workshop ({location})"


def _candidate_rows(result):
    best = result.get("best_match") or {}
    best_id = best.get("item_id")
    seen = {best_id} if best_id is not None else set()
    rows = []
    for match in result.get("matches") or ():
        item_id = match.get("item_id")
        if item_id in seen:
            continue
        seen.add(item_id)
        rows.append({
            "rank": len(rows) + 2,
            "title": _text(match.get("title"), "Untitled Workshop item"),
            "item_id": str(item_id or "Unavailable"),
            "certainty": certainty_for(result, match),
            "percent": _percent(match.get("similarity_percent")),
            "matched_data": _matched_data(match),
            "url": _text(match.get("url"), "Unavailable"),
        })
        if len(rows) == 3:
            break
    return rows


def _channel_rows(best):
    channels = _channels(best)
    rows = []
    for key, label in _CHANNELS:
        data = channels.get(key)
        data = data if isinstance(data, dict) else {}
        rows.append({
            "channel": label,
            "shared": _number(data.get("shared")),
            "input_coverage": _percent(data.get("query_coverage_percent")),
        })
    return rows


def _evidence_rows(best):
    rows = []
    for entry in best.get("evidence") or ():
        count = entry.get("indexed_occurrences")
        scope = entry.get("occurrence_scope")
        rarity = (f"{_number(count)} sampled index hits" if scope == "sampled" else
                  f"{_number(count)} indexed files" if scope == "full" else
                  f"{_number(count)} index hits" if count is not None else "Unavailable")
        rows.append({
            "channel": _EVIDENCE_CHANNELS.get(entry.get("channel"),
                                              _text(entry.get("channel"))),
            "matches": _number(entry.get("matching_components")),
            "input_position": _position(entry.get("query_position")),
            "workshop_position": _position(entry.get("workshop_position")),
            "rarity": rarity,
        })
    return rows


def build_result_tables(result, *, xml_path, seconds, memory_bytes=None, source="local",
                        endpoint=None, description=None, scanned_at=None,
                        app_uptime_seconds=None):
    """Convert a scan result to table rows without changing the scan result.

    ``memory_bytes`` is the application's current process working set, when
    available; it is not memory allocated by this individual comparison.
    ``description`` may be supplied by a metadata lookup. The current
    fingerprint index does not store Workshop descriptions.
    """
    best = result.get("best_match") or {}
    coverage = result.get("coverage") or {}
    certainty = certainty_for(result, best)
    item_id = best.get("item_id")
    title = _text(best.get("title"), "Untitled Workshop item") if best else "No match in index"
    matched_vehicle = f"{title} (ID {item_id})" if item_id else title
    vehicle_description = (description if description is not None else
                           best.get("description"))
    if scanned_at is None:
        scanned_at = datetime.now().astimezone()
    if isinstance(scanned_at, datetime):
        scanned_at = scanned_at.strftime("%Y-%m-%d %H:%M:%S %Z")
    indexed_files = coverage.get("indexed_files", 0)
    searched_files = coverage.get("searched_files", indexed_files)
    summary = [
        ("XML input", str(Path(xml_path))),
        ("Certainty level", certainty),
        ("Of which percent", f"{_percent(best.get('similarity_percent'))} of input structure"
         if best else "Unavailable"),
        ("Of which data matches", _matched_data(best)),
        ("Matched vehicle", matched_vehicle),
        ("Matched vehicle link", _text(best.get("url")) if best else "Unavailable"),
        ("Matched vehicle description",
         _text(vehicle_description, "Not available in current index")),
        ("Time taken to establish", _seconds(seconds)),
        ("Memory allocation", _memory(memory_bytes)),
        ("From which source", _source_label(source, best)),
        ("Result status", _text(result.get("status"), "Unknown")),
        ("Suspicion status", _text(result.get("suspicion_level"), "Unknown")),
    ]
    if best:
        summary.extend([
            ("Workshop vehicle covered", _percent(best.get("workshop_coverage_percent"))),
            ("Multi-signal overlap", _percent(best.get("combined_similarity_percent"))),
            ("Shared structural neighborhoods", _number(best.get("shared_neighborhoods"))),
            ("Rare structural neighborhoods", _number(best.get("rare_shared_neighborhoods"))),
            ("Largest matching cluster", _number(best.get("largest_matching_cluster"))),
            ("Matching section indicated", "Yes" if best.get("partial_copy_evidence") else "No"),
            ("Controller or logic overlap indicated",
             "Yes" if best.get("semantic_copy_evidence") else "No"),
        ])
    program = [
        ("Scan completed", _text(scanned_at)),
        ("Search location", _text(endpoint) if source == "shared" else "This computer"),
        ("Indexed Workshop items", _number(coverage.get("indexed_items"))),
        ("Indexed vehicle XML files", _number(indexed_files)),
        ("Vehicle XML files included in search", _number(searched_files)),
        ("Known Workshop item IDs", _number(coverage.get("known_items"))),
        ("Workshop discovery complete", "Yes" if coverage.get("discovery_complete") else
         "No" if coverage.get("discovery_complete") is False else "Unknown"),
        ("Last Workshop discovery", _timestamp(coverage.get("last_discovery"))),
        ("App uptime", _seconds(app_uptime_seconds)),
        ("Search method", "MinHash/LSH candidate search, structural reranking"),
        ("Report note", _text(result.get("note"),
                               "Only indexed accessible files were checked. Similarity is heuristic.")),
    ]
    return {
        "certainty": certainty,
        "summary": summary,
        "candidates": _candidate_rows(result),
        "channels": _channel_rows(best) if best else [],
        "evidence": _evidence_rows(best) if best else [],
        "program": program,
    }
