"""Report-model checks for the GUI's comparison tables."""

import copy
import unittest

from stormcopy.result_tables import build_result_tables, certainty_for


def _match(item_id="123", *, confidence="high", geometry=40, micro=0):
    return {
        "item_id": item_id,
        "title": f"Vehicle {item_id}",
        "url": f"https://steamcommunity.com/sharedfiles/filedetails/?id={item_id}",
        "confidence": confidence,
        "similarity_percent": 82.5 if geometry else 0.0,
        "workshop_coverage_percent": 72.0,
        "combined_similarity_percent": 78.4,
        "shared_neighborhoods": geometry,
        "rare_shared_neighborhoods": 18 if geometry else 0,
        "largest_matching_cluster": 12 if geometry else 0,
        "partial_copy_evidence": bool(geometry),
        "semantic_copy_evidence": bool(micro),
        "channels": {
            "geometry": {"shared": geometry,
                         "query_coverage_percent": 82.5 if geometry else 0.0},
            "winnowing": {"shared": 6 if geometry else 0,
                          "query_coverage_percent": 64.0 if geometry else 0.0},
            "microcontrollers": {"shared": micro,
                                 "query_coverage_percent": 100.0 if micro else 0.0},
            "logic": {"shared": 1 if micro else 0,
                      "query_coverage_percent": 50.0 if micro else 0.0},
            "component_types": {"shared": 9, "query_coverage_percent": 60.0},
        },
        "evidence": [{"channel": "geometry", "matching_components": 8,
                      "indexed_occurrences": 1, "occurrence_scope": "sampled",
                      "query_position": [1, 2, 3], "workshop_position": [4, 5, 6]}],
    }


def _result(best, matches=None):
    return {
        "status": "strong overlap" if best else "no strong match in indexed set",
        "suspicion_level": "high" if best else "low within indexed set",
        "best_match": best,
        "matches": matches if matches is not None else ([best] if best else []),
        "coverage": {"indexed_items": 100, "known_items": 150,
                     "indexed_files": 125, "searched_files": 120,
                     "discovery_complete": False,
                     "last_discovery": "2026-09-29T12:00:00Z"},
        "note": "Only indexed files were checked.",
    }


class ResultTableTests(unittest.TestCase):
    def test_full_report_has_requested_fields_and_directional_percentage(self):
        best = _match(micro=2)
        result = _result(best)
        before = copy.deepcopy(result)
        tables = build_result_tables(
            result, xml_path="C:/vehicles/input.xml", seconds=2.34,
            memory_bytes=104857600, source="shared", endpoint="http://127.0.0.1:8766",
            description="A workshop vessel", scanned_at="2026-09-30 12:34:56",
            app_uptime_seconds=65)
        summary = dict(tables["summary"])
        program = dict(tables["program"])
        self.assertEqual(result, before)
        self.assertEqual(tables["certainty"], "High")
        self.assertEqual(summary["Of which percent"], "82.5% of input structure")
        self.assertIn("Microcontrollers: 2", summary["Of which data matches"])
        self.assertEqual(summary["Matched vehicle description"], "A workshop vessel")
        self.assertEqual(summary["Time taken to establish"], "2.3 s")
        self.assertEqual(summary["Memory allocation"],
                         "100.0 MiB (process working set)")
        self.assertEqual(summary["From which source"], "Steam Workshop (shared index)")
        self.assertEqual(program["Vehicle XML files included in search"], "120")
        self.assertEqual(program["Scan completed"], "2026-09-30 12:34:56")
        self.assertEqual(tables["channels"][2]["input_coverage"], "100.0%")
        self.assertEqual(tables["evidence"][0]["input_position"], "(1, 2, 3)")
        self.assertEqual(tables["evidence"][0]["rarity"], "1 sampled index hits")

    def test_controller_only_match_has_its_own_certainty_and_zero_geometry(self):
        best = _match(confidence="medium", geometry=0, micro=2)
        result = _result(best)
        tables = build_result_tables(result, xml_path="controller.xml", seconds=0.3)
        summary = dict(tables["summary"])
        self.assertEqual(tables["certainty"], "Holds some matched microcontrollers")
        self.assertEqual(summary["Of which percent"], "0.0% of input structure")
        self.assertIn("Microcontrollers: 2", summary["Of which data matches"])
        self.assertEqual(summary["Matched vehicle description"],
                         "Not available in current index")

    def test_three_distinct_alternatives_and_their_own_certainty(self):
        best = _match("100")
        other = _match("200", confidence="low")
        matches = [best, _match("100"), other, _match("300", confidence="medium"),
                   _match("400"), _match("500")]
        tables = build_result_tables(_result(best, matches),
                                     xml_path="input.xml", seconds=1)
        candidates = tables["candidates"]
        self.assertEqual([row["item_id"] for row in candidates],
                         ["200", "300", "400"])
        self.assertEqual([row["rank"] for row in candidates], [2, 3, 4])
        self.assertEqual(candidates[0]["certainty"], "Low")
        self.assertEqual(candidates[0]["percent"], "82.5%")

    def test_empty_or_unsearchable_index_does_not_claim_a_clean_vehicle(self):
        indexed = _result(None)
        tables = build_result_tables(indexed, xml_path="input.xml", seconds=0.1)
        self.assertEqual(tables["certainty"], "Uncertain")
        self.assertEqual(tables["candidates"], [])
        self.assertEqual(dict(tables["summary"])["Matched vehicle"], "No match in index")
        self.assertEqual(dict(tables["summary"])["From which source"],
                         "No matched source (local index)")
        indexed["coverage"]["searched_files"] = 125
        self.assertEqual(certainty_for(indexed), "No match found")
        indexed["coverage"]["searched_files"] = 0
        self.assertEqual(certainty_for(indexed), "Uncertain")
        indexed["status"] = "no index"
        self.assertEqual(build_result_tables(indexed, xml_path="input.xml", seconds=0)
                         ["certainty"], "Uncertain")

    def test_non_workshop_item_is_identified_as_other_source(self):
        best = _match("custom-id", confidence="low")
        best["url"] = None
        tables = build_result_tables(_result(best), xml_path="input.xml", seconds=0,
                                     source="local")
        self.assertEqual(dict(tables["summary"])["From which source"],
                         "Other indexed source (local index)")
        self.assertEqual(tables["certainty"], "Low")

    def test_last_discovery_epoch_is_shown_as_a_date(self):
        result = _result(_match())
        result["coverage"]["last_discovery"] = "1790447148"
        rows = dict(build_result_tables(result, xml_path="input.xml", seconds=0.05)["program"])
        self.assertTrue(rows["Last Workshop discovery"].startswith("2026-09-"))


if __name__ == "__main__":
    unittest.main()
