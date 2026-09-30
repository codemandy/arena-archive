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
# A regenerable cache; the Mac app keeps it outside iCloud so it never syncs.
THUMBS = Path(os.getenv("ARENA_THUMBS", ASSETS.parent / "thumbs"))
THUMB_EDGE = 800
THUMB_MIN_BYTES = 250_000
READ_ONLY = os.getenv("ARENA_READONLY") == "1"
# How a channel lays out its blocks; the first is the default.
BLOCK_VIEWS = ("large", "small", "stack", "abc")


def esc(value: object) -> str:
    return html.escape(str(value or ""))


def title_markup(value: object) -> str:
    return esc(value).replace("_", "_<wbr>")


# Select blocks in a channel, or channels on the main page, and act on them together.
# Click SELECT (or ⌘-click / shift-click an item) to start. A plain string, so no doubled braces.
SELECTION_SCRIPT = r"""<script>
(() => {
  const bar = document.getElementById('selection-bar');
  const grid = document.querySelector('[data-select-kind]');
  if (!bar || !grid) return;
  const kind = grid.dataset.selectKind;
  const channelId = kind === 'blocks' ? grid.dataset.dropChannel : '';
  const toggle = document.querySelector('[data-select-toggle]');
  const count = document.getElementById('selection-count');
  const selectAllButton = bar.querySelector('[data-batch="all"]');
  const dialog = document.getElementById('batch-dialog');
  const form = document.getElementById('batch-form');
  const selected = new Set();
  let selecting = false;
  let anchor = null;
  let run = null;
  const allItems = () => [...grid.querySelectorAll('[data-select-id]')];
  const visibleItems = () => allItems().filter((item) => item.offsetParent !== null);
  const selectedIds = () => allItems().map((item) => item.dataset.selectId).filter((id) => selected.has(id));
  const noun = (n) => `${n} ${kind === 'blocks' ? (n === 1 ? 'block' : 'blocks') : (n === 1 ? 'channel' : 'channels')}`;
  const esc = (value) => String(value ?? '').replace(/[&<>"']/g, (c) => ({ '&': '&amp;', '<': '&lt;', '>': '&gt;', '"': '&quot;', "'": '&#39;' }[c]));
  const nameOf = (id) => grid.querySelector(`[data-select-id="${CSS.escape(id)}"] :is(h2, h3)`)?.textContent.trim() || '';
  const render = () => {
    allItems().forEach((item) => item.classList.toggle('is-selected', selected.has(item.dataset.selectId)));
    count.textContent = `${selected.size} SELECTED`;
    bar.querySelectorAll('[data-needs]').forEach((button) => { button.disabled = selected.size < Number(button.dataset.needs); });
    const visible = visibleItems();
    selectAllButton.textContent = visible.length && visible.every((item) => selected.has(item.dataset.selectId)) ? 'SELECT NONE' : 'SELECT ALL';
    bar.hidden = !selecting;
    document.body.classList.toggle('selecting', selecting);
    if (toggle) {
      toggle.setAttribute('aria-pressed', String(selecting));
      toggle.textContent = selecting ? 'DONE SELECTING' : `SELECT ${kind.toUpperCase()}`;
    }
  };
  const setSelecting = (on) => {
    selecting = on;
    if (!on) { selected.clear(); anchor = null; }
    render();
  };
  const selectAll = () => {
    const visible = visibleItems();
    const all = visible.every((item) => selected.has(item.dataset.selectId));
    visible.forEach((item) => all ? selected.delete(item.dataset.selectId) : selected.add(item.dataset.selectId));
    render();
  };
  if (toggle) toggle.addEventListener('click', () => setSelecting(!selecting));
  // Captured before the page's own click handling, so a selecting click never opens a block or channel.
  document.addEventListener('click', (event) => {
    const item = event.target.closest('[data-select-id]');
    if (!item || !grid.contains(item) || !modal.hidden) return;
    if (!selecting && !event.metaKey && !event.shiftKey) return;
    event.preventDefault();
    event.stopPropagation();
    const id = item.dataset.selectId;
    const visible = visibleItems();
    const from = visible.findIndex((other) => other.dataset.selectId === anchor);
    if (event.shiftKey && from >= 0) {
      const to = visible.indexOf(item);
      visible.slice(Math.min(from, to), Math.max(from, to) + 1).forEach((other) => selected.add(other.dataset.selectId));
    } else if (selected.has(id)) {
      selected.delete(id);
    } else {
      selected.add(id);
    }
    anchor = id;
    selecting = true;
    render();
  }, true);
  document.addEventListener('keydown', (event) => {
    if (!modal.hidden || dialog.open || event.target.closest('input, textarea, select, [contenteditable]')) return;
    if (selecting && (event.metaKey || event.ctrlKey) && event.key.toLowerCase() === 'a') { event.preventDefault(); selectAll(); }
    else if (selecting && event.key === 'Escape') setSelecting(false);
    else if (selected.size && (event.key === 'Backspace' || event.key === 'Delete')) { event.preventDefault(); openAction('delete'); }
  });
  bar.addEventListener('click', (event) => {
    const button = event.target.closest('[data-batch]');
    if (!button || button.disabled) return;
    if (button.dataset.batch === 'done') setSelecting(false);
    else if (button.dataset.batch === 'all') selectAll();
    else openAction(button.dataset.batch);
  });

  const post = async (path, fields) => {
    const body = new URLSearchParams(fields);
    for (const id of selectedIds()) body.append(kind === 'blocks' ? 'block_ids' : 'channel_ids', id);
    if (channelId) body.append('channel_id', channelId);
    const response = await fetch(path, { method: 'POST', headers: { 'Content-Type': 'application/x-www-form-urlencoded' }, body });
    if (!response.ok) {
      const page = new DOMParser().parseFromString(await response.text(), 'text/html');
      throw new Error(page.querySelector('.notice p')?.textContent || 'The archive could not be updated.');
    }
    return response.json();
  };
  // Mirrors batch_titles() in server.py so the preview matches what gets saved.
  const batchTitles = (title, n, numbered) => {
    if (!numbered || (n === 1 && !title.includes('#'))) return Array(n).fill(title);
    const width = String(n).length;
    return Array.from({ length: n }, (_, index) => {
      const number = String(index + 1).padStart(width, '0');
      return title.includes('#') ? title.replaceAll('#', number) : `${title} ${number}`;
    });
  };
  const footer = (label, danger = false) => `<p class='batch-error' hidden></p><div class='batch-actions'><button type='button' data-cancel>CANCEL</button><button type='submit' class='${danger ? 'danger' : ''}'>${esc(label)}</button></div>`;
  const field = (name) => form.elements.namedItem(name);
  const checked = (name) => (field(name)?.checked ? '1' : '0');

  const openAction = async (action) => {
    const ids = selectedIds();
    const n = ids.length;
    if (!n) return;
    const things = noun(n).toUpperCase();
    if (action === 'rename') {
      form.innerHTML = `<p class='eyebrow'>RENAME ${things}</p>
        <label class='batch-field'><span>New name</span><input name='title' required autocomplete='off' value='${esc(nameOf(ids[0]))}'></label>
        ${n > 1 ? `<label class='batch-check'><input type='checkbox' name='numbered' checked> Number them in order. Put <b>#</b> in the name to place the number.</label>` : ''}
        <p class='batch-hint' data-preview></p>${footer(`RENAME ${things}`)}`;
      const preview = () => {
        const names = batchTitles(field('title').value.trim(), n, field('numbered')?.checked);
        form.querySelector('[data-preview]').textContent = n > 1 && names[0] ? `→ ${names.length > 3 ? `${names[0]}, ${names[1]} … ${names.at(-1)}` : names.join(', ')}` : '';
      };
      form.oninput = preview;
      preview();
      run = () => post(kind === 'blocks' ? '/rename-blocks' : '/rename-channels', { title: field('title').value.trim(), numbered: checked('numbered') });
    } else if (action === 'merge') {
      form.innerHTML = `<p class='eyebrow'>MERGE ${things}</p>
        <p class='batch-hint'>Their blocks go into one new channel, in order. Where one of them sits inside another channel, the new channel takes its place.</p>
        <label class='batch-field'><span>New channel name</span><input name='title' required autocomplete='off' placeholder='Name the merged channel'></label>
        <label class='batch-check'><input type='checkbox' name='keep'> Keep the original channels</label>${footer('MERGE')}`;
      run = () => post('/merge-channels', { title: field('title').value.trim(), keep: checked('keep') });
    } else if (action === 'delete') {
      const detail = kind === 'blocks'
        ? 'They are taken out of this channel. A block that is also in another channel stays there; the rest are deleted with their files.'
        : 'Their blocks are deleted with their files, unless a block is also in another channel.';
      form.innerHTML = `<p class='eyebrow'>DELETE ${things}</p><p class='batch-question'>Delete ${esc(noun(n))}?</p><p class='batch-hint'>${detail} This can’t be undone.</p>${footer(`DELETE ${things}`, true)}`;
      run = () => post(kind === 'blocks' ? '/delete-blocks' : '/delete-channels', {});
    } else if (action === 'move') {
      await openMove(ids, things);
    }
    form.querySelector('.batch-error').hidden = true;
    if (!dialog.open) dialog.showModal();
    // Deleting can't be undone, so Return alone never confirms it.
    (form.querySelector('input:not([type=checkbox])') || form.querySelector(action === 'delete' ? '[data-cancel]' : '[type=submit]')).focus();
    form.querySelector('input[name=title]')?.select();
  };

  // Pick an existing channel from the list, or type a name to make a new one.
  const openMove = async (ids, things) => {
    const nestNote = kind === 'blocks'
      ? `<label class='batch-check' data-new-only hidden><input type='checkbox' name='nest' checked> Put the new channel inside this one</label><label class='batch-check'><input type='checkbox' name='keep'> Keep them in this channel too</label>`
      : `<p class='batch-hint'>They go inside the channel you choose, and stay on the main page too.</p>`;
    form.innerHTML = `<p class='eyebrow'>MOVE ${things} TO</p>
      <input class='batch-search' name='search' type='search' autocomplete='off' placeholder='Find a channel, or type a new name' aria-label='Channel'>
      <div class='batch-targets' role='listbox' aria-label='Channels'></div>${nestNote}${footer('MOVE')}`;
    let channels = [];
    try { channels = (await (await fetch('/api/channels')).json()).channels; } catch (error) {}
    const excluded = new Set(kind === 'blocks' ? [channelId] : ids);
    channels = channels.filter((channel) => !excluded.has(String(channel.id)));
    const list = form.querySelector('.batch-targets');
    const search = field('search');
    const submit = form.querySelector('[type=submit]');
    let target = null;
    const choose = (option) => {
      list.querySelectorAll('[role=option]').forEach((other) => other.setAttribute('aria-selected', String(other === option)));
      target = option ? { id: option.dataset.target || '', title: option.dataset.newTitle || '' } : null;
      form.querySelectorAll('[data-new-only]').forEach((element) => { element.hidden = !(target && target.title); });
      submit.disabled = !target;
      submit.textContent = !target ? 'MOVE' : target.title ? `MOVE TO NEW CHANNEL` : `MOVE ${things}`;
    };
    const draw = () => {
      const term = search.value.trim();
      const lower = term.toLowerCase();
      const matches = channels.filter((channel) => !lower || channel.title.toLowerCase().includes(lower));
      const exact = matches.findIndex((channel) => channel.title.toLowerCase() === lower);
      if (exact > 0) matches.unshift(...matches.splice(exact, 1));
      const make = term ? `<button type='button' role='option' class='batch-new' data-new-title='${esc(term)}'><span>NEW CHANNEL</span><strong>${esc(term)}</strong></button>` : '';
      const rows = matches.map((channel) => `<button type='button' role='option' data-target='${channel.id}'><span>${esc((channel.category || 'Uncategorized').toUpperCase())} · ${channel.block_count}</span><strong>${esc(channel.title)}</strong></button>`);
      list.innerHTML = exact >= 0 ? rows[0] + make + rows.slice(1).join('') : make + rows.join('');
      if (!list.innerHTML) list.innerHTML = `<p class='batch-hint'>Type a name to make a new channel.</p>`;
      // Typing finds a channel first; a new one is only picked when nothing matches.
      choose(term ? list.querySelector('[data-target]') || list.querySelector('[role=option]') : null);
    };
    search.addEventListener('input', draw);
    search.addEventListener('keydown', (event) => {
      if (event.key !== 'ArrowDown' && event.key !== 'ArrowUp') return;
      event.preventDefault();
      const options = [...list.querySelectorAll('[role=option]')];
      const index = options.findIndex((option) => option.getAttribute('aria-selected') === 'true');
      const next = options[Math.max(0, Math.min(options.length - 1, index + (event.key === 'ArrowDown' ? 1 : -1)))];
      if (next) { choose(next); next.scrollIntoView({ block: 'nearest' }); }
    });
    list.addEventListener('click', (event) => { const option = event.target.closest('[role=option]'); if (option) choose(option); });
    list.addEventListener('dblclick', (event) => { if (event.target.closest('[role=option]')) form.requestSubmit(); });
    draw();
    run = () => {
      if (!target) throw new Error('Choose a channel first.');
      const fields = target.title ? { new_title: target.title } : { target_id: target.id };
      if (kind === 'blocks') Object.assign(fields, { nest: checked('nest'), keep: checked('keep') });
      return post(kind === 'blocks' ? '/move-blocks' : '/move-channels', fields);
    };
  };

  form.addEventListener('click', (event) => { if (event.target.closest('[data-cancel]')) dialog.close(); });
  dialog.addEventListener('click', (event) => { if (event.target === dialog) dialog.close(); });
  dialog.addEventListener('close', () => { form.oninput = null; form.replaceChildren(); run = null; });
  form.addEventListener('submit', async (event) => {
    event.preventDefault();
    const submit = form.querySelector('[type=submit]');
    const message = form.querySelector('.batch-error');
    submit.disabled = true;
    try {
      await run();
      window.location.reload();
    } catch (error) {
      message.textContent = error.message;
      message.hidden = false;
      submit.disabled = false;
    }
  });
  render();
})();
</script>"""


