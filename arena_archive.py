#!/usr/bin/env python3
"""Import a public Are.na profile into a local SQLite archive."""

from __future__ import annotations

import argparse
import email.utils
import hashlib
import json
import mimetypes
import os
import re
import sqlite3
import sys
import time
from pathlib import Path
from urllib.error import HTTPError, URLError
from urllib.parse import quote, urlparse
from urllib.request import Request, urlopen

API_ROOT = "https://api.are.na/v3"


SCHEMA = """
PRAGMA foreign_keys = ON;
CREATE TABLE IF NOT EXISTS channels (
  id INTEGER PRIMARY KEY, slug TEXT NOT NULL, title TEXT NOT NULL,
  description TEXT, visibility TEXT, category TEXT NOT NULL DEFAULT '', created_at TEXT, updated_at TEXT,
  raw_json TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS categories (name TEXT PRIMARY KEY);
CREATE TABLE IF NOT EXISTS blocks (
  id INTEGER PRIMARY KEY, type TEXT NOT NULL, title TEXT, content TEXT,
  description TEXT, author_name TEXT, author_slug TEXT, source_url TEXT,
  created_at TEXT, updated_at TEXT, raw_json TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS channel_blocks (
  channel_id INTEGER NOT NULL REFERENCES channels(id) ON DELETE CASCADE,
  block_id INTEGER NOT NULL REFERENCES blocks(id) ON DELETE CASCADE,
  position INTEGER NOT NULL, connected_at TEXT, raw_json TEXT NOT NULL,
  PRIMARY KEY (channel_id, block_id)
);
CREATE TABLE IF NOT EXISTS assets (
  block_id INTEGER PRIMARY KEY REFERENCES blocks(id) ON DELETE CASCADE,
  path TEXT NOT NULL, source_url TEXT NOT NULL, content_type TEXT,
  size INTEGER NOT NULL, status TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS imports (
  id INTEGER PRIMARY KEY AUTOINCREMENT, profile TEXT NOT NULL,
  started_at TEXT NOT NULL, finished_at TEXT, channel_count INTEGER DEFAULT 0,
  block_count INTEGER DEFAULT 0, omitted_count INTEGER DEFAULT 0
);
CREATE TABLE IF NOT EXISTS import_errors (
  id INTEGER PRIMARY KEY AUTOINCREMENT, import_id INTEGER REFERENCES imports(id),
  channel_id INTEGER, block_id INTEGER, message TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_channel_blocks_position
  ON channel_blocks(channel_id, position);
CREATE INDEX IF NOT EXISTS idx_blocks_search
  ON blocks(title, content, description, author_name);
"""


def text(value: object) -> str:
    if value is None:
        return ""
    if isinstance(value, dict):
        return str(value.get("plain") or value.get("markdown") or value.get("html") or "")
    return str(value)


def nested(data: dict, *keys: str) -> object:
    value: object = data
    for key in keys:
        if not isinstance(value, dict):
            return None
        value = value.get(key)
    return value


