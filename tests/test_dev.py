"""End-to-end against a real catalog (``scripts/dev.sh test-dev``).

Uses $EVILFLOWERS_DEV_URL (default https://dev.evilflowers.elvira.stuba.sk),
$EVILFLOWERS_DEV_CATALOG (default calibre-sync-test, created when missing) and
$EVILFLOWERS_TOKEN. Writes only to that catalog. Every run uses unique titles,
so runs never collide with each other; entries are left behind (the plugin never
deletes), so point this at a throwaway catalog.

``DevClientTests`` checks the browsing, shelf, highlight and API key calls against
the live server; it adds coverless entries to the same catalog and revokes the one
API key it creates.
"""

from __future__ import annotations

import logging
import os
import shutil
import tempfile
import time
import unittest
from datetime import UTC, datetime
from pathlib import Path
from typing import Any
from uuid import UUID, uuid4

from calibre.ebooks.metadata.book.base import Metadata
from calibre.library import db as open_library
from calibre.utils.img import create_canvas, image_to_data
from epub import epub

from calibre_plugin.remote import auth
from calibre_plugin.remote.client import CatalogClient
from calibre_plugin.remote.models import CatalogRef, EntryForm, EntryRef
from calibre_plugin.settings import Settings
from calibre_plugin.sync.engine import SyncEngine
from calibre_plugin.sync.plan import State
from calibre_plugin.vendor.evilflowers_protocol.errors import NotFound, Unauthorized

URL = os.environ.get("EVILFLOWERS_DEV_URL", "https://dev.evilflowers.elvira.stuba.sk")
CATALOG = os.environ.get("EVILFLOWERS_DEV_CATALOG", "calibre-sync-test")
TOKEN = os.environ.get("EVILFLOWERS_TOKEN", "")

if os.environ.get("VERBOSE"):
    logging.basicConfig(level=logging.DEBUG)


def ensure_catalog(client: CatalogClient) -> CatalogRef:
    try:
        return client.resolve_catalog(CATALOG)
    except NotFound:
        response = client.transport.request(
            "POST",
            "catalogs",
            json_body={"url_name": CATALOG, "title": "Calibre sync tests", "is_public": False},
        )
        return response.model(CatalogRef)


@unittest.skipUnless(TOKEN, "no EVILFLOWERS_TOKEN")
class DevCatalogTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls) -> None:
        cls.client = CatalogClient(URL, TOKEN, user_agent="evilflowers-calibre-tests")
        cls.user = cls.client.me()  # the first call the plugin's dialog and "Test connection" make
        cls.catalog = ensure_catalog(cls.client)

    def setUp(self) -> None:
        self.tmp = Path(tempfile.mkdtemp(prefix="evilflowers-dev-"))
        self.addCleanup(shutil.rmtree, self.tmp, True)
        self.run_id = time.strftime("%Y%m%d-%H%M%S")

    def library(self, name: str) -> Any:
        db = open_library(str(self.tmp / name)).new_api
        self.addCleanup(db.close)
        return db

    def engine(self, db: Any) -> SyncEngine:
        return SyncEngine(db, self.client, self.catalog, Settings(URL, TOKEN, CATALOG))

    def mine(self, items: list[Any]) -> list[Any]:
        """Only this run's books: the catalog also holds every earlier run's entries."""
        return [item for item in items if self.run_id in item.title]

    def test_round_trip(self) -> None:
        a = self.library("a")
        title = f"Calibre sync test {self.run_id}"
        mi = Metadata(title, ["Ján Novák", "Mary Shelley"])
        mi.languages = ["slk"]
        mi.tags = ["calibre-sync-test"]
        mi.comments = "<p>Created by the calibre-evilflowers test suite.</p>"
        mi.identifiers = {"isbn": f"978{int(time.time()) % 10_000_000_000:010d}"}
        mi.publisher = "T. Egerton"
        mi.pubdate = datetime(1813, 1, 28, tzinfo=UTC)
        source = self.tmp / "book.epub"
        source.write_bytes(epub(title))
        [book_id], _ = a.add_books([(mi, {"EPUB": str(source)})], run_hooks=False)
        a.set_cover({book_id: image_to_data(create_canvas(300, 450, "#336699"), fmt="JPEG")})
        uid = a.field_for("uuid", book_id)

        engine = self.engine(a)
        [item] = [i for i in engine.scan() if i.book is not None]
        self.assertEqual(item.state, State.LOCAL_ONLY)
        report = engine.apply([item])
        self.assertEqual([o.message for o in report.failed], [])

        remote = self.client.get_entry(self.catalog.id, UUID(uid))
        self.assertEqual(str(remote.id), uid)
        self.assertEqual([acq.calibre_format for acq in remote.acquisitions], ["EPUB"])
        [again] = [i for i in self.engine(a).scan() if i.book is not None]
        self.assertEqual(again.state, State.IN_SYNC, "a fresh push must scan as in sync")

        b = self.library("b")
        engine_b = self.engine(b)
        pulls = self.mine(engine_b.scan())
        self.assertEqual([(i.title, i.state) for i in pulls], [(title, State.REMOTE_ONLY)])
        report = engine_b.apply(pulls)
        self.assertEqual([o.message for o in report.failed], [])
        pulled = b.lookup_by_uuid(uid)
        self.assertIsNotNone(pulled)
        self.assertEqual(b.format(pulled, "EPUB"), epub(title))
        self.assertEqual(b.field_for("authors", pulled), ("Ján Novák", "Mary Shelley"))
        self.assertEqual(b.field_for("languages", pulled), ("slk",))
        self.assertTrue(b.cover(pulled), "the cover should come down with the book")
        self.assertEqual(b.field_for("publisher", pulled), "T. Egerton")
        self.assertEqual(b.field_for("pubdate", pulled).date().isoformat(), "1813-01-28")
        self.assertEqual(b.field_for("identifiers", pulled), mi.identifiers)
        self.assertEqual(self.mine([i for i in self.engine(b).scan()])[0].state, State.IN_SYNC)

        # A local edit travels back.
        a.set_field("comments", {book_id: "<p>edited in library a</p>"})
        engine = self.engine(a)
        [changed] = [i for i in engine.scan() if i.book is not None]
        self.assertEqual(changed.state, State.LOCAL_CHANGED)
        self.assertEqual(engine.apply([changed]).failed, [])
        [seen] = self.mine(self.engine(b).scan())
        self.assertEqual(seen.state, State.REMOTE_CHANGED)


