import json
import sqlite3
import tempfile
import threading
import unittest
from http.server import ThreadingHTTPServer
from pathlib import Path
from urllib.error import HTTPError
from urllib.parse import urlencode
from urllib.request import Request, urlopen

import server
from arena_archive import ArenaClient, import_archive


class QuietHandler(server.Handler):
    def log_message(self, *args):
        pass


class ServerTestCase(unittest.TestCase):
    """A server on a fresh, empty archive, with helpers to make channels and blocks."""

    def setUp(self):
        self.directory = tempfile.TemporaryDirectory()
        root = Path(self.directory.name)
        empty = {"profile": {"data": [], "meta": {}}, "contents": {}}
        import_archive("test", root / "archive.db", root / "assets", ArenaClient(fixture=empty))
        server.DATABASE = root / "archive.db"
        server.ASSETS = root / "assets"
        server.THUMBS = root / "thumbs"
        self.httpd = ThreadingHTTPServer(("127.0.0.1", 0), QuietHandler)
        threading.Thread(target=self.httpd.serve_forever, daemon=True).start()
        self.base = f"http://127.0.0.1:{self.httpd.server_address[1]}"

    def tearDown(self):
        self.httpd.shutdown()
        self.httpd.server_close()
        self.directory.cleanup()

    def post(self, path, fields):
        request = Request(self.base + path, data=urlencode(fields, doseq=True).encode(), method="POST")
        with urlopen(request) as response:
            body = response.read()
            return json.loads(body) if response.headers.get("Content-Type") == "application/json" else response

    def query(self, sql, *args):
        connection = sqlite3.connect(server.DATABASE)
        rows = connection.execute(sql, args).fetchall()
        connection.close()
        return rows

    def channel(self, title):
        connection = server.db()
        channel_id = server.insert_channel(connection, title)
        connection.commit()
        connection.close()
        return channel_id

    def block(self, channel_id, title):
        self.post("/create-block", {"channel_id": channel_id, "title": title, "content": title})
        return self.query("SELECT MIN(id) FROM blocks")[0][0]

    def blocks_in(self, channel_id):
        return [row[0] for row in self.query("SELECT b.title FROM channel_blocks cb JOIN blocks b ON b.id = cb.block_id WHERE cb.channel_id = ? ORDER BY cb.position", channel_id)]

