"""The catalog browser, driven headless (Qt offscreen) against the fake catalog.

Runs in the suite once "test_browse" is listed in tests/run.py, or alone with
`scripts/dev.sh debug tests/test_browse.py`.
"""

from __future__ import annotations

import os
import shutil
import sys
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from typing import Any

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
sys.path.insert(0, str(Path(__file__).resolve().parent))
sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from calibre.ebooks.metadata.book.base import Metadata
from calibre.library import db as open_library
from epub import epub
from fake_catalog import TOKEN, FakeCatalog
from qt.core import QBuffer, QColor, QIcon, QImage, QIODevice, QItemSelectionModel, Qt
from test_gui import FakeGui, app, wait_for

from calibre_plugin.settings import prefs

EPUB = "application/epub+zip"
PHYSICS = {"term": "physics", "label": "Physics"}
POETRY = {"term": "poetry", "label": "Poetry"}


def jpeg() -> bytes:
    """A small real JPEG, so the cover decodes like a server's would."""
    image = QImage(30, 40, QImage.Format.Format_RGB32)
    image.fill(QColor(200, 80, 40))
    buffer = QBuffer()
    buffer.open(QIODevice.OpenModeFlag.WriteOnly)
    image.save(buffer, "JPEG")
    return bytes(buffer.data())