# The channel page header: the name and description edit in place, the category is a
# picker, and + ADD (or ⌘V anywhere) adds text, links and files.
CHANNEL_SCRIPT = r"""<script>
(() => {
  const heading = document.querySelector('.channel-heading[data-channel-id]');
  if (!heading || readOnly) return;
  const channelId = heading.dataset.channelId;
  const send = async (path, fields) => {
    const response = await fetch(path, { method: 'POST', headers: { 'Content-Type': 'application/x-www-form-urlencoded' }, body: new URLSearchParams({ channel_id: channelId, ...fields }) });
    if (!response.ok) {
      const page = new DOMParser().parseFromString(await response.text(), 'text/html');
      throw new Error(page.querySelector('.notice p')?.textContent || 'The archive could not be updated.');
    }
    return response.json();
  };

  // Popovers: one open at a time, closed by a click elsewhere or Escape.
  let openPopover = null;
  const closePopover = () => {
    if (!openPopover) return;
    openPopover.popover.hidden = true;
    openPopover.anchor.setAttribute('aria-expanded', 'false');
    openPopover = null;
  };
  const showPopover = (popover, anchor, place) => {
    if (openPopover && openPopover.popover === popover) { closePopover(); return; }
    closePopover();
    popover.hidden = false;
    anchor.setAttribute('aria-expanded', 'true');
    if (place) place(popover, anchor);
    openPopover = { popover, anchor };
    popover.querySelector('input:not([type=file]), textarea')?.focus();
  };
  document.addEventListener('click', (event) => {
    if (openPopover && !openPopover.popover.contains(event.target) && !openPopover.anchor.contains(event.target)) closePopover();
    const menu = document.querySelector('.channel-menu[open]');
    if (menu && !menu.contains(event.target)) menu.open = false;
  });
  document.addEventListener('keydown', (event) => {
    if (event.key !== 'Escape') return;
    if (openPopover) { const { anchor } = openPopover; closePopover(); anchor.focus(); }
    document.querySelector('.channel-menu[open]')?.removeAttribute('open');
  });

  // Name and description: edit in place. Return saves, Escape puts the old text back.
  document.querySelectorAll('[data-channel-field]').forEach((field) => {
    if (!field.isContentEditable) return;
    const name = field.dataset.channelField;
    let saved = field.textContent.trim();
    field.addEventListener('keydown', (event) => {
      if (event.key === 'Enter' && (name === 'title' || !event.shiftKey)) { event.preventDefault(); field.blur(); }
      if (event.key === 'Escape') { field.textContent = saved; field.blur(); }
    });
    field.addEventListener('paste', (event) => {
      event.preventDefault();
      document.execCommand('insertText', false, event.clipboardData.getData('text/plain'));
    });
    field.addEventListener('blur', async () => {
      const value = name === 'title' ? field.textContent.replace(/\s+/g, ' ').trim() : field.textContent.trim();
      if (!value) field.textContent = '';
      if (value === saved || (name === 'title' && !value)) { field.textContent = saved; return; }
      try {
        await send('/set-channel', { [name]: value });
        saved = value;
        field.textContent = value;
        if (name === 'title') document.title = `${value} · CHANNEL`;
      } catch (error) {
        field.textContent = saved;
        alert(error.message);
      }
    });
  });

  // Category: pick one, or type a name to make a new one.
  const categoryButton = document.querySelector('[data-category-toggle]');
  const categoryPopover = document.getElementById('category-popover');
  if (categoryButton && categoryPopover) {
    const search = categoryPopover.querySelector('.popover-search');
    const list = categoryPopover.querySelector('.popover-list');
    const options = () => [...list.querySelectorAll('[role=option]')];
    const highlight = (option) => options().forEach((other) => other.classList.toggle('is-active', other === option));
    const filter = () => {
      const term = search.value.trim();
      const lower = term.toLowerCase();
      list.querySelector('.popover-new')?.remove();
      options().forEach((option) => { option.hidden = Boolean(lower) && !option.textContent.toLowerCase().includes(lower); });
      if (term && !options().some((option) => option.dataset.categoryValue.toLowerCase() === lower)) {
        const make = document.createElement('button');
        make.type = 'button';
        make.setAttribute('role', 'option');
        make.className = 'popover-new';
        make.dataset.categoryValue = term;
        make.innerHTML = '<span>NEW CATEGORY</span> ';
        make.append(term);
        list.append(make);
      }
      highlight(term ? options().find((option) => !option.hidden) : null);
    };
    const choose = async (option) => {
      const value = option.dataset.categoryValue;
      try {
        const saved = await send('/set-channel', { category: value });
        categoryButton.querySelector('[data-category-label]').textContent = (saved.category || 'Uncategorized').toUpperCase();
        categoryButton.setAttribute('aria-label', `Category: ${saved.category || 'Uncategorized'}`);
        if (option.classList.contains('popover-new')) { option.classList.remove('popover-new'); option.textContent = value; }
        options().forEach((other) => other.setAttribute('aria-selected', String(other.dataset.categoryValue === saved.category)));
        search.value = '';
        filter();
        closePopover();
        categoryButton.focus();
      } catch (error) { alert(error.message); }
    };
    categoryButton.addEventListener('click', () => showPopover(categoryPopover, categoryButton, (popover, anchor) => {
      const box = anchor.getBoundingClientRect();
      popover.style.top = `${box.bottom + window.scrollY + 8}px`;
      popover.style.left = `${Math.max(16, Math.min(box.left + window.scrollX, document.documentElement.clientWidth - popover.offsetWidth - 16))}px`;
    }));
    search.addEventListener('input', filter);
    search.addEventListener('keydown', (event) => {
      const visible = options().filter((option) => !option.hidden);
      const index = visible.findIndex((option) => option.classList.contains('is-active'));
      if (event.key === 'ArrowDown' || event.key === 'ArrowUp') {
        event.preventDefault();
        const next = visible[Math.max(0, Math.min(visible.length - 1, index + (event.key === 'ArrowDown' ? 1 : -1)))];
        highlight(next);
        next?.scrollIntoView({ block: 'nearest' });
      } else if (event.key === 'Enter' && visible[Math.max(0, index)]) {
        event.preventDefault();
        choose(visible[Math.max(0, index)]);
      }
    });
    list.addEventListener('click', (event) => { const option = event.target.closest('[role=option]'); if (option) choose(option); });
  }

  // + ADD: a lone link becomes a link block, other text a text block, files become blocks.
  const addButton = document.querySelector('[data-add-toggle]');
  const addPopover = document.getElementById('add-popover');
  const upload = async (files) => {
    let added = 0;
    for (const file of files) {
      const form = new FormData();
      form.append('channel_id', channelId);
      form.append('file', file, file.name || 'pasted');
      if ((await fetch('/upload-file', { method: 'POST', body: form })).ok) added += 1;
    }
    if (!added) throw new Error('The files could not be added.');
  };
  if (addButton && addPopover) {
    const form = addPopover.querySelector('[data-add-form]');
    const text = form.elements.text;
    const message = form.querySelector('.batch-error');
    const run = async (work) => {
      message.hidden = true;
      form.querySelectorAll('button').forEach((button) => { button.disabled = true; });
      try { await work(); window.location.reload(); }
      catch (error) { message.textContent = error.message; message.hidden = false; form.querySelectorAll('button').forEach((button) => { button.disabled = false; }); }
    };
    addButton.addEventListener('click', () => showPopover(addPopover, addButton));
    form.addEventListener('submit', (event) => {
      event.preventDefault();
      if (!text.value.trim()) { text.focus(); return; }
      run(() => send('/add-block', { text: text.value }));
    });
    text.addEventListener('keydown', (event) => { if (event.key === 'Enter' && (event.metaKey || event.ctrlKey)) form.requestSubmit(); });
    form.querySelector('[data-add-file]').addEventListener('change', (event) => { if (event.target.files.length) run(() => upload([...event.target.files])); });
  }
  // ⌘V on the page (not in a field) adds what's on the clipboard to this channel.
  document.addEventListener('paste', async (event) => {
    if (!modal.hidden || document.querySelector('dialog[open]') || event.target.closest('input, textarea, [contenteditable]')) return;
    const files = [...event.clipboardData.files];
    const pasted = event.clipboardData.getData('text/plain');
    if (!files.length && !pasted.trim()) return;
    event.preventDefault();
    document.body.classList.add('is-adding');
    try {
      if (files.length) await upload(files); else await send('/add-block', { text: pasted });
      window.location.reload();
    } catch (error) {
      document.body.classList.remove('is-adding');
      alert(error.message);
    }
  });

  // Delete lives in the ⋯ menu and asks first.
  const deleteDialog = document.getElementById('delete-channel-dialog');
  document.querySelector('[data-delete-channel]')?.addEventListener('click', () => {
    document.querySelector('.channel-menu').open = false;
    deleteDialog.showModal();
    deleteDialog.querySelector('[data-dialog-cancel]').focus();
  });
  deleteDialog?.addEventListener('click', (event) => { if (event.target === deleteDialog || event.target.closest('[data-dialog-cancel]')) deleteDialog.close(); });
})();
</script>"""


