# EvilFlowers for Calibre

A Calibre plugin that keeps a Calibre library in sync with a catalog on an
[EvilFlowers](https://github.com/EvilFlowersCatalog/EvilFlowersCatalog) server. Point
several computers at the same catalog and they share one library: books, metadata,
covers and EPUB/PDF/MOBI files.

- Books new in Calibre are uploaded. Entries new in the catalog are downloaded.
- A change on one side is applied to the other. When both sides changed, you choose.
- **Nothing is ever deleted**, on either side.

Requires Calibre 7 or newer (tested on Calibre 9.15).

## Use

1. Install `dist/evilflowers-<version>.zip` (Preferences → Plugins → *Load plugin from
   file*) and add the **EvilFlowers** button to a toolbar.
2. In the button's menu, choose **Settings…**. Enter the server URL, an API key and
   the catalog's `url_name`, then press **Test connection**.
3. Click the button. The plugin compares the library with the catalog and lists
   every book with what it will do: *upload*, *download* or *skip*. Change any row
   (right-click for several at once), then press **Synchronize**.

| Status | Meaning | Default |
| --- | --- | --- |
| new in Calibre / new in catalog | exists on one side only | upload / download |
| changed in Calibre / changed in catalog | one side changed since the last sync | upload / download |
| changed on both sides | conflict | skip: pick a side |
| same book on both sides, never synced | same UUID, no sync record | skip: pick a side |
| deleted in Calibre / deleted in catalog | gone on one side after a sync | skip (download or upload restores it) |

What travels: title, authors, language, tags (as categories), comments (as summary),
publisher, publication date, identifiers (ISBN, DOI, Google, Dataverse), cover, and
EPUB/PDF/MOBI files. Everything else (series, rating, custom columns, other formats)
stays local and is never overwritten by a download.

**Upgrading from 0.x:** remove the old *EvilFlowers OPDS client* plugin first. Its
settings (server, API key, catalog) are carried over.

## How it works

```
calibre_plugin/
  __init__.py          plugin declaration (no Qt imports)
  ui.py                toolbar button and menu
  dialog.py            sync window: plan table, per-row actions, progress
  config.py            settings page
  worker.py            QThread running a scan or sync off the GUI thread
  settings.py          preferences (Qt-free)
  remote/              API layer
    client.py          endpoints, on the vendored transport; downloads
    models.py          dataclass wire models
  sync/                engine (Qt-free, tested without a GUI)
    plan.py            pure planner: book × entry × baseline → state + action
    engine.py          scan and apply: one push or pull per item
    mapping.py         Calibre Metadata ⇄ catalog entry
    local.py           bulk snapshot of the library
    state.py           sync baselines, stored in the library's metadata.db
  vendor/evilflowers_protocol/   stdlib layers of evilflowers-protocol-python
```

- **Identity.** A book's Calibre UUID becomes its entry id on upload, and a
  downloaded entry keeps its id as the book's UUID. The same book has the same id
  in every library and in the catalog.
- **Change detection.** After each book's sync, a *baseline* records both sides as
  they were: the book's `last_modified`, the entry's `updated_at` plus a fingerprint
  of its synced fields, and which acquisition holds which format. Each side is
  compared with its own baseline, never with the other side's clock. Baselines live
  in the library (`metadata.db`, per-book plugin data), so they travel with the
  library, not the Calibre profile. Writing them does not touch `last_modified`.
- **Deleted books** are detected through a per-library list of entry ids this
  library has synced, so a book deleted locally is not downloaded again as new.
- **Files** are streamed (multipart upload, chunked download). When a format
  changes, the new file is uploaded and the old acquisition removed. The plugin only
  ever removes acquisitions it uploaded itself.
- **Downloads** follow the server's redirect by hand. The API token goes only to
  the catalog's own origin, never to a storage host a redirect points at.

### evilflowers-protocol-python

Calibre runs plugins on its own bundled Python, which has no `pydantic`.
`pydantic_core` is a compiled extension that cannot be imported from a plugin ZIP, so
the plugin vendors only the library's stdlib layers (`transport.py`, `errors.py`)
with a small patch that swaps pydantic models for `from_dict`/`to_dict` protocols.
See `calibre_plugin/vendor/evilflowers_protocol/UPSTREAM.md`. To update:

```shell
python3 scripts/vendor_protocol.py /path/to/evilflowers-protocol-python
```

### Server behaviour the plugin works around

Observed on dev on 2026-11-08. These are worth fixing in the catalog:

- Downloading a file bumps the entry's `updated_at` (it saves `popularity`). The
  plugin double-checks moved entries against a field fingerprint.
- The live API sends `authors` and `image`, but the OpenAPI spec on `main` says
  `entry_authors` and `image_url`. The plugin reads both spellings.
- `image` is set even when an entry has no cover, and its URL is `http://`. The
  plugin uses `image_mime` to decide whether a cover exists.
- `GET /data/v1/covers/{id}` answers 500 instead of 404 for an entry without a
  cover. A failed cover download never fails a book's sync.
- Downloading through `/data/v1/acquisitions/{id}` creates a user-acquisition
  record on every download.

### Known limitations

- Uploading over an entry that already has a file of the same type which this
  library did not upload (status *same book on both sides, never synced*) adds a
  second file instead of replacing it. The API exposes no content hash to tell
  whether the two are the same.
- A cover replaced in the catalog by another image of the same type is not
  detected as a change. The API exposes no cover checksum.

## Development

Everything runs against an isolated Calibre profile and library under `.dev/`
(gitignored). Your normal Calibre profile and library are never used: every command
sets `CALIBRE_CONFIG_DIRECTORY=.dev/config` and opens `.dev/library`.

```shell
scripts/dev.sh run         # seed .dev/library if needed, install the plugin, start Calibre in debug mode
scripts/dev.sh test        # offline suite: planner, models, engine, headless GUI (fake catalog)
scripts/dev.sh test-dev    # + end-to-end against the dev catalog (needs a token)
scripts/dev.sh test -k pull
scripts/dev.sh calibredb list
scripts/dev.sh debug some_script.py
scripts/dev.sh build       # dist/evilflowers-<version>.zip
scripts/dev.sh reset       # delete .dev/config and .dev/library
```

- `run` refuses to start while another Calibre GUI is running. Calibre's
  single-instance socket does not depend on the config directory, so a second
  instance would hand over to the running one and its real library.
- `test` and `test-dev` run under Calibre's own Python (`calibre-debug -e`) with real
  `calibre.db` libraries in temporary folders. They refuse to run against any profile
  outside `.dev/`.
- `test-dev` reads the token from `$ELVIRA_DEV_API_KEY` or from
  `EVILFLOWERS_TOKEN=…` in `.dev/secrets.env`. It writes only to the
  `calibre-sync-test` catalog on dev (created if missing), with unique titles, and
  leaves its entries there.
- Lint with `uvx ruff check` and `uvx ruff format` (configured in `pyproject.toml`,
  targeting Python 3.11 for Calibre 7).

## Resources

- [Calibre: Writing your own plugins](https://manual.calibre-ebook.com/creating_plugins.html)
- [Calibre: database API](https://manual.calibre-ebook.com/db_api.html)
- [evilflowers-protocol-python](https://github.com/EvilFlowersCatalog/evilflowers-protocol-python)

---
Made with ❤️ 🍺 and ☕️ Jakub Dubec
