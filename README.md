# Are.na Archive

A one-time, public Are.na archive importer with a local browser.

## Usage

```bash
python3 arena_archive.py import maus-cats
python3 server.py
```

Then open <http://127.0.0.1:8765>.

## Mac app

`mac/build.sh` builds a native app with the free Command Line Tools
(`xcode-select --install`). You don't need Xcode or a developer account.

```bash
./mac/build.sh --install
```

On first launch, choose the folder that has `archive.db` and `assets/` (for
example this project folder). The app reads and writes those files in place
and remembers the folder. Use File › Choose Archive Folder… (⌘O) to switch.

### Menu bar tray

The app puts a tray icon in the menu bar. Drag anything onto it: files from
Finder, images from a browser or Photos, links, or selected text. A channel
list opens under the icon:

- Drop onto a channel to file it there.
- Drop onto the icon itself to hold it, then click a channel (or search and
  press Return) to file everything that's held.

Images become image blocks. Other files become attachment blocks. Links and
text become link and text blocks. Recent channels and favorites are listed
first. The list scrolls when you hover near its top or bottom edge while
dragging.

By default the archive keeps a copy and the original file stays where it is.
Turn on "Move Files to Trash After Filing" in the tray's ⋯ menu to move
originals to the Trash instead. Closing the archive window keeps the tray
running; quit from the ⋯ menu or with ⌘Q.

## Planned: iCloud sync

The goal is to sync the archive between Macs through iCloud Drive, with no
paid developer account. `mac/ArenaArchive.swift` already has a working
version, but it's switched off. Turn it on with
`defaults write studio.oxoy.arena-archive useICloud -bool true`. It works like
this:

- On first launch, it copies `archive.db` and `assets/` to iCloud Drive ›
  ArenaArchive.
- It edits a local copy of the database in
  `~/Library/Application Support/ArenaArchive` and writes it back to iCloud
  every 20 seconds, before sleep, and on quit. SQLite files can't safely be
  live-synced.
- `lock.json` in the iCloud folder shows which Mac has the archive open. The
  other Mac can take over or open it read-only.
- If both Macs changed the database while out of sync, the local changes are
  saved as `archive conflict <Mac> <date>.db`.

Before relying on it:

- Test it on two real Macs.
- Decide how assets evicted by "Optimize Mac Storage" should behave.
- Add a way to run the importer against the synced folder.

## Importer

The importer stores `archive.db` and downloaded files under `assets/`. Set
`ARENA_TOKEN` enables importing channels visible to that account, including
private channels. Keep the resulting archive local.

Images dropped into a channel are stored under
`assets/channels/<channel-id>/`. Browsers provide a copy of a local file to a
web app, so the original file on your computer is not moved or deleted.

The API importer spaces requests and retries HTTP 429 responses using
Are.na's `Retry-After` value. With a personal read token, it uses the V3
`scope=my` search to find your full channel set, including private channels.
Without a token, it falls back to publicly discoverable, non-private channels
owned by the profile:

```bash
ARENA_TOKEN=your_read_token python3 arena_archive.py import maus-cats
```

Useful options:

```bash
python3 arena_archive.py import maus-cats --database my-archive.db --assets my-assets
python3 arena_archive.py import maus-cats --fixture fixtures/profile.json
```

The fixture option is intended for development and tests. API requests are
paginated and limited to the account's owned channels and their first-level
contents; nested channel contents are not traversed.