def layout(title: str, body: str) -> str:
    return f"""<!doctype html><html lang='en'><head><meta charset='utf-8'>
<meta name='viewport' content='width=device-width,initial-scale=1'>
<title>{esc(title)} · CHANNEL</title><link rel='stylesheet' href='/style.css'></head>
<body><header class='topbar'><a class='wordmark' href='/'>CHANNEL</a><nav class='main-nav'><a href='/'>CHANNEL</a><button class='update-button' id='update-button' type='button' hidden>CHECK FOR UPDATES</button></nav></header>
<main>{body}</main><div class='modal' id='post-modal' hidden role='dialog' aria-modal='true' aria-label='Post detail'>
<div class='modal-backdrop' data-close-modal></div><section class='modal-panel'>
<button class='modal-close' type='button' data-close-modal aria-label='Close post'>CLOSE ×</button>
<div class='modal-zoom' aria-label='Image zoom'><button type='button' data-zoom='out'>−</button><button type='button' data-zoom='reset'>100%</button><button type='button' data-zoom='in'>+</button></div>
<div id='modal-content'></div></section></div>
<script>
// Update button: only inside the Mac app, which answers through window.channelUpdate.
const updateButton = document.getElementById('update-button');
const appBridge = window.webkit && window.webkit.messageHandlers && window.webkit.messageHandlers.channel;
if (updateButton && appBridge) {{
  updateButton.hidden = false;
  const labels = {{ checking: 'CHECKING…', current: 'UP TO DATE', error: 'CAN’T CHECK', updating: 'UPDATING…' }};
  window.channelUpdate = (update) => {{
    updateButton.dataset.state = update.state;
    updateButton.textContent = update.state === 'available' ? `UPDATE · ${{update.count}} NEW` : labels[update.state] || 'CHECK FOR UPDATES';
    updateButton.title = update.detail || '';
  }};
  updateButton.addEventListener('click', () => {{
    const state = updateButton.dataset.state;
    if (state === 'checking' || state === 'updating') return;
    appBridge.postMessage({{ action: state === 'available' ? 'update' : 'check' }});
  }});
  appBridge.postMessage({{ action: 'status' }});
}}
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
document.addEventListener('dragstart', (event) => {{
  const channel = event.target.closest('[data-draggable-channel]');
  if (channel) {{
    event.dataTransfer.effectAllowed = 'copy';
    event.dataTransfer.setData('application/x-archive-channel', channel.dataset.draggableChannel);
    document.body.classList.add('dragging');
    return;
  }}
  const block = event.target.closest('[data-draggable-block]');
  if (!block) return;
  event.dataTransfer.effectAllowed = 'copy';
  event.dataTransfer.setData('text/plain', block.dataset.blockId);
  document.body.classList.add('dragging');
}});
document.addEventListener('dragend', () => document.body.classList.remove('dragging'));
// On a channel page, files dropped anywhere outside another drop target go into this channel.
const pageChannel = document.querySelector('.block-grid[data-drop-channel]');
const dropTarget = (event) => event.target.closest('[data-drop-channel]') || (event.dataTransfer.types.includes('Files') ? pageChannel : null);
const endPageDrop = () => {{ document.body.classList.remove('page-drop'); if (pageChannel) pageChannel.classList.remove('drop-ready'); }};
document.addEventListener('dragover', (event) => {{
  if (event.dataTransfer.types.includes('Files')) event.preventDefault();
  const target = dropTarget(event);
  document.body.classList.toggle('page-drop', Boolean(target) && target === pageChannel && event.dataTransfer.types.includes('Files'));
  if (!target) return;
  event.preventDefault();
  if (target !== pageChannel) target.classList.add('drop-ready');
}});
document.addEventListener('dragleave', (event) => {{
  if (!event.relatedTarget) endPageDrop();
  const target = event.target.closest('[data-drop-channel]');
  if (target && !target.contains(event.relatedTarget)) target.classList.remove('drop-ready');
}});
document.addEventListener('drop', async (event) => {{
  const target = dropTarget(event);
  endPageDrop();
  if (event.dataTransfer.files.length) event.preventDefault();
  if (!target) return;
  event.preventDefault();
  target.classList.remove('drop-ready');
  if (event.dataTransfer.files.length) {{
    let uploaded = 0;
    for (const [index, file] of [...event.dataTransfer.files].entries()) {{
    if (!file.type.startsWith('image/')) continue;
    let fileHandle = null;
    const droppedItem = event.dataTransfer.items[index];
    if (droppedItem && droppedItem.getAsFileSystemHandle) {{
      try {{
        fileHandle = await droppedItem.getAsFileSystemHandle();
        if (fileHandle && fileHandle.requestPermission) {{
          const permission = await fileHandle.requestPermission({{ mode: 'readwrite' }});
          if (permission !== 'granted') fileHandle = null;
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
        try {{ await fileHandle.remove(); }} catch (error) {{}}
      }}
    }}
    }}
    if (!uploaded) {{ alert('No image files were added.'); return; }}
    window.location.reload();
  }} else if (event.dataTransfer.types.includes('application/x-archive-channel')) {{
    const channelId = event.dataTransfer.getData('application/x-archive-channel');
    if (channelId === target.dataset.dropChannel) return;
    const response = await fetch('/connect-channel', {{ method: 'POST', headers: {{ 'Content-Type': 'application/x-www-form-urlencoded' }}, body: new URLSearchParams({{ source_id: channelId, channel_id: target.dataset.dropChannel }}) }});
    if (response.ok) window.location.reload(); else alert('Could not add the channel.');
  }} else {{
    const blockId = event.dataTransfer.getData('text/plain');
    if (!/^-?\\d+$/.test(blockId)) return;
    const response = await fetch('/connect-block', {{ method: 'POST', headers: {{ 'Content-Type': 'application/x-www-form-urlencoded' }}, body: new URLSearchParams({{ block_id: blockId, channel_id: target.dataset.dropChannel }}) }});
    if (response.ok) window.location.reload(); else alert('Could not add the block.');
  }}
}});
// A channel shows its blocks large, small, stacked or A–Z. A cookie remembers the choice,
// so the server renders the page that way next time.
const blockGrid = document.querySelector('.block-grid[data-view]');
document.querySelectorAll('[data-view-option]').forEach((button, index, buttons) => {{
  button.addEventListener('click', () => {{
    const view = button.dataset.viewOption;
    const reorder = (view === 'abc') !== (blockGrid.dataset.view === 'abc');
    document.cookie = `block_view=${{view}}; path=/; max-age=31536000; SameSite=Lax`;
    // The server sorts the blocks, so going into or out of A–Z reloads the page.
    if (reorder) {{ window.location.reload(); return; }}
    blockGrid.dataset.view = view;
    buttons.forEach((other) => other.setAttribute('aria-pressed', String(other === button)));
  }});
}});
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
</script>{SELECTION_SCRIPT}{CHANNEL_SCRIPT}</body></html>"""


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


