#!/usr/bin/env python3
"""Serve the local Are.na archive."""

from __future__ import annotations

import html
import json
import cgi
import hashlib
import io
import mimetypes
import os
import re
import sqlite3
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import parse_qs, quote, unquote, urlparse

ROOT = Path(__file__).parent
DATABASE = Path(os.getenv("ARENA_DATABASE", ROOT / "archive.db"))
ASSETS = Path(os.getenv("ARENA_ASSETS", ROOT / "assets"))


def esc(value: object) -> str:
    return html.escape(str(value or ""))


def title_markup(value: object) -> str:
    return esc(value).replace("_", "_<wbr>")


def layout(title: str, body: str) -> str:
    return f"""<!doctype html><html lang='en'><head><meta charset='utf-8'>
<meta name='viewport' content='width=device-width,initial-scale=1'>
<title>{esc(title)} · Are.na archive</title><link rel='stylesheet' href='/style.css'></head>
<body><header class='topbar'><a class='wordmark' href='/'>ARE.NA <span>ARCHIVE</span></a><nav class='main-nav'><a href='/'>CHANNELS</a></nav></header>
<main>{body}</main><div class='modal' id='post-modal' hidden role='dialog' aria-modal='true' aria-label='Post detail'>
<div class='modal-backdrop' data-close-modal></div><section class='modal-panel'>
<button class='modal-close' type='button' data-close-modal aria-label='Close post'>CLOSE ×</button>
<div class='modal-zoom' aria-label='Image zoom'><button type='button' data-zoom='out'>−</button><button type='button' data-zoom='reset'>100%</button><button type='button' data-zoom='in'>+</button></div>
<div id='modal-content'></div></section></div>
<script>
const modal = document.getElementById('post-modal');
const modalContent = document.getElementById('modal-content');
let modalScale = 1;
let modalPanX = 0;
let modalPanY = 0;
let panStartX = 0;
let panStartY = 0;
let panOriginX = 0;
let panOriginY = 0;
let isPanning = false;
let justPanned = false;
const applyZoom = () => {{ const image = modalContent.querySelector('.block-visual img'); if (image) image.style.transform = `translate(${{modalPanX}}px, ${{modalPanY}}px) scale(${{modalScale}})`; }};
modalContent.addEventListener('pointerdown', (event) => {{
  const image = event.target.closest('.block-visual img');
  if (!image || modalScale <= 1) return;
  image.draggable = false;
  isPanning = true;
  justPanned = false;
  panStartX = event.clientX;
  panStartY = event.clientY;
  panOriginX = modalPanX;
  panOriginY = modalPanY;
  image.setPointerCapture(event.pointerId);
  event.preventDefault();
}});
modalContent.addEventListener('dragstart', (event) => {{
  if (event.target.closest('.block-visual img')) event.preventDefault();
}});
modalContent.addEventListener('pointermove', (event) => {{
  if (!isPanning) return;
  if (Math.abs(event.clientX - panStartX) > 3 || Math.abs(event.clientY - panStartY) > 3) justPanned = true;
  modalPanX = panOriginX + event.clientX - panStartX;
  modalPanY = panOriginY + event.clientY - panStartY;
  applyZoom();
}});
modalContent.addEventListener('pointerup', () => {{ isPanning = false; setTimeout(() => {{ justPanned = false; }}, 250); }});
modalContent.addEventListener('pointercancel', () => {{ isPanning = false; justPanned = false; }});
modalContent.addEventListener('click', (event) => {{
  const image = event.target.closest('.block-visual img');
  if (!image || justPanned) return;
  event.preventDefault();
  event.stopPropagation();
  modalScale = modalScale > 1 ? 1 : 2;
  if (modalScale === 1) {{ modalPanX = 0; modalPanY = 0; }}
  applyZoom();
}});
document.addEventListener('click', (event) => {{
  const zoom = event.target.closest('[data-zoom]');
  if (!zoom) return;
  if (zoom.dataset.zoom === 'in') modalScale = Math.min(3, modalScale + .25);
  if (zoom.dataset.zoom === 'out') modalScale = Math.max(.5, modalScale - .25);
  if (zoom.dataset.zoom === 'reset') {{ modalScale = 1; modalPanX = 0; modalPanY = 0; }}
  if (modalScale <= 1) {{ modalPanX = 0; modalPanY = 0; }}
  applyZoom();
}});
const closeModal = () => {{ modal.hidden = true; modalContent.replaceChildren(); document.body.classList.remove('modal-open'); }};
document.addEventListener('click', (event) => {{
  const close = event.target.closest('[data-close-modal]');
  if (close) {{ closeModal(); return; }}
  const categoryLink = event.target.closest('[data-category-link]');
  if (categoryLink) {{ event.preventDefault(); window.location.assign(categoryLink.dataset.categoryUrl); return; }}
  if (event.target.closest('#modal-content')) return;
  const block = event.target.closest('.block');
  if (!block || event.target.closest('a, button, form')) return;
  const clone = block.cloneNode(true);
  const sourceTypes = ['image', 'link', 'text', 'embed'];
  const visual = clone.querySelector('.block-visual');
  if (clone.dataset.source && visual && sourceTypes.includes(clone.dataset.type)) {{
    const source = document.createElement('a');
    source.href = clone.dataset.source;
    source.target = '_blank';
    source.rel = 'noreferrer';
    source.className = 'modal-source';
    while (visual.firstChild) source.append(visual.firstChild);
    visual.append(source);
  }}
  modalContent.replaceChildren(clone);
  modalScale = 1;
  modalPanX = 0;
  modalPanY = 0;
  applyZoom();
  modal.hidden = false;
  document.body.classList.add('modal-open');
  modal.querySelector('.modal-close').focus();
}});
document.addEventListener('keydown', (event) => {{ if (event.key === 'Escape' && !modal.hidden) closeModal(); }});
document.addEventListener('dragstart', (event) => {{
  const block = event.target.closest('[data-draggable-block]');
  if (!block) return;
  event.dataTransfer.effectAllowed = 'copy';
  event.dataTransfer.setData('text/plain', block.dataset.blockId);
  document.body.classList.add('dragging');
}});
document.addEventListener('dragend', () => document.body.classList.remove('dragging'));
document.addEventListener('dragover', (event) => {{
  if (event.dataTransfer.types.includes('Files')) event.preventDefault();
  const target = event.target.closest('[data-drop-channel]');
  if (!target) return;
  event.preventDefault();
  target.classList.add('drop-ready');
}});
document.addEventListener('dragleave', (event) => {{
  const target = event.target.closest('[data-drop-channel]');
  if (target && !target.contains(event.relatedTarget)) target.classList.remove('drop-ready');
}});
document.addEventListener('drop', async (event) => {{
  const target = event.target.closest('[data-drop-channel]');
  if (event.dataTransfer.files.length) event.preventDefault();
  if (!target) return;
  event.preventDefault();
  target.classList.remove('drop-ready');
  if (event.dataTransfer.files.length) {{
    let uploaded = 0;
    let copyOnly = false;
    for (const [index, file] of [...event.dataTransfer.files].entries()) {{
    if (!file.type.startsWith('image/')) continue;
    let fileHandle = null;
    const droppedItem = event.dataTransfer.items[index];
    if (droppedItem && droppedItem.getAsFileSystemHandle) {{
      try {{
        fileHandle = await droppedItem.getAsFileSystemHandle();
        if (fileHandle && fileHandle.requestPermission) {{
          const permission = await fileHandle.requestPermission({{ mode: 'readwrite' }});
          if (permission !== 'granted') {{ copyOnly = true; fileHandle = null; }}
        }}
      }} catch (error) {{
        fileHandle = null;
      }}
    }}
    const form = new FormData();
    form.append('channel_id', target.dataset.dropChannel);
    form.append('image', file, file.name);
    const response = await fetch('/upload-image', {{ method: 'POST', body: form }});
    if (response.ok) {{
      uploaded += 1;
      if (fileHandle && fileHandle.remove) {{
        try {{ await fileHandle.remove(); }} catch (error) {{ copyOnly = true; }}
      }} else {{ copyOnly = true; }}
    }}
    }}
    if (!uploaded) {{ alert('No image files were added.'); return; }}
    if (copyOnly) alert(`${{uploaded}} image(s) archived; some originals could not be moved.`);
    window.location.reload();
  }} else {{
    const blockId = event.dataTransfer.getData('text/plain');
    const response = await fetch('/connect-block', {{ method: 'POST', headers: {{ 'Content-Type': 'application/x-www-form-urlencoded' }}, body: new URLSearchParams({{ block_id: blockId, channel_id: target.dataset.dropChannel }}) }});
    if (response.ok) window.location.reload();
  }}
}});
const liveSearch = document.getElementById('archive-search');
const channelCount = document.getElementById('channel-count');
if (liveSearch && channelCount) {{
  const cards = [...document.querySelectorAll('.channel-card')];
  const total = cards.length;
  liveSearch.addEventListener('input', () => {{
    const term = liveSearch.value.trim().toLowerCase();
    let visible = 0;
    cards.forEach((card) => {{
      const match = !term || card.textContent.toLowerCase().includes(term);
      card.hidden = !match;
      if (match) visible += 1;
    }});
    channelCount.textContent = `${{visible}} of ${{total}} channels`;
  }});
}}
</script></body></html>"""