class ArenaClient:
    def __init__(self, token: str | None = None, fixture: dict | None = None):
        self.token = token
        self.fixture = fixture
        self.last_request = 0.0

    def get(self, path: str, params: dict[str, object] | None = None) -> dict:
        if self.fixture is not None:
            if path.startswith("/users/"):
                return self.fixture["profile"]
            match = re.search(r"/channels/([^/]+)/contents", path)
            return self.fixture["contents"][match.group(1)] if match else {}
        query = ""
        if params:
            query = "?" + "&".join(f"{quote(str(k))}={quote(str(v))}" for k, v in params.items())
        headers = {"Accept": "application/json", "User-Agent": "arena-local-archive/1.0"}
        if self.token:
            headers["Authorization"] = f"Bearer {self.token}"
        for attempt in range(7):
            wait = 0.75 - (time.monotonic() - self.last_request)
            if wait > 0:
                time.sleep(wait)
            request = Request(API_ROOT + path + query, headers=headers)
            try:
                with urlopen(request, timeout=45) as response:
                    self.last_request = time.monotonic()
                    return json.loads(response.read().decode("utf-8"))
            except HTTPError as error:
                self.last_request = time.monotonic()
                if error.code != 429 or attempt == 6:
                    raise
                retry_after = error.headers.get("Retry-After", "")
                try:
                    delay = max(1.0, float(retry_after))
                except ValueError:
                    try:
                        retry_at = email.utils.parsedate_to_datetime(retry_after).timestamp()
                        delay = max(1.0, retry_at - time.time())
                    except (TypeError, ValueError, OverflowError):
                        delay = min(60.0, 2.0 ** attempt)
                print(f"Rate limited by Are.na; retrying in {delay:.0f}s...", file=sys.stderr)
                time.sleep(delay)
        raise RuntimeError("Are.na request retry limit reached")

    def pages(self, path: str, params: dict[str, object] | None = None) -> list[dict]:
        results: list[dict] = []
        page = 1
        while True:
            request_params = {"page": page, "per": 100}
            if params:
                request_params.update(params)
            payload = self.get(path, request_params)
            items = payload.get("data", payload if isinstance(payload, list) else [])
            results.extend(items)
            meta = payload.get("meta", {}) if isinstance(payload, dict) else {}
            if not meta.get("has_more_pages") and not meta.get("next_page"):
                break
            page = int(meta.get("next_page") or page + 1)
        return results


def block_type(item: dict) -> str:
    value = item.get("type") or item.get("class") or item.get("base_class") or "block"
    return str(value).lower().replace("block", "") or "block"


def source_url(item: dict) -> str:
    source = item.get("source")
    if isinstance(source, dict):
        return text(source.get("url"))
    return text(source or item.get("url"))


def media_url(item: dict) -> str:
    # Attachments carry both a preview image and the file itself; archive the file.
    attachment = item.get("attachment")
    if isinstance(attachment, dict):
        link = text(attachment.get("url") or attachment.get("src"))
        if link:
            return link
    image = item.get("image") or item.get("source")
    if isinstance(image, dict):
        original = image.get("original") or image.get("large") or image.get("display")
        if isinstance(original, dict):
            return text(original.get("src") or original.get("url"))
        return text(original or image.get("src") or image.get("url"))
    return ""


def safe_filename(block_id: int, url: str, content_type: str | None) -> str:
    suffix = Path(urlparse(url).path).suffix.lower()
    if not suffix:
        suffix = mimetypes.guess_extension(content_type or "") or ".bin"
    suffix = re.sub(r"[^a-z0-9.]", "", suffix)[:8] or ".bin"
    return f"{block_id}-{hashlib.sha1(url.encode()).hexdigest()[:10]}{suffix}"


def download(url: str, destination: Path) -> tuple[str, int, str]:
    for attempt in range(5):
        request = Request(url, headers={"User-Agent": "arena-local-archive/1.0"})
        try:
            with urlopen(request, timeout=60) as response:
                data = response.read()
                destination.write_bytes(data)
                return response.headers.get_content_type(), len(data), str(destination)
        except HTTPError as error:
            if error.code != 429 or attempt == 4:
                raise
            try:
                delay = max(1.0, float(error.headers.get("Retry-After", "")))
            except ValueError:
                delay = min(60.0, 2.0 ** attempt)
            print(f"Media host rate limited; retrying in {delay:.0f}s...", file=sys.stderr)
            time.sleep(delay)
    raise RuntimeError("Media download retry limit reached")


