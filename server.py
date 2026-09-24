#!/usr/bin/env python3
"""Serve the local Are.na archive."""

from __future__ import annotations

import html
import json
import hashlib
import mimetypes
import os
import re
import sqlite3
import subprocess
import threading
import time
from email.parser import BytesParser
from email.policy import default as email_policy
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import parse_qs, quote, unquote, urlparse

ROOT = Path(__file__).parent
DATABASE = Path(os.getenv("ARENA_DATABASE", ROOT / "archive.db"))
ASSETS = Path(os.getenv("ARENA_ASSETS", ROOT / "assets"))
THUMBS = ASSETS.parent / "thumbs"
THUMB_EDGE = 800
THUMB_MIN_BYTES = 250_000
READ_ONLY = os.getenv("ARENA_READONLY") == "1"
# The Mac app starts the server with ARENA_PARENT_PID and files drops through its
# own menu bar tray, so the page leaves its tray out there.
NATIVE_APP = bool(os.getenv("ARENA_PARENT_PID"))


def esc(value: object) -> str:
    return html.escape(str(value or ""))


def title_markup(value: object) -> str:
    return esc(value).replace("_", "_<wbr>")


TRAY_TOGGLE = "<button class='tray-toggle' id='tray-toggle' type='button' aria-expanded='false' aria-controls='tray' title='Drop files, links or text here'>TRAY ↓<span id='tray-count' hidden></span></button>"

TRAY_PANEL = """<section class='tray' id='tray' hidden aria-label='Drop tray'>
<div class='tray-head'><p class='eyebrow'>FILE INTO A CHANNEL</p><button type='button' class='tray-close' id='tray-close'>CLOSE ×</button></div>
<div class='tray-held'><p id='tray-label'></p><button type='button' id='tray-clear' hidden>CLEAR</button></div>
<input id='tray-search' type='search' placeholder='Find a channel' autocomplete='off' aria-label='Find a channel'>
<div class='tray-list' id='tray-list'></div>
<div class='tray-foot'><p id='tray-status' role='status' aria-live='polite'></p><button type='button' id='tray-choose'>ADD FILES…</button><input id='tray-files' type='file' multiple hidden></div>
<label class='tray-option' hidden title="Off: the archive keeps a copy and your original stays put. On: the browser asks to delete each original once it's filed. Deleted files skip the Trash."><input type='checkbox' id='tray-remove'> DELETE ORIGINALS AFTER FILING</label>
</section>"""

