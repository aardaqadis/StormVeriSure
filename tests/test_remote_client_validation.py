"""A malformed shared-search response must not reach the Tkinter renderer."""

from copy import deepcopy
import unittest
from unittest.mock import patch

from stormcopy.remote_client import RemoteSearchError, scan_remote


def _match():
    return {
        "item_id": "12345", "title": "Workshop vehicle",
        "url": "https://steamcommunity.com/sharedfiles/filedetails/?id=12345",
        "similarity_percent": 82.5,
        "workshop_coverage_percent": 68.0,
        "combined_similarity_percent": 77.1,
        "minhash_jaccard_estimate_percent": None,
        "shared_neighborhoods": 57,
        "rare_shared_neighborhoods": 12,
        "confidence": "high",
        "partial_copy_evidence": False,
        "semantic_copy_evidence": True,
        "evidence": [{"channel": "geometry", "matching_components": 5,
                      "query_position": [1, 2, 3],
                      "workshop_position": [10, 11, 12]}],
    }


def _result():
    best = _match()
    return {
        "status": "strong overlap", "suspicion_level": "high",
        "coverage": {"indexed_items": 10, "indexed_files": 12,
                     "known_items": 20, "searched_files": 12,
                     "modern_search_files": 12},
        "best_match": best, "matches": [deepcopy(best)],
    }


def _scan(response):
    with patch("stormcopy.remote_client.fingerprint_file",
               return_value={"components": 3, "features": {}}), \
            patch("stormcopy.remote_client._request_json", return_value=response):
        return scan_remote("vehicle.xml", "http://127.0.0.1:8766")


class RemoteClientValidationTests(unittest.TestCase):
    def test_valid_result_and_empty_index(self):
        response = _result()
        self.assertIs(_scan(response), response)
        response = {"status": "no index", "suspicion_level": "unknown",
                    "coverage": {"indexed_items": 0, "indexed_files": 0,
                                 "known_items": 0}, "matches": []}
        self.assertIs(_scan(response), response)

    def test_rejects_malformed_numeric_fields_before_gui_receives_them(self):
        for field, value in (("similarity_percent", "82.5"),
                             ("workshop_coverage_percent", True),
                             ("combined_similarity_percent", float("nan")),
                             ("combined_similarity_percent", 10 ** 1000),
                             ("shared_neighborhoods", -1),
                             ("rare_shared_neighborhoods", "12"),
                             ("minhash_jaccard_estimate_percent", 101)):
            with self.subTest(field=field, value=value):
                response = _result()
                response["best_match"][field] = value
                response["matches"][0][field] = value
                with self.assertRaises(RemoteSearchError):
                    _scan(response)

    def test_rejects_bad_evidence_and_untrusted_workshop_url(self):
        mutations = (
            lambda match: match.update(evidence="not a list"),
            lambda match: match["evidence"][0].update(matching_components=True),
            lambda match: match["evidence"][0].update(query_position=[1, 2, "3"]),
            lambda match: match.update(channels="invalid"),
            lambda match: match.update(channels={"geometry": {
                "shared": 3, "query_coverage_percent": "50%"}}),
            lambda match: match.update(url="file:///C:/private.xml"),
            lambda match: match.update(url="https://example.com/?id=12345"),
        )
        for mutate in mutations:
            with self.subTest(mutation=mutate):
                response = _result()
                mutate(response["best_match"])
                mutate(response["matches"][0])
                with self.assertRaises(RemoteSearchError):
                    _scan(response)

    def test_rejects_invalid_secondary_match_and_inconsistent_best_match(self):
        response = _result()
        secondary = _match()
        secondary["confidence"] = None
        response["matches"].append(secondary)
        with self.assertRaises(RemoteSearchError):
            _scan(response)

        response = _result()
        response["matches"][0]["item_id"] = "other"
        with self.assertRaises(RemoteSearchError):
            _scan(response)


if __name__ == "__main__":
    unittest.main()