def db():
    connection = sqlite3.connect(DATABASE)
    connection.row_factory = sqlite3.Row
    try:
        connection.execute("ALTER TABLE channels ADD COLUMN category TEXT NOT NULL DEFAULT ''")
    except sqlite3.OperationalError:
        pass
    connection.execute("CREATE TABLE IF NOT EXISTS categories (name TEXT PRIMARY KEY)")
    connection.execute("INSERT OR IGNORE INTO categories(name) SELECT category FROM channels WHERE category IS NOT NULL AND category != ''")
    connection.commit()
    return connection


def local_id(connection: sqlite3.Connection, table: str) -> int:
    value = connection.execute(f"SELECT COALESCE(MIN(id), 0) FROM {table}").fetchone()[0]
    return min(-1, int(value) - 1)


def cleanup_block(connection: sqlite3.Connection, block_id: int) -> None:
    if connection.execute("SELECT 1 FROM channel_blocks WHERE block_id = ? LIMIT 1", (block_id,)).fetchone():
        return
    asset = connection.execute("SELECT path FROM assets WHERE block_id = ?", (block_id,)).fetchone()
    if asset:
        relative = Path(asset["path"])
        if relative.parts and relative.parts[0] == "assets":
            relative = Path(*relative.parts[1:])
        (ASSETS / relative).unlink(missing_ok=True)
        connection.execute("DELETE FROM assets WHERE block_id = ?", (block_id,))
    connection.execute("DELETE FROM blocks WHERE id = ?", (block_id,))