# Drag and drop, and the drop tray. Plain JavaScript, kept out of the layout f-string.
DROP_SCRIPT = r"""// Files, links and text from outside the page become blocks in the channel they
// are dropped on (on a channel page, anywhere outside another target). Block and
// channel cards dragged within the page are connected instead.
const pageChannel = document.querySelector('.block-grid[data-drop-channel]');
const elementOf = (event) => event.target instanceof Element ? event.target : event.target.parentElement;
let pageDrag = false;
document.addEventListener('dragstart', (event) => {
  pageDrag = true;
  const origin = elementOf(event);
  const channel = origin && origin.closest('[data-draggable-channel]');
  if (channel) {
    event.dataTransfer.effectAllowed = 'copy';
    event.dataTransfer.setData('application/x-archive-channel', channel.dataset.draggableChannel);
    document.body.classList.add('dragging');
    return;
  }
  const block = origin && origin.closest('[data-draggable-block]');
  if (!block) return;
  event.dataTransfer.effectAllowed = 'copy';
  event.dataTransfer.setData('application/x-archive-block', block.dataset.blockId);
  document.body.classList.add('dragging');
});
document.addEventListener('dragend', () => { pageDrag = false; document.body.classList.remove('dragging'); });
const isArchiveDrag = (transfer) => ['application/x-archive-block', 'application/x-archive-channel'].some((type) => transfer.types.includes(type));
// Dragging a picture or selected text within the page is not something to file.
const isExternal = (transfer) => !pageDrag && ['Files', 'text/uri-list', 'text/plain'].some((type) => transfer.types.includes(type));

// Reads a drop, most specific first: files, then a link, then plain text. File
// handles are only available during the drop event, so they're requested now.
const canRemoveOriginals = window.isSecureContext && typeof DataTransferItem !== 'undefined' && 'getAsFileSystemHandle' in DataTransferItem.prototype;
const isWebLink = (value) => /^https?:\/\/\S+$/i.test(value || '');
const hostOf = (url) => { try { return new URL(url).host || url; } catch (error) { return url; } };
const readDrop = (transfer) => {
  const files = [];
  for (const entry of transfer.items) {
    if (entry.kind !== 'file') continue;
    const file = entry.getAsFile();
    if (!file) continue;
    const folder = Boolean(entry.webkitGetAsEntry && entry.webkitGetAsEntry()?.isDirectory);
    const handle = canRemoveOriginals ? entry.getAsFileSystemHandle().catch(() => null) : null;
    files.push({ kind: 'file', file, folder, handle, label: file.name });
  }
  if (files.length) return files;
  const [mozUrl, mozTitle] = (transfer.getData('text/x-moz-url') || '').split(/\r?\n/);
  const uri = mozUrl || (transfer.getData('text/uri-list') || '').split(/\r?\n/).map((line) => line.trim()).find((line) => line && !line.startsWith('#'));
  if (isWebLink(uri)) return [{ kind: 'link', url: uri, title: mozTitle || '', label: mozTitle || hostOf(uri) }];
  const text = (transfer.getData('text/plain') || '').trim();
  if (!text) return [];
  if (isWebLink(text)) return [{ kind: 'link', url: text, title: '', label: hostOf(text) }];
  return [{ kind: 'text', text, label: `“${text.slice(0, 40)}”` }];
};
const fileItem = (file) => ({ kind: 'file', file, folder: false, handle: null, label: file.name });

// Filing goes through the same endpoints as the rest of the page.
const postForm = (path, fields) => fetch(path, { method: 'POST', body: new URLSearchParams(fields), redirect: 'manual' });
const succeeded = (response) => response.ok || response.type === 'opaqueredirect';
// The server answers errors with an HTML notice; pull out its message.
const failure = async (response) => {
  if (response.status === 403) return 'The archive is read-only right now.';
  const page = new DOMParser().parseFromString(await response.text(), 'text/html');
  const paragraph = page.querySelector('.notice p');
  return paragraph ? paragraph.textContent : `The archive answered with error ${response.status}.`;
};
const addItem = async (item, channelId) => {
  let response;
  if (item.kind === 'file') {
    if (item.folder) throw new Error(`${item.file.name} is a folder. Drop the files inside it instead.`);
    const form = new FormData();
    form.append('channel_id', channelId);
    form.append('file', item.file, item.file.name);
    response = await fetch('/upload-file', { method: 'POST', body: form });
  } else if (item.kind === 'link') {
    response = await postForm('/create-block', { channel_id: channelId, type: 'link', title: item.title || hostOf(item.url), content: '', source_url: item.url });
  } else {
    const firstLine = item.text.split(/\r?\n/)[0];
    response = await postForm('/create-block', { channel_id: channelId, type: 'text', title: firstLine.slice(0, 60), content: item.text });
  }
  if (!succeeded(response)) throw new Error(await failure(response));
};
// Chromium can delete a dropped file once the browser has asked for permission.
// Anything else keeps the original, like any browser upload.
const removeOriginal = async (item) => {
  const handle = item.handle && await item.handle;
  if (!handle || !handle.remove) return;
  try {
    if (handle.requestPermission && await handle.requestPermission({ mode: 'readwrite' }) !== 'granted') return;
    await handle.remove();
  } catch (error) {}
};
const channelNames = new Map();
const channelName = (id) => {
  if (channelNames.has(Number(id))) return channelNames.get(Number(id));
  const card = document.querySelector(`[data-drop-channel="${id}"]:not(.block-grid)`);
  const heading = card ? card.querySelector('h2, strong') : pageChannel && pageChannel.dataset.dropChannel === String(id) ? document.querySelector('.channel-heading h1') : null;
  return heading ? heading.textContent.trim() : 'the channel';
};
const count = (n) => n === 1 ? '1 item' : `${n} items`;
const recentChannels = () => { try { return JSON.parse(localStorage.getItem('channel.recent') || '[]').filter(Number.isInteger); } catch (error) { return []; } };
const rememberRecent = (id) => { try { localStorage.setItem('channel.recent', JSON.stringify([id, ...recentChannels().filter((other) => other !== id)].slice(0, 4))); } catch (error) {} };
const fileItems = async (items, channelId, { removeOriginals, report }) => {
  const name = channelName(channelId);
  report(`Adding ${count(items.length)} to ${name}…`);
  const failed = [];
  let firstError = null;
  for (const item of items) {
    try {
      await addItem(item, channelId);
      if (removeOriginals && item.kind === 'file') await removeOriginal(item);
    } catch (error) {
      failed.push(item);
      firstError = firstError || error;
    }
  }
  const added = items.length - failed.length;
  if (added) rememberRecent(Number(channelId));
  const message = firstError ? `Added ${added} of ${items.length} to ${name}. ${firstError.message}` : `Added ${count(added)} to ${name}.`;
  return { added, failed, message };
};

const toast = document.getElementById('toast');
let toastTimer = null;
const notify = (message) => {
  toast.textContent = message;
  toast.hidden = false;
  clearTimeout(toastTimer);
  toastTimer = setTimeout(() => { toast.hidden = true; }, 4000);
};
// A reload after filing shows the new blocks; the message is carried across it.
try {
  const flashed = sessionStorage.getItem('channel.flash');
  if (flashed) { sessionStorage.removeItem('channel.flash'); notify(flashed); }
} catch (error) {}
const showsChannel = (id) => location.pathname === '/' || location.pathname === `/channel/${id}`;
const changed = (channelId, message) => {
  if (!showsChannel(channelId)) return notify(message);
  try { sessionStorage.setItem('channel.flash', message); } catch (error) {}
  window.location.reload();
};
const connectDropped = async (transfer, channelId) => {
  const channel = transfer.getData('application/x-archive-channel');
  if (channel) {
    if (channel === String(channelId)) return;
    const response = await postForm('/connect-channel', { source_id: channel, channel_id: channelId });
    return response.ok ? changed(channelId, `Nested the channel in ${channelName(channelId)}.`) : notify('Could not add the channel.');
  }
  const blockId = transfer.getData('application/x-archive-block');
  if (!/^-?\d+$/.test(blockId)) return;
  const response = await postForm('/connect-block', { block_id: blockId, channel_id: channelId });
  return response.ok ? changed(channelId, `Added the block to ${channelName(channelId)}.`) : notify('Could not add the block.');
};

const dropTarget = (event) => {
  const element = elementOf(event);
  if (!element || element.closest('input, textarea, select, #post-modal, #tray, #tray-toggle')) return null;
  return element.closest('[data-drop-channel]') || (isExternal(event.dataTransfer) ? pageChannel : null);
};
const endPageDrop = () => { document.body.classList.remove('page-drop', 'external-drag'); if (pageChannel) pageChannel.classList.remove('drop-ready'); };
document.addEventListener('dragover', (event) => {
  const transfer = event.dataTransfer;
  // Never let the browser navigate away to a dropped file.
  if (transfer.types.includes('Files')) event.preventDefault();
  const external = isExternal(transfer);
  document.body.classList.toggle('external-drag', external);
  const target = dropTarget(event);
  document.body.classList.toggle('page-drop', Boolean(target) && target === pageChannel && external);
  if (!target) return;
  event.preventDefault();
  if (target !== pageChannel) target.classList.add('drop-ready');
});
document.addEventListener('dragleave', (event) => {
  if (!event.relatedTarget) endPageDrop();
  const target = elementOf(event)?.closest('[data-drop-channel]');
  if (target && !target.contains(event.relatedTarget)) target.classList.remove('drop-ready');
});
document.addEventListener('drop', async (event) => {
  const transfer = event.dataTransfer;
  const target = dropTarget(event);
  endPageDrop();
  if (transfer.types.includes('Files')) event.preventDefault();
  if (!target) return;
  event.preventDefault();
  target.classList.remove('drop-ready');
  const channelId = target.dataset.dropChannel;
  if (isArchiveDrag(transfer)) return connectDropped(transfer, channelId);
  if (pageDrag) return;
  const items = readDrop(transfer);
  if (!items.length) return notify('Nothing to add from that drop.');
  // Like the Mac app, a drop on the page moves files into the archive where the browser allows it.
  const { added, message } = await fileItems(items, channelId, { removeOriginals: true, report: notify });
  if (added) changed(channelId, message); else notify(message);
});

// The drop tray: the web version of the Mac app's menu bar tray. Drop onto a
// channel to file there, or onto the tray to hold items and pick a channel later.
const tray = document.getElementById('tray');
if (tray) {
  const toggle = document.getElementById('tray-toggle');
  const heldCount = document.getElementById('tray-count');
  const list = document.getElementById('tray-list');
  const search = document.getElementById('tray-search');
  const label = document.getElementById('tray-label');
  const clear = document.getElementById('tray-clear');
  const status = document.getElementById('tray-status');
  const option = document.getElementById('tray-remove');
  const picker = document.getElementById('tray-files');
  let channels = [];
  let placeholder = 'Opening archive…';
  let held = [];
  let closeTimer = null;
  const say = (message) => { status.textContent = message; status.title = message; };

  const note = (text) => {
    const paragraph = document.createElement('p');
    paragraph.className = 'tray-note';
    paragraph.textContent = text;
    list.append(paragraph);
  };
  const section = (title, rows) => {
    if (!rows.length) return;
    const heading = document.createElement('p');
    heading.className = 'tray-section';
    heading.textContent = title;
    list.append(heading);
    for (const channel of rows) {
      const row = document.createElement('button');
      row.type = 'button';
      row.className = 'tray-row';
      row.dataset.trayChannel = channel.id;
      const name = document.createElement('strong');
      name.textContent = channel.title;
      const meta = document.createElement('span');
      meta.textContent = `${channel.favorite ? '★ ' : ''}${channel.category || 'Uncategorized'} · ${channel.block_count} ${channel.block_count === 1 ? 'block' : 'blocks'}`;
      row.append(name, meta);
      list.append(row);
    }
  };
  const matching = (term) => channels.filter((channel) => `${channel.title} ${channel.category}`.toLowerCase().includes(term));
  const render = () => {
    list.replaceChildren();
    if (placeholder) return note(placeholder);
    const term = search.value.trim().toLowerCase();
    if (term) {
      const matches = matching(term);
      if (matches.length) section('Matches', matches); else note(`No channels match “${search.value.trim()}”.`);
    } else {
      section('Recent', recentChannels().map((id) => channels.find((channel) => channel.id === id)).filter(Boolean));
      section('Favorites', channels.filter((channel) => channel.favorite));
      section('All channels', channels);
      if (!channels.length) note('No channels yet. Create one on the channels page.');
    }
    list.scrollTop = 0;
  };
  const refresh = async () => {
    try {
      const response = await fetch('/api/channels');
      if (!response.ok) throw new Error(await failure(response));
      const data = await response.json();
      channels = data.channels;
      channels.forEach((channel) => channelNames.set(channel.id, channel.title));
      placeholder = null;
      if (data.read_only) say('The archive is read-only right now.');
    } catch (error) {
      placeholder = `Could not load channels. ${error.message}`;
    }
    render();
  };

  const updateHeld = () => {
    heldCount.hidden = !held.length;
    heldCount.textContent = held.length;
    clear.hidden = !held.length;
    label.textContent = held.length
      ? `${held.length === 1 ? '1 item held' : `${held.length} items held`}. Click a channel to file ${held.length === 1 ? 'it' : 'them'}.\n${held.map((item) => item.label).join(', ')}`
      : 'Drop onto a channel below to file it there, or here to hold it and pick a channel later.';
  };
  const cancelAutoClose = () => { clearTimeout(closeTimer); closeTimer = null; };
  const openTray = (focus) => {
    cancelAutoClose();
    if (tray.hidden) {
      tray.hidden = false;
      toggle.setAttribute('aria-expanded', 'true');
      render();
      refresh();
    }
    if (focus) search.focus();
  };
  const closeTray = () => {
    cancelAutoClose();
    tray.hidden = true;
    toggle.setAttribute('aria-expanded', 'false');
    search.value = '';
    render();
  };
  const hold = (items) => {
    held = held.concat(items);
    updateHeld();
    say('');
    openTray(false);
  };
  const afterFiling = (channelId, { added, failed, message }) => {
    say(message);
    if (!added) return;
    refresh();
    if (failed.length || held.length) return;
    if (showsChannel(channelId)) return changed(channelId, message);
    // Close shortly after a successful drop, as the menu bar tray does.
    closeTimer = setTimeout(closeTray, 1600);
  };
  const fileInto = async (items, channelId) => {
    afterFiling(channelId, await fileItems(items, channelId, { removeOriginals: option.checked, report: say }));
  };
  // Clicking a channel files whatever is held, or opens the channel.
  const pick = async (channelId) => {
    if (!held.length) { window.location.assign(`/channel/${channelId}`); return; }
    const items = held;
    held = [];
    updateHeld();
    const result = await fileItems(items, channelId, { removeOriginals: option.checked, report: say });
    // Anything that failed stays held so you can try another channel.
    held = result.failed.concat(held);
    updateHeld();
    afterFiling(channelId, result);
  };

  toggle.addEventListener('click', () => { if (tray.hidden) openTray(true); else closeTray(); });
  document.getElementById('tray-close').addEventListener('click', closeTray);
  clear.addEventListener('click', () => { held = []; updateHeld(); say(''); });
  list.addEventListener('click', (event) => {
    const row = event.target.closest('[data-tray-channel]');
    if (row) pick(Number(row.dataset.trayChannel));
  });
  search.addEventListener('input', render);
  // Return files into (or opens) the first match.
  search.addEventListener('keydown', (event) => {
    if (event.key !== 'Enter') return;
    event.preventDefault();
    const term = search.value.trim().toLowerCase();
    const first = term && matching(term)[0];
    if (first) pick(first.id);
  });
  document.getElementById('tray-choose').addEventListener('click', () => picker.click());
  picker.addEventListener('change', () => {
    if (picker.files.length) hold([...picker.files].map(fileItem));
    picker.value = '';
  });
  if (canRemoveOriginals) {
    option.closest('label').hidden = false;
    try { option.checked = localStorage.getItem('channel.removeOriginals') === '1'; } catch (error) {}
    option.addEventListener('change', () => { try { localStorage.setItem('channel.removeOriginals', option.checked ? '1' : '0'); } catch (error) {} });
  }
  document.addEventListener('pointerdown', (event) => {
    if (!tray.hidden && !tray.contains(event.target) && !toggle.contains(event.target)) closeTray();
  });
  document.addEventListener('keydown', (event) => {
    if (event.key === 'Escape' && !tray.hidden && modal.hidden) closeTray();
  });
  window.addEventListener('beforeunload', (event) => {
    if (!held.length) return;
    event.preventDefault();
    event.returnValue = '';
  });

  // Dragging over the toggle opens the tray; dropping on it holds the items.
  const clearMarks = () => {
    tray.classList.remove('hold-ready');
    toggle.classList.remove('drop-ready');
    list.querySelectorAll('.drop-ready').forEach((row) => row.classList.remove('drop-ready'));
  };
  const holdDrop = (event) => {
    if (pageDrag) return;
    const items = readDrop(event.dataTransfer);
    if (items.length) hold(items); else say('Nothing to add from that drop.');
  };
  toggle.addEventListener('dragover', (event) => {
    event.preventDefault();
    event.stopPropagation();
    toggle.classList.add('drop-ready');
    openTray(false);
  });
  toggle.addEventListener('dragleave', () => toggle.classList.remove('drop-ready'));
  toggle.addEventListener('drop', (event) => {
    event.preventDefault();
    event.stopPropagation();
    endPageDrop();
    clearMarks();
    holdDrop(event);
  });
  tray.addEventListener('dragover', (event) => {
    event.preventDefault();
    event.stopPropagation();
    document.body.classList.remove('page-drop');
    cancelAutoClose();
    // You can't scroll while dragging, so hovering near the list's edges scrolls it.
    const box = list.getBoundingClientRect();
    if (event.clientY < box.top + 36) list.scrollTop -= 14;
    else if (event.clientY > box.bottom - 36) list.scrollTop += 14;
    const row = elementOf(event)?.closest('[data-tray-channel]');
    list.querySelectorAll('.drop-ready').forEach((other) => { if (other !== row) other.classList.remove('drop-ready'); });
    if (row) row.classList.add('drop-ready');
    tray.classList.toggle('hold-ready', !row && isExternal(event.dataTransfer));
  });
  tray.addEventListener('dragleave', (event) => { if (!tray.contains(event.relatedTarget)) clearMarks(); });
  tray.addEventListener('drop', (event) => {
    event.preventDefault();
    event.stopPropagation();
    endPageDrop();
    clearMarks();
    const row = elementOf(event)?.closest('[data-tray-channel]');
    if (!row) return holdDrop(event);
    const channelId = Number(row.dataset.trayChannel);
    if (isArchiveDrag(event.dataTransfer)) return connectDropped(event.dataTransfer, channelId);
    if (pageDrag) return;
    const items = readDrop(event.dataTransfer);
    if (items.length) fileInto(items, channelId); else say('Nothing to add from that drop.');
  });
  updateHeld();
}
"""


