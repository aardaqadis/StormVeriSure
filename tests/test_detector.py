import tempfile
from contextlib import redirect_stdout
import io
from pathlib import Path
import json
import subprocess
import sys
import threading
import time
import unittest
import urllib.parse
from unittest.mock import patch
from types import SimpleNamespace

from stormcopy.fingerprint import fingerprint_bytes
from stormcopy.__main__ import main as cli_main
from stormcopy.index import connect, ensure_download_columns, index_directory, index_file, scan, upsert_item
from stormcopy.steam import discover, download_pending, refresh_sizes, refresh_tags


def vehicle(parts, reverse=False, author="A"):
    chunks = [f'<c d="{kind}"><o r="1,0,0,0,1,0,0,0,1" sc="red"><vp x="{x}" y="{y}" z="{z}"/></o></c>'
              for x, y, z, kind in parts]
    if reverse:
        chunks.reverse()
    return f'<vehicle data_version="3"><authors><author username="{author}"/></authors><bodies><body><components>{"".join(chunks)}</components></body></bodies></vehicle>'.encode()


class DetectorTests(unittest.TestCase):
    def test_reorder_metadata_and_translation(self):
        parts = [(x, 0, z, "wedge" if (x + z) % 4 == 0 else "block")
                 for x in range(10) for z in range(10)]
        shifted = [(x + 20, y - 2, z + 40, kind) for x, y, z, kind in parts]
        a = fingerprint_bytes(vehicle(parts))
        b = fingerprint_bytes(vehicle(shifted, reverse=True, author="B"))
        self.assertEqual(a["features"], b["features"])

    def test_subassembly_and_unrelated(self):
        core = [(x, 0, z, "wedge" if (x * 7 + z * 3) % 5 == 0 else "block")
                for x in range(10) for z in range(10)]
        extra = [(x, 0, z, "engine") for x in range(11, 16) for z in range(5)]
        different = [(x, 0, z, "wheel") for x in range(10) for z in range(10)]
        with tempfile.TemporaryDirectory() as tmp:
            p = Path(tmp)
            source, copy, other = (p / name for name in ("source.xml", "copy.xml", "other.xml"))
            source.write_bytes(vehicle(core))
            copy.write_bytes(vehicle(core + extra, reverse=True, author="C"))
            other.write_bytes(vehicle(different))
            db = connect(p / "test.sqlite")
            index_file(db, source, "123", "Source")
            result = scan(db, copy)
            self.assertEqual(result["best_match"]["item_id"], "123")
            self.assertGreater(result["best_match"]["shared_neighborhoods"], 50)
            self.assertEqual(scan(db, other)["status"], "no strong match in indexed set")
            db.close()

    def test_reject_entity_and_non_vehicle(self):
        for xml in (b'<!DOCTYPE vehicle [<!ENTITY x "x">]><vehicle>&x;</vehicle>', b'<root/>'):
            with self.assertRaises(ValueError):
                fingerprint_bytes(xml)

    def test_stormworks_numeric_and_duplicate_attributes(self):
        xml = (b'<vehicle><bodies><body><initial_local_transform 00="1" 01="0"/>'
               b'<components><c d="block" value="0" value="1"><o><vp x="0" y="0" z="0"/></o></c>'
               b'<c><o><vp x="1" y="0" z="0"/></o></c>'
               b'<c><o><vp x="2" y="0" z="0"/></o></c></components></body></bodies></vehicle>')
        fp = fingerprint_bytes(xml)
        self.assertEqual(fp["components"], 3)
        self.assertTrue(fp["features"])

    def test_directory_ignores_non_vehicle_xml(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            (root / "microcontroller.xml").write_text("<microcontroller/>")
            (root / "vehicle_1.xml").write_bytes(vehicle(
                [(x, 0, 0, "block") for x in range(4)]))
            db = connect(root / "index.sqlite")
            stats = index_directory(db, root)
            self.assertEqual(stats["indexed_or_updated"], 1)
            self.assertEqual(stats["ignored_non_vehicle"], 1)
            self.assertEqual(stats["skipped_errors"], 0)
            db.close()

    def test_cli_accepts_pasted_or_explicit_file_path(self):
        parts = [(x, 0, z, "wedge" if (x + z) % 3 == 0 else "block")
                 for x in range(8) for z in range(8)]
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            source = root / "Workshop source.xml"
            submitted = root / "Vehicle to scan.xml"
            source.write_bytes(vehicle(parts))
            submitted.write_bytes(vehicle(parts, reverse=True, author="Other"))
            db_path = root / "workshop.sqlite"
            db = connect(db_path)
            index_file(db, source, "123", "Workshop source")
            db.close()
            base = [sys.executable, "-m", "stormcopy", "--db", str(db_path)]
            project = Path(__file__).resolve().parents[1]
            prompted = subprocess.run(base + ["--json", "scan"],
                                      input=f'"{submitted}"\n', text=True,
                                      capture_output=True, cwd=project, check=True)
            direct = subprocess.run(base + ["scan", str(submitted), "--json"], text=True,
                                    capture_output=True, cwd=project, check=True)
            readable = subprocess.run(base + ["scan", str(submitted)], text=True,
                                      capture_output=True, cwd=project, check=True)
            status = subprocess.run(base + ["status", "--json"], text=True,
                                    capture_output=True, cwd=project, check=True)
            prompted_result = json.loads(prompted.stdout)
            direct_result = json.loads(direct.stdout)
            self.assertEqual(prompted_result["best_match"]["item_id"], "123")
            self.assertEqual(direct_result["best_match"]["item_id"], "123")
            self.assertEqual(prompted_result["best_match"]["similarity_percent"], 100.0)
            self.assertEqual(json.loads(status.stdout)["indexed_vehicle_files"], 1)
            self.assertEqual(json.loads(status.stdout)["queued_downloads"], 1)
            self.assertIn("Best match: Workshop source", readable.stdout)
            self.assertIn("Geometry similarity: 100.0%", readable.stdout)
            self.assertFalse(readable.stdout.lstrip().startswith("{"))

    def test_legacy_database_migrates_and_discovers_sizes(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "legacy.sqlite"
            db = connect(path)
            db.execute("DROP TABLE items")
            db.execute("CREATE TABLE items (id TEXT PRIMARY KEY,title TEXT,updated INTEGER DEFAULT 0,"
                       "downloaded INTEGER DEFAULT 0,indexed INTEGER DEFAULT 0,error TEXT)")
            db.commit()
            response = {"response": {"publishedfiledetails": [
                {"publishedfileid": "123", "title": "Small", "time_updated": 5, "file_size": "100"}],
                "next_cursor": None}}
            with patch("stormcopy.steam._request_json", return_value=response) as request:
                result = discover(db, api_key="test-key", delay=0)
                self.assertEqual(discover(db, api_key="test-key", delay=0)["pages"], 0)
            self.assertEqual(request.call_count, 1)
            self.assertTrue(result["complete"])
            self.assertEqual(db.execute("SELECT size_bytes FROM items WHERE id='123'").fetchone()[0], 100)
            self.assertIn("tag_checked", {row["name"] for row in db.execute("PRAGMA table_info(items)")})
            db.close()

    def test_filtered_discovery_saves_tags_and_separate_cursor(self):
        with tempfile.TemporaryDirectory() as tmp:
            db = connect(Path(tmp) / "index.sqlite")
            first = {"response": {"publishedfiledetails": [
                {"publishedfileid": "101", "title": "Air rescue", "file_size": "200",
                 "tags": [{"tag": "Vehicle"}, {"tag": "Air"}, {"tag": "Rescue"}]},
                {"publishedfileid": "102", "title": "Unwanted", "file_size": "100",
                 "tags": [{"tag": "Vehicle"}, {"tag": "Air"}, {"tag": "WIP"}]}],
                "next_cursor": "after-first"}}
            second = {"response": {"publishedfiledetails": [], "next_cursor": None}}
            with patch("stormcopy.steam._request_json", side_effect=[first, second, second]) as request:
                a = discover(db, api_key="test-key", tags=("Vehicle", "Air"),
                             excluded_tags=("WIP",), max_pages=1, delay=0)
                b = discover(db, api_key="test-key", tags=("Vehicle", "Sea"),
                             excluded_tags=("WIP",), max_pages=1, delay=0)
                c = discover(db, api_key="test-key", tags=("Vehicle", "air"),
                             excluded_tags=("WIP",), max_pages=1, delay=0)
            self.assertFalse(a["complete"])
            self.assertTrue(b["complete"])
            self.assertTrue(c["complete"])
            self.assertEqual(a["items_seen"], 1)
            first_url = request.call_args_list[0].args[0]
            payload = json.loads(urllib.parse.parse_qs(
                urllib.parse.urlsplit(first_url).query)["input_json"][0])
            self.assertEqual(payload["requiredtags"], ["Vehicle", "Air"])
            self.assertEqual(payload["excludedtags"], ["WIP"])
            self.assertTrue(payload["match_all_tags"])
            third_payload = json.loads(urllib.parse.parse_qs(urllib.parse.urlsplit(
                request.call_args_list[2].args[0]).query)["input_json"][0])
            self.assertEqual(third_payload["cursor"], "*")
            self.assertEqual(db.execute("SELECT COUNT(*) FROM item_tags WHERE item_id='101'").fetchone()[0], 3)
            self.assertIsNone(db.execute("SELECT id FROM items WHERE id='102'").fetchone())
            self.assertIsNone(db.execute("SELECT value FROM state WHERE key='cursor_published'").fetchone())
            db.close()

    def test_excluded_tag_needs_confirmed_tag_details(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            db = connect(root / "index.sqlite")
            ensure_download_columns(db)
            upsert_item(db, "101", size_bytes=100)
            result = download_pending(db, root / "cache", max_items=0,
                                      excluded_tags=("WIP",), delay=0)
            self.assertEqual(result["selected"], 0)
            self.assertEqual(db.execute("SELECT downloaded FROM items WHERE id='101'").fetchone()[0], 0)
            db.close()

    def test_public_tag_lookup_filters_cached_downloads(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            cache = root / "cache"
            db = connect(root / "index.sqlite")
            ensure_download_columns(db)
            for item_id in ("101", "102", "103"):
                upsert_item(db, item_id, size_bytes=100)
            details = {"response": {"publishedfiledetails": [
                {"publishedfileid": "101", "result": 1, "consumer_app_id": 573090,
                 "tags": [{"tag": "Vehicle"}, {"tag": "Air"}]},
                {"publishedfileid": "102", "result": 1, "consumer_app_id": 573090,
                 "tags": [{"tag": "Vehicle"}, {"tag": "Air"}, {"tag": "WIP"}]},
                {"publishedfileid": "103", "result": 1, "consumer_app_id": 573090,
                 "tags": [{"tag": "Vehicle"}, {"tag": "Sea"}]}]}}
            with patch("stormcopy.steam._request_json", return_value=details) as request:
                tag_result = refresh_tags(db, delay=0)
            self.assertEqual(tag_result["checked"], 3)
            self.assertEqual(request.call_count, 1)
            downloaded = []

            def fake_steamcmd(command, **_kwargs):
                item_id = command[command.index("+workshop_download_item") + 2]
                downloaded.append(item_id)
                folder = cache / "steamapps" / "workshop" / "content" / "573090" / item_id
                folder.mkdir(parents=True)
                (folder / "vehicle.xml").write_text("<vehicle/>")
                return SimpleNamespace(returncode=0,
                                       stdout=f"Success. Downloaded item {item_id}", stderr="")

            with patch("stormcopy.steam.subprocess.run", side_effect=fake_steamcmd):
                result = download_pending(db, cache, delay=0, cache_only=True,
                                          tags=("vehicle", "air"), excluded_tags=("wip",))
            self.assertEqual((result["selected"], downloaded), (1, ["101"]))
            self.assertEqual(db.execute("SELECT downloaded FROM items WHERE id='103'").fetchone()[0], 0)
            db.close()

    def test_unavailable_tag_details_wait_a_day_before_retry(self):
        with tempfile.TemporaryDirectory() as tmp:
            db = connect(Path(tmp) / "index.sqlite")
            ensure_download_columns(db)
            upsert_item(db, "101", updated=10)
            empty = {"response": {"publishedfiledetails": []}}
            with patch("stormcopy.steam._request_json", return_value=empty) as request:
                self.assertEqual(refresh_tags(db, delay=0)["checked"], 1)
                self.assertEqual(refresh_tags(db, delay=0)["checked"], 0)
                self.assertEqual(request.call_count, 1)
            row = db.execute("SELECT tag_checked,tag_attempted FROM items WHERE id='101'").fetchone()
            self.assertEqual(row["tag_checked"], 0)
            self.assertGreater(row["tag_attempted"], 0)
            upsert_item(db, "101", updated=11)
            self.assertEqual(db.execute("SELECT tag_attempted FROM items WHERE id='101'").fetchone()[0], 0)
            with patch("stormcopy.steam._request_json", return_value=empty) as request:
                self.assertEqual(refresh_tags(db, delay=0)["checked"], 1)
                self.assertEqual(request.call_count, 1)
            db.close()

    def test_keyed_filtered_download_refreshes_older_known_tags(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            database = root / "index.sqlite"
            cache = root / "cache"
            db = connect(database)
            ensure_download_columns(db)
            upsert_item(db, "101", updated=10, size_bytes=50)
            db.execute("INSERT INTO item_tags VALUES (?,?,?)", ("101", "Vehicle", "vehicle"))
            db.execute("UPDATE items SET tag_checked=1,tag_attempted=1 WHERE id='101'")
            db.commit()
            db.close()

            discovery = {"response": {"publishedfiledetails": [
                {"publishedfileid": "102", "result": 1, "title": "New vehicle",
                 "time_updated": 20, "file_size": "100",
                 "tags": [{"tag": "Vehicle"}]}], "next_cursor": None}}
            old_details = {"response": {"publishedfiledetails": [
                {"publishedfileid": "101", "result": 1,
                 "consumer_app_id": 573090, "time_updated": 10,
                 "tags": [{"tag": "Vehicle"}]}]}}
            downloaded = []

            def fake_steamcmd(command, **_kwargs):
                item_id = command[command.index("+workshop_download_item") + 2]
                downloaded.append(item_id)
                folder = cache / "steamapps" / "workshop" / "content" / "573090" / item_id
                folder.mkdir(parents=True)
                return SimpleNamespace(returncode=0,
                                       stdout=f"Success. Downloaded item {item_id}", stderr="")

            argv = ["stormcopy", "--db", str(database), "download", "--cache", str(cache),
                    "--tag", "Vehicle", "--discover-pages", "1", "--max-items", "0",
                    "--workers", "1", "--batch-size", "1", "--delay", "0",
                    "--metadata-delay", "0", "--no-size-refresh", "--cache-only", "--json"]
            output = io.StringIO()
            with patch.dict("os.environ", {"STEAM_API_KEY": "test-key"}), \
                    patch.object(sys, "argv", argv), \
                    patch("stormcopy.steam._request_json",
                          side_effect=[discovery, old_details]) as request, \
                    patch("stormcopy.steam.subprocess.run", side_effect=fake_steamcmd), \
                    redirect_stdout(output):
                cli_main()
            report = json.loads(output.getvalue())
            self.assertEqual(request.call_count, 2)
            self.assertEqual(report["selected"], 2)
            self.assertEqual(report["cached"], 2)
            self.assertEqual(downloaded, ["101", "102"])
            self.assertEqual(report["search_scope"], "matching Workshop pages and known items")

    def test_tag_cli_lists_tags_and_limits_download_scope(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            db_path = root / "index.sqlite"
            db = connect(db_path)
            ensure_download_columns(db)
            upsert_item(db, "101", size_bytes=100)
            db.execute("INSERT INTO item_tags VALUES (?,?,?)", ("101", "Air", "air"))
            db.execute("UPDATE items SET tag_checked=? WHERE id='101'", (int(time.time()),))
            db.commit()
            db.close()
            base = [sys.executable, "-m", "stormcopy", "--db", str(db_path)]
            project = Path(__file__).resolve().parents[1]
            listed = subprocess.run(base + ["tags", "--json"], text=True,
                                    capture_output=True, cwd=project, check=True)
            self.assertEqual(json.loads(listed.stdout)["tags"][0]["tag"], "Air")
            readable = subprocess.run(base + ["tags"], text=True,
                                      capture_output=True, cwd=project, check=True)
            self.assertIn("Known Workshop tags", readable.stdout)
            filtered = subprocess.run(base + ["download", "--tag", "Sea", "--known-only",
                                              "--no-size-refresh", "--cache", str(root / "cache"),
                                              "--json"], text=True, capture_output=True,
                                      cwd=project, check=True)
            result = json.loads(filtered.stdout)
            self.assertEqual(result["selected"], 0)
            self.assertEqual(result["search_scope"], "known items only")

    def test_size_refresh_and_batched_smallest_first_download(self):
        parts = [(x, 0, z, "block") for x in range(5) for z in range(5)]
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            db = connect(root / "index.sqlite")
            ensure_download_columns(db)
            for item_id, size in (("100", 500), ("200", None), ("300", 50), ("400", 100)):
                upsert_item(db, item_id, updated=1, size_bytes=size)
            detail = {"response": {"publishedfiledetails": [
                {"publishedfileid": "200", "result": 1, "consumer_app_id": 573090,
                 "file_size": "75", "time_updated": 1}]}}
            with patch("stormcopy.steam._request_json", return_value=detail) as request:
                sizes = refresh_sizes(db, delay=0)
            self.assertEqual(sizes["sizes_added"], 1)
            self.assertIn("publishedfileids%5B0%5D=200", request.call_args.args[2].decode())
            batches = []

            def fake_steamcmd(command, **_kwargs):
                ids = [command[i + 2] for i, part in enumerate(command)
                       if part == "+workshop_download_item"]
                batches.append(ids)
                for item_id in ids:
                    folder = root / "cache" / "steamapps" / "workshop" / "content" / "573090" / item_id
                    folder.mkdir(parents=True)
                    (folder / "vehicle.xml").write_bytes(vehicle(parts))
                return SimpleNamespace(returncode=0, stdout="".join(
                    f"Success. Downloaded item {item_id}\n" for item_id in ids), stderr="")

            with patch("stormcopy.steam.subprocess.run", side_effect=fake_steamcmd):
                result = download_pending(db, root / "cache", "steamcmd", max_items=0,
                                          delay=0, batch_size=2)
            self.assertEqual(batches, [["300", "200"], ["400", "100"]])
            self.assertEqual(result["downloaded_and_indexed"], 4)
            self.assertEqual(download_pending(db, root / "cache", "steamcmd", delay=0)["selected"], 0)
            db.close()

    def test_failed_download_does_not_block_following_item(self):
        parts = [(x, 0, 0, "block") for x in range(5)]
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            db = connect(root / "index.sqlite")
            ensure_download_columns(db)
            upsert_item(db, "11", updated=1, size_bytes=10)
            upsert_item(db, "22", updated=1, size_bytes=20)

            def fake_steamcmd(command, **_kwargs):
                stale = root / "cache" / "steamapps" / "workshop" / "content" / "573090" / "11"
                stale.mkdir(parents=True)
                (stale / "vehicle.xml").write_bytes(vehicle(parts))
                folder = root / "cache" / "steamapps" / "workshop" / "content" / "573090" / "22"
                folder.mkdir(parents=True)
                (folder / "vehicle.xml").write_bytes(vehicle(parts))
                return SimpleNamespace(returncode=0, stdout=(
                    "ERROR! Download item 11 failed (Failure)\n"
                    "Success. Downloaded item 22\n"), stderr="")

            events = []
            with patch("stormcopy.steam.subprocess.run", side_effect=fake_steamcmd):
                result = download_pending(db, root / "cache", "steamcmd", max_items=0,
                                          delay=0, batch_size=2, progress=events.append)
            self.assertEqual((result["cached"], result["failed"]), (1, 1))
            self.assertEqual((events[-1]["done"], events[-1]["total"]), (2, 2))
            self.assertEqual(db.execute("SELECT downloaded FROM items WHERE id='11'").fetchone()[0], 0)
            self.assertGreater(db.execute("SELECT retry_after FROM items WHERE id='11'").fetchone()[0], 0)
            self.assertEqual(download_pending(db, root / "cache", "steamcmd", delay=0)["selected"], 0)
            db.close()

    def test_cache_only_can_be_indexed_later(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            cache = root / "cache"
            db = connect(root / "index.sqlite")
            ensure_download_columns(db)
            upsert_item(db, "123", size_bytes=25)

            def fake_steamcmd(command, **_kwargs):
                folder = cache / "steamapps" / "workshop" / "content" / "573090" / "123"
                folder.mkdir(parents=True)
                (folder / "vehicle.xml").write_bytes(vehicle(
                    [(x, 0, 0, "block") for x in range(5)]))
                return SimpleNamespace(returncode=0,
                                       stdout="Success. Downloaded item 123", stderr="")

            with patch("stormcopy.steam.subprocess.run", side_effect=fake_steamcmd):
                result = download_pending(db, cache, "steamcmd", delay=0, cache_only=True)
            self.assertEqual(result["cached_unindexed"], 1)
            self.assertEqual(db.execute("SELECT COUNT(*) FROM files").fetchone()[0], 0)
            content = cache / "steamapps" / "workshop" / "content" / "573090"
            self.assertEqual(index_directory(db, content)["indexed_or_updated"], 1)
            db.close()

    def test_parallel_workers_use_separate_homes_and_one_cache(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            cache = root / "cache"
            cache.mkdir()
            executable = cache / "steamcmd.exe"
            executable.write_bytes(b"MZ test")
            db = connect(root / "index.sqlite")
            ensure_download_columns(db)
            upsert_item(db, "11", size_bytes=10)
            upsert_item(db, "22", size_bytes=20)
            simultaneous = threading.Barrier(2)
            homes = []

            def fake_steamcmd(command, **kwargs):
                home = Path(command[2])
                homes.append(home)
                self.assertEqual(Path(kwargs["cwd"]), home)
                simultaneous.wait(timeout=10)
                item_id = command[command.index("+workshop_download_item") + 2]
                folder = home / "steamapps" / "workshop" / "content" / "573090" / item_id
                folder.mkdir(parents=True)
                (folder / "vehicle.xml").write_text("<vehicle/>")
                return SimpleNamespace(returncode=0,
                                       stdout=f"Success. Downloaded item {item_id}", stderr="")

            with patch("stormcopy.steam.subprocess.run", side_effect=fake_steamcmd):
                result = download_pending(db, cache, executable, batch_size=1,
                                          delay=0, cache_only=True, workers=2)
            self.assertEqual((result["cached"], result["workers_used"]), (2, 2))
            self.assertEqual(len(set(homes)), 2)
            for item_id in ("11", "22"):
                self.assertTrue((cache / "steamapps" / "workshop" / "content" /
                                 "573090" / item_id / "vehicle.xml").is_file())
                self.assertFalse(any((home / "steamapps" / "workshop" / "content" /
                                      "573090" / item_id).exists() for home in homes))
            db.close()

    def test_parallel_refresh_restores_previous_cache_on_index_error(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            cache = root / "cache"
            cache.mkdir()
            executable = cache / "steamcmd.exe"
            executable.write_bytes(b"MZ test")
            db = connect(root / "index.sqlite")
            ensure_download_columns(db)
            upsert_item(db, "11", updated=1, size_bytes=10)
            upsert_item(db, "22", updated=1, size_bytes=20)
            old_folder = cache / "steamapps" / "workshop" / "content" / "573090" / "11"
            old_folder.mkdir(parents=True)
            old_xml = old_folder / "vehicle.xml"
            old_xml.write_bytes(vehicle([(x, 0, 0, "block") for x in range(5)]))
            index_file(db, old_xml, "11")

            def fake_steamcmd(command, **_kwargs):
                item_id = command[command.index("+workshop_download_item") + 2]
                folder = Path(command[2]) / "steamapps" / "workshop" / "content" / "573090" / item_id
                folder.mkdir(parents=True)
                (folder / "replacement.xml").write_text("<vehicle/>")
                return SimpleNamespace(returncode=0,
                                       stdout=f"Success. Downloaded item {item_id}", stderr="")

            def fail_new_index(_db, _folder, item_id, _title):
                if item_id == "11":
                    raise OSError("simulated index failure")
                return 0

            with patch("stormcopy.steam.subprocess.run", side_effect=fake_steamcmd), \
                 patch("stormcopy.steam._index_cached_item", side_effect=fail_new_index):
                result = download_pending(db, cache, executable, batch_size=1,
                                          delay=0, force=True, workers=2)
            self.assertEqual(result["failed"], 1)
            self.assertTrue(old_xml.is_file())
            self.assertFalse((old_folder / "replacement.xml").exists())
            self.assertEqual(db.execute("SELECT COUNT(*) FROM files WHERE item_id='11'").fetchone()[0], 1)
            db.close()

    def test_reindexing_new_workshop_root_removes_old_item_paths(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            first = root / "first" / "123"
            second = root / "second" / "123"
            first.mkdir(parents=True)
            second.mkdir(parents=True)
            (first / "vehicle.xml").write_bytes(vehicle([(x, 0, 0, "block") for x in range(5)]))
            (second / "vehicle.xml").write_bytes(vehicle([(x, 0, 0, "wedge") for x in range(5)]))
            db = connect(root / "index.sqlite")
            index_directory(db, first.parent)
            index_directory(db, second.parent)
            paths = [row[0] for row in db.execute("SELECT path FROM files WHERE item_id='123'")]
            self.assertEqual(paths, [str((second / "vehicle.xml").resolve())])
            db.close()


if __name__ == "__main__":
    unittest.main()