@unittest.skipUnless(TOKEN, "no EVILFLOWERS_TOKEN")
class DevClientTests(unittest.TestCase):
    """The browsing, shelf, highlight and API key calls against the live server.

    Writes only to the test catalog: one fresh entry per run (unique title) for the
    shelf test, and one API key that is revoked before the test ends.
    """

    @classmethod
    def setUpClass(cls) -> None:
        cls.client = CatalogClient(URL, TOKEN, user_agent="evilflowers-calibre-tests")
        cls.catalog = ensure_catalog(cls.client)
        # No timestamp: DevCatalogTests.mine() picks entries by its own timestamp in the title.
        cls.run_id = f"run{uuid4().hex[:12]}"

    def setUp(self) -> None:
        self.tmp = Path(tempfile.mkdtemp(prefix="evilflowers-dev-"))
        self.addCleanup(shutil.rmtree, self.tmp, True)

    def all_entries(self) -> list[EntryRef]:
        return list(self.client.iter_entries(self.catalog.id))

    def fresh_entry(self, label: str) -> EntryRef:
        """A new entry with no cover and no files, only in the test catalog."""
        self.assertEqual(self.catalog.url_name, CATALOG)
        return self.client.create_entry(
            self.catalog.id, EntryForm(title=f"Calibre {label} test {self.run_id}", language_code="sk")
        )

    def test_list_catalogs(self) -> None:
        self.assertIn(CATALOG, [catalog.url_name for catalog in self.client.list_catalogs()])

    def test_me_has_name_fields(self) -> None:
        raw = self.client.transport.request("GET", "users/me").body["response"]
        me = self.client.me()
        self.assertEqual((str(me.id), me.username), (raw["id"], raw["username"]))
        self.assertEqual((me.name, me.surname), (raw.get("name") or "", raw.get("surname") or ""))
        expected = " ".join(part for part in (me.name, me.surname) if part) or me.username
        self.assertEqual(me.display_name, expected)
        self.assertTrue(me.display_name)

    def test_search_entries_pages(self) -> None:
        self.fresh_entry("search")  # at least one match, whatever earlier runs left
        everything = self.client.search_entries(self.catalog.id, query="Calibre test", limit=1)
        self.assertEqual(everything.page, 1)
        self.assertGreaterEqual(everything.total, 1)
        self.assertEqual(everything.pages, everything.total)  # limit=1: one entry per page
        self.assertEqual(len(everything.items), 1)
        mine = self.client.search_entries(self.catalog.id, query=f"search test {self.run_id}")
        self.assertEqual([entry.title for entry in mine.items], [f"Calibre search test {self.run_id}"])
        self.assertEqual((mine.page, mine.pages, mine.total), (1, 1, 1))
        if everything.pages > 1:
            second = self.client.search_entries(self.catalog.id, query="Calibre test", page=2, limit=1)
            self.assertEqual(second.page, 2)
            self.assertNotEqual(second.items[0].id, everything.items[0].id)
        with self.assertRaises(NotFound):
            self.client.search_entries(self.catalog.id, query="Calibre test", page=everything.pages + 1, limit=1)
        nothing = self.client.search_entries(self.catalog.id, query=f"no-such-word-{uuid4().hex}")
        self.assertEqual((nothing.items, nothing.page, nothing.pages, nothing.total), ([], 1, 1, 0))

    def test_facets_and_categories(self) -> None:
        facets = self.client.facets(self.catalog.id)
        categories = self.client.list_categories(self.catalog.id)
        total = self.client.search_entries(self.catalog.id, limit=1).total
        self.assertTrue(facets.languages, "the test catalog has entries, so some language")
        self.assertTrue(all(len(code) == 2 and count > 0 for code, count in facets.languages))
        self.assertLessEqual(sum(count for _, count in facets.languages), total)
        self.assertTrue(all(label for label in categories.values()))
        self.assertLessEqual({category_id for category_id, _ in facets.categories}, set(categories))

    def test_download_thumbnail(self) -> None:
        entries = self.all_entries()
        with_cover = next((entry for entry in entries if entry.has_cover), None)
        if with_cover is None:
            self.skipTest("no entry with a cover in the test catalog; run test_round_trip first")
        self.assertTrue(with_cover.thumbnail and with_cover.thumbnail.endswith(f"/data/v1/thumbnails/{with_cover.id}"))
        path = self.client.download_thumbnail(with_cover.id, self.tmp / "with.img")
        self.assertIsNotNone(path)
        self.assertGreater(path.stat().st_size, 0)
        without = next((entry for entry in entries if not entry.has_cover), None) or self.fresh_entry("cover")
        self.assertIsNone(without.thumbnail, "the server's placeholder URL must not count as a thumbnail")
        self.assertIsNone(self.client.download_thumbnail(without.id, self.tmp / "without.img"))
        self.assertIsNone(self.client.download_thumbnail(uuid4(), self.tmp / "missing.img"))

    def test_shelf_round_trip(self) -> None:
        entry = self.fresh_entry("shelf")
        self.assertEqual(entry.catalog_id, self.catalog.id)
        record = self.client.add_to_shelf(entry.id)
        self.addCleanup(self.client.remove_from_shelf, record.id)
        self.assertEqual(record.entry_id, entry.id)
        self.assertEqual(self.client.add_to_shelf(entry.id).id, record.id, "adding twice keeps one record")
        shelf = {item.id: item.entry_id for item in self.client.list_shelf(self.catalog.id)}
        self.assertEqual(shelf.get(record.id), entry.id)

        def shelf_record_id() -> UUID | None:
            page = self.client.search_entries(self.catalog.id, query=f"shelf test {self.run_id}")
            [found] = page.items
            return found.shelf_record_id

        self.assertEqual(shelf_record_id(), record.id)
        self.client.remove_from_shelf(record.id)
        self.client.remove_from_shelf(record.id)  # already gone counts as done
        self.assertNotIn(record.id, {item.id for item in self.client.list_shelf(self.catalog.id)})
        self.assertIsNone(shelf_record_id())

    def test_list_highlights_empty(self) -> None:
        me = self.client.me()
        with_files = next((entry for entry in self.all_entries() if entry.acquisitions), None)
        if with_files is None:
            self.skipTest("no entry with files in the test catalog; run test_round_trip first")
        self.assertEqual(self.client.list_highlights(with_files, me.id), [])
        self.assertEqual(self.client.list_highlights(self.fresh_entry("highlights"), me.id), [])

    def test_login_with_wrong_password(self) -> None:
        # POST /token takes no Authorization header; a wrong password is a plain 401.
        with self.assertRaises(Unauthorized) as caught:
            auth.login(
                URL, f"calibre-evilflowers-nobody-{uuid4().hex}", uuid4().hex, user_agent="evilflowers-calibre-tests"
            )
        self.assertEqual(caught.exception.status, 401)
        self.assertIn(auth.WRONG_CREDENTIALS, str(caught.exception))

    def test_api_key_lifecycle(self) -> None:
        name = f"calibre-evilflowers test-dev {self.run_id}"
        key = auth.create_api_key(URL, TOKEN, name, user_agent="evilflowers-calibre-tests")
        self.addCleanup(self.client.revoke_api_key, key.id)  # also when an assertion fails
        self.assertEqual(key.name, name)
        self.assertNotIn(key.token, repr(key))
        self.assertNotEqual(key.token, TOKEN)
        with_key = CatalogClient(URL, key.token, user_agent="evilflowers-calibre-tests", retries=0)
        self.assertEqual(with_key.me().id, self.client.me().id)
        self.client.revoke_api_key(key.id)
        with self.assertRaises(Unauthorized) as caught:
            with_key.me()
        self.assertEqual(caught.exception.status, 401)
        self.assertNotIn(key.token, str(caught.exception))
        self.client.revoke_api_key(key.id)  # already gone counts as revoked