def layout(title: str, body: str) -> str:
    return f"""<!doctype html><html lang='en'><head><meta charset='utf-8'>
<meta name='viewport' content='width=device-width,initial-scale=1'>
<title>{esc(title)} · CHANNEL</title><link rel='stylesheet' href='/style.css'></head>
<body><header class='topbar'><a class='wordmark' href='/'>CHANNEL</a><nav class='main-nav'><a href='/'>CHANNEL</a></nav>{'' if NATIVE_APP else TRAY_TOGGLE}</header>
{'' if NATIVE_APP else TRAY_PANEL}<p class='toast' id='toast' role='status' aria-live='polite' hidden></p>
<main>{body}</main><div class='modal' id='post-modal' hidden role='dialog' aria-modal='true' aria-label='Post detail'>
<div class='modal-backdrop' data-close-modal></div><section class='modal-panel'>
<button class='modal-close' type='button' data-close-modal aria-label='Close post'>CLOSE ×</button>
<div class='modal-zoom' aria-label='Image zoom'><button type='button' data-zoom='out'>−</button><button type='button' data-zoom='reset'>100%</button><button type='button' data-zoom='in'>+</button></div>
<div id='modal-content'></div></section></div>
<script>
const readOnly = {'true' if READ_ONLY else 'false'};
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
const closeModal = () => {{ modalContent.querySelector(':focus')?.blur(); modal.hidden = true; modalContent.replaceChildren(); document.body.classList.remove('modal-open'); }};
document.addEventListener('click', (event) => {{
  const close = event.target.closest('[data-close-modal]');
  if (close) {{ closeModal(); return; }}
  const categoryLink = event.target.closest('[data-category-link]');
  if (categoryLink) {{ event.preventDefault(); window.location.assign(categoryLink.dataset.categoryUrl); return; }}
  if (event.target.closest('#modal-content')) return;
  const block = event.target.closest('.block');
  if (!block || event.target.closest('a, button, form')) return;
  openBlock(block);
}});
let currentBlock = null;
const openBlock = (block) => {{
  currentBlock = block;
  const clone = block.cloneNode(true);
  // A draggable clone would start a drag instead of letting you select text in the fields.
  clone.removeAttribute('draggable');
  clone.removeAttribute('data-draggable-block');
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
  if (block.dataset.download) {{
    const download = document.createElement('a');
    download.className = 'modal-download';
    download.href = block.dataset.download;
    download.download = '';
    download.textContent = 'DOWNLOAD ↓';
    clone.querySelector('.block-visual').append(download);
  }}
  clone.append(titleEditor(block, clone), linkEditor(block), noteEditor(block, clone));
  showClone(clone);
  // The grid thumbnail shows instantly; the full image replaces it once decoded.
  const image = clone.querySelector('img[data-full]');
  if (image) {{
    image.removeAttribute('loading');
    loadFull(image.dataset.full).then((full) => {{ if (currentBlock === block && full) image.src = full.src; }});
  }}
  const blocks = visibleBlocks();
  const index = blocks.indexOf(block);
  for (const neighbour of [blocks[index + 1], blocks[index - 1]]) {{
    const next = neighbour && neighbour.querySelector('img[data-full]');
    if (next) loadFull(next.dataset.full);
  }}
}};
// The name and note are edited in the modal and saved as you type.
const saveField = (path, block, body) => fetch(path, {{ method: 'POST', headers: {{ 'Content-Type': 'application/x-www-form-urlencoded' }}, body: new URLSearchParams({{ block_id: block.dataset.blockId, ...body }}) }});
const autosave = (field, save) => {{
  let pending = null;
  field.addEventListener('input', () => {{ clearTimeout(pending); pending = setTimeout(save, 600); }});
  field.addEventListener('blur', () => {{ clearTimeout(pending); save(); }});
  field.addEventListener('keydown', (event) => event.stopPropagation());
}};
const titleEditor = (block, clone) => {{
  const stored = block.querySelector('h3');
  clone.querySelector('h3')?.remove();
  const editor = document.createElement('input');
  editor.className = 'modal-title';
  editor.type = 'text';
  editor.placeholder = readOnly ? 'Names are read-only' : 'Untitled';
  editor.setAttribute('aria-label', 'Name');
  editor.value = stored ? stored.textContent : '';
  editor.readOnly = readOnly;
  if (!readOnly) autosave(editor, () => {{
    const title = editor.value.trim();
    if (stored) stored.textContent = title;
    saveField('/set-title', block, {{ title }});
  }});
  return editor;
}};
const linkEditor = (block) => {{
  const editor = document.createElement('input');
  editor.className = 'modal-link';
  editor.type = 'url';
  editor.placeholder = readOnly ? 'Links are read-only' : 'Add a link';
  editor.setAttribute('aria-label', 'Link');
  editor.value = block.dataset.source || '';
  editor.readOnly = readOnly;
  if (!readOnly) autosave(editor, async () => {{
    const response = await saveField('/set-source', block, {{ source_url: editor.value.trim() }});
    if (!response.ok) return;
    const {{ source_url: link }} = await response.json();
    if (document.activeElement !== editor) editor.value = link;
    if (link) block.dataset.source = link; else delete block.dataset.source;
    const meta = block.querySelector('.block-meta');
    let anchor = meta && meta.querySelector('a');
    if (link && meta && !anchor) {{
      anchor = document.createElement('a');
      anchor.target = '_blank';
      anchor.rel = 'noreferrer';
      anchor.textContent = 'SOURCE ↗';
      meta.append(anchor);
    }}
    if (anchor) {{ if (link) anchor.href = link; else anchor.remove(); }}
  }});
  return editor;
}};
// Each block carries a note, edited in the modal and saved as you type.
const noteEditor = (block, clone) => {{
  const stored = block.querySelector('.block-note');
  clone.querySelector('.block-note')?.remove();
  const editor = document.createElement('textarea');
  editor.className = 'modal-note';
  editor.rows = 2;
  editor.placeholder = readOnly ? 'Notes are read-only' : 'Add a note';
  editor.setAttribute('aria-label', 'Note');
  editor.value = stored ? stored.textContent : '';
  editor.readOnly = readOnly;
  if (readOnly) return editor;
  autosave(editor, () => {{
    const note = editor.value.trim();
    if (stored) {{ stored.textContent = note; stored.hidden = !note; }}
    saveField('/set-note', block, {{ note }});
  }});
  return editor;
}};
const fullImages = new Map();
const loadFull = (url) => {{
  if (!fullImages.has(url)) {{
    const full = new Image();
    const loaded = new Promise((resolve) => {{ full.onload = () => resolve(full); full.onerror = () => resolve(null); }});
    full.src = url;
    // Prefer a decoded image, but never wait on decode() for long (it stalls in background windows).
    fullImages.set(url, loaded.then((image) => image && Promise.race([image.decode().catch(() => {{}}), new Promise((resolve) => setTimeout(resolve, 250))]).then(() => image)));
    if (fullImages.size > 12) fullImages.delete(fullImages.keys().next().value);
  }}
  return fullImages.get(url);
}};
// The Mac app asks which channel sits under a dropped file.
window.channelAt = (x, y) => {{
  const element = document.elementFromPoint(x, y);
  const target = (element && element.closest('[data-drop-channel]')) || pageChannel;
  return target ? target.dataset.dropChannel : '';
}};
const visibleBlocks = () => [...document.querySelectorAll('main .block')].filter((block) => block.offsetParent !== null);
const showClone = (clone) => {{
  modalContent.replaceChildren(clone);
  modalScale = 1;
  modalPanX = 0;
  modalPanY = 0;
  applyZoom();
  modal.hidden = false;
  document.body.classList.add('modal-open');
  modal.querySelector('.modal-close').focus();
}};
const stepModal = (direction) => {{
  const blocks = visibleBlocks();
  const next = blocks[blocks.indexOf(currentBlock) + direction];
  if (next) openBlock(next);
}};
document.addEventListener('keydown', (event) => {{
  if (modal.hidden) return;
  if (event.key === 'Escape') closeModal();
  if (event.key === 'ArrowRight') {{ event.preventDefault(); stepModal(1); }}
  if (event.key === 'ArrowLeft') {{ event.preventDefault(); stepModal(-1); }}
}});
{DROP_SCRIPT}
const liveSearch = document.getElementById('archive-search');
const channelCount = document.getElementById('channel-count');
if (liveSearch) {{
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
    if (channelCount) channelCount.textContent = `${{visible}} of ${{total}} channels`;
  }});
}}
</script></body></html>"""