def timestamp() -> str:
    return time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())


def insert_channel(connection: sqlite3.Connection, title: str, category: str = "", description: str = "", visibility: str = "private") -> int:
    channel_id = local_id(connection, "channels")
    now = timestamp()
    connection.execute("INSERT INTO channels (id, slug, title, description, visibility, category, created_at, updated_at, raw_json) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)", (channel_id, f"local-{abs(channel_id)}", title, description, visibility, category, now, now, json.dumps({"local": True})))
    if category:
        connection.execute("INSERT OR IGNORE INTO categories(name) VALUES (?)", (category,))
    return channel_id


def require_channel(connection: sqlite3.Connection, channel_id: int) -> sqlite3.Row:
    channel = connection.execute("SELECT * FROM channels WHERE id = ?", (channel_id,)).fetchone()
    if not channel:
        raise ValueError("channel not found")
    return channel


def append_block(connection: sqlite3.Connection, channel_id: int, block_id: int, position: int | None = None) -> None:
    if position is None:
        position = connection.execute("SELECT COALESCE(MAX(position), -1) + 1 FROM channel_blocks WHERE channel_id = ?", (channel_id,)).fetchone()[0]
    now = timestamp()
    connection.execute("INSERT OR IGNORE INTO channel_blocks VALUES (?, ?, ?, ?, ?)", (channel_id, block_id, position, now, json.dumps({"local": True, "connected_at": now})))


