"""Highlights: rendering (pure), reading them for library books, and the dialog driven headless."""

from __future__ import annotations

import os
import shutil
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from typing import Any
from uuid import UUID

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

from calibre.ebooks.metadata.book.base import Metadata
from calibre.library import db as open_library
from epub import epub
from fake_catalog import TOKEN, FakeCatalog

from calibre_plugin.remote.client import CatalogClient
from calibre_plugin.remote.models import Highlight
from calibre_plugin.settings import Settings
from calibre_plugin.sync.highlights import (
    collect,
    render_books_html,
    render_html,
    render_text,
    text_column_name,
)

EPUB = "application/epub+zip"


def mark(page: int, text: str | None, title: str = "My highlights", created: str = "2026-01-01") -> Highlight:
    return Highlight(title, "highlight", page, text, "", created)


class RenderTests(unittest.TestCase):
    def test_every_piece_of_text_is_escaped(self) -> None:
        page = render_html([mark(3, "<script>alert(1)</script> & co", title="Notes <b>bold</b>")])
        self.assertNotIn("<script>", page)
        self.assertNotIn("<b>", page)
        self.assertIn("&lt;script&gt;alert(1)&lt;/script&gt; &amp; co", page)
        self.assertIn("<h3>Notes &lt;b&gt;bold&lt;/b&gt;</h3>", page)
        books = render_books_html([SimpleNamespace(title="A <i>book</i>", highlights=[mark(1, "x")])])
        self.assertIn("<h2>A &lt;i&gt;book&lt;/i&gt;</h2>", books)

    def test_groups_by_annotation_title_in_page_order(self) -> None:
        highlights = [
            mark(2, "first", title="Chapter notes"),
            mark(5, "second", title="Agent picks"),
            mark(9, "third", title="Chapter notes"),
        ]
        self.assertEqual(
            render_text(highlights),
            "Chapter notes\n  p. 2 — “first”\n  p. 9 — “third”\n\nAgent picks\n  p. 5 — “second”",
        )
        page = render_html(highlights)
        self.assertLess(page.index("Chapter notes"), page.index("Agent picks"))
        self.assertLess(page.index("first"), page.index("third"))
        self.assertEqual(page.count("<ul>"), 2)

    def test_untitled_group_whitespace_and_missing_text(self) -> None:
        text = render_text([mark(1, "  spread\n over   lines ", title=" "), mark(4, None, title="")])
        self.assertEqual(text, "Highlights\n  p. 1 — “spread over lines”\n  p. 4 — (no text)")
        self.assertEqual(render_html([]), "")
        self.assertEqual(render_text([]), "")