def db():
    connection = sqlite3.connect(DATABASE)
    connection.row_factory = sqlite3.Row
    for table, column in (("channels", "category TEXT NOT NULL DEFAULT ''"), ("channels", "favorite INTEGER NOT NULL DEFAULT 0"), ("blocks", "note TEXT NOT NULL DEFAULT ''")):
        try:
            connection.execute(f"ALTER TABLE {table} ADD COLUMN {column}")
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
        for suffix in (".jpg", ".png", ".ql.png"):
            (THUMBS / (relative.name + suffix)).unlink(missing_ok=True)
        connection.execute("DELETE FROM assets WHERE block_id = ?", (block_id,))
    connection.execute("DELETE FROM blocks WHERE id = ?", (block_id,))


def form_value(values: dict[str, list[str]], key: str) -> str:
    return values.get(key, [""])[0].strip()


def preview(source: Path) -> Path | None:
    """A Quick Look picture of a PDF, document or video, drawn once and kept."""
    target = THUMBS / (source.name + ".ql.png")
    if target.exists():
        return target
    THUMBS.mkdir(parents=True, exist_ok=True)
    scratch = THUMBS / f".ql-{threading.get_ident()}"
    scratch.mkdir(parents=True, exist_ok=True)
    subprocess.run(["qlmanage", "-t", "-s", str(THUMB_EDGE), "-o", str(scratch), str(source)], capture_output=True)
    drawn = next(iter(sorted(scratch.glob("*.png"))), None) if scratch.exists() else None
    if drawn:
        drawn.replace(target)
    if scratch.exists():
        for leftover in scratch.iterdir():
            leftover.unlink(missing_ok=True)
        scratch.rmdir()
    return target if target.exists() else None