def insert_block(connection: sqlite3.Connection, channel_id: int, kind: str, title: str, content: str = "", source: str = "") -> int:
    require_channel(connection, channel_id)
    block_id = local_id(connection, "blocks")
    now = timestamp()
    connection.execute("INSERT INTO blocks (id, type, title, content, description, author_name, author_slug, source_url, created_at, updated_at, raw_json) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)", (block_id, kind, title, content, "", "Local archive", "local", source, now, now, json.dumps({"local": True, "created_at": now})))
    append_block(connection, channel_id, block_id)
    return block_id


def block_from_text(text: str) -> tuple[str, str, str, str]:
    """(type, title, content, source) for pasted text: a lone URL becomes a link block."""
    text = text.strip()
    if re.fullmatch(r"(https?://|www\.)\S+", text):
        source = text if text.startswith("http") else "https://" + text
        parsed = urlparse(source)
        return "link", (parsed.netloc + parsed.path).removeprefix("www.").rstrip("/")[:300], "", source
    first_line = text.splitlines()[0].strip() if text else ""
    return "text", (first_line[:80] + ("…" if len(first_line) > 80 else "")) or "Untitled block", text, ""


def short_date(value: str | None) -> str:
    try:
        return time.strftime("%-d %b %Y", time.strptime((value or "")[:10], "%Y-%m-%d")).upper()
    except ValueError:
        return ""


def channel_block(connection: sqlite3.Connection, channel_id: int) -> int:
    """The 'channel' block that links to a channel page, made on first use."""
    existing = connection.execute("SELECT id FROM blocks WHERE type = 'channel' AND source_url = ?", (f"/channel/{channel_id}",)).fetchone()
    if existing:
        return existing["id"]
    channel = require_channel(connection, channel_id)
    block_id = local_id(connection, "blocks")
    now = timestamp()
    connection.execute("INSERT INTO blocks (id, type, title, content, description, author_name, author_slug, source_url, created_at, updated_at, raw_json) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)", (block_id, "channel", channel["title"] or channel["slug"], "", "", "Local archive", "local", f"/channel/{channel_id}", now, now, json.dumps({"local": True, "channel_id": channel_id})))
    return block_id


def linked_channel(connection: sqlite3.Connection, block_id: int) -> int | None:
    """The channel a 'channel' block points to, if it is one."""
    row = connection.execute("SELECT source_url FROM blocks WHERE id = ? AND type = 'channel'", (block_id,)).fetchone()
    match = re.fullmatch(r"/channel/(-?\d+)", row["source_url"] or "") if row else None
    return int(match.group(1)) if match else None


def set_channel_title(connection: sqlite3.Connection, channel_id: int, title: str) -> None:
    connection.execute("UPDATE channels SET title = ?, updated_at = ? WHERE id = ?", (title, timestamp(), channel_id))
    connection.execute("UPDATE blocks SET title = ? WHERE type = 'channel' AND source_url = ?", (title, f"/channel/{channel_id}"))


def set_block_title(connection: sqlite3.Connection, block_id: int, title: str) -> None:
    if not connection.execute("SELECT 1 FROM blocks WHERE id = ?", (block_id,)).fetchone():
        raise ValueError("block not found")
    # A nested channel's name is the channel's name, so renaming one renames both.
    channel_id = linked_channel(connection, block_id)
    if channel_id is not None and title and connection.execute("SELECT 1 FROM channels WHERE id = ?", (channel_id,)).fetchone():
        set_channel_title(connection, channel_id, title)
    connection.execute("UPDATE blocks SET title = ? WHERE id = ?", (title, block_id))


def delete_channel_rows(connection: sqlite3.Connection, channel_id: int) -> None:
    block_ids = [row[0] for row in connection.execute("SELECT block_id FROM channel_blocks WHERE channel_id = ?", (channel_id,)).fetchall()]
    nested = [row[0] for row in connection.execute("SELECT id FROM blocks WHERE type = 'channel' AND source_url = ?", (f"/channel/{channel_id}",)).fetchall()]
    connection.execute("DELETE FROM channel_blocks WHERE channel_id = ?", (channel_id,))
    connection.executemany("DELETE FROM channel_blocks WHERE block_id = ?", [(block_id,) for block_id in nested])
    connection.execute("DELETE FROM channels WHERE id = ?", (channel_id,))
    for block_id in block_ids + nested:
        cleanup_block(connection, block_id)