class BrowseTests(unittest.TestCase):
    def setUp(self) -> None:
        app()
        self.fake = FakeCatalog().start()
        self.addCleanup(self.fake.stop)
        self.tmp = Path(tempfile.mkdtemp(prefix="evilflowers-browse-"))
        self.addCleanup(shutil.rmtree, self.tmp, True)
        saved = {key: prefs[key] for key in ("base_url", "token", "catalog", "direction")}
        self.addCleanup(lambda: [prefs.__setitem__(k, v) for k, v in saved.items()])
        prefs["base_url"], prefs["token"], prefs["catalog"] = self.fake.base_url, TOKEN, self.fake.catalog_url_name
        prefs["direction"] = "both"
        self.db = open_library(str(self.tmp / "lib")).new_api
        self.addCleanup(self.db.close)

        self.alpha = self.fake.add_remote_entry(
            "Alpha physics", {EPUB: epub("Alpha physics")}, categories=[PHYSICS], cover=jpeg()
        )
        self.beta = self.fake.add_remote_entry("Beta physics", {EPUB: epub("Beta physics")}, categories=[PHYSICS])
        self.gamma = self.fake.add_remote_entry(
            "Gamma verses", {EPUB: epub("Gamma verses")}, categories=[POETRY], language_code="sk"
        )

    def open_dialog(self) -> Any:
        from calibre_plugin.browse import BrowseDialog

        self.gui = FakeGui(self.db)
        dialog = BrowseDialog(self.gui, QIcon(), SimpleNamespace(show_settings=lambda parent=None: None))
        self.addCleanup(dialog.reject)
        dialog.show()  # showEvent connects and runs the first search
        wait_for(lambda: not dialog.busy and dialog.page == 1)
        return dialog

    def titles(self, dialog: Any) -> list[str]:
        return [entry.title for entry in dialog.model.entries]

    def select(self, dialog: Any, *titles: str) -> None:
        selection = dialog.table.selectionModel()
        selection.clearSelection()
        flags = QItemSelectionModel.SelectionFlag.Select | QItemSelectionModel.SelectionFlag.Rows
        for row, entry in enumerate(dialog.model.entries):
            if entry.title in titles:
                selection.select(dialog.model.index(row, 0), flags)

    def search(self, dialog: Any, text: str) -> None:
        generation = dialog._generation
        dialog.search.setText(text)
        dialog.search.returnPressed.emit()
        wait_for(lambda: dialog._generation > generation and not dialog.busy)

    def test_browse_searches_pages_and_filters(self) -> None:
        dialog = self.open_dialog()
        self.assertEqual(dialog.header.title(), "Browse Calibre test catalog")
        # The fake serves two per page: the first page, then "Load more" for the rest.
        self.assertEqual(self.titles(dialog), ["Alpha physics", "Beta physics"])
        self.assertEqual((dialog.pages, dialog.total), (2, 3))
        self.assertTrue(dialog.more_button.isVisibleTo(dialog))
        self.assertEqual(dialog.count.text(), "Showing 2 of 3")
        dialog.load_more()
        wait_for(lambda: not dialog.busy and dialog.page == 2)
        self.assertEqual(self.titles(dialog), ["Alpha physics", "Beta physics", "Gamma verses"])
        self.assertFalse(dialog.more_button.isVisibleTo(dialog))

        # Facet counts name the categories and languages.
        categories = [dialog.category.itemText(i) for i in range(dialog.category.count())]
        self.assertEqual(categories, ["All categories", "Physics (2)", "Poetry (1)"])
        languages = [dialog.language.itemText(i) for i in range(dialog.language.count())]
        self.assertEqual(languages, ["All languages", "English (2)", "Slovak (1)"])

        # Picking a category searches again, and the choice survives the refill.
        generation = dialog._generation
        dialog.category.setCurrentIndex(2)
        wait_for(lambda: dialog._generation > generation and not dialog.busy)
        self.assertEqual(self.titles(dialog), ["Gamma verses"])
        self.assertEqual(dialog.category.currentText(), "Poetry (1)")
        dialog.category.setCurrentIndex(0)
        wait_for(lambda: not dialog.busy and len(dialog.model.entries) == 2)

        self.search(dialog, "physics")
        self.assertEqual(sorted(self.titles(dialog)), ["Alpha physics", "Beta physics"])
        self.assertIs(dialog.stack.currentWidget(), dialog.table)

        self.search(dialog, "nothing like this")
        self.assertEqual(dialog.model.entries, [])
        self.assertIs(dialog.stack.currentWidget(), dialog.empty)
        self.assertIn("No books match “nothing like this”", dialog.empty.text())

    def test_browse_search_is_debounced(self) -> None:
        dialog = self.open_dialog()
        before = sum(1 for method, path in self.fake.requests if path == "/api/v1/entries")
        for text in ("g", "ga", "gam", "gamma"):
            dialog.search.setText(text)
        wait_for(lambda: dialog.search.text() == "gamma" and not dialog.busy and len(dialog.model.entries) == 1)
        after = sum(1 for method, path in self.fake.requests if path == "/api/v1/entries")
        self.assertEqual(after - before, 1, "typing quickly sends a single search")
        self.assertEqual(self.titles(dialog), ["Gamma verses"])

    def test_browse_loads_covers_with_a_placeholder_for_none(self) -> None:
        dialog = self.open_dialog()
        wait_for(lambda: self.alpha in dialog.model.covers)
        self.assertNotIn(self.beta, dialog.model.covers, "no cover: the placeholder stays")
        thumbnail_requests = [path for _method, path in self.fake.requests if path.startswith("/data/v1/thumbnails/")]
        self.assertEqual(thumbnail_requests, [f"/data/v1/thumbnails/{self.alpha}"])

    def test_browse_toggles_the_shelf(self) -> None:
        dialog = self.open_dialog()
        self.select(dialog, "Alpha physics")
        self.assertTrue(dialog.shelve_button.isEnabled())
        self.assertFalse(dialog.unshelve_button.isEnabled())
        dialog.add_to_shelf()
        wait_for(lambda: not dialog.busy and dialog.model.entries[0].shelf_record_id is not None)
        self.assertEqual(list(self.fake.shelf.values()), [self.alpha])
        self.assertEqual(dialog.model.data(dialog.model.index(0, 3)), "On your shelf")
        self.assertTrue(dialog.unshelve_button.isEnabled())

        dialog.remove_from_shelf()
        wait_for(lambda: not dialog.busy and dialog.model.entries[0].shelf_record_id is None)
        self.assertEqual(self.fake.shelf, {})
        self.assertEqual(dialog.model.data(dialog.model.index(0, 3)), "")

    def test_browse_downloads_into_the_library(self) -> None:
        dialog = self.open_dialog()
        self.select(dialog, "Beta physics")
        self.assertTrue(dialog.download_button.isEnabled())
        dialog.download()
        wait_for(lambda: not dialog.busy and dialog.model.in_library(dialog.model.entries[1]))

        [book_id] = self.db.all_book_ids()
        self.assertEqual(self.db.field_for("uuid", book_id), self.beta)
        self.assertEqual(self.db.field_for("title", book_id), "Beta physics")
        self.assertIn("EPUB", self.db.formats(book_id))
        self.assertEqual(self.gui.added, [1], "the new book is announced to the library view")
        self.assertEqual(self.gui.view_ids, [book_id])
        self.assertEqual(dialog.model.data(dialog.model.index(1, 3)), "In your library")
        self.assertIn("Downloaded 1 book", dialog.status.text())
        self.assertFalse(dialog.download_button.isEnabled(), "a book already here cannot be downloaded again")

        # The download wrote a baseline: a sync now sees the book as in sync.
        from calibre_plugin.remote.client import CatalogClient
        from calibre_plugin.settings import Settings
        from calibre_plugin.sync.engine import SyncEngine
        from calibre_plugin.sync.plan import State

        client = CatalogClient(self.fake.base_url, TOKEN, user_agent="test")
        plan = SyncEngine(self.db, client, client.resolve_catalog(self.fake.catalog_url_name), Settings.load()).scan()
        states = {item.title: item.state for item in plan}
        self.assertEqual(states["Beta physics"], State.IN_SYNC)

    def test_browse_marks_books_already_in_the_library(self) -> None:
        path = self.tmp / "gamma.epub"
        path.write_bytes(epub("Gamma verses"))
        mi = Metadata("Gamma verses", ["Someone"])
        mi.uuid = self.gamma
        self.db.add_books([(mi, {"EPUB": str(path)})], preserve_uuid=True, run_hooks=False)
        dialog = self.open_dialog()
        self.search(dialog, "gamma")
        self.assertEqual(dialog.model.data(dialog.model.index(0, 3)), "In your library")
        self.select(dialog, "Gamma verses")
        self.assertFalse(dialog.download_button.isEnabled())

    def test_download_skips_a_book_added_after_the_window_looked(self) -> None:
        dialog = self.open_dialog()
        self.search(dialog, "gamma")
        self.select(dialog, "Gamma verses")
        self.assertTrue(dialog.download_button.isEnabled(), "not here when the window looked")
        # Meanwhile a sync (or the user) adds the same book.
        path = self.tmp / "gamma.epub"
        path.write_bytes(epub("Gamma verses"))
        mi = Metadata("Gamma verses", ["Someone"])
        mi.uuid = self.gamma
        self.db.add_books([(mi, {"EPUB": str(path)})], preserve_uuid=True, run_hooks=False)
        dialog.download()
        wait_for(lambda: not dialog.busy)
        self.assertEqual(len(self.db.all_book_ids()), 1, "never a second book for the same entry")
        self.assertIn("already in your library", dialog.status.text())
        self.assertEqual(self.gui.added, [])

    def test_upload_only_never_downloads(self) -> None:
        prefs["direction"] = "push"
        dialog = self.open_dialog()
        self.select(dialog, "Beta physics")
        self.assertFalse(dialog.download_button.isEnabled())
        self.assertIn("Upload only", dialog.download_button.toolTip())
        dialog.download()
        self.assertFalse(dialog.busy)
        self.assertEqual(self.db.all_book_ids(), set())

    def test_closing_never_waits_and_drops_late_answers(self) -> None:
        import threading
        import time

        from calibre_plugin import worker

        dialog = self.open_dialog()
        release = threading.Event()
        self.addCleanup(release.set)
        answers: list[Any] = []
        task = dialog.run_task(lambda: release.wait(20) and "late", answers.append)
        self.assertIsNone(task.parent())

        started = time.monotonic()
        dialog.reject()
        self.assertLess(time.monotonic() - started, 2, "closing does not block Calibre")
        self.assertTrue(dialog._abort.is_set(), "a download in flight stops after its current book")
        self.assertIn(task, worker._running, "the request finishes on its own")
        release.set()
        wait_for(lambda: task not in worker._running)
        app().processEvents()
        self.assertEqual(answers, [], "its answer reaches nobody")

    def test_header_names_the_host_not_the_api_address(self) -> None:
        dialog = self.open_dialog()
        self.assertNotIn("/api/v1", dialog.header.subtitle())
        self.assertIn("127.0.0.1", dialog.header.subtitle())
        self.assertEqual(dialog.model.headerData(3, Qt.Orientation.Horizontal), "Status")
        self.assertTrue(dialog.settings_button.toolTip())

    def test_browse_without_settings_asks_for_them(self) -> None:
        from calibre_plugin.browse import BrowseDialog

        prefs["token"] = ""
        dialog = BrowseDialog(FakeGui(self.db), QIcon(), SimpleNamespace(show_settings=lambda parent=None: None))
        self.addCleanup(dialog.reject)
        dialog.show()
        self.assertIn("Settings", dialog.status.text())
        self.assertEqual(self.fake.requests, [])
        self.assertFalse(dialog.download_button.isEnabled())

    def test_browse_shows_a_server_error_inline(self) -> None:
        prefs["token"] = "wrong"
        from calibre_plugin.browse import BrowseDialog

        dialog = BrowseDialog(FakeGui(self.db), QIcon(), SimpleNamespace(show_settings=lambda parent=None: None))
        self.addCleanup(dialog.reject)
        dialog.show()
        wait_for(lambda: not dialog.busy)
        self.assertIn("rejected the API key", dialog.status.text())


if __name__ == "__main__":
    # calibre-debug -e does not register this script as sys.modules["__main__"].
    suite = unittest.TestLoader().loadTestsFromTestCase(BrowseTests)
    raise SystemExit(0 if unittest.TextTestRunner(verbosity=2).run(suite).wasSuccessful() else 1)