def import_archive(profile: str, database: Path, assets: Path, client: ArenaClient) -> dict:
    database.parent.mkdir(parents=True, exist_ok=True)
    assets.mkdir(parents=True, exist_ok=True)
    db = sqlite3.connect(database)
    db.executescript(SCHEMA)
    try:
        db.execute("ALTER TABLE channels ADD COLUMN category TEXT NOT NULL DEFAULT ''")
    except sqlite3.OperationalError:
        pass
    # This is a one-time archive: rerunning should produce a clean snapshot.
    db.executescript("DELETE FROM channel_blocks; DELETE FROM assets; DELETE FROM blocks; DELETE FROM channels;")
    for old_asset in assets.iterdir():
        if old_asset.is_file():
            old_asset.unlink()
    started = time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())
    import_id = db.execute(
        "INSERT INTO imports(profile, started_at) VALUES (?, ?)", (profile, started)
    ).lastrowid
    authenticated_scope = False
    if client.token:
        try:
            # V3's authenticated search scope represents the account's channel set,
            # including channels not created by the user but present in their space.
            channels = client.pages("/search", {"query": "*", "type": "Channel", "scope": "my"})
            authenticated_scope = True
        except HTTPError as error:
            if error.code != 401:
                raise
            print("Warning: Are.na rejected ARENA_TOKEN; using public owned-channel fallback.", file=sys.stderr)
            channels = client.pages(f"/users/{quote(profile)}/contents", {"type": "Channel"})
    else:
        channels = client.pages(f"/users/{quote(profile)}/contents", {"type": "Channel"})
    channels = [
        item for item in channels
        if str(item.get("type", item.get("base_class", ""))).lower() in {"channel", "channels"}
        and (authenticated_scope or text(item.get("visibility") or item.get("status")).lower() != "private")
        and (authenticated_scope or (item.get("owner") or {}).get("slug") == profile)
    ]
    omitted = 0
    block_ids: set[int] = set()
    for channel in channels:
        channel_id = int(channel["id"])
        db.execute(
            "INSERT OR REPLACE INTO channels (id, slug, title, description, visibility, category, created_at, updated_at, raw_json) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)",
            (channel_id, text(channel.get("slug")), text(channel.get("title")),
             text(channel.get("description")), text(channel.get("visibility") or channel.get("status")),
             "", text(channel.get("created_at")), text(channel.get("updated_at")), json.dumps(channel)),
        )
        try:
            contents = client.pages(f"/channels/{channel_id}/contents")
        except Exception as error:
            db.execute("INSERT INTO import_errors(import_id, channel_id, message) VALUES (?, ?, ?)", (import_id, channel_id, str(error)))
            continue
        for position, item in enumerate(contents):
            if str(item.get("type", item.get("base_class", ""))).lower() in {"channel", "channels"}:
                continue
            block_id = int(item["id"])
            kind = block_type(item)
            url = media_url(item) if kind in {"image", "attachment"} else ""
            if kind in {"image", "attachment"} and not url:
                omitted += 1
                db.execute("INSERT INTO import_errors(import_id, channel_id, block_id, message) VALUES (?, ?, ?, ?)", (import_id, channel_id, block_id, "missing media URL"))
                continue
            author = item.get("user") or item.get("owner") or {}
            db.execute(
                "INSERT OR IGNORE INTO blocks (id, type, title, content, description, author_name, author_slug, source_url, created_at, updated_at, raw_json) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
                (block_id, kind, text(item.get("title") or item.get("generated_title")),
                 text(item.get("content")), text(item.get("description")), text(author.get("full_name") or author.get("username")),
                 text(author.get("slug")), source_url(item), text(item.get("created_at")), text(item.get("updated_at")), json.dumps(item)),
            )
            db.execute(
                "INSERT OR REPLACE INTO channel_blocks VALUES (?, ?, ?, ?, ?)",
                (channel_id, block_id, int(item.get("connection", {}).get("position", item.get("position", position))),
                 text(item.get("connected_at")), json.dumps(item)),
            )
            if url and block_id not in block_ids:
                filename = safe_filename(block_id, url, None)
                target = assets / filename
                try:
                    content_type, size, path = download(url, target)
                    db.execute("INSERT OR REPLACE INTO assets VALUES (?, ?, ?, ?, ?, ?)", (block_id, str(Path("assets") / filename), url, content_type, size, "available"))
                except (HTTPError, URLError, OSError, TimeoutError) as error:
                    omitted += 1
                    db.execute("DELETE FROM channel_blocks WHERE channel_id = ? AND block_id = ?", (channel_id, block_id))
                    db.execute("DELETE FROM blocks WHERE id = ? AND NOT EXISTS (SELECT 1 FROM channel_blocks WHERE block_id = ?)", (block_id, block_id))
                    db.execute("INSERT INTO import_errors(import_id, channel_id, block_id, message) VALUES (?, ?, ?, ?)", (import_id, channel_id, block_id, f"media download failed: {error}"))
                    continue
                block_ids.add(block_id)
        db.commit()
    finished = time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())
    counts = db.execute("SELECT COUNT(*) FROM blocks").fetchone()[0]
    db.execute("UPDATE imports SET finished_at = ?, channel_count = ?, block_count = ?, omitted_count = ? WHERE id = ?", (finished, len(channels), counts, omitted, import_id))
    db.commit()
    db.close()
    return {"channels": len(channels), "blocks": counts, "omitted": omitted, "database": str(database), "assets": str(assets)}