def merge_channel_rows(connection: sqlite3.Connection, channel_ids: list[int], title: str, keep: bool) -> int:
    """Put every block of the channels into one new channel, in order, without duplicates."""
    sources = [require_channel(connection, channel_id) for channel_id in channel_ids]
    merged_id = insert_channel(connection, title, sources[0]["category"], next((row["description"] for row in sources if row["description"]), ""))
    if any(row["favorite"] for row in sources):
        connection.execute("UPDATE channels SET favorite = 1 WHERE id = ?", (merged_id,))
    position = 0
    for channel_id in channel_ids:
        for row in connection.execute("SELECT block_id FROM channel_blocks WHERE channel_id = ? ORDER BY position", (channel_id,)).fetchall():
            # A merged channel that contained another one would otherwise contain itself.
            if linked_channel(connection, row["block_id"]) in channel_ids:
                continue
            append_block(connection, merged_id, row["block_id"], position)
            position += 1
    if keep:
        return merged_id
    # Wherever an old channel was nested, nest the merged channel instead.
    placeholders = ",".join("?" * len(channel_ids))
    links = [row[0] for row in connection.execute(f"SELECT id FROM blocks WHERE type = 'channel' AND source_url IN ({placeholders})", [f"/channel/{channel_id}" for channel_id in channel_ids]).fetchall()]
    if links:
        link_marks = ",".join("?" * len(links))
        parents = connection.execute(f"SELECT channel_id, MIN(position) AS position FROM channel_blocks WHERE block_id IN ({link_marks}) AND channel_id NOT IN ({placeholders}) GROUP BY channel_id", links + channel_ids).fetchall()
        merged_link = channel_block(connection, merged_id)
        for parent in parents:
            append_block(connection, parent["channel_id"], merged_link, parent["position"])
    for channel_id in channel_ids:
        delete_channel_rows(connection, channel_id)
    return merged_id


def form_value(values: dict[str, list[str]], key: str) -> str:
    return values.get(key, [""])[0].strip()


def form_ids(values: dict[str, list[str]], key: str) -> list[int]:
    ids: list[int] = []
    for value in values.get(key, []):
        for part in value.split(","):
            if part.strip() and int(part) not in ids:
                ids.append(int(part))
    if not ids:
        raise ValueError("nothing selected")
    return ids


def batch_titles(title: str, count: int, numbered: bool) -> list[str]:
    """One name per item. Numbers replace '#' in the name, or are added at the end."""
    title = title.strip()[:300]
    if not title:
        raise ValueError("name cannot be empty")
    if not numbered or (count == 1 and "#" not in title):
        return [title] * count
    width = len(str(count))
    return [title.replace("#", str(index).zfill(width)) if "#" in title else f"{title} {str(index).zfill(width)}" for index in range(1, count + 1)]


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


def select_button(kind: str) -> str:
    if READ_ONLY:
        return ""
    return f"<button class='select-button' type='button' data-select-toggle aria-pressed='false'>SELECT {kind.upper()}</button>"


def selection_bar(kind: str) -> str:
    """Batch actions for the selected blocks or channels; the script fills in the count."""
    if READ_ONLY:
        return ""
    merge = "<button type='button' data-batch='merge' data-needs='2'>MERGE</button>" if kind == "channels" else ""
    return f"<div class='selection-bar' id='selection-bar' data-kind='{kind}' hidden><span class='selection-count' id='selection-count' aria-live='polite'>0 SELECTED</span><button type='button' data-batch='all'>SELECT ALL</button><button type='button' data-batch='rename' data-needs='1'>RENAME</button><button type='button' data-batch='move' data-needs='1'>MOVE TO…</button>{merge}<button type='button' class='danger' data-batch='delete' data-needs='1'>DELETE</button><button type='button' data-batch='done'>DONE</button></div><dialog class='batch-dialog' id='batch-dialog'><form id='batch-form' method='dialog'></form></dialog>"


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
        draggable = f" draggable='true' data-draggable-block data-select-id='{row['id']}'"
    return f"<article class='block' data-type='{esc(row['type'])}' data-block-id='{row['id']}'{source_attribute}{download}{draggable}><div class='block-visual'>{visual}{remove}</div><div class='block-meta'><span>{kind}</span><span>{esc(row['author_name'])}</span>{source}</div><h3>{title_markup(row['title'])}</h3>{note_markup}</article>"


