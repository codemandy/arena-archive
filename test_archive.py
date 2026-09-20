import json
import sqlite3
import tempfile
import unittest
from pathlib import Path

from arena_archive import ArenaClient, import_archive


class ArchiveImportTest(unittest.TestCase):
    def test_owned_first_level_contents_are_imported_and_deduplicated(self):
        fixture = {
            "profile": {
                "data": [
                    {"id": 10, "type": "Channel", "slug": "one", "title": "One", "owner": {"slug": "test"}},
                    {"id": 11, "type": "Channel", "slug": "two", "title": "Two", "owner": {"slug": "test"}},
                ],
                "meta": {},
            },
            "contents": {
                "10": {"data": [
                    {"id": 20, "type": "TextBlock", "content": "shared note", "position": 0,
                     "user": {"full_name": "author", "slug": "author"}},
                    {"id": 99, "type": "Channel", "title": "Nested", "position": 1},
                ], "meta": {}},
                "11": {"data": [
                    {"id": 20, "type": "TextBlock", "content": "shared note", "position": 0,
                     "user": {"full_name": "author", "slug": "author"}},
                ], "meta": {}},
            },
        }
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            result = import_archive("test", root / "archive.db", root / "assets", ArenaClient(fixture=fixture))
            self.assertEqual(result["channels"], 2)
            self.assertEqual(result["blocks"], 1)
            connection = sqlite3.connect(root / "archive.db")
            self.assertEqual(connection.execute("SELECT COUNT(*) FROM channel_blocks").fetchone()[0], 2)
            self.assertEqual(connection.execute("SELECT COUNT(*) FROM channels").fetchone()[0], 2)
            connection.close()


if __name__ == "__main__":
    unittest.main()
