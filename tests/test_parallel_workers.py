"""Check that requested SteamCMD workers can actually run at the same time."""

from pathlib import Path
import tempfile
import threading
from types import SimpleNamespace
import unittest
from unittest.mock import patch

from stormcopy.index import connect, upsert_item
from stormcopy.steam import download_pending


class ParallelWorkerTests(unittest.TestCase):
    def test_sixteen_processes_start_with_default_batch_size(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            cache = root / "cache"
            cache.mkdir()
            executable = cache / "steamcmd.exe"
            executable.write_bytes(b"MZ test")
            workshop = root / "library" / "steamapps" / "workshop" / "content" / "573090"
            workshop.mkdir(parents=True)
            db = connect(root / "index.sqlite")
            for number in range(1, 51):
                upsert_item(db, str(number), updated=1, size_bytes=number)

            gate = threading.Barrier(16)
            lock = threading.Lock()
            started = active = peak = 0
            first_homes = []
            events = []

            def steamcmd(command, **_kwargs):
                nonlocal started, active, peak
                home = Path(command[2])
                ids = [command[position + 2]
                       for position, part in enumerate(command)
                       if part == "+workshop_download_item"]
                with lock:
                    started += 1
                    active += 1
                    peak = max(peak, active)
                    initial = started <= 16
                    if initial:
                        first_homes.append(home)
                if initial:
                    gate.wait(timeout=10)
                for item_id in ids:
                    folder = home / "steamapps" / "workshop" / "content" / "573090" / item_id
                    folder.mkdir(parents=True)
                    (folder / "vehicle.xml").write_text(item_id, encoding="utf-8")
                with lock:
                    active -= 1
                return SimpleNamespace(returncode=0,
                                       stdout="\n".join(f"Success. Downloaded item {item_id}"
                                                        for item_id in ids), stderr="")

            try:
                with patch("stormcopy.steam.subprocess.run", side_effect=steamcmd):
                    result = download_pending(db, cache, executable, max_items=50, delay=0,
                                              batch_size=10, workers=16, cache_only=True,
                                              workshop_folder=workshop, progress=events.append)
                self.assertEqual((result["selected"], result["cached"], result["failed"]),
                                 (50, 50, 0))
                self.assertEqual((result["workers_requested"], result["workers_used"],
                                  result["batch_size_used"], result["batches"]),
                                 (16, 16, 3, 17))
                self.assertEqual(peak, 16)
                self.assertEqual(len(set(first_homes)), 16)
                self.assertTrue(any(event.get("phase") == "Prepare workers" and
                                    event.get("done") == 16 for event in events))
                self.assertTrue(any(event.get("active") == 16 for event in events))
                self.assertTrue((workshop / "50" / "vehicle.xml").is_file())
            finally:
                db.close()

    def test_seventeen_workers_are_rejected(self):
        with tempfile.TemporaryDirectory() as tmp:
            db = connect(Path(tmp) / "index.sqlite")
            try:
                with self.assertRaisesRegex(ValueError, "workers 1-16"):
                    download_pending(db, Path(tmp) / "cache", workers=17)
            finally:
                db.close()


if __name__ == "__main__":
    unittest.main()