def abc_grid(blocks: list[sqlite3.Row]) -> str:
    """Blocks sorted by name under a heading for each first letter; untitled ones go last."""
    def name(row: sqlite3.Row) -> str:
        return (row["title"] or "").strip()

    # Numbers compare as numbers, so "img 2" comes before "img 10".
    def key(row: sqlite3.Row) -> list[object]:
        return [int(part) if part.isdigit() else part for part in re.split(r"(\d+)", name(row).casefold())]

    rows = sorted((row for row in blocks if name(row)), key=key) + [row for row in blocks if not name(row)]
    parts: list[str] = []
    heading = None
    for row in rows:
        title = name(row)
        letter = (title[0].upper() if title[0].isalpha() else "#") if title else "UNTITLED"
        if letter != heading:
            heading = letter
            parts.append(f"<h2 class='letter-heading'>{esc(letter)}</h2>")
        parts.append(block_card(row))
    return "".join(parts)


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
                self.send_html(layout("Archive error", f"<section class='notice'><h1>Could not upload image</h1><p>{esc(error)}</p></section>"), 400)
            return
        # Blank values are kept, so a field can be cleared (an empty category or description).
        values = parse_qs(body.decode("utf-8"), keep_blank_values=True)
        try:
            if self.path == "/create-channel":
                self.create_channel(values)
            elif self.path == "/set-channel":
                self.set_channel(values)
            elif self.path == "/create-block":
                self.create_block(values)
            elif self.path == "/add-block":
                self.add_block(values)
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
            elif self.path == "/rename-blocks":
                self.rename_blocks(values)
            elif self.path == "/move-blocks":
                self.move_blocks(values)
            elif self.path == "/delete-blocks":
                self.delete_blocks(values)
            elif self.path == "/rename-channels":
                self.rename_channels(values)
            elif self.path == "/move-channels":
                self.move_channels(values)
            elif self.path == "/merge-channels":
                self.merge_channels(values)
            elif self.path == "/delete-channels":
                self.delete_channels(values)
            else:
                self.send_error(404)
        except (sqlite3.Error, ValueError, OSError) as error:
            self.send_html(layout("Archive error", f"<section class='notice'><h1>Could not update archive</h1><p>{esc(error)}</p></section>"), 400)

    def cookie(self, name: str) -> str:
        for part in self.headers.get("Cookie", "").split(";"):
            key, _, value = part.strip().partition("=")
            if key == name:
                return unquote(value)
        return ""

    def redirect(self, location: str) -> None:
        self.send_response(303)
        self.send_header("Location", location)
        self.end_headers()

    def send_json(self, value: object) -> None:
        data = json.dumps(value).encode()
        self.send_response(200)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(data)))
        self.end_headers()
        self.wfile.write(data)

    def create_channel(self, values: dict[str, list[str]]) -> None:
        title = form_value(values, "title") or "Untitled channel"
        description = form_value(values, "description")
        visibility = form_value(values, "visibility") or "private"
        if visibility not in {"public", "closed", "private"}:
            visibility = "private"
        connection = db()
        channel_id = insert_channel(connection, title, form_value(values, "category"), description, visibility)
        connection.commit()
        connection.close()
        self.redirect(f"/channel/{channel_id}")

    def set_channel(self, values: dict[str, list[str]]) -> None:
        """Save whichever of a channel's title, description and category were sent."""
        channel_id = int(form_value(values, "channel_id"))
        connection = db()
        require_channel(connection, channel_id)
        if "title" in values:
            title = form_value(values, "title")[:300]
            if not title:
                raise ValueError("channel name cannot be empty")
            set_channel_title(connection, channel_id, title)
        if "description" in values:
            connection.execute("UPDATE channels SET description = ? WHERE id = ?", (values["description"][0].strip()[:2000], channel_id))
        if "category" in values:
            category = form_value(values, "category")[:100]
            connection.execute("UPDATE channels SET category = ? WHERE id = ?", (category, channel_id))
            if category:
                connection.execute("INSERT OR IGNORE INTO categories(name) VALUES (?)", (category,))
        connection.execute("UPDATE channels SET updated_at = ? WHERE id = ?", (timestamp(), channel_id))
        channel = require_channel(connection, channel_id)
        connection.commit()
        connection.close()
        self.send_json({"title": channel["title"], "description": channel["description"] or "", "category": channel["category"] or ""})

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
        connection = db()
        insert_block(connection, channel_id, kind, form_value(values, "title") or "Untitled block", form_value(values, "content"), form_value(values, "source_url") if kind == "link" else "")
        connection.commit()
        connection.close()
        self.redirect(f"/channel/{channel_id}")

    def add_block(self, values: dict[str, list[str]]) -> None:
        """A block from typed or pasted text: a lone link becomes a link block, anything else text."""
        channel_id = int(form_value(values, "channel_id"))
        text = values.get("text", [""])[0].strip()
        if not text:
            raise ValueError("nothing to add")
        connection = db()
        block_id = insert_block(connection, channel_id, *block_from_text(text[:20000]))
        connection.commit()
        connection.close()
        self.send_json({"block_id": block_id})

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
        set_block_title(connection, block_id, title)
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
        self.send_json({"source_url": link})

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
        require_channel(connection, channel_id)
        if not connection.execute("SELECT 1 FROM blocks WHERE id = ?", (block_id,)).fetchone():
            raise ValueError("block not found")
        append_block(connection, channel_id, block_id)
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
        require_channel(connection, channel_id)
        # Nested channels are 'channel' blocks that link to the channel page.
        append_block(connection, channel_id, channel_block(connection, source_id))
        connection.commit()
        connection.close()
        self.send_response(204)
        self.end_headers()

    def rename_blocks(self, values: dict[str, list[str]]) -> None:
        block_ids = form_ids(values, "block_ids")
        titles = batch_titles(form_value(values, "title"), len(block_ids), form_value(values, "numbered") == "1")
        connection = db()
        for block_id, title in zip(block_ids, titles):
            set_block_title(connection, block_id, title)
        connection.commit()
        connection.close()
        self.send_json({"titles": titles})

    def move_blocks(self, values: dict[str, list[str]]) -> None:
        """Move blocks to another channel, or to a new one that can sit inside this channel."""
        channel_id = int(form_value(values, "channel_id"))
        block_ids = form_ids(values, "block_ids")
        keep = form_value(values, "keep") == "1"
        new_title = form_value(values, "new_title")
        connection = db()
        channel = require_channel(connection, channel_id)
        if new_title:
            target_id = insert_channel(connection, new_title, channel["category"])
        else:
            target_id = int(form_value(values, "target_id"))
            require_channel(connection, target_id)
            if target_id == channel_id:
                raise ValueError("the blocks are already in this channel")
        marks = ",".join("?" * len(block_ids))
        rows = connection.execute(f"SELECT block_id, position FROM channel_blocks WHERE channel_id = ? AND block_id IN ({marks})", [channel_id] + block_ids).fetchall()
        positions = {row["block_id"]: row["position"] for row in rows}
        # A channel can't be moved into itself.
        moved = [block_id for block_id in block_ids if block_id in positions and linked_channel(connection, block_id) != target_id]
        for block_id in moved:
            append_block(connection, target_id, block_id)
        if not keep:
            connection.executemany("DELETE FROM channel_blocks WHERE channel_id = ? AND block_id = ?", [(channel_id, block_id) for block_id in moved])
        if new_title and form_value(values, "nest") == "1":
            # The new channel takes the place of the first block that went into it.
            first = min((positions[block_id] for block_id in moved), default=None) if not keep else None
            append_block(connection, channel_id, channel_block(connection, target_id), first)
        connection.execute("UPDATE channels SET updated_at = ? WHERE id IN (?, ?)", (timestamp(), channel_id, target_id))
        connection.commit()
        connection.close()
        self.send_json({"channel_id": target_id, "moved": len(moved)})

    def delete_blocks(self, values: dict[str, list[str]]) -> None:
        """Take blocks out of a channel. A block no other channel holds is deleted with its file."""
        channel_id = int(form_value(values, "channel_id"))
        block_ids = form_ids(values, "block_ids")
        connection = db()
        for block_id in block_ids:
            connection.execute("DELETE FROM channel_blocks WHERE channel_id = ? AND block_id = ?", (channel_id, block_id))
            cleanup_block(connection, block_id)
        connection.commit()
        connection.close()
        self.send_json({"deleted": len(block_ids)})

    def rename_channels(self, values: dict[str, list[str]]) -> None:
        channel_ids = form_ids(values, "channel_ids")
        titles = batch_titles(form_value(values, "title"), len(channel_ids), form_value(values, "numbered") == "1")
        connection = db()
        for channel_id, title in zip(channel_ids, titles):
            require_channel(connection, channel_id)
            set_channel_title(connection, channel_id, title)
        connection.commit()
        connection.close()
        self.send_json({"titles": titles})

    def move_channels(self, values: dict[str, list[str]]) -> None:
        """Nest channels inside another channel, or inside a new one."""
        channel_ids = form_ids(values, "channel_ids")
        new_title = form_value(values, "new_title")
        connection = db()
        for channel_id in channel_ids:
            require_channel(connection, channel_id)
        if new_title:
            target_id = insert_channel(connection, new_title)
        else:
            target_id = int(form_value(values, "target_id"))
            require_channel(connection, target_id)
        for channel_id in channel_ids:
            if channel_id != target_id:
                append_block(connection, target_id, channel_block(connection, channel_id))
        connection.execute("UPDATE channels SET updated_at = ? WHERE id = ?", (timestamp(), target_id))
        connection.commit()
        connection.close()
        self.send_json({"channel_id": target_id})

    def merge_channels(self, values: dict[str, list[str]]) -> None:
        channel_ids = form_ids(values, "channel_ids")
        if len(channel_ids) < 2:
            raise ValueError("choose at least two channels to merge")
        title = form_value(values, "title")
        if not title:
            raise ValueError("channel name cannot be empty")
        connection = db()
        merged_id = merge_channel_rows(connection, channel_ids, title, form_value(values, "keep") == "1")
        connection.commit()
        connection.close()
        self.send_json({"channel_id": merged_id})

    def delete_channels(self, values: dict[str, list[str]]) -> None:
        channel_ids = form_ids(values, "channel_ids")
        connection = db()
        for channel_id in channel_ids:
            delete_channel_rows(connection, channel_id)
        connection.commit()
        connection.close()
        self.send_json({"deleted": len(channel_ids)})

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
        self.send_json({"channels": channels, "read_only": READ_ONLY})

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
        delete_channel_rows(connection, channel_id)
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
        cards = "".join(f"<a class='channel-card' href='/channel/{row['id']}' data-drop-channel='{row['id']}' draggable='true' data-draggable-channel='{row['id']}' data-select-id='{row['id']}'><span class='eyebrow category-chip' data-category-link data-category-url='/?sort=category&direction=asc&category={quote(row['category'] or '')}'>{'★ ' if row['favorite'] else ''}{esc(row['category'] or 'UNCATEGORIZED').upper()} · {row['block_count']} BLOCKS</span><h2>{title_markup(row['title'] or row['slug'])}</h2><p>{esc(row['description'])}</p></a>" for row in channels)
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
        controls = f"<div class='view-line'><nav class='view-tabs'>{show_tabs}</nav><p class='view-label'>SORT</p><nav class='view-tabs'>{sort_tabs}</nav><p class='view-label channel-count' id='channel-count'>{len(channels)} channels</p>{select_button('channels')}{search}</div>"
        rows = [("SHOW", controls), ("FAVORITES", f"<div class='favorite-links'>{favorite_links}</div>"), ("CATEGORIES", f"<div class='category-links'>{category_links}</div>")]
        view = "<section class='view-panel'>" + "".join(f"<div class='view-row'><p class='view-label'>{label}</p>{content}</div>" for label, content in rows) + "</section>"
        create = "<section class='editor-panel'><p class='eyebrow'>EDITING</p><div class='editing-actions'><form method='post' action='/create-channel' class='editor-form'><input name='title' placeholder='New channel title' required><input name='description' placeholder='Description'><input name='category' placeholder='Category'><button type='submit'>CREATE CHANNEL</button></form><form method='post' action='/create-category' class='category-form'><input name='category' placeholder='New category' required><button type='submit'>CREATE CATEGORY</button></form></div></section>"
        self.send_html(layout("Channels", f"{view}{create}<section class='channel-grid' data-select-kind='channels'>{cards or empty}</section>{selection_bar('channels')}"))

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
        targets = connection.execute("SELECT id, title, category, favorite FROM channels WHERE id != ? ORDER BY favorite DESC, lower(title)", (channel_id,)).fetchall()
        connection.close()
        block_view = self.cookie("block_view")
        if block_view not in BLOCK_VIEWS:
            block_view = BLOCK_VIEWS[0]
        grid = abc_grid(blocks) if block_view == "abc" else "".join(block_card(row) for row in blocks)
        empty = '<div class="notice">No imported blocks in this channel.</div>'
        view_switch = "<div class='view-switch' role='group' aria-label='Block view'><span class='view-label'>VIEW</span>" + "".join(f"<button type='button' data-view-option='{view}' aria-pressed='{'true' if view == block_view else 'false'}'>{view.upper()}</button>" for view in BLOCK_VIEWS) + "</div>"
        editable = "" if READ_ONLY else " contenteditable='plaintext-only' spellcheck='false'"
        category = channel["category"] or ""
        # Category: a chip in the facts line that opens a picker, instead of a form.
        if READ_ONLY:
            category_chip = f"<span>{esc(category or 'Uncategorized').upper()}</span>"
            category_picker = ""
        else:
            category_chip = f"<button type='button' class='fact-button' data-category-toggle aria-expanded='false' aria-label='Category: {esc(category or 'Uncategorized')}'><span data-category-label>{esc(category or 'Uncategorized').upper()}</span> ▾</button>"
            options = "".join(f"<button type='button' role='option' data-category-value='{esc(row['name'])}' aria-selected='{'true' if row['name'] == category else 'false'}'>{esc(row['name'])}</button>" for row in categories)
            category_picker = f"<div class='popover category-popover' id='category-popover' hidden><input type='search' class='popover-search' placeholder='Find or make a category' aria-label='Category' autocomplete='off'><div class='popover-list' role='listbox'><button type='button' role='option' data-category-value='' aria-selected='{'true' if not category else 'false'}'>Uncategorized</button>{options}</div></div>"
        updated = short_date(channel["updated_at"])
        updated_fact = f"<span aria-hidden='true'>·</span><span>UPDATED {updated}</span>" if updated else ""
        facts = f"<p class='channel-facts'><span>{esc(channel['visibility'] or 'unknown').upper()}</span><span aria-hidden='true'>·</span>{category_chip}<span aria-hidden='true'>·</span><span>{len(blocks)} {'BLOCK' if len(blocks) == 1 else 'BLOCKS'}</span>{updated_fact}</p>"
        title = f"<h1 class='channel-title' data-channel-field='title'{editable} aria-label='Channel name'>{title_markup(channel['title'] or channel['slug'])}</h1>"
        description = f"<p class='channel-description' data-channel-field='description'{editable} data-placeholder='Add a description' aria-label='Description'>{esc(channel['description'])}</p>" if not READ_ONLY or channel["description"] else ""
        star = f"<form method='post' action='/toggle-favorite'><input type='hidden' name='channel_id' value='{channel_id}'><button class='icon-button star-button' type='submit' aria-pressed='{'true' if channel['favorite'] else 'false'}' title='{'Remove from favorites' if channel['favorite'] else 'Add to favorites'}'>{'★' if channel['favorite'] else '☆'}</button></form>"
        menu = "" if READ_ONLY else "<details class='channel-menu'><summary class='icon-button' title='More' aria-label='More actions'>⋯</summary><div class='popover menu-popover'><button type='button' class='danger' data-delete-channel>DELETE CHANNEL…</button></div></details>"
        delete_dialog = "" if READ_ONLY else f"<dialog class='batch-dialog' id='delete-channel-dialog'><form method='post' action='/delete-channel'><input type='hidden' name='channel_id' value='{channel_id}'><p class='eyebrow'>DELETE CHANNEL</p><p class='batch-question'>Delete “{esc(channel['title'])}”?</p><p class='batch-hint'>Its blocks are deleted with their files, unless a block is also in another channel. This can’t be undone.</p><div class='batch-actions'><button type='button' data-dialog-cancel>CANCEL</button><button type='submit' class='danger'>DELETE CHANNEL</button></div></form></dialog>"
        heading = f"<section class='channel-heading' data-channel-id='{channel_id}'><div class='channel-topline'><a class='back' href='/'>← BACK</a><div class='channel-tools'>{star}{menu}</div></div>{facts}{title}{description}</section>"
        # One toolbar for the grid: view, select, add. It sticks under the top bar.
        add = "" if READ_ONLY else "<button type='button' class='select-button' data-add-toggle aria-expanded='false'>+ ADD</button>"
        add_panel = "" if READ_ONLY else "<div class='popover add-popover' id='add-popover' hidden><form data-add-form><textarea name='text' rows='3' placeholder='Paste a link or type text' aria-label='Link or text'></textarea><div class='add-actions'><label class='add-file'><input type='file' multiple hidden data-add-file>CHOOSE FILES…</label><span class='batch-hint'>⌘↩ adds. ⌘V on the page adds too.</span><button type='submit'>ADD</button></div><p class='batch-error' hidden></p></form></div>"
        toolbar = f"<div class='channel-toolbar'>{view_switch}<div class='toolbar-actions'>{select_button('blocks')}{add}{add_panel}</div></div>"
        # While a block is dragged, the channels to drop it on slide in from the side.
        target_cards = "".join(f"<div class='drop-channel' data-drop-channel='{row['id']}'><span>{'★ ' if row['favorite'] else ''}{esc(row['category'] or 'UNCATEGORIZED').upper()}</span><strong>{title_markup(row['title'])}</strong></div>" for row in targets)
        drag_shelf = f"<aside class='drag-shelf' aria-hidden='true'><p class='eyebrow'>DROP TO ADD TO</p><div class='drag-shelf-list'>{target_cards}</div></aside>" if targets and not READ_ONLY else ""
        body = f"{heading}{category_picker}{toolbar}<section class='block-grid' data-view='{block_view}' data-drop-channel='{channel_id}' data-select-kind='blocks'>{grid or empty}</section>{selection_bar('blocks')}{drag_shelf}{delete_dialog}"
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