class BatchEditTest(ServerTestCase):
    """Selecting several blocks or channels and renaming, moving, merging or deleting them."""

    def test_rename_blocks_numbers_them_in_order(self):
        home = self.channel("Home")
        ids = [self.block(home, name) for name in ("a", "b", "c")]
        result = self.post("/rename-blocks", {"block_ids": ids, "title": "Sketch #", "numbered": "1"})
        self.assertEqual(result["titles"], ["Sketch 1", "Sketch 2", "Sketch 3"])
        self.assertEqual(self.blocks_in(home), ["Sketch 1", "Sketch 2", "Sketch 3"])
        self.post("/rename-blocks", {"block_ids": ids, "title": "Same", "numbered": "0"})
        self.assertEqual(self.blocks_in(home), ["Same", "Same", "Same"])

    def test_move_blocks_to_existing_channel(self):
        home, other = self.channel("Home"), self.channel("Other")
        ids = [self.block(home, name) for name in ("a", "b", "c")]
        self.post("/move-blocks", {"channel_id": home, "block_ids": [ids[0], ids[2]], "target_id": other})
        self.assertEqual(self.blocks_in(home), ["b"])
        self.assertEqual(self.blocks_in(other), ["a", "c"])
        self.post("/move-blocks", {"channel_id": home, "block_ids": [ids[1]], "target_id": other, "keep": "1"})
        self.assertEqual(self.blocks_in(home), ["b"])
        self.assertEqual(self.blocks_in(other), ["a", "c", "b"])

    def test_move_blocks_into_new_nested_channel(self):
        home = self.channel("Home")
        ids = [self.block(home, name) for name in ("a", "b", "c", "d")]
        result = self.post("/move-blocks", {"channel_id": home, "block_ids": [ids[1], ids[2]], "new_title": "Group", "nest": "1"})
        self.assertEqual(self.blocks_in(result["channel_id"]), ["b", "c"])
        # The nested channel takes the place of the first moved block.
        self.assertEqual(self.blocks_in(home), ["a", "Group", "d"])

    def test_move_blocks_into_new_standalone_channel(self):
        home = self.channel("Home")
        ids = [self.block(home, name) for name in ("a", "b")]
        result = self.post("/move-blocks", {"channel_id": home, "block_ids": ids, "new_title": "Alone", "nest": "0"})
        self.assertEqual(self.blocks_in(home), [])
        self.assertEqual(self.blocks_in(result["channel_id"]), ["a", "b"])

    def test_delete_blocks_keeps_blocks_other_channels_hold(self):
        home, other = self.channel("Home"), self.channel("Other")
        shared, alone = self.block(home, "shared"), self.block(home, "alone")
        self.post("/connect-block", {"channel_id": other, "block_id": shared})
        self.post("/delete-blocks", {"channel_id": home, "block_ids": [shared, alone]})
        self.assertEqual(self.blocks_in(home), [])
        self.assertEqual(self.blocks_in(other), ["shared"])
        self.assertEqual(self.query("SELECT COUNT(*) FROM blocks WHERE id = ?", alone)[0][0], 0)

    def test_rename_channels_updates_nested_links(self):
        one, two, parent = self.channel("One"), self.channel("Two"), self.channel("Parent")
        self.post("/connect-channel", {"source_id": one, "channel_id": parent})
        self.post("/rename-channels", {"channel_ids": [one, two], "title": "Board", "numbered": "1"})
        self.assertEqual([row[0] for row in self.query("SELECT title FROM channels WHERE id IN (?, ?) ORDER BY id DESC", one, two)], ["Board 1", "Board 2"])
        self.assertEqual(self.blocks_in(parent), ["Board 1"])

    def test_renaming_a_nested_channel_block_renames_the_channel(self):
        inner, parent = self.channel("Inner"), self.channel("Parent")
        self.post("/connect-channel", {"source_id": inner, "channel_id": parent})
        link = self.query("SELECT id FROM blocks WHERE type = 'channel'")[0][0]
        self.post("/rename-blocks", {"block_ids": [link], "title": "Renamed"})
        self.assertEqual(self.query("SELECT title FROM channels WHERE id = ?", inner)[0][0], "Renamed")

    def test_move_channels_into_new_channel(self):
        one, two = self.channel("One"), self.channel("Two")
        result = self.post("/move-channels", {"channel_ids": [one, two], "new_title": "Shelf"})
        self.assertEqual(self.blocks_in(result["channel_id"]), ["One", "Two"])
        self.assertEqual(self.query("SELECT COUNT(*) FROM channels")[0][0], 3)

    def test_merge_channels(self):
        one, two, parent = self.channel("One"), self.channel("Two"), self.channel("Parent")
        shared = self.block(one, "shared")
        self.block(one, "a")
        self.post("/connect-block", {"channel_id": two, "block_id": shared})
        self.block(two, "b")
        self.post("/connect-channel", {"source_id": two, "channel_id": one})
        self.post("/connect-channel", {"source_id": one, "channel_id": parent})
        self.post("/connect-channel", {"source_id": two, "channel_id": parent})
        result = self.post("/merge-channels", {"channel_ids": [one, two], "title": "Merged"})
        merged = result["channel_id"]
        # Blocks come in order, once each, and the merged channel doesn't contain itself.
        self.assertEqual(self.blocks_in(merged), ["shared", "a", "b"])
        self.assertEqual(self.query("SELECT COUNT(*) FROM channels WHERE id IN (?, ?)", one, two)[0][0], 0)
        self.assertEqual(self.blocks_in(parent), ["Merged"])

    def test_merge_can_keep_originals(self):
        one, two = self.channel("One"), self.channel("Two")
        self.block(one, "a")
        self.block(two, "b")
        merged = self.post("/merge-channels", {"channel_ids": [one, two], "title": "Both", "keep": "1"})["channel_id"]
        self.assertEqual(self.blocks_in(merged), ["a", "b"])
        self.assertEqual(self.blocks_in(one), ["a"])

    def test_delete_channels(self):
        one, two, keep = self.channel("One"), self.channel("Two"), self.channel("Keep")
        shared = self.block(one, "shared")
        self.post("/connect-block", {"channel_id": keep, "block_id": shared})
        self.post("/delete-channels", {"channel_ids": [one, two]})
        self.assertEqual([row[0] for row in self.query("SELECT title FROM channels")], ["Keep"])
        self.assertEqual(self.blocks_in(keep), ["shared"])

    def test_empty_selection_is_an_error(self):
        with self.assertRaises(HTTPError):
            self.post("/delete-channels", {})

    def test_pages_render_selection_controls(self):
        home = self.channel("Home")
        self.block(home, "a")
        for path in ("/", f"/channel/{home}"):
            with urlopen(self.base + path) as response:
                page = response.read().decode()
            self.assertIn("data-select-kind", page)
            self.assertIn("id='selection-bar'", page)
            self.assertIn("data-select-id", page)


