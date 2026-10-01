"""Public Workshop description lookup without a Steam Web API key."""

from io import BytesIO
import json
import unittest
import urllib.error
from unittest.mock import patch

from stormcopy import workshop_metadata as metadata


def _reply(item_id="1234567890", app_id=573090, description="[b]A ship[/b]"):
    value = {"response": {"publishedfiledetails": [{
        "publishedfileid": item_id, "result": 1,
        "consumer_app_id": app_id, "description": description,
    }]}}
    return BytesIO(json.dumps(value).encode("utf-8"))


class WorkshopMetadataTests(unittest.TestCase):
    def setUp(self):
        with metadata._cache_lock:
            metadata._cache.clear()

    def test_fetch_cleans_description_and_caches_it(self):
        description = ("[h1]Vehicle[/h1]\n[img]https://example.test/image.png[/img]"
                       "<p>Engine &amp; pump</p><script>hidden()</script>"
                       "[url=https://example.test]Build notes[/url]")
        with patch("stormcopy.workshop_metadata.urllib.request.urlopen",
                   return_value=_reply(description=description)) as urlopen:
            first = metadata.get_workshop_description("1234567890", timeout=2.0)
            second = metadata.get_workshop_description("1234567890")

        self.assertEqual(first, "Vehicle\n\nEngine & pump\nBuild notes")
        self.assertEqual(second, first)
        urlopen.assert_called_once()
        request = urlopen.call_args.args[0]
        self.assertIn(b"publishedfileids%5B0%5D=1234567890", request.data)
        self.assertEqual(urlopen.call_args.kwargs["timeout"], 2.0)

    def test_non_steam_id_is_not_requested(self):
        with patch("stormcopy.workshop_metadata.urllib.request.urlopen") as urlopen:
            for item_id in ("local", "addon/foo", "123?x", "0", "18446744073709551616", None):
                self.assertIsNone(metadata.get_workshop_description(item_id))
        urlopen.assert_not_called()

    def test_rejects_wrong_game_or_wrong_item(self):
        with patch("stormcopy.workshop_metadata.urllib.request.urlopen",
                   return_value=_reply(app_id=4000)):
            self.assertIsNone(metadata.get_workshop_description("1234567890"))
        with metadata._cache_lock:
            metadata._cache.clear()
        with patch("stormcopy.workshop_metadata.urllib.request.urlopen",
                   return_value=_reply(item_id="777")):
            self.assertIsNone(metadata.get_workshop_description("1234567890"))

    def test_network_failure_is_soft_and_cached_briefly(self):
        failure = urllib.error.HTTPError(metadata.DETAILS_URL, 429, "Rate limited", {}, None)
        with patch("stormcopy.workshop_metadata.urllib.request.urlopen",
                   side_effect=failure) as urlopen:
            self.assertIsNone(metadata.get_workshop_description("1234567890"))
            self.assertIsNone(metadata.get_workshop_description("1234567890"))
        urlopen.assert_called_once()

    def test_short_description_fallback_and_invalid_response(self):
        payload = {"response": {"publishedfiledetails": [{
            "publishedfileid": "1234567890", "result": 1,
            "consumer_app_id": 573090, "short_description": "[b]Small ferry[/b]",
        }]}}
        with patch("stormcopy.workshop_metadata.urllib.request.urlopen",
                   return_value=BytesIO(json.dumps(payload).encode())):
            self.assertEqual(metadata.get_workshop_description("1234567890"),
                             "Small ferry")
        with metadata._cache_lock:
            metadata._cache.clear()
        with patch("stormcopy.workshop_metadata.urllib.request.urlopen",
                   return_value=BytesIO(b"[]")):
            self.assertIsNone(metadata.get_workshop_description("1234567890"))


if __name__ == "__main__":
    unittest.main()