def form_value(values: dict[str, list[str]], key: str) -> str:
    return values.get(key, [""])[0].strip()


def block_card(row: sqlite3.Row) -> str:
    asset = row["asset_path"]
    kind = esc(row["type"]).upper()
    source_value = esc(row["source_url"])
    if asset and row["type"] == "image":
        visual = f"<img src='/{esc(asset)}' alt='{esc(row['title'] or row['author_name'] or kind)}' loading='lazy'>"
    elif asset:
        visual = f"<a class='download-block' href='/{esc(asset)}' download>DOWNLOAD {kind}<br><strong>{esc(row['title'] or 'Attached file')}</strong></a>"
    elif row["type"] == "channel":
        visual = f"<a class='channel-block' href='{esc(row['source_url'])}'><span>CHANNEL</span><strong>{title_markup(row['title'] or 'Untitled channel')}</strong></a>"
    elif row["type"] == "text":
        visual = f"<div class='text-block'>{esc(row['content'])}</div>"
    else:
        visual = f"<div class='empty-block'><span>{kind}</span><strong>{title_markup(row['title'] or row['source_url'] or 'Untitled block')}</strong></div>"
    source = f"<a href='{esc(row['source_url'])}' target='_blank' rel='noreferrer'>SOURCE ↗</a>" if row["source_url"] and row["type"] != "channel" else ""
    source_attribute = f" data-source='{source_value}'" if source_value else ""
    remove = ""
    draggable = ""
    if "parent_channel_id" in row.keys():
        remove = f"<form class='block-remove' method='post' action='/remove-block'><input type='hidden' name='channel_id' value='{row['parent_channel_id']}'><input type='hidden' name='block_id' value='{row['id']}'><button type='submit'>REMOVE</button></form>"
        draggable = f" draggable='true' data-draggable-block data-block-id='{row['id']}'"
    return f"<article class='block' data-type='{esc(row['type'])}'{source_attribute}{draggable}><div class='block-visual'>{visual}{remove}</div><div class='block-meta'><span>{kind}</span><span>{esc(row['author_name'])}</span>{source}</div><h3>{title_markup(row['title'])}</h3></article>"


