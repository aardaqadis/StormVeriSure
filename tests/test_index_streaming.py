"""Directory indexing behavior with bounded-memory path tracking."""

from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

from stormcopy.index import connect, index_directory, index_file


def vehicle(kind):
    components = "".join(
        f'<c d="{kind}_{i}"><o><vp x="{i}" y="0" z="0"/></o></c>'
        for i in range(6))
    return f'<vehicle><bodies><body><components>{components}</components></body></bodies></vehicle>'.encode()


class StreamingDirectoryIndexTests(unittest.TestCase):
    def test_two_pass_scan_and_stale_cleanup_keep_error_items(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            old_root = root / "old"
            new_root = root / "new"
            db = connect(root / "index.sqlite")
            try:
                for item_id in ("101", "202", "303"):
                    folder = old_root / item_id
                    folder.mkdir(parents=True)
                    path = folder / "vehicle.xml"
                    path.write_bytes(vehicle(f"old_{item_id}"))
                    index_file(db, path, item_id)

                (new_root / "101").mkdir(parents=True)
                (new_root / "101" / "replacement.xml").write_bytes(vehicle("replacement"))
                (new_root / "202").mkdir()
                (new_root / "202" / "broken.xml").write_bytes(b"<vehicle><c")
                (new_root / "303").mkdir()
                (new_root / "303" / "microcontroller.xml").write_bytes(b"<microcontroller/>")
                (new_root / "404").mkdir()
                (new_root / "404" / "other.xml").write_bytes(b"<other/>")

                events = []
                real_rglob = Path.rglob
                traversals = []

                def counted_rglob(path, pattern):
                    traversals.append((path, pattern))
                    yield from real_rglob(path, pattern)

                with patch.object(Path, "rglob", counted_rglob):
                    stats = index_directory(db, new_root, progress=events.append)

                self.assertEqual(len(traversals), 2)
                self.assertEqual(stats["examined"], 4)
                self.assertEqual(stats["indexed_or_updated"], 1)
                self.assertEqual(stats["ignored_non_vehicle"], 2)
                self.assertEqual(stats["skipped_errors"], 1)
                paths = {item_id: [row[0] for row in db.execute(
                    "SELECT path FROM files WHERE item_id=?", (item_id,))]
                    for item_id in ("101", "202", "303")}
                self.assertEqual(paths["101"], [str((new_root / "101" /
                                                    "replacement.xml").resolve())])
                self.assertEqual(paths["202"], [str((old_root / "202" /
                                                    "vehicle.xml").resolve())])
                self.assertEqual(paths["303"], [])
                self.assertEqual(events[-1]["done"], 4)
                self.assertEqual(events[-1]["total"], 4)
                self.assertEqual(db.execute(
                    "SELECT COUNT(*) FROM sqlite_temp_master "
                    "WHERE name LIKE '_stormcopy_%'").fetchone()[0], 0)
            finally:
                db.close()

    def test_interruption_cleans_up_temporary_tracking(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            folder = root / "123"
            folder.mkdir()
            (folder / "vehicle.xml").write_bytes(vehicle("part"))
            db = connect(root / "index.sqlite")
            try:
                with patch("stormcopy.index.index_file", side_effect=KeyboardInterrupt):
                    with self.assertRaises(KeyboardInterrupt):
                        index_directory(db, root)
                self.assertEqual(db.execute(
                    "SELECT COUNT(*) FROM sqlite_temp_master "
                    "WHERE name LIKE '_stormcopy_%'").fetchone()[0], 0)
            finally:
                db.close()

    def test_stale_cleanup_continues_past_one_batch(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            folder = root / "123"
            folder.mkdir()
            (folder / "other.xml").write_bytes(b"<other/>")
            db = connect(root / "index.sqlite")
            try:
                with db:
                    db.execute("INSERT INTO items(id) VALUES ('123')")
                    db.executemany("INSERT INTO files VALUES (?,?,?,?,?)",
                                   (("123", str(root / f"old-{i}.xml"), 1, 1, 3)
                                    for i in range(501)))
                self.assertEqual(index_directory(db, root)["ignored_non_vehicle"], 1)
                self.assertEqual(db.execute("SELECT COUNT(*) FROM files").fetchone()[0], 0)
            finally:
                db.close()


if __name__ == "__main__":
    unittest.main()
