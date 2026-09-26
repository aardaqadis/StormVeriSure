"""Offline diagnostics for denied Steam Workshop discovery requests."""

import unittest
import urllib.error
from unittest.mock import patch

from stormcopy import steam


class SteamApiErrorTests(unittest.TestCase):
    def test_query_files_403_is_actionable_and_does_not_retry(self):
        fake_key = "0123456789abcdef0123456789abcdef"
        forbidden = urllib.error.HTTPError(
            "https://api.steampowered.com/", 403, "Forbidden", {}, None)
        with patch("stormcopy.steam.urllib.request.urlopen",
                   side_effect=forbidden) as urlopen, \
             patch("stormcopy.steam.time.sleep") as sleep:
            with self.assertRaises(RuntimeError) as raised:
                steam._query_files(fake_key, 0, "published", "*", (), (), True)

        message = str(raised.exception)
        self.assertIn("403", message)
        self.assertIn("set-api-key", message)
        self.assertNotIn(fake_key, message)
        urlopen.assert_called_once()
        sleep.assert_not_called()


if __name__ == "__main__":
    unittest.main()