def thumbnail(source: Path, content_type: str) -> Path:
    """Downscale a large image once with macOS `sips`; small images and GIFs are served as-is."""
    if content_type == "image/gif" or source.stat().st_size < THUMB_MIN_BYTES:
        return source
    png = content_type == "image/png"
    target = THUMBS / (source.name + (".png" if png else ".jpg"))
    if target.exists():
        return target
    THUMBS.mkdir(parents=True, exist_ok=True)
    scratch = target.with_name(f".{target.name}.{threading.get_ident()}")
    result = subprocess.run(["sips", "-Z", str(THUMB_EDGE), "-s", "format", "png" if png else "jpeg", str(source), "--out", str(scratch)], capture_output=True)
    if result.returncode or not scratch.exists():
        scratch.unlink(missing_ok=True)
        return source
    scratch.replace(target)
    return target


def block_card(row: sqlite3.Row) -> str:
    asset = row["asset_path"]
    kind = esc(row["type"]).upper()
    source_value = esc(row["source_url"])
    if asset and row["type"] == "image":
        thumb = "/thumbs/" + asset.removeprefix("assets/")
        visual = f"<img src='{esc(thumb)}' data-full='/{esc(asset)}' alt='{esc(row['title'] or row['author_name'] or kind)}' loading='lazy' decoding='async'>"
    elif asset:
        # A div, not a link: attachments open the modal like every other block, and
        # the modal carries the download.
        preview_src = "/thumbs/" + asset.removeprefix("assets/")
        picture = f"<img class='block-preview' src='{esc(preview_src)}' alt='' loading='lazy' decoding='async' onerror='this.remove()'>"
        visual = f"<div class='download-block'>{picture}{kind}<br><strong>{esc(row['title'] or 'Attached file')}</strong></div>"
    elif row["type"] == "channel":
        visual = f"<a class='channel-block' href='{esc(row['source_url'])}'><span>CHANNEL</span><strong>{title_markup(row['title'] or 'Untitled channel')}</strong></a>"
    elif row["type"] == "text":
        visual = f"<div class='text-block'>{esc(row['content'])}</div>"
    else:
        visual = f"<div class='empty-block'><span>{kind}</span><strong>{title_markup(row['title'] or row['source_url'] or 'Untitled block')}</strong></div>"
    source = f"<a href='{esc(row['source_url'])}' target='_blank' rel='noreferrer'>SOURCE ↗</a>" if row["source_url"] and row["type"] != "channel" else ""
    download = f" data-download='/{esc(asset)}'" if asset and row["type"] != "image" else ""
    source_attribute = f" data-source='{source_value}'" if source_value else ""
    note = row["note"] if "note" in row.keys() else ""
    note_markup = f"<p class='block-note'{'' if note else ' hidden'}>{esc(note)}</p>"
    remove = ""
    draggable = ""
    if "parent_channel_id" in row.keys():
        remove = f"<form class='block-remove' method='post' action='/remove-block'><input type='hidden' name='channel_id' value='{row['parent_channel_id']}'><input type='hidden' name='block_id' value='{row['id']}'><button type='submit'>REMOVE</button></form>"
        draggable = " draggable='true' data-draggable-block"
    return f"<article class='block' data-type='{esc(row['type'])}' data-block-id='{row['id']}'{source_attribute}{download}{draggable}><div class='block-visual'>{visual}{remove}</div><div class='block-meta'><span>{kind}</span><span>{esc(row['author_name'])}</span>{source}</div><h3>{title_markup(row['title'])}</h3>{note_markup}</article>"


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
        if path.startswith(("/assets/", "/thumbs/")):
            relative = Path(path.split("/", 2)[2])
            if ".." in relative.parts:
                self.send_error(400)
                return
            target = ASSETS / relative
            if target.exists() and target.is_file():
                connection = db()
                asset = connection.execute("SELECT content_type FROM assets WHERE path = ?", (str(Path("assets") / relative),)).fetchone()
                connection.close()
                content_type = asset["content_type"] if asset else "application/octet-stream"
                if path.startswith("/thumbs/"):
                    if content_type.startswith("image/"):
                        target = thumbnail(target, content_type)
                    else:
                        drawn = preview(target)
                        if drawn is None:
                            self.send_error(404)
                            return
                        target = drawn
                    if target.parent == THUMBS:
                        content_type = "image/png" if target.suffix == ".png" else "image/jpeg"
                data = target.read_bytes()
                self.send_response(200)
                self.send_header("Content-Type", content_type)
                self.send_header("Content-Length", str(len(data)))
                # Asset names carry a content hash, so the browser can keep them indefinitely.
                self.send_header("Cache-Control", "public, max-age=31536000, immutable")
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
            elif path == "/api/channels":
                self.channels_json()
            else:
                self.send_error(404)
        except (sqlite3.Error, ValueError) as error:
            self.send_html(layout("Archive error", f"<section class='notice'><h1>Archive unavailable</h1><p>{esc(error)}</p><p>Run the importer first.</p></section>"), 500)

    def do_POST(self) -> None:
        length = int(self.headers.get("Content-Length", "0"))
        body = self.rfile.read(length)
        if READ_ONLY:
            self.send_html(layout("Read-only", "<section class='notice'><h1>Archive is read-only</h1><p>It is open on another Mac. Quit it there, then reopen the app here to edit.</p></section>"), 403)
            return
        if self.path in ("/upload-image", "/upload-file"):
            try:
                self.upload_file(body, images_only=self.path == "/upload-image")
            except (sqlite3.Error, ValueError, OSError) as error:
                self.send_html(layout("Archive error", f"<section class='notice'><h1>Could not upload file</h1><p>{esc(error)}</p></section>"), 400)
            return
        values = parse_qs(body.decode("utf-8"))
        try:
            if self.path == "/create-channel":
                self.create_channel(values)
            elif self.path == "/update-channel":
                self.update_channel(values)
            elif self.path == "/rename-channel":
                self.rename_channel(values)
            elif self.path == "/create-block":
                self.create_block(values)
            elif self.path == "/create-category":
                self.create_category(values)
            elif self.path == "/remove-block":
                self.remove_block(values)
            elif self.path == "/connect-block":
                self.connect_block(values)
            elif self.path == "/connect-channel":
                self.connect_channel(values)
            elif self.path == "/delete-channel":
                self.delete_channel(values)
            elif self.path == "/set-title":
                self.set_title(values)
            elif self.path == "/set-source":
                self.set_source(values)
            elif self.path == "/set-note":
                self.set_note(values)
            elif self.path == "/toggle-favorite":
                self.toggle_favorite(values)
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

    def rename_channel(self, values: dict[str, list[str]]) -> None:
        channel_id = int(form_value(values, "channel_id"))
        title = form_value(values, "title")
        if not title:
            raise ValueError("channel name cannot be empty")
        connection = db()
        connection.execute("UPDATE channels SET title = ?, updated_at = ? WHERE id = ?", (title, time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()), channel_id))
        connection.execute("UPDATE blocks SET title = ? WHERE type = 'channel' AND source_url = ?", (title, f"/channel/{channel_id}"))
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
        connection.execute("INSERT INTO blocks (id, type, title, content, description, author_name, author_slug, source_url, created_at, updated_at, raw_json) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)", (block_id, kind, title, content, "", "Local archive", "local", source, now, now, raw))
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

    def set_title(self, values: dict[str, list[str]]) -> None:
        block_id = int(form_value(values, "block_id"))
        title = form_value(values, "title")[:300]
        connection = db()
        if not connection.execute("SELECT 1 FROM blocks WHERE id = ?", (block_id,)).fetchone():
            raise ValueError("block not found")
        connection.execute("UPDATE blocks SET title = ? WHERE id = ?", (title, block_id))
        connection.commit()
        connection.close()
        self.send_response(204)
        self.end_headers()

    def set_source(self, values: dict[str, list[str]]) -> None:
        block_id = int(form_value(values, "block_id"))
        link = form_value(values, "source_url")[:2000]
        if link and not re.match(r"^[a-zA-Z][a-zA-Z0-9+.-]*:", link):
            link = "https://" + link
        connection = db()
        if not connection.execute("SELECT 1 FROM blocks WHERE id = ?", (block_id,)).fetchone():
            raise ValueError("block not found")
        connection.execute("UPDATE blocks SET source_url = ? WHERE id = ?", (link, block_id))
        connection.commit()
        connection.close()
        data = json.dumps({"source_url": link}).encode()
        self.send_response(200)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(data)))
        self.end_headers()
        self.wfile.write(data)

    def set_note(self, values: dict[str, list[str]]) -> None:
        block_id = int(form_value(values, "block_id"))
        note = values.get("note", [""])[0].strip()[:2000]
        connection = db()
        if not connection.execute("SELECT 1 FROM blocks WHERE id = ?", (block_id,)).fetchone():
            raise ValueError("block not found")
        connection.execute("UPDATE blocks SET note = ? WHERE id = ?", (note, block_id))
        connection.commit()
        connection.close()
        self.send_response(204)
        self.end_headers()

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

    def connect_channel(self, values: dict[str, list[str]]) -> None:
        source_id = int(form_value(values, "source_id"))
        channel_id = int(form_value(values, "channel_id"))
        if source_id == channel_id:
            raise ValueError("a channel cannot contain itself")
        connection = db()
        source = connection.execute("SELECT title, slug FROM channels WHERE id = ?", (source_id,)).fetchone()
        if not source or not connection.execute("SELECT 1 FROM channels WHERE id = ?", (channel_id,)).fetchone():
            raise ValueError("channel not found")
        now = time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())
        # Nested channels are 'channel' blocks that link to the channel page; reuse one if it exists.
        existing = connection.execute("SELECT id FROM blocks WHERE type = 'channel' AND source_url = ?", (f"/channel/{source_id}",)).fetchone()
        if existing:
            block_id = existing["id"]
        else:
            block_id = local_id(connection, "blocks")
            connection.execute("INSERT INTO blocks (id, type, title, content, description, author_name, author_slug, source_url, created_at, updated_at, raw_json) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)", (block_id, "channel", source["title"] or source["slug"], "", "", "Local archive", "local", f"/channel/{source_id}", now, now, json.dumps({"local": True, "channel_id": source_id})))
        position = connection.execute("SELECT COALESCE(MAX(position), -1) + 1 FROM channel_blocks WHERE channel_id = ?", (channel_id,)).fetchone()[0]
        connection.execute("INSERT OR IGNORE INTO channel_blocks VALUES (?, ?, ?, ?, ?)", (channel_id, block_id, position, now, json.dumps({"local": True, "connected_at": now})))
        connection.commit()
        connection.close()
        self.send_response(204)
        self.end_headers()

    def upload_file(self, body: bytes, images_only: bool) -> None:
        content_type = self.headers.get("Content-Type", "")
        if not content_type.startswith("multipart/form-data"):
            raise ValueError("image upload must use multipart form data")
        message = BytesParser(policy=email_policy).parsebytes(f"Content-Type: {content_type}\r\n\r\n".encode() + body)
        fields = {part.get_param("name", header="content-disposition"): part for part in message.iter_parts()}
        channel_id = int((fields["channel_id"].get_content() if "channel_id" in fields else "0").strip() or "0")
        image = fields.get("image") or fields.get("file")
        if image is None or image.get_filename() is None:
            raise ValueError("file missing")
        upload_name = image.get_filename()
        image_type = image.get_content_type() if image.get_content_type() != "application/octet-stream" else mimetypes.guess_type(upload_name)[0] or "application/octet-stream"
        is_image = image_type.startswith("image/")
        if images_only and not is_image:
            raise ValueError("only image files are supported")
        source = fields["source_url"].get_content().strip() if "source_url" in fields else ""
        connection = db()
        if not connection.execute("SELECT 1 FROM channels WHERE id = ?", (channel_id,)).fetchone():
            raise ValueError("channel not found")
        data = image.get_payload(decode=True) or b""
        if not data:
            raise ValueError("image file is empty")
        block_id = local_id(connection, "blocks")
        digest = hashlib.sha1(data).hexdigest()[:12]
        suffix = Path(upload_name or "image").suffix.lower() or mimetypes.guess_extension(image_type) or ".bin"
        original_name = re.sub(r"[^A-Za-z0-9._-]", "_", Path(upload_name or "image").name)
        if not Path(original_name).suffix:
            original_name += suffix
        filename = f"local-{abs(block_id)}-{digest}-{original_name}"
        channel_assets = ASSETS / "channels" / str(channel_id)
        channel_assets.mkdir(parents=True, exist_ok=True)
        (channel_assets / filename).write_bytes(data)
        now = time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())
        title = Path(upload_name or "Untitled image").name
        raw = json.dumps({"local": True, "filename": title, "created_at": now})
        position = connection.execute("SELECT COALESCE(MAX(position), -1) + 1 FROM channel_blocks WHERE channel_id = ?", (channel_id,)).fetchone()[0]
        connection.execute("INSERT INTO blocks (id, type, title, content, description, author_name, author_slug, source_url, created_at, updated_at, raw_json) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)", (block_id, "image" if is_image else "attachment", title, "", "", "Local archive", "local", source, now, now, raw))
        connection.execute("INSERT INTO assets VALUES (?, ?, ?, ?, ?, ?)", (block_id, str(Path("assets") / "channels" / str(channel_id) / filename), "", image_type, len(data), "available"))
        connection.execute("INSERT INTO channel_blocks VALUES (?, ?, ?, ?, ?)", (channel_id, block_id, position, now, raw))
        connection.commit()
        connection.close()
        self.send_response(204)
        self.end_headers()

    def channels_json(self) -> None:
        connection = db()
        rows = connection.execute("SELECT c.id, c.title, c.slug, c.category, c.favorite, c.updated_at, COUNT(cb.block_id) AS block_count FROM channels c LEFT JOIN channel_blocks cb ON cb.channel_id = c.id GROUP BY c.id ORDER BY lower(c.title)").fetchall()
        connection.close()
        channels = [{"id": row["id"], "title": row["title"] or row["slug"] or "Untitled channel", "category": row["category"] or "", "favorite": bool(row["favorite"]), "updated_at": row["updated_at"] or "", "block_count": row["block_count"]} for row in rows]
        data = json.dumps({"channels": channels, "read_only": READ_ONLY}).encode()
        self.send_response(200)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(data)))
        self.end_headers()
        self.wfile.write(data)

    def toggle_favorite(self, values: dict[str, list[str]]) -> None:
        channel_id = int(form_value(values, "channel_id"))
        connection = db()
        connection.execute("UPDATE channels SET favorite = 1 - favorite WHERE id = ?", (channel_id,))
        connection.commit()
        connection.close()
        next_url = form_value(values, "next")
        self.redirect(next_url if next_url.startswith("/") and not next_url.startswith("//") else f"/channel/{channel_id}")

    def delete_channel(self, values: dict[str, list[str]]) -> None:
        channel_id = int(form_value(values, "channel_id"))
        connection = db()
        block_ids = [row[0] for row in connection.execute("SELECT block_id FROM channel_blocks WHERE channel_id = ?", (channel_id,)).fetchall()]
        nested = [row[0] for row in connection.execute("SELECT id FROM blocks WHERE type = 'channel' AND source_url = ?", (f"/channel/{channel_id}",)).fetchall()]
        connection.execute("DELETE FROM channel_blocks WHERE channel_id = ?", (channel_id,))
        connection.executemany("DELETE FROM channel_blocks WHERE block_id = ?", [(block_id,) for block_id in nested])
        connection.execute("DELETE FROM channels WHERE id = ?", (channel_id,))
        block_ids += nested
        for block_id in block_ids:
            cleanup_block(connection, block_id)
        connection.commit()
        connection.close()
        self.redirect("/")

    def index(self, query_string: str = "") -> None:
        params = parse_qs(query_string)
        sort = params.get("sort", ["abc"])[0]
        category = params.get("category", [""])[0]
        favorites_only = params.get("favorites", [""])[0] == "1"
        direction = "DESC" if params.get("direction", ["asc"])[0].lower() == "desc" else "ASC"
        order = {"newest": f"COALESCE(c.updated_at, '') {direction}, lower(c.title) ASC", "category": f"lower(c.category) {direction}, lower(c.title) ASC", "abc": f"lower(c.title) {direction}"}.get(sort, "lower(c.title) ASC")
        connection = db()
        conditions = (["c.category = ?"] if category else []) + (["c.favorite = 1"] if favorites_only else [])
        where = "WHERE " + " AND ".join(conditions) if conditions else ""
        values = (category,) if category else ()
        channels = connection.execute(f"SELECT c.*, COUNT(cb.block_id) AS block_count FROM channels c LEFT JOIN channel_blocks cb ON cb.channel_id = c.id {where} GROUP BY c.id ORDER BY {order}", values).fetchall()
        categories = connection.execute("SELECT name FROM categories ORDER BY lower(name)").fetchall()
        favorites = connection.execute("SELECT id, title, slug FROM channels WHERE favorite = 1 ORDER BY lower(title)").fetchall()
        connection.close()
        cards = "".join(f"<a class='channel-card' href='/channel/{row['id']}' data-drop-channel='{row['id']}' draggable='true' data-draggable-channel='{row['id']}'><span class='eyebrow category-chip' data-category-link data-category-url='/?sort=category&direction=asc&category={quote(row['category'] or '')}'>{'★ ' if row['favorite'] else ''}{esc(row['category'] or 'UNCATEGORIZED').upper()} · {row['block_count']} BLOCKS</span><h2>{title_markup(row['title'] or row['slug'])}</h2><p>{esc(row['description'])}</p></a>" for row in channels)
        empty = '<div class="notice">No channels imported yet.</div>'
        filtered = bool(category or favorites_only)
        favorites_param = "&favorites=1" if favorites_only else ""
        next_abc_direction = "desc" if sort == "abc" and direction == "ASC" else "asc"
        abc_arrow = "↓" if sort == "abc" and direction == "DESC" else "↑"
        show_tabs = f"<a class='{('active' if not filtered else '')}' href='/view?sort={sort if sort != 'category' else 'abc'}&direction={direction.lower()}'>ALL</a><a class='{('active' if favorites_only else '')}' href='/view?sort={sort if sort != 'category' else 'abc'}&direction={direction.lower()}&favorites=1'>FAVORITES</a>"
        sort_tabs = f"<a class='{('active' if sort == 'abc' else '')}' href='/view?sort=abc&direction={next_abc_direction}{favorites_param}'>A–Z {abc_arrow}</a><a class='{('active' if sort == 'newest' else '')}' href='/view?sort=newest&direction=desc{favorites_param}'>NEWEST</a>"
        favorite_links = "".join(f"<a href='/channel/{row['id']}'>{title_markup(row['title'] or row['slug'])}</a>" for row in favorites) or "<em>Star a channel to pin it here</em>"
        category_links = "".join(f"<a class='{('active' if row['name'] == category else '')}' href='/?sort=category&direction=asc&category={quote(row['name'])}'>{esc(row['name'])}</a>" for row in categories) or "<em>No categories yet</em>"
        search = "<form method='get' action='/search' role='search' class='search-form'><label class='sr-only' for='archive-search'>Search archive</label><input id='archive-search' name='q' type='search' placeholder='Search archive' autocomplete='off'><button type='submit'>SEARCH</button></form>"
        controls = f"<div class='view-line'><nav class='view-tabs'>{show_tabs}</nav><p class='view-label'>SORT</p><nav class='view-tabs'>{sort_tabs}</nav><p class='view-label channel-count' id='channel-count'>{len(channels)} channels</p>{search}</div>"
        rows = [("SHOW", controls), ("FAVORITES", f"<div class='favorite-links'>{favorite_links}</div>"), ("CATEGORIES", f"<div class='category-links'>{category_links}</div>")]
        view = "<section class='view-panel'>" + "".join(f"<div class='view-row'><p class='view-label'>{label}</p>{content}</div>" for label, content in rows) + "</section>"
        create = "<section class='editor-panel'><p class='eyebrow'>EDITING</p><div class='editing-actions'><form method='post' action='/create-channel' class='editor-form'><input name='title' placeholder='New channel title' required><input name='description' placeholder='Description'><input name='category' placeholder='Category'><button type='submit'>CREATE CHANNEL</button></form><form method='post' action='/create-category' class='category-form'><input name='category' placeholder='New category' required><button type='submit'>CREATE CATEGORY</button></form></div></section>"
        self.send_html(layout("Channels", f"{view}{create}<section class='channel-grid'>{cards or empty}</section>"))

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
        editor = f"<section class='editor-panel'><p class='eyebrow'>CHANNEL NAME</p><form method='post' action='/rename-channel' class='editor-form'><input type='hidden' name='channel_id' value='{channel_id}'><input name='title' value='{esc(channel['title'])}' placeholder='Channel name' required aria-label='Channel name'><button type='submit'>RENAME CHANNEL</button></form><p class='eyebrow'>CHANNEL CATEGORY</p><form method='post' action='/update-channel' class='editor-form'><input type='hidden' name='channel_id' value='{channel_id}'><select name='category'>{category_options}</select><input name='new_category' placeholder='Or make a new category'><button type='submit'>SAVE CATEGORY</button></form><p class='eyebrow'>ADD LOCAL BLOCK</p><form method='post' action='/create-block' class='editor-form block-editor'><input type='hidden' name='channel_id' value='{channel_id}'><select name='type'><option value='text'>Text</option><option value='link'>Link</option></select><input name='title' placeholder='Title'><textarea name='content' placeholder='Text or link description'></textarea><input name='source_url' type='url' placeholder='Source URL (for links)'><button type='submit'>ADD BLOCK</button></form></section>"
        favorite_label = "★ FAVORITE" if channel["favorite"] else "☆ ADD TO FAVORITES"
        favorite = f"<form method='post' action='/toggle-favorite'><input type='hidden' name='channel_id' value='{channel_id}'><button class='favorite-button{' is-favorite' if channel['favorite'] else ''}' type='submit' title='{'Remove from favorites' if channel['favorite'] else 'Add to favorites'}'>{favorite_label}</button></form>"
        delete = f"<form method='post' action='/delete-channel' onsubmit=\"return confirm('Delete this local channel?')\"><input type='hidden' name='channel_id' value='{channel_id}'><button class='danger-button' type='submit'>DELETE CHANNEL</button></form>"
        body = f"<a class='back' href='/'>← BACK</a><section class='channel-heading'><p class='eyebrow'>CHANNEL · {esc(channel['visibility'] or 'UNKNOWN').upper()}</p><h1>{title_markup(channel['title'] or channel['slug'])}</h1><p>{esc(channel['description'])}</p><div class='channel-actions'>{favorite}{delete}</div></section>{drop_shelf}{editor}<section class='block-grid' data-drop-channel='{channel_id}'>{grid or empty}</section>"
        self.send_html(layout(channel["title"], body))

    def search(self, query: str) -> None:
        connection = db()
        term = f"%{query}%"
        rows = connection.execute("SELECT DISTINCT b.*, a.path AS asset_path, a.content_type AS asset_content_type FROM blocks b LEFT JOIN assets a ON a.block_id = b.id LEFT JOIN channel_blocks cb ON cb.block_id = b.id WHERE b.title LIKE ? OR b.content LIKE ? OR b.description LIKE ? OR b.author_name LIKE ? OR b.source_url LIKE ? ORDER BY b.updated_at DESC", (term, term, term, term, term)).fetchall() if query else []
        connection.close()
        grid = "".join(block_card(row) for row in rows)
        empty = '<div class="notice">No matching blocks.</div>'
        self.send_html(layout("Search", f"<section class='hero compact'><p class='eyebrow'>SEARCH</p><h1>{esc(query) or 'Search archive'}</h1><p>{len(rows)} matching blocks.</p></section><section class='block-grid'>{grid or empty}</section>"))


def exit_with_parent(parent_pid: int) -> None:
    while True:
        time.sleep(2)
        if os.getppid() != parent_pid:
            os._exit(0)


if __name__ == "__main__":
    if os.getenv("ARENA_PARENT_PID"):
        threading.Thread(target=exit_with_parent, args=(int(os.environ["ARENA_PARENT_PID"]),), daemon=True).start()
    port = int(os.getenv("PORT", "8765"))
    print(f"Serving {DATABASE} at http://127.0.0.1:{port}")
    ThreadingHTTPServer(("127.0.0.1", port), Handler).serve_forever()