class Handler(BaseHTTPRequestHandler):
    def send_html(self, content: str, status: int = 200) -> None:
        data = content.encode()
        self.send_response(status)
        self.send_header("Content-Type", "text/html; charset=utf-8")
        self.send_header("Content-Length", str(len(data)))
        self.end_headers()
        self.wfile.write(data)

    def do_GET(self) -> None:
        parsed = urlparse(self.path)
        path = unquote(parsed.path)
        if path == "/style.css":
            data = (ROOT / "style.css").read_bytes()
            self.send_response(200)
            self.send_header("Content-Type", "text/css; charset=utf-8")
            self.send_header("Content-Length", str(len(data)))
            self.end_headers()
            self.wfile.write(data)
            return
        if path.startswith("/assets/"):
            relative = Path(path.removeprefix("/assets/"))
            if ".." in relative.parts:
                self.send_error(400)
                return
            target = ASSETS / relative
            if target.exists() and target.is_file():
                data = target.read_bytes()
                connection = db()
                asset = connection.execute("SELECT content_type FROM assets WHERE path = ?", (str(Path("assets") / relative),)).fetchone()
                connection.close()
                self.send_response(200)
                self.send_header("Content-Type", (asset["content_type"] if asset else "application/octet-stream"))
                self.send_header("Content-Length", str(len(data)))
                self.end_headers()
                self.wfile.write(data)
                return
            self.send_error(404)
            return
        try:
            if path == "/":
                self.index(parsed.query)
            elif path == "/view":
                self.view(parsed.query)
            elif path == "/search":
                self.search(parse_qs(parsed.query).get("q", [""])[0])
            elif path.startswith("/channel/"):
                self.channel(int(path.rsplit("/", 1)[1]))
            else:
                self.send_error(404)
        except (sqlite3.Error, ValueError) as error:
            self.send_html(layout("Archive error", f"<section class='notice'><h1>Archive unavailable</h1><p>{esc(error)}</p><p>Run the importer first.</p></section>"), 500)

    def do_POST(self) -> None:
        length = int(self.headers.get("Content-Length", "0"))
        body = self.rfile.read(length)
        if self.path == "/upload-image":
            try:
                self.upload_image(body)
            except (sqlite3.Error, ValueError, OSError) as error:
                self.send_html(layout("Archive error", f"<section class='notice'><h1>Could not upload image</h1><p>{esc(error)}</p></section>"), 400)
            return
        values = parse_qs(body.decode("utf-8"))
        try:
            if self.path == "/create-channel":
                self.create_channel(values)
            elif self.path == "/update-channel":
                self.update_channel(values)
            elif self.path == "/create-block":
                self.create_block(values)
            elif self.path == "/create-category":
                self.create_category(values)
            elif self.path == "/remove-block":
                self.remove_block(values)
            elif self.path == "/connect-block":
                self.connect_block(values)
            elif self.path == "/delete-channel":
                self.delete_channel(values)
            else:
                self.send_error(404)
        except (sqlite3.Error, ValueError, OSError) as error:
            self.send_html(layout("Archive error", f"<section class='notice'><h1>Could not update archive</h1><p>{esc(error)}</p></section>"), 400)

    def redirect(self, location: str) -> None:
        self.send_response(303)
        self.send_header("Location", location)
        self.end_headers()

    def create_channel(self, values: dict[str, list[str]]) -> None:
        title = form_value(values, "title") or "Untitled channel"
        description = form_value(values, "description")
        visibility = form_value(values, "visibility") or "private"
        if visibility not in {"public", "closed", "private"}:
            visibility = "private"
        connection = db()
        channel_id = local_id(connection, "channels")
        now = time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())
        category = form_value(values, "category")
        connection.execute("INSERT INTO channels (id, slug, title, description, visibility, category, created_at, updated_at, raw_json) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)", (channel_id, f"local-{abs(channel_id)}", title, description, visibility, category, now, now, json.dumps({"local": True})))
        if category:
            connection.execute("INSERT OR IGNORE INTO categories(name) VALUES (?)", (category,))
        connection.commit()
        connection.close()
        self.redirect(f"/channel/{channel_id}")

    def update_channel(self, values: dict[str, list[str]]) -> None:
        channel_id = int(form_value(values, "channel_id"))
        category = form_value(values, "new_category") or form_value(values, "category")
        connection = db()
        connection.execute("UPDATE channels SET category = ?, updated_at = ? WHERE id = ?", (category, time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()), channel_id))
        if category:
            connection.execute("INSERT OR IGNORE INTO categories(name) VALUES (?)", (category,))
        connection.commit()
        connection.close()
        self.redirect(f"/channel/{channel_id}")

    def create_category(self, values: dict[str, list[str]]) -> None:
        category = form_value(values, "category")
        if category:
            connection = db()
            connection.execute("INSERT OR IGNORE INTO categories(name) VALUES (?)", (category,))
            connection.commit()
            connection.close()
        self.redirect("/view")

    def create_block(self, values: dict[str, list[str]]) -> None:
        channel_id = int(form_value(values, "channel_id"))
        kind = form_value(values, "type") or "text"
        if kind not in {"text", "link"}:
            kind = "text"
        title = form_value(values, "title") or "Untitled block"
        content = form_value(values, "content")
        source = form_value(values, "source_url") if kind == "link" else ""
        connection = db()
        if not connection.execute("SELECT 1 FROM channels WHERE id = ?", (channel_id,)).fetchone():
            raise ValueError("channel not found")
        block_id = local_id(connection, "blocks")
        position = connection.execute("SELECT COALESCE(MAX(position), -1) + 1 FROM channel_blocks WHERE channel_id = ?", (channel_id,)).fetchone()[0]
        now = time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())
        raw = json.dumps({"local": True, "created_at": now})
        connection.execute("INSERT INTO blocks VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)", (block_id, kind, title, content, "", "Local archive", "local", source, now, now, raw))
        connection.execute("INSERT INTO channel_blocks VALUES (?, ?, ?, ?, ?)", (channel_id, block_id, position, now, raw))
        connection.commit()
        connection.close()
        self.redirect(f"/channel/{channel_id}")

    def remove_block(self, values: dict[str, list[str]]) -> None:
        channel_id = int(form_value(values, "channel_id"))
        block_id = int(form_value(values, "block_id"))
        connection = db()
        connection.execute("DELETE FROM channel_blocks WHERE channel_id = ? AND block_id = ?", (channel_id, block_id))
        cleanup_block(connection, block_id)
        connection.commit()
        connection.close()
        self.redirect(f"/channel/{channel_id}")

    def connect_block(self, values: dict[str, list[str]]) -> None:
        channel_id = int(form_value(values, "channel_id"))
        block_id = int(form_value(values, "block_id"))
        connection = db()
        if not connection.execute("SELECT 1 FROM channels WHERE id = ?", (channel_id,)).fetchone():
            raise ValueError("channel not found")
        if not connection.execute("SELECT 1 FROM blocks WHERE id = ?", (block_id,)).fetchone():
            raise ValueError("block not found")
        position = connection.execute("SELECT COALESCE(MAX(position), -1) + 1 FROM channel_blocks WHERE channel_id = ?", (channel_id,)).fetchone()[0]
        now = time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())
        connection.execute("INSERT OR IGNORE INTO channel_blocks VALUES (?, ?, ?, ?, ?)", (channel_id, block_id, position, now, json.dumps({"local": True, "connected_at": now})))
        connection.commit()
        connection.close()
        self.send_response(204)
        self.end_headers()

    def upload_image(self, body: bytes) -> None:
        content_type = self.headers.get("Content-Type", "")
        if not content_type.startswith("multipart/form-data"):
            raise ValueError("image upload must use multipart form data")
        form = cgi.FieldStorage(fp=io.BytesIO(body), headers=self.headers, environ={"REQUEST_METHOD": "POST", "CONTENT_TYPE": content_type, "CONTENT_LENGTH": str(len(body))})
        channel_id = int(form.getvalue("channel_id", "0"))
        image = form["image"] if "image" in form else None
        if image is None or not getattr(image, "file", None):
            raise ValueError("image file missing")
        image_type = image.type or mimetypes.guess_type(image.filename or "")[0] or ""
        if not image_type.startswith("image/"):
            raise ValueError("only image files are supported")
        connection = db()
        if not connection.execute("SELECT 1 FROM channels WHERE id = ?", (channel_id,)).fetchone():
            raise ValueError("channel not found")
        data = image.file.read()
        if not data:
            raise ValueError("image file is empty")
        block_id = local_id(connection, "blocks")
        digest = hashlib.sha1(data).hexdigest()[:12]
        suffix = Path(image.filename or "image").suffix.lower() or mimetypes.guess_extension(image_type) or ".bin"
        original_name = re.sub(r"[^A-Za-z0-9._-]", "_", Path(image.filename or "image").name)
        if not Path(original_name).suffix:
            original_name += suffix
        filename = f"local-{abs(block_id)}-{digest}-{original_name}"
        channel_assets = ASSETS / "channels" / str(channel_id)
        channel_assets.mkdir(parents=True, exist_ok=True)
        (channel_assets / filename).write_bytes(data)
        now = time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())
        title = Path(image.filename or "Untitled image").name
        raw = json.dumps({"local": True, "filename": title, "created_at": now})
        position = connection.execute("SELECT COALESCE(MAX(position), -1) + 1 FROM channel_blocks WHERE channel_id = ?", (channel_id,)).fetchone()[0]
        connection.execute("INSERT INTO blocks VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)", (block_id, "image", title, "", "", "Local archive", "local", "", now, now, raw))
        connection.execute("INSERT INTO assets VALUES (?, ?, ?, ?, ?, ?)", (block_id, str(Path("assets") / "channels" / str(channel_id) / filename), "", image_type, len(data), "available"))
        connection.execute("INSERT INTO channel_blocks VALUES (?, ?, ?, ?, ?)", (channel_id, block_id, position, now, raw))
        connection.commit()
        connection.close()
        self.send_response(204)
        self.end_headers()

    def delete_channel(self, values: dict[str, list[str]]) -> None:
        channel_id = int(form_value(values, "channel_id"))
        connection = db()
        block_ids = [row[0] for row in connection.execute("SELECT block_id FROM channel_blocks WHERE channel_id = ?", (channel_id,)).fetchall()]
        connection.execute("DELETE FROM channels WHERE id = ?", (channel_id,))
        for block_id in block_ids:
            cleanup_block(connection, block_id)
        connection.commit()
        connection.close()
        self.redirect("/")

    def index(self, query_string: str = "") -> None:
        params = parse_qs(query_string)
        sort = params.get("sort", ["abc"])[0]
        category = params.get("category", [""])[0]
        direction = "DESC" if params.get("direction", ["asc"])[0].lower() == "desc" else "ASC"
        order = {"newest": f"COALESCE(c.updated_at, '') {direction}, lower(c.title) ASC", "category": f"lower(c.category) {direction}, lower(c.title) ASC", "abc": f"lower(c.title) {direction}"}.get(sort, "lower(c.title) ASC")
        connection = db()
        where = "WHERE c.category = ?" if category else ""
        values = (category,) if category else ()
        channels = connection.execute(f"SELECT c.*, COUNT(cb.block_id) AS block_count FROM channels c LEFT JOIN channel_blocks cb ON cb.channel_id = c.id {where} GROUP BY c.id ORDER BY {order}", values).fetchall()
        categories = connection.execute("SELECT name FROM categories ORDER BY lower(name)").fetchall()
        connection.close()
        cards = "".join(f"<a class='channel-card' href='/channel/{row['id']}' data-drop-channel='{row['id']}'><span class='eyebrow category-chip' data-category-link data-category-url='/?sort=category&direction=asc&category={quote(row['category'] or '')}'>{esc(row['category'] or 'UNCATEGORIZED').upper()} · {row['block_count']} BLOCKS</span><h2>{title_markup(row['title'] or row['slug'])}</h2><p>{esc(row['description'])}</p></a>" for row in channels)
        empty = '<div class="notice">No channels imported yet.</div>'
        next_abc_direction = "desc" if sort == "abc" and not category and direction == "ASC" else "asc"
        category_links = "".join(f"<a class='{('active' if row['name'] == category else '')}' href='/?sort=category&direction=asc&category={quote(row['name'])}'>{esc(row['name'])}</a>" for row in categories)
        view = f"<section class='view-panel'><p class='eyebrow'>VIEW</p><nav class='view-tabs'><a class='{('active' if not category else '')}' href='/view?sort=abc&direction=asc'>ALL</a><a class='{('active' if sort == 'abc' and not category else '')}' href='/view?sort=abc&direction={next_abc_direction}'>ABC {'↓' if sort == 'abc' and direction == 'DESC' else '↑'}</a><a class='{('active' if sort == 'newest' and not category else '')}' href='/view?sort=newest&direction=desc'>NEWEST</a></nav><div class='category-links'>{category_links or '<span>NO CATEGORIES</span>'}</div><div class='view-actions'><form method='get' action='/search' role='search' class='search-form'><label class='sr-only' for='archive-search'>Search archive</label><input id='archive-search' name='q' type='search' placeholder='Search archive semantically' autocomplete='off'><button type='submit'>SEARCH</button></form></div></section>"
        create = "<section class='editor-panel'><p class='eyebrow'>EDITING</p><div class='editing-actions'><form method='post' action='/create-channel' class='editor-form'><input name='title' placeholder='New channel title' required><input name='description' placeholder='Description'><input name='category' placeholder='Category'><button type='submit'>CREATE CHANNEL</button></form><form method='post' action='/create-category' class='category-form'><input name='category' placeholder='New category' required><button type='submit'>CREATE CATEGORY</button></form></div></section>"
        self.send_html(layout("Channels", f"<section class='hero'><p class='eyebrow'>PERSONAL ARCHIVE</p><h1>Your channels.</h1><p id='channel-count'>{len(channels)} channels</p></section>{view}{create}<section class='channel-grid'>{cards or empty}</section>"))

    def view(self, query_string: str) -> None:
        self.redirect("/?" + query_string if query_string else "/")

    def channel(self, channel_id: int) -> None:
        connection = db()
        channel = connection.execute("SELECT * FROM channels WHERE id = ?", (channel_id,)).fetchone()
        if not channel:
            self.send_error(404)
            return
        blocks = connection.execute("SELECT b.*, a.path AS asset_path, a.content_type AS asset_content_type, cb.channel_id AS parent_channel_id FROM blocks b JOIN channel_blocks cb ON cb.block_id = b.id LEFT JOIN assets a ON a.block_id = b.id WHERE cb.channel_id = ? ORDER BY cb.position", (channel_id,)).fetchall()
        categories = connection.execute("SELECT name FROM categories ORDER BY lower(name)").fetchall()
        targets = connection.execute("SELECT id, title, category FROM channels WHERE id != ? ORDER BY lower(title)", (channel_id,)).fetchall()
        connection.close()
        grid = "".join(block_card(row) for row in blocks)
        empty = '<div class="notice">No imported blocks in this channel.</div>'
        category_options = "<option value=''>Uncategorized</option>" + "".join(f"<option value='{esc(row['name'])}'{' selected' if row['name'] == channel['category'] else ''}>{esc(row['name'])}</option>" for row in categories)
        target_cards = "".join(f"<div class='drop-channel' data-drop-channel='{row['id']}'><span>{esc(row['category'] or 'UNCATEGORIZED').upper()}</span><strong>{title_markup(row['title'])}</strong></div>" for row in targets)
        drop_shelf = f"<details class='drop-shelf'><summary>DRAG TO ADD TO ANOTHER CHANNEL</summary><input class='drop-search' type='search' placeholder='Find a channel' oninput=\"this.parentElement.querySelectorAll('[data-drop-channel]').forEach((card) => card.hidden = !card.textContent.toLowerCase().includes(this.value.toLowerCase()))\"><div class='drop-channel-grid'>{target_cards}</div></details>"
        editor = f"<section class='editor-panel'><p class='eyebrow'>CHANNEL CATEGORY</p><form method='post' action='/update-channel' class='editor-form'><input type='hidden' name='channel_id' value='{channel_id}'><select name='category'>{category_options}</select><input name='new_category' placeholder='Or make a new category'><button type='submit'>SAVE CATEGORY</button></form><p class='eyebrow'>ADD LOCAL BLOCK</p><form method='post' action='/create-block' class='editor-form block-editor'><input type='hidden' name='channel_id' value='{channel_id}'><select name='type'><option value='text'>Text</option><option value='link'>Link</option></select><input name='title' placeholder='Title'><textarea name='content' placeholder='Text or link description'></textarea><input name='source_url' type='url' placeholder='Source URL (for links)'><button type='submit'>ADD BLOCK</button></form></section>"
        delete = f"<form method='post' action='/delete-channel' onsubmit=\"return confirm('Delete this local channel?')\"><input type='hidden' name='channel_id' value='{channel_id}'><button class='danger-button' type='submit'>DELETE CHANNEL</button></form>"
        body = f"<a class='back' href='/'>← ALL CHANNELS</a><section class='channel-heading'><p class='eyebrow'>CHANNEL · {esc(channel['visibility'] or 'UNKNOWN').upper()}</p><h1>{title_markup(channel['title'] or channel['slug'])}</h1><p>{esc(channel['description'])}</p>{delete}</section>{drop_shelf}{editor}<section class='block-grid' data-drop-channel='{channel_id}'>{grid or empty}</section>"
        self.send_html(layout(channel["title"], body))

    def search(self, query: str) -> None:
        connection = db()
        term = f"%{query}%"
        rows = connection.execute("SELECT DISTINCT b.*, a.path AS asset_path, a.content_type AS asset_content_type FROM blocks b LEFT JOIN assets a ON a.block_id = b.id LEFT JOIN channel_blocks cb ON cb.block_id = b.id WHERE b.title LIKE ? OR b.content LIKE ? OR b.description LIKE ? OR b.author_name LIKE ? OR b.source_url LIKE ? ORDER BY b.updated_at DESC", (term, term, term, term, term)).fetchall() if query else []
        connection.close()
        grid = "".join(block_card(row) for row in rows)
        empty = '<div class="notice">No matching blocks.</div>'
        self.send_html(layout("Search", f"<section class='hero compact'><p class='eyebrow'>SEARCH</p><h1>{esc(query) or 'Search archive'}</h1><p>{len(rows)} matching blocks.</p></section><section class='block-grid'>{grid or empty}</section>"))


if __name__ == "__main__":
    port = int(os.getenv("PORT", "8765"))
    print(f"Serving {DATABASE} at http://127.0.0.1:{port}")
    ThreadingHTTPServer(("127.0.0.1", port), Handler).serve_forever()
