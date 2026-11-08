"""The sync engine against real (temporary) Calibre libraries and the fake catalog."""

from __future__ import annotations

import shutil
import tempfile
import unittest
from dataclasses import replace
from pathlib import Path
from typing import Any

from calibre.ebooks.metadata.book.base import Metadata
from calibre.library import db as open_library
from epub import epub
from fake_catalog import TOKEN, FakeCatalog

from calibre_plugin.remote.client import CatalogClient
from calibre_plugin.settings import Direction, Settings
from calibre_plugin.sync.engine import SyncEngine
from calibre_plugin.sync.mapping import entry_form
from calibre_plugin.sync.plan import Action, PlanItem, State

EPUB = "application/epub+zip"
JPEG = b"\xff\xd8\xff\xe0" + b"\x00" * 64  # enough of a JPEG for a byte round trip


class EngineTests(unittest.TestCase):
    def setUp(self) -> None:
        self.fake = FakeCatalog().start()
        self.addCleanup(self.fake.stop)
        self.tmp = Path(tempfile.mkdtemp(prefix="evilflowers-test-"))
        self.addCleanup(shutil.rmtree, self.tmp, True)
        self.client = CatalogClient(self.fake.base_url, TOKEN, retries=0, user_agent="evilflowers-calibre-tests")
        self.catalog = self.client.resolve_catalog(self.fake.catalog_url_name)

    # ---------------------------------------------------------------- helpers

    def library(self, name: str) -> Any:
        db = open_library(str(self.tmp / name)).new_api
        self.addCleanup(db.close)  # before rmtree: cleanups run last-in, first-out
        return db

    def add_book(self, db: Any, title: str, files: dict[str, bytes], **fields: Any) -> int:
        mi = Metadata(title, fields.pop("authors", ["Ján Novák"]))
        for key, value in fields.items():
            setattr(mi, key, value)
        paths = {fmt: self.write(f"{title}.{fmt.lower()}", content) for fmt, content in files.items()}
        [book_id], _ = db.add_books([(mi, paths)], run_hooks=False)
        return book_id

    def write(self, name: str, content: bytes) -> str:
        path = self.tmp / name
        path.write_bytes(content)
        return str(path)

    def engine(self, db: Any, direction: Direction = Direction.BOTH, *, shelf_only: bool = False) -> SyncEngine:
        settings = Settings(self.fake.base_url, TOKEN, self.fake.catalog_url_name, direction, shelf_only=shelf_only)
        return SyncEngine(db, self.client, self.catalog, settings)

    def states(self, db: Any) -> dict[str, State]:
        return {item.title: item.state for item in self.engine(db).scan()}

    def sync(self, db: Any, choose: dict[str, Action] | None = None) -> list[PlanItem]:
        engine = self.engine(db)
        plan = [
            item.with_action(choose[item.title]) if choose and item.title in choose else item for item in engine.scan()
        ]
        report = engine.apply(plan)
        self.assertEqual([o.message for o in report.failed], [])
        return plan

    # ------------------------------------------------------------------ tests

    def test_push_then_pull_round_trip(self) -> None:
        a = self.library("a")
        book_id = self.add_book(
            a,
            "Marína",
            {"EPUB": epub("v1"), "TXT": b"plain"},
            authors=["Andrej Sládkovič"],
            languages=["slk"],
            tags=["Poetry", "Classics"],
            comments="<p>báseň</p>",
            identifiers={"isbn": "9780000000001", "goodreads": "42"},
        )
        a.set_cover({book_id: JPEG})
        uid = a.field_for("uuid", book_id)

        self.assertEqual(self.states(a), {"Marína": State.LOCAL_ONLY})
        self.sync(a)

        remote = self.fake.entries[uid]  # the entry id is the Calibre UUID
        self.assertEqual(remote.data["language_code"], "sk")
        self.assertEqual(remote.data["authors"], [{"name": "Andrej", "surname": "Sládkovič"}])
        self.assertEqual(remote.data["identifiers"], {"isbn": "9780000000001"})  # goodreads is not a catalog type
        self.assertEqual([(acq.mime, acq.content) for acq in remote.acquisitions], [(EPUB, epub("v1"))])  # no TXT
        self.assertIsNotNone(remote.cover)
        self.assertEqual(self.states(a), {"Marína": State.IN_SYNC})

        b = self.library("b")
        self.assertEqual(self.states(b), {"Marína": State.REMOTE_ONLY})
        self.sync(b)
        [pulled] = b.all_book_ids()
        self.assertEqual(b.field_for("uuid", pulled), uid)
        self.assertEqual(b.field_for("authors", pulled), ("Andrej Sládkovič",))
        self.assertEqual(b.field_for("languages", pulled), ("slk",))
        self.assertEqual(set(b.field_for("tags", pulled)), {"Poetry", "Classics"})
        self.assertEqual(b.format(pulled, "EPUB"), epub("v1"))
        self.assertTrue(b.cover(pulled))
        self.assertEqual(self.states(b), {"Marína": State.IN_SYNC})
        self.assertEqual(self.fake.storage_auth_headers, [], "the token leaked to the storage host")

    def test_changes_flow_both_ways(self) -> None:
        a, b = self.library("a"), self.library("b")
        book_id = self.add_book(a, "Original", {"EPUB": epub("v1")})
        uid = a.field_for("uuid", book_id)
        self.sync(a)
        self.sync(b)
        b_id = b.lookup_by_uuid(uid)

        # Edited elsewhere -> pulled here; fields the catalog does not hold stay as they are.
        a.set_field("rating", {book_id: 8})
        self.sync(a)
        self.fake.edit_entry(uid, title="Renamed remotely", language_code="en")
        self.assertEqual({item.state for item in self.engine(a).scan()}, {State.REMOTE_CHANGED})
        self.sync(a)
        self.assertEqual(a.field_for("title", book_id), "Renamed remotely")
        self.assertEqual(a.field_for("rating", book_id), 8)

        # Edited here -> pushed, including a replaced file.
        a.set_field("title", {book_id: "Renamed locally"})
        a.add_format(book_id, "EPUB", self.write("v2.epub", epub("v2")), replace=True, run_hooks=False)
        self.assertEqual({item.state for item in self.engine(a).scan()}, {State.LOCAL_CHANGED})
        self.sync(a)
        remote = self.fake.entries[uid]
        self.assertEqual(remote.data["title"], "Renamed locally")
        self.assertEqual([acq.content for acq in remote.acquisitions], [epub("v2")], "the old file should be replaced")

        # The other library picks both changes up.
        self.sync(b)
        self.assertEqual(b.field_for("title", b_id), "Renamed locally")
        self.assertEqual(b.format(b_id, "EPUB"), epub("v2"))

    def test_conflict_is_skipped_until_the_user_picks_a_side(self) -> None:
        a = self.library("a")
        book_id = self.add_book(a, "Contested", {"EPUB": epub("x")})
        uid = a.field_for("uuid", book_id)
        self.sync(a)
        a.set_field("comments", {book_id: "<p>local</p>"})
        self.fake.edit_entry(uid, summary="<p>remote</p>")

        [item] = self.engine(a).scan()
        self.assertEqual((item.state, item.action), (State.CONFLICT, Action.SKIP))
        self.engine(a).apply([item])
        self.assertEqual(self.fake.entries[uid].data["summary"], "<p>remote</p>")

        self.sync(a, choose={"Contested": Action.PUSH})
        self.assertEqual(self.fake.entries[uid].data["summary"], "<p>local</p>")
        self.assertEqual({item.state for item in self.engine(a).scan()}, {State.IN_SYNC})

    def test_deletions_are_never_propagated(self) -> None:
        a, b = self.library("a"), self.library("b")
        first = self.add_book(a, "Deleted here", {"EPUB": epub("1")})
        self.add_book(a, "Deleted there", {"EPUB": epub("2")})
        self.sync(a)
        self.sync(b)

        a.remove_books([first])
        self.fake.delete_entry(_uuid_of(a, "Deleted there"))
        self.assertEqual(self.states(a), {"Deleted here": State.LOCAL_DELETED, "Deleted there": State.REMOTE_DELETED})
        plan = self.sync(a)
        self.assertTrue(all(item.action is Action.SKIP for item in plan))
        self.assertEqual(len(a.all_book_ids()), 1)

    def test_duplicate_title_fails_that_book_only(self) -> None:
        self.fake.add_remote_entry("Taken", {})
        a = self.library("a")
        self.add_book(a, "Taken", {"EPUB": epub("x")})
        self.add_book(a, "Free", {"EPUB": epub("y")})
        engine = self.engine(a)
        plan = [
            item if item.state is not State.REMOTE_ONLY else item.with_action(Action.SKIP) for item in engine.scan()
        ]
        report = engine.apply(plan)
        self.assertEqual([o.item.title for o in report.failed], ["Taken"])
        self.assertIn("title, ISBN or DOI", report.failed[0].message)
        self.assertEqual(report.count(Action.PUSH), 1)

    def test_entry_created_without_baseline_is_linked_on_push(self) -> None:
        a = self.library("a")
        book_id = self.add_book(a, "Half done", {"EPUB": epub("x")})
        uid = a.field_for("uuid", book_id)
        # A run that created the entry and died before writing the baseline.
        self.client.create_entry(self.catalog.id, entry_form(a, book_id, create=True, default_language="en"))
        self.assertEqual(self.states(a), {"Half done": State.UNLINKED})
        self.sync(a, choose={"Half done": Action.PUSH})
        self.assertEqual([acq.content for acq in self.fake.entries[uid].acquisitions], [epub("x")])
        self.assertEqual(self.states(a), {"Half done": State.IN_SYNC})

    def test_push_only_never_writes_the_library(self) -> None:
        self.fake.add_remote_entry("Remote book", {EPUB: epub("r")})
        a = self.library("a")
        self.add_book(a, "Local book", {"EPUB": epub("l")})
        engine = self.engine(a, Direction.PUSH)
        engine.apply(engine.scan())
        self.assertEqual(len(a.all_book_ids()), 1)
        self.assertEqual(len(self.fake.entries), 2)

    def test_pull_keeps_local_only_formats(self) -> None:
        a, b = self.library("a"), self.library("b")
        book_id = self.add_book(a, "Mixed", {"EPUB": epub("e"), "TXT": b"t"})
        self.sync(a)
        self.fake.edit_entry(a.field_for("uuid", book_id), summary="<p>new</p>")
        self.sync(a)
        self.assertEqual(set(a.formats(book_id)), {"EPUB", "TXT"})
        self.sync(b)
        [pulled] = b.all_book_ids()
        self.assertEqual(set(b.formats(pulled)), {"EPUB"})

    def test_shelf_only_downloads_shelved_entries_and_keeps_linked_books(self) -> None:
        a = self.library("a")
        linked = self.fake.add_remote_entry("Linked earlier", {EPUB: epub("l")})
        self.sync(a)  # pulled while every entry was offered; not on the shelf
        shelved = self.fake.add_remote_entry("On my shelf", {EPUB: epub("s")})
        self.fake.add_remote_entry("Not on my shelf", {EPUB: epub("n")})
        self.fake.add_shelf_record(shelved)
        self.fake.edit_entry(linked, title="Linked, renamed")

        progress: list[str] = []
        engine = self.engine(a, shelf_only=True)
        engine._progress = lambda _fraction, message: progress.append(message)
        plan = engine.scan()
        self.assertEqual(
            {item.title: item.state for item in plan},
            {"On my shelf": State.REMOTE_ONLY, "Linked earlier": State.REMOTE_CHANGED},
        )
        self.assertIn("1 not on your shelf", progress[-1])
        self.assertEqual(engine.apply(plan).failed, [])
        self.assertEqual(
            sorted(a.all_field_for("title", a.all_book_ids()).values()), ["Linked, renamed", "On my shelf"]
        )
        self.assertNotIn(("GET", "/api/v1/shelf-records"), self.fake.requests, "the entry list already says")

        # Shelf-only off again: the other entry is offered as usual.
        self.assertEqual(self.states(a)["Not on my shelf"], State.REMOTE_ONLY)

    def test_shelf_only_asks_the_shelf_when_entries_carry_no_shelf_ids(self) -> None:
        a = self.library("a")
        shelved = self.fake.add_remote_entry("On my shelf", {EPUB: epub("s")})
        self.fake.add_remote_entry("Not on my shelf", {EPUB: epub("n")})
        self.fake.add_shelf_record(shelved)
        # A server that does not send shelf_record_id in the entry list.
        iter_entries = self.client.iter_entries
        self.client.iter_entries = lambda catalog_id: (  # type: ignore[method-assign]
            replace(entry, shelf_record_id=None) for entry in iter_entries(catalog_id)
        )
        self.assertEqual([item.title for item in self.engine(a, shelf_only=True).scan()], ["On my shelf"])
        self.assertIn(("GET", "/api/v1/shelf-records"), self.fake.requests)

    def test_saving_highlights_to_a_column_keeps_books_in_sync(self) -> None:
        from calibre_plugin.sync.highlights import save_to_column

        setup = open_library(str(self.tmp / "a")).new_api
        setup.create_custom_column("highlights", "Highlights", "comments", False)
        setup.close()  # a new column appears only once the library is opened again
        a = self.library("a")
        in_sync = self.add_book(a, "Untouched", {"EPUB": epub("u")})
        edited = self.add_book(a, "Edited", {"EPUB": epub("e")})
        self.sync(a)
        a.set_field("title", {edited: "Edited locally"})

        save_to_column(a, "#highlights", {in_sync: "<p>one</p>", edited: "<p>two</p>"})
        self.assertEqual(a.field_for("#highlights", in_sync), "<p>one</p>")
        self.assertEqual(self.states(a), {"Untouched": State.IN_SYNC, "Edited locally": State.LOCAL_CHANGED})
        with self.assertRaises(ValueError):
            save_to_column(a, "#nope", {in_sync: "<p>x</p>"})

    def test_bad_token_fails_fast(self) -> None:
        from calibre_plugin.vendor.evilflowers_protocol.errors import Unauthorized

        client = CatalogClient(self.fake.base_url, "wrong", retries=0, user_agent="t")
        with self.assertRaises(Unauthorized):
            client.me()


def _uuid_of(db: Any, title: str) -> str:
    for book_id in db.all_book_ids():
        if db.field_for("title", book_id) == title:
            return db.field_for("uuid", book_id)
    raise KeyError(title)


if __name__ == "__main__":
    unittest.main()
