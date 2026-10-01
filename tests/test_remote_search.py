"""Client/server checks for the optional shared fingerprint index."""

import json
from pathlib import Path
import tempfile
import threading
import unittest
from unittest.mock import patch
from urllib import error, request

from stormcopy.index import connect, index_file, upsert_item
from stormcopy.remote_client import RemoteSearchError, remote_coverage, scan_remote
from stormcopy.search_index import SEARCH_INDEX_VERSION
from stormcopy.search_service import make_server


def _vehicle(author="PrivateAuthor-DoNotUpload"):
    components = []
    for x in range(8):
        for z in range(8):
            kind = "wedge" if (x * 7 + z * 3) % 5 == 0 else "block"
            components.append(
                f'<c d="{kind}"><o sc="red"><vp x="{x}" y="0" z="{z}"/></o></c>')
    return (f'<vehicle data_version="3"><authors><author username="{author}"/>'
            '</authors><bodies><body><components>' + "".join(components) +
            '</components></body></bodies></vehicle>').encode("utf-8")


class RemoteSearchTests(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        root = Path(temporary.name)
        self.source = root / "private-workshop-cache" / "original.xml"
        self.source.parent.mkdir()
        self.source.write_bytes(_vehicle())
        self.query = root / "private-upload-name.xml"
        self.query.write_bytes(_vehicle(author="Different Private Author"))
        db_path = root / "index.sqlite"
        db = connect(db_path)
        index_file(db, self.source, "123456", "Workshop source")
        upsert_item(db, "789012", "Discovered but not indexed")
        db.close()

        server = make_server(db_path, host="127.0.0.1", port=0,
                             token="test-access-token")
        thread = threading.Thread(target=server.serve_forever, daemon=True)
        thread.start()

        def stop_server():
            server.shutdown()
            server.server_close()
            thread.join(timeout=5)

        self.addCleanup(stop_server)
        self.endpoint = f"http://127.0.0.1:{server.server_port}"

    def test_remote_scan_matches_and_redacts_server_paths(self):
        coverage = remote_coverage(self.endpoint, token="test-access-token")
        self.assertEqual(coverage["known_items"], 2)
        self.assertEqual(coverage["indexed_items"], 1)
        self.assertEqual(coverage["indexed_files"], 1)
        self.assertEqual(coverage["modern_search_files"], 1)
        self.assertEqual(coverage["searched_files"], 1)
        self.assertFalse(coverage["discovery_complete"])

        result = scan_remote(self.query, self.endpoint, token="test-access-token")
        best = result["best_match"]
        self.assertEqual(best["item_id"], "123456")
        self.assertEqual(best["title"], "Workshop source")
        self.assertEqual(best["similarity_percent"], 100.0)
        self.assertEqual(result["coverage"]["known_items"], 2)
        self.assertEqual(result["coverage"]["searched_files"], 1)
        self.assertTrue(best["evidence"])
        self.assertNotIn("file", best)
        self.assertNotIn("file", result["matches"][0])
        self.assertTrue(all("fingerprint" not in entry
                            for match in result["matches"]
                            for entry in match["evidence"]))
        self.assertNotIn(str(self.source), json.dumps(result))

    def test_root_is_json_status_not_a_browser_page(self):
        req = request.Request(
            self.endpoint + "/",
            headers={"Authorization": "Bearer test-access-token"})
        with request.urlopen(req, timeout=5) as response:
            result = json.load(response)
            self.assertEqual(response.status, 200)
            self.assertIn("application/json", response.headers["Content-Type"])
        self.assertEqual(result["status"], "ok")
        self.assertEqual(result["coverage"]["indexed_items"], 1)

    def test_xml_upload_page_endpoint_is_removed(self):
        req = request.Request(
            self.endpoint + "/v1/scan-xml", self.query.read_bytes(),
            headers={"Authorization": "Bearer test-access-token",
                     "Content-Type": "application/xml"},
            method="POST")
        with self.assertRaises(error.HTTPError) as caught:
            request.urlopen(req, timeout=5)
        with caught.exception as response:
            self.assertEqual(response.code, 404)

    def test_scan_uploads_fingerprints_without_xml_or_local_paths(self):
        original_build_opener = request.build_opener
        captured = []

        class InspectingOpener:
            def __init__(self):
                self.transport = original_build_opener()

            def open(self, req, **kwargs):
                captured.append(req.data)
                self_test.assertEqual(req.get_method(), "POST")
                self_test.assertEqual(req.get_header("Content-type"), "application/json")
                return self.transport.open(req, **kwargs)

        self_test = self
        with patch("stormcopy.remote_client.request.build_opener",
                   side_effect=lambda *args: InspectingOpener()):
            result = scan_remote(self.query, self.endpoint,
                                 token="test-access-token")
        self.assertEqual(result["best_match"]["item_id"], "123456")
        self.assertEqual(len(captured), 1)
        body = captured[0]
        self.assertNotIn(b"<vehicle", body)
        self.assertNotIn(b"PrivateAuthor", body)
        self.assertNotIn(str(self.query).encode(), body)
        self.assertNotIn(str(self.source).encode(), body)
        payload = json.loads(body)
        self.assertEqual(set(payload), {"fingerprint_version", "fingerprint"})
        self.assertEqual(payload["fingerprint_version"], SEARCH_INDEX_VERSION)
        self.assertTrue(payload["fingerprint"]["features"])

    def test_authentication_is_required_for_coverage_and_scan(self):
        with self.assertRaisesRegex(RemoteSearchError, "HTTP 401"):
            remote_coverage(self.endpoint)
        with self.assertRaisesRegex(RemoteSearchError, "HTTP 401"):
            scan_remote(self.query, self.endpoint, token="wrong-token")
        self.assertEqual(remote_coverage(self.endpoint,
                                         token="test-access-token")["indexed_items"], 1)

    def test_server_rejects_bad_version_and_raw_xml(self):
        headers = {"Authorization": "Bearer test-access-token",
                   "Content-Type": "application/json"}
        bad_version = json.dumps({"fingerprint_version": SEARCH_INDEX_VERSION + 1,
                                  "fingerprint": {}}).encode()
        req = request.Request(self.endpoint + "/v1/scan", bad_version,
                              headers=headers, method="POST")
        with self.assertRaises(error.HTTPError) as caught:
            request.urlopen(req, timeout=5)
        with caught.exception as response:
            self.assertEqual(response.code, 422)
            self.assertIn("version", json.load(response)["error"].lower())

        req = request.Request(self.endpoint + "/v1/scan", self.query.read_bytes(),
                              headers={**headers, "Content-Type": "application/xml"},
                              method="POST")
        with self.assertRaises(error.HTTPError) as caught:
            request.urlopen(req, timeout=5)
        with caught.exception as response:
            self.assertEqual(response.code, 415)

    def test_nonlocal_http_is_rejected_while_https_is_allowed(self):
        with patch("stormcopy.remote_client._request_json") as transport:
            with self.assertRaisesRegex(RemoteSearchError, "must use HTTPS"):
                remote_coverage("http://search.example.com")
            with self.assertRaisesRegex(RemoteSearchError, "must use HTTPS"):
                remote_coverage("http://localhost.evil.example")
            transport.assert_not_called()

            transport.return_value = {"status": "ok", "coverage": {
                "indexed_items": 1, "indexed_files": 1, "known_items": 2}}
            remote_coverage("https://search.example.com/base/")
            self.assertEqual(transport.call_args.args[0],
                             "https://search.example.com/base/v1/coverage")

    def test_public_binding_requires_authentication(self):
        with patch.dict("stormcopy.search_service.os.environ", {}, clear=True):
            with self.assertRaisesRegex(ValueError, "STORMCOPY_SEARCH_TOKEN"):
                make_server(self.source.parent.parent / "index.sqlite",
                            host="0.0.0.0", port=0, token="")

    def test_missing_index_fails_before_service_starts(self):
        with self.assertRaisesRegex(OSError, "index is unavailable"):
            make_server(self.query.parent / "missing.sqlite", port=0, token="")


if __name__ == "__main__":
    unittest.main()