class ChannelHeaderTest(ServerTestCase):
    """The channel page edits its name, description and category in place, and adds blocks."""

    def test_set_channel_saves_only_the_fields_sent(self):
        home, parent = self.channel("Home"), self.channel("Parent")
        self.post("/connect-channel", {"source_id": home, "channel_id": parent})
        saved = self.post("/set-channel", {"channel_id": home, "title": "  Studio  "})
        self.assertEqual(saved["title"], "Studio")
        self.assertEqual(self.blocks_in(parent), ["Studio"])
        saved = self.post("/set-channel", {"channel_id": home, "description": "Work in progress", "category": "Art"})
        self.assertEqual((saved["title"], saved["description"], saved["category"]), ("Studio", "Work in progress", "Art"))
        self.assertEqual(self.query("SELECT name FROM categories"), [("Art",)])
        self.post("/set-channel", {"channel_id": home, "category": ""})
        self.assertEqual(self.query("SELECT category FROM channels WHERE id = ?", home)[0][0], "")

    def test_set_channel_refuses_an_empty_name(self):
        home = self.channel("Home")
        with self.assertRaises(HTTPError):
            self.post("/set-channel", {"channel_id": home, "title": " "})
        self.assertEqual(self.query("SELECT title FROM channels WHERE id = ?", home)[0][0], "Home")

    def test_add_block_turns_a_lone_link_into_a_link_block(self):
        home = self.channel("Home")
        self.post("/add-block", {"channel_id": home, "text": "https://www.are.na/blog/"})
        self.post("/add-block", {"channel_id": home, "text": "First line\nmore text"})
        rows = self.query("SELECT b.type, b.title, b.source_url, b.content FROM channel_blocks cb JOIN blocks b ON b.id = cb.block_id WHERE cb.channel_id = ? ORDER BY cb.position", home)
        self.assertEqual(rows, [("link", "are.na/blog", "https://www.are.na/blog/", ""), ("text", "First line", "", "First line\nmore text")])

    def test_channel_page_has_no_forms_for_editing(self):
        home = self.channel("Home")
        with urlopen(f"{self.base}/channel/{home}") as response:
            page = response.read().decode()
        self.assertIn("data-channel-field='title'", page)
        self.assertIn("class='channel-toolbar'", page)
        self.assertNotIn("action='/rename-channel'", page)
        self.assertNotIn("ADD LOCAL BLOCK", page)


if __name__ == "__main__":
    unittest.main()
