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

### Updating

Push changes from the Mac where you made them. The button at the top right of
the archive window checks GitHub when you open the app (at most every 10
minutes) and when you click it. When there are new commits it turns yellow and
says how many, and clicking it runs the update. You can also choose
**CHANNEL › Update CHANNEL…**, or run:

```bash
./mac/update.sh
```

It pulls from GitHub, quits CHANNEL (which writes the archive back to iCloud),
rebuilds, reinstalls and reopens it. It stops if the project folder has
uncommitted changes.

In iCloud mode, each Mac records its build in iCloud Drive › CHANNEL ›
versions. When you open CHANNEL on a Mac with an older build than your other
Mac, it tells you and offers to update.

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

### iCloud sync

To share the archive between Macs, choose **File › Move Archive to iCloud
Drive…**. It copies `archive.db` and `assets/` from your archive folder to
iCloud Drive › CHANNEL, and the app uses that copy from then on. The originals
stay where they are. On your other Mac, build and install the app, then choose
the same menu item. It finds the archive already in iCloud and uses it.

How it syncs:

- The app edits a local copy of the database in
  `~/Library/Application Support/ArenaArchive` and writes it back to iCloud
  every 20 seconds, before sleep, and on quit. SQLite files can't safely be
  synced while they're open.
- Assets are read from and written to iCloud directly. Thumbnails are a local
  cache in the same Application Support folder, so they don't sync.
- `lock.json` in the iCloud folder shows which Mac has the archive open. The
  other Mac can take over or open it read-only.
- If both Macs changed the database while out of sync, the local changes are
  saved as `archive conflict <Mac> <date>.db` next to it. Nothing is
  overwritten.

Wait for iCloud to finish syncing before opening the app on the other Mac.
To import into the synced archive, quit the app first and point the importer
at the iCloud folder:

```bash
cd ~/Library/Mobile\ Documents/com~apple~CloudDocs/CHANNEL
python3 /path/to/arena_archive.py import maus-cats --database archive.db --assets assets
```

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