def repair_attachments(database: Path, assets: Path, dry_run: bool = False) -> dict:
    """Earlier imports stored Are.na's preview image for attachment blocks.
    Fetch the real file for any attachment whose stored asset is the preview."""
    db = sqlite3.connect(database)
    db.row_factory = sqlite3.Row
    rows = db.execute("SELECT b.id, b.title, b.raw_json, a.path, a.content_type FROM blocks b JOIN assets a ON a.block_id = b.id WHERE b.type = 'attachment'").fetchall()
    wrong = []
    for row in rows:
        attachment = (json.loads(row["raw_json"]).get("attachment") or {})
        url = text(attachment.get("url") or attachment.get("src"))
        if url and row["content_type"] != attachment.get("content_type"):
            wrong.append((row, url))
    if dry_run:
        db.close()
        return {"repairable": len(wrong), "titles": [row["title"] for row, _ in wrong]}
    repaired, failed = 0, []
    for row, url in wrong:
        filename = safe_filename(row["id"], url, None)
        target = assets / filename
        try:
            content_type, size, _ = download(url, target)
        except (HTTPError, URLError, OSError, TimeoutError, RuntimeError) as error:
            failed.append(f"{row['title']}: {error}")
            continue
        previous = Path(row["path"])
        if previous.name != filename:
            (assets / previous.name).unlink(missing_ok=True)
        db.execute("UPDATE assets SET path = ?, source_url = ?, content_type = ?, size = ? WHERE block_id = ?",
                   (str(Path("assets") / filename), url, content_type, size, row["id"]))
        db.commit()
        repaired += 1
    db.close()
    return {"repaired": repaired, "failed": failed}


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    sub = parser.add_subparsers(dest="command", required=True)
    command = sub.add_parser("import")
    command.add_argument("profile")
    command.add_argument("--database", type=Path, default=Path("archive.db"))
    command.add_argument("--assets", type=Path, default=Path("assets"))
    command.add_argument("--fixture", type=Path)
    repair = sub.add_parser("repair-attachments")
    repair.add_argument("--database", type=Path, default=Path("archive.db"))
    repair.add_argument("--assets", type=Path, default=Path("assets"))
    repair.add_argument("--dry-run", action="store_true")
    args = parser.parse_args()
    if args.command == "repair-attachments":
        print(json.dumps(repair_attachments(args.database, args.assets, args.dry_run), indent=2))
        return
    fixture = json.loads(args.fixture.read_text()) if args.fixture else None
    try:
        result = import_archive(args.profile, args.database, args.assets, ArenaClient(os.getenv("ARENA_TOKEN"), fixture))
    except (HTTPError, URLError, OSError, KeyError, ValueError) as error:
        print(f"Import failed: {error}", file=sys.stderr)
        raise SystemExit(1)
    print(json.dumps(result, indent=2))


if __name__ == "__main__":
    main()
