import io
import json
import os
import time
import unittest
from unittest.mock import patch

from stormcopy.console import Console


class FakeTTY(io.StringIO):
    def isatty(self):
        return True


class ConsoleTests(unittest.TestCase):
    def scan_result(self):
        best = {
            "item_id": "123", "title": "Workshop Ship", "url": "https://example.test/123",
            "similarity_percent": 82.5, "workshop_coverage_percent": 77.2,
            "confidence": "high", "shared_neighborhoods": 140,
            "rare_shared_neighborhoods": 48,
            "evidence": [{"query_position": [1, 2, 3],
                          "workshop_position": [4, 5, 6], "matching_components": 8,
                          "indexed_occurrences": 1}],
        }
        return {"status": "strong overlap", "suspicion_level": "high",
                "best_match": best, "matches": [best],
                "coverage": {"indexed_items": 100, "known_items": 120,
                             "indexed_files": 130, "discovery_complete": False},
                "note": "Similarity is local geometry coverage, not proof of copying."}

    def test_default_scan_is_readable_and_includes_evidence(self):
        stdout, stderr = io.StringIO(), io.StringIO()
        console = Console(stdout=stdout, stderr=stderr)
        console.render("scan", self.scan_result())
        report = stdout.getvalue()
        self.assertIn("Status: Strong overlap", report)
        self.assertIn("Best match: Workshop Ship (ID 123)", report)
        self.assertIn("Geometry similarity: 82.5%", report)
        self.assertIn("Confidence: high (heuristic)", report)
        self.assertIn("(1, 2, 3) <-> (4, 5, 6)", report)
        self.assertIn("100 Workshop items", report)
        self.assertNotIn("\x1b[", report)
        self.assertFalse(report.lstrip().startswith("{"))
        self.assertEqual(stderr.getvalue(), "")

    def test_json_is_unchanged_and_stdout_stays_parseable(self):
        stdout, stderr = io.StringIO(), io.StringIO()
        result = self.scan_result()
        console = Console(json_mode=True, stdout=stdout, stderr=stderr)
        console.progress({"phase": "download", "done": 0, "total": 2})
        console.render("scan", result)
        self.assertEqual(json.loads(stdout.getvalue()), result)
        self.assertNotIn("\x1b[", stdout.getvalue() + stderr.getvalue())
        self.assertIn("Download:", stderr.getvalue())

    def test_no_match_and_status_are_readable(self):
        stdout = io.StringIO()
        console = Console(stdout=stdout, stderr=io.StringIO())
        console.render("scan", {"status": "no index", "suspicion_level": "unknown",
                                "message": "Index Workshop XML first.",
                                "coverage": {"indexed_items": 0, "known_items": 5,
                                             "indexed_files": 0}})
        self.assertIn("Index Workshop XML first.", stdout.getvalue())
        self.assertIn("0 Workshop items", stdout.getvalue())
        stdout.seek(0)
        stdout.truncate(0)
        console.render("status", {"known_items": 10, "cached_items": 3,
                                  "queued_downloads": 7, "indexed_vehicle_files": 4})
        self.assertIn("Queued downloads: 7", stdout.getvalue())
        self.assertFalse(stdout.getvalue().startswith("{"))

    def test_progress_is_plain_and_throttled_when_redirected(self):
        stdout, stderr = io.StringIO(), io.StringIO()
        console = Console(stdout=stdout, stderr=stderr, progress_interval=3600)
        for done in (0, 1, 2):
            console.progress({"phase": "download", "done": done, "total": 2,
                              "detail": "smallest first"})
        console.finish_progress()
        lines = stderr.getvalue().splitlines()
        self.assertEqual(len(lines), 2)
        self.assertIn("0/2 (0%)", lines[0])
        self.assertIn("2/2 (100%)", lines[1])
        self.assertNotIn("\r", stderr.getvalue())
        self.assertNotIn("\x1b[", stderr.getvalue())
        self.assertEqual(stdout.getvalue(), "")

    def test_tty_bar_and_no_color_opt_out(self):
        with patch("stormcopy.console._enable_windows_vt", return_value=True), \
             patch.dict(os.environ, {"TERM": "xterm"}, clear=True):
            stdout, stderr = FakeTTY(), FakeTTY()
            console = Console(stdout=stdout, stderr=stderr)
            console.progress({"phase": "index", "done": 1, "total": 4})
            console.finish_progress()
            console.render("scan", self.scan_result())
            self.assertIn("\x1b[", stdout.getvalue() + stderr.getvalue())
            self.assertIn("\r", stderr.getvalue())
            with patch.dict(os.environ, {"NO_COLOR": ""}):
                stdout, stderr = FakeTTY(), FakeTTY()
                console = Console(stdout=stdout, stderr=stderr)
                console.progress({"phase": "download", "done": 1, "total": 4})
                console.render("scan", self.scan_result())
                self.assertNotIn("\x1b[", stdout.getvalue() + stderr.getvalue())

    def test_warning_remains_visible_above_progress(self):
        stdout, stderr = FakeTTY(), FakeTTY()
        with patch.dict(os.environ, {"TERM": "xterm", "NO_COLOR": ""}):
            console = Console(stdout=stdout, stderr=stderr,
                              heartbeat_interval=0.02)
            console.progress({"phase": "download", "done": 1, "total": 3})
            console.progress({"phase": "download", "done": 1, "total": 3,
                              "warning": "Steam rate limit reported"})
            time.sleep(0.05)
            console.finish_progress()
        self.assertIn("Warning: Steam rate limit reported\n", stderr.getvalue())
        self.assertEqual(stderr.getvalue().count("Warning:"), 1)

    def test_heartbeat_shows_elapsed_time_and_stops_after_render(self):
        stdout, stderr = io.StringIO(), io.StringIO()
        console = Console(json_mode=True, stdout=stdout, stderr=stderr,
                          progress_interval=0, heartbeat_interval=0.02)
        console.progress({"phase": "download", "done": 1, "total": 10})
        deadline = time.monotonic() + 1
        while stderr.getvalue().count("Download:") < 2 and time.monotonic() < deadline:
            time.sleep(0.01)
        self.assertGreaterEqual(stderr.getvalue().count("Download:"), 2)
        self.assertIn("elapsed", stderr.getvalue())
        result = {"selected": 10, "cached": 1, "failed": 0}
        console.render("download", result)
        self.assertEqual(json.loads(stdout.getvalue()), result)
        stopped_output = stderr.getvalue()
        time.sleep(0.06)
        self.assertEqual(stderr.getvalue(), stopped_output)

    def test_untrusted_title_cannot_inject_terminal_controls(self):
        result = self.scan_result()
        result["best_match"]["title"] = "Bad\x1b[31m\n\u202eTitle"
        stdout = io.StringIO()
        Console(stdout=stdout, stderr=io.StringIO()).render("scan", result)
        self.assertIn("Bad [31m Title", stdout.getvalue())
        self.assertNotIn("\x1b", stdout.getvalue())
        self.assertNotIn("\u202e", stdout.getvalue())


if __name__ == "__main__":
    unittest.main()