class LibraryTests(unittest.TestCase):
    """Reading highlights for library books, and the dialog, against the fake catalog."""

    def setUp(self) -> None:
        self.fake = FakeCatalog().start()
        self.addCleanup(self.fake.stop)
        self.tmp = Path(tempfile.mkdtemp(prefix="evilflowers-highlights-"))
        self.addCleanup(shutil.rmtree, self.tmp, True)
        self.client = CatalogClient(self.fake.base_url, TOKEN, retries=0, user_agent="evilflowers-calibre-tests")
        self.catalog = self.client.resolve_catalog(self.fake.catalog_url_name)
        setup = open_library(str(self.tmp / "lib")).new_api
        setup.create_custom_column("highlights", "Highlights", "comments", False)
        setup.create_custom_column("shelf", "Shelf", "text", False)
        setup.close()  # new columns appear once the library is opened again
        self.db = open_library(str(self.tmp / "lib")).new_api
        self.addCleanup(self.db.close)

        self.marked = self.fake.add_remote_entry("Marked", {EPUB: epub("m")})
        self.fake.add_highlight(self.marked, 12, "Later <passage>")
        self.fake.add_highlight(self.marked, 3, "Early passage")
        self.fake.add_highlight(self.marked, 5, "Someone else's", user_id=str(UUID(int=99)))
        self.plain = self.fake.add_remote_entry("Plain", {EPUB: epub("p")})
        self.marked_id = self.add_book("Marked", self.marked)
        self.plain_id = self.add_book("Plain", self.plain)
        self.local_id = self.add_book("Only here", None)

    def add_book(self, title: str, uuid: str | None) -> int:
        mi = Metadata(title, ["A B"])
        if uuid:
            mi.uuid = uuid
        [book_id], _ = self.db.add_books([(mi, {})], preserve_uuid=True, run_hooks=False)
        return book_id

    def settings(self, column: str = "#highlights") -> Settings:
        return Settings(self.fake.base_url, TOKEN, self.fake.catalog_url_name, highlights_column=column)

    def test_collect_links_by_uuid_and_reads_only_this_users_marks(self) -> None:
        user_id = self.client.me().id
        books = collect(self.db, self.client, self.catalog, [self.marked_id, self.plain_id, self.local_id], user_id)
        self.assertEqual(
            [(b.title, b.linked) for b in books], [("Marked", True), ("Plain", True), ("Only here", False)]
        )
        self.assertEqual([h.text for h in books[0].highlights], ["Early passage", "Later <passage>"])
        self.assertEqual(books[1].highlights, [])

    def test_text_column_name_accepts_only_long_text_columns(self) -> None:
        self.assertEqual(text_column_name(self.db, "#highlights"), "Highlights")
        self.assertIsNone(text_column_name(self.db, "#shelf"))  # a short text column
        self.assertIsNone(text_column_name(self.db, "#missing"))
        self.assertIsNone(text_column_name(self.db, "comments"))  # built-in, not a custom column

    # ------------------------------------------------------------------ dialog

    def dialog(self, book_ids: list[int], settings: Settings | None = None) -> Any:
        from test_gui import FakeGui, app, wait_for

        from calibre_plugin.highlights_dialog import HighlightsDialog

        app()
        gui = FakeGui(self.db)
        dialog = HighlightsDialog(gui, book_ids, settings=settings or self.settings())
        self.addCleanup(dialog.reject)
        dialog.show()  # showEvent starts loading
        wait_for(lambda: dialog.worker is None and bool(dialog.books))
        return dialog

    def test_dialog_shows_copies_and_saves_highlights(self) -> None:
        from qt.core import QApplication

        dialog = self.dialog([self.marked_id, self.local_id])
        shown = dialog.browser.toPlainText()
        self.assertIn("Marked", shown)
        self.assertIn("p. 3 — “Early passage”", shown)
        self.assertLess(shown.index("Early"), shown.index("Later <passage>"))
        self.assertNotIn("Someone else", shown)
        self.assertIn("2 highlights", dialog.status.text())
        self.assertIn("Not in the catalog: Only here", dialog.status.text())

        dialog.copy()
        self.assertIn("p. 12 — “Later <passage>”", QApplication.clipboard().text())

        self.assertEqual(dialog.save_button.text(), "Save to Highlights")
        self.assertTrue(dialog.save_button.isEnabled())
        dialog.save()
        saved = self.db.field_for("#highlights", self.marked_id)
        self.assertIn("Later &lt;passage&gt;", saved)
        self.assertIsNone(self.db.field_for("#highlights", self.local_id))
        self.assertEqual(dialog.gui.refreshed, [[self.marked_id]])

    def test_closing_while_loading_returns_at_once(self) -> None:
        import threading
        import time

        from test_gui import FakeGui, app, wait_for

        from calibre_plugin import highlights_dialog, worker

        app()
        release = threading.Event()
        self.addCleanup(release.set)
        original = highlights_dialog.load_highlights
        highlights_dialog.load_highlights = lambda *_args: release.wait(20) and []
        self.addCleanup(setattr, highlights_dialog, "load_highlights", original)
        dialog = highlights_dialog.HighlightsDialog(FakeGui(self.db), [self.marked_id], settings=self.settings())
        dialog.show()
        task = dialog.worker
        self.assertIsNotNone(task)
        self.assertIsNone(task.parent(), "not a child of a window that deletes itself on close")
        started = time.monotonic()
        dialog.reject()
        self.assertLess(time.monotonic() - started, 2)
        release.set()
        wait_for(lambda: task not in worker._running)

    def test_a_failed_save_is_reported_inline(self) -> None:
        dialog = self.dialog([self.marked_id])
        dialog.settings = self.settings(column="#shelf")  # not a long-text column: the write is refused
        dialog.save()
        self.assertIn("Could not save the highlights", dialog.status.text())
        from calibre_plugin.login import error_style

        self.assertEqual(dialog.status.styleSheet(), error_style(), "in the error colour, not a modal box")

    def test_dialog_empty_states_and_no_save_without_a_column(self) -> None:
        dialog = self.dialog([self.plain_id], self.settings(column=""))
        self.assertIn("No highlights for this book yet", dialog.browser.toPlainText())
        self.assertFalse(dialog.copy_button.isEnabled())
        self.assertIsNone(dialog.save_button)

        dialog = self.dialog([self.local_id])
        self.assertIn("isn't in the EvilFlowers catalog", dialog.browser.toPlainText())


if __name__ == "__main__":
    unittest.main()
