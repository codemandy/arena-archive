# Are.na Archive

A one-time, public Are.na archive importer with a local browser.

## Usage

```bash
python3 arena_archive.py import maus-cats
python3 server.py
```

Then open <http://127.0.0.1:8765>.

The importer stores `archive.db` and downloaded files under `assets/`. Set
`ARENA_TOKEN` enables importing channels visible to that account, including
private channels. Keep the resulting archive local.

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
