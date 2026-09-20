#!/usr/bin/env python3
"""Serve the local Are.na archive."""

from __future__ import annotations

import html
import os
import sqlite3
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import parse_qs, quote, unquote, urlparse

ROOT = Path(__file__).parent
DATABASE = Path(os.getenv("ARENA_DATABASE", ROOT / "archive.db"))
ASSETS = Path(os.getenv("ARENA_ASSETS", ROOT / "assets"))


def esc(value: object) -> str:
    return html.escape(str(value or ""))


def layout(title: str, body: str) -> str:
    return f"""<!doctype html><html lang='en'><head><meta charset='utf-8'>
<meta name='viewport' content='width=device-width,initial-scale=1'>
<title>{esc(title)} · Are.na archive</title><link rel='stylesheet' href='/style.css'></head>
<body><header class='topbar'><a class='wordmark' href='/'>ARE.NA <span>ARCHIVE</span></a>
<form action='/search'><input name='q' placeholder='Search archive' aria-label='Search archive'></form></header>
<main>{body}</main><div class='modal' id='post-modal' hidden role='dialog' aria-modal='true' aria-label='Post detail'>
<div class='modal-backdrop' data-close-modal></div><section class='modal-panel'>
<button class='modal-close' type='button' data-close-modal aria-label='Close post'>CLOSE ×</button>
<div id='modal-content'></div></section></div>
<script>
const modal = document.getElementById('post-modal');
const modalContent = document.getElementById('modal-content');
const closeModal = () => {{ modal.hidden = true; modalContent.replaceChildren(); document.body.classList.remove('modal-open'); }};
document.addEventListener('click', (event) => {{
  const close = event.target.closest('[data-close-modal]');
  if (close) {{ closeModal(); return; }}
  const block = event.target.closest('.block');
  if (!block || event.target.closest('a')) return;
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
  modal.hidden = false;
  document.body.classList.add('modal-open');
  modal.querySelector('.modal-close').focus();
}});
document.addEventListener('keydown', (event) => {{ if (event.key === 'Escape' && !modal.hidden) closeModal(); }});
</script></body></html>"""


def db():
    connection = sqlite3.connect(DATABASE)
    connection.row_factory = sqlite3.Row
    return connection


def block_card(row: sqlite3.Row) -> str:
    asset = row["asset_path"]
    kind = esc(row["type"]).upper()
    source_value = esc(row["source_url"])
    if asset and row["type"] == "image":
        visual = f"<img src='/{esc(asset)}' alt='{esc(row['title'] or row['author_name'] or kind)}' loading='lazy'>"
    elif asset:
        visual = f"<a class='download-block' href='/{esc(asset)}' download>DOWNLOAD {kind}<br><strong>{esc(row['title'] or 'Attached file')}</strong></a>"
    elif row["type"] == "text":
        visual = f"<div class='text-block'>{esc(row['content'])}</div>"
    else:
        visual = f"<div class='empty-block'><span>{kind}</span><strong>{esc(row['title'] or row['source_url'] or 'Untitled block')}</strong></div>"
    source = f"<a href='{esc(row['source_url'])}' target='_blank' rel='noreferrer'>SOURCE ↗</a>" if row["source_url"] else ""
    source_attribute = f" data-source='{source_value}'" if source_value else ""
    return f"<article class='block' data-type='{esc(row['type'])}'{source_attribute}><div class='block-visual'>{visual}</div><div class='block-meta'><span>{kind}</span><span>{esc(row['author_name'])}</span>{source}</div><h3>{esc(row['title'])}</h3></article>"


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
            target = ASSETS / Path(path.removeprefix("/assets/")).name
            if target.exists() and target.is_file():
                data = target.read_bytes()
                connection = db()
                asset = connection.execute("SELECT content_type FROM assets WHERE path = ?", (str(Path("assets") / target.name),)).fetchone()
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
                self.index()
            elif path == "/search":
                self.search(parse_qs(parsed.query).get("q", [""])[0])
            elif path.startswith("/channel/"):
                self.channel(int(path.rsplit("/", 1)[1]))
            else:
                self.send_error(404)
        except (sqlite3.Error, ValueError) as error:
            self.send_html(layout("Archive error", f"<section class='notice'><h1>Archive unavailable</h1><p>{esc(error)}</p><p>Run the importer first.</p></section>"), 500)

    def index(self) -> None:
        connection = db()
        channels = connection.execute("SELECT c.*, COUNT(cb.block_id) AS block_count FROM channels c LEFT JOIN channel_blocks cb ON cb.channel_id = c.id GROUP BY c.id ORDER BY lower(c.title)").fetchall()
        connection.close()
        cards = "".join(f"<a class='channel-card' href='/channel/{row['id']}'><span class='eyebrow'>CHANNEL · {row['block_count']} BLOCKS · {esc(row['visibility'] or 'UNKNOWN').upper()}</span><h2>{esc(row['title'] or row['slug'])}</h2><p>{esc(row['description'])}</p></a>" for row in channels)
        empty = '<div class="notice">No channels imported yet.</div>'
        self.send_html(layout("Channels", f"<section class='hero'><p class='eyebrow'>PERSONAL ARCHIVE</p><h1>Your channels.</h1><p>Public Are.na material, stored locally.</p></section><section class='channel-grid'>{cards or empty}</section>"))

    def channel(self, channel_id: int) -> None:
        connection = db()
        channel = connection.execute("SELECT * FROM channels WHERE id = ?", (channel_id,)).fetchone()
        if not channel:
            self.send_error(404)
            return
        blocks = connection.execute("SELECT b.*, a.path AS asset_path, a.content_type AS asset_content_type FROM blocks b JOIN channel_blocks cb ON cb.block_id = b.id LEFT JOIN assets a ON a.block_id = b.id WHERE cb.channel_id = ? ORDER BY cb.position", (channel_id,)).fetchall()
        connection.close()
        grid = "".join(block_card(row) for row in blocks)
        empty = '<div class="notice">No imported blocks in this channel.</div>'
        body = f"<a class='back' href='/'>← ALL CHANNELS</a><section class='channel-heading'><p class='eyebrow'>CHANNEL</p><h1>{esc(channel['title'] or channel['slug'])}</h1><p>{esc(channel['description'])}</p></section><section class='block-grid'>{grid or empty}</section>"
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
