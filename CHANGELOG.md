# Changelog

All notable changes to this plugin. The format follows
[Keep a Changelog](https://keepachangelog.com/en/1.1.0/), and versions follow
[Semantic Versioning](https://semver.org/).

## Unreleased

### Added

- GitHub Actions: CI (lint, tests under Calibre on Linux, plugin ZIP build) on
  every push and pull request; publishing a GitHub release attaches the plugin ZIP
  to it. `scripts/release.py` checks that the version agrees everywhere and
  extracts the release notes from this file.

## 2.1.0 - 2026-11-08

### Added

- **Sign in with your EvilFlowers account.** *Settings → Sign in…* asks for your
  username and password once, creates an API key named *Calibre on &lt;computer&gt;*
  and stores only that key. The password is never saved or logged. **Sign out**
  and **Switch account** revoke the key on the server. Pasting your own API key
  still works under *Use an API key instead*.
- **Catalog picker.** After sign-in, the catalog is chosen from the catalogs your
  account can see instead of typed by its `url_name`.
- **Browse catalog…** in the toolbar menu: search the catalog, filter by language
  and category, page through results with covers, and download just the books you
  pick. Badges show what is already *In your library* or *On your shelf*.
- **EvilFlowers shelf.** Add and remove books from your shelf in the browser, and
  turn on *Only download books on my shelf* to keep a large catalog from flooding
  the library. Books already linked keep syncing either way.
- **Highlights for selected book…** shows the passages you highlighted in the
  EvilFlowers reader, grouped and with page numbers. Copy them, or save them into a
  long-text custom column. Read-only: nothing is sent back.
- **New look.** Toolbar icon and window headers made from the EvilFlowers painting,
  with light and dark variants that follow Calibre's theme, also while a window is
  open.
- Every setting and form field has a one-line explanation under it and a tooltip.
- `scripts/dev.sh screenshots` renders every window in light and dark; screenshots
  are in [docs/](docs/README.md).
- Live end-to-end tests for the new endpoints (`scripts/dev.sh test-dev`): catalogs,
  search, facets, categories, thumbnails, shelf, highlights and the API key
  lifecycle.

### Changed

- **Sync window redesign.** Count chips filter the list (*to review*, *to upload*,
  *to download*, *need a decision*, *in sync*, *all*). Rows show covers, a status
  phrase and the action as a coloured pill. Selecting a book changed on both sides
  opens a side-by-side comparison with *Keep Calibre version* / *Keep catalog
  version* / *Skip*.
- The sync window shows a set-up page on first run, a loading state while
  comparing, *Your library is in sync ✓* when there is nothing to do, and errors in
  a banner with *Retry*, instead of message boxes.
- Row and chip colours come from Calibre's palette, so they read well in both
  themes.
- *Test connection* runs in the background and appears only with a pasted API key;
  signing in already proves the connection.
- The Server field shows and stores the site address (`https://host`). An address
  saved by 2.0 with `/api/v1` on the end is shown without it.

### Fixed

- A server reached under a path (`https://host/library`) now works; 2.0 accepted
  only a bare host or a full `…/api/v1` address.

## 2.0.0 - 2026-11-08

### Changed

- Rewritten as a two-way sync between a Calibre library and an EvilFlowers catalog:
  books, metadata, covers and EPUB/PDF/MOBI files. Nothing is ever deleted on
  either side. Replaces the 0.x *EvilFlowers OPDS client* plugin, whose settings
  (server, API key, catalog) are carried over.
