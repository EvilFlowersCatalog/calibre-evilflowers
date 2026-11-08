"""The settings widget and the sync dialog, driven headless (Qt offscreen) against the fake catalog."""

from __future__ import annotations

import os
import shutil
import tempfile
import unittest
import uuid
from pathlib import Path
from types import SimpleNamespace
from typing import Any

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

from calibre.ebooks.metadata.book.base import Metadata
from calibre.gui2 import Application
from calibre.library import db as open_library
from epub import epub
from fake_catalog import TOKEN, FakeCatalog
from qt.core import QBuffer, QColor, QEventLoop, QIcon, QImage, QIODevice, Qt, QTimer, QWidget

from calibre_plugin.settings import prefs
from calibre_plugin.sync.plan import Action, State

_app = None


def app() -> Any:
    global _app
    if _app is None:
        # Another test module may have created the application already; Qt allows only one.
        _app = Application.instance() or Application([])
    return _app


def wait_for(predicate: Any, timeout_ms: int = 20_000) -> None:
    loop = QEventLoop()
    deadline = QTimer()
    deadline.setSingleShot(True)
    deadline.timeout.connect(loop.quit)
    poll = QTimer()
    poll.timeout.connect(lambda: predicate() and loop.quit())
    deadline.start(timeout_ms)
    poll.start(20)
    loop.exec()
    poll.stop()
    if not predicate():
        raise AssertionError("timed out waiting for the dialog")


def png(color: str = "red") -> bytes:
    image = QImage(60, 90, QImage.Format.Format_RGB32)
    image.fill(QColor(color))
    buffer = QBuffer()
    buffer.open(QIODevice.OpenModeFlag.WriteOnly)
    image.save(buffer, "PNG")
    return bytes(buffer.data())


class FakeGui(QWidget):
    """Just the parts of Calibre's main window the dialog touches."""

    def __init__(self, db: Any) -> None:
        super().__init__()
        self.view_ids: list[int] = []
        self.current_db = SimpleNamespace(new_api=db, data=SimpleNamespace(books_added=self.view_ids.extend))
        self.refreshed: list[Any] = []
        self.added: list[int] = []
        self.iactions = {"Add Books": SimpleNamespace(refresh_gui=lambda n, recount=True: self.added.append(n))}
        self.library_view = SimpleNamespace(model=lambda: SimpleNamespace(refresh_ids=self.refreshed.append))
        self.tags_view = SimpleNamespace(recount=lambda: None)


class GuiTests(unittest.TestCase):
    def setUp(self) -> None:
        app()
        self.fake = FakeCatalog().start()
        self.addCleanup(self.fake.stop)
        self.tmp = Path(tempfile.mkdtemp(prefix="evilflowers-gui-"))
        self.addCleanup(shutil.rmtree, self.tmp, True)
        saved = {key: prefs[key] for key in ("base_url", "token", "catalog", "direction", "show_in_sync")}
        self.addCleanup(lambda: [prefs.__setitem__(k, v) for k, v in saved.items()])
        prefs["base_url"], prefs["token"], prefs["catalog"] = self.fake.base_url, TOKEN, self.fake.catalog_url_name
        prefs["direction"] = "both"
        prefs["show_in_sync"] = False
        self.db = open_library(str(self.tmp / "lib")).new_api
        self.addCleanup(self.db.close)

    def add_book(self, title: str) -> int:
        path = self.tmp / f"{title}.epub"
        path.write_bytes(epub(title))
        [book_id], _ = self.db.add_books([(Metadata(title, ["A B"]), {"EPUB": str(path)})], run_hooks=False)
        return book_id

    def open_dialog(self) -> Any:
        """A shown sync window whose first scan has finished."""
        from calibre_plugin.dialog import SyncDialog

        dialog = SyncDialog(FakeGui(self.db), QIcon(), SimpleNamespace(show_settings=lambda parent=None: None))
        self.addCleanup(dialog.reject)
        dialog.show()
        wait_for(lambda: dialog.worker is None and dialog.phase != "loading")
        return dialog

    def sync(self, dialog: Any) -> None:
        dialog.synchronize()
        wait_for(lambda: dialog.worker is None and dialog.model.results)

    def rescan(self, dialog: Any) -> None:
        dialog.scan()
        wait_for(lambda: dialog.worker is None and dialog.phase != "loading")

    def row_of(self, dialog: Any, title: str) -> int:
        return next(i for i, item in enumerate(dialog.model.items) if item.title == title)

    def test_config_widget_round_trip_and_connection_test(self) -> None:
        from calibre_plugin.config import ConfigWidget

        widget = ConfigWidget()
        self.assertEqual(widget.current().catalog, self.fake.catalog_url_name)
        widget.test_connection()  # off the GUI thread: the button waits, the page does not
        self.assertFalse(widget.test_button.isEnabled())
        wait_for(lambda: widget.test_button.isEnabled())
        self.assertIn("Signed in as tester", widget.status.text())
        me = [path for _method, path in self.fake.requests if path.endswith("/users/me")]
        self.assertEqual(len(me), 1, "one request for who is signed in, not two")
        widget.token.setText("wrong")
        widget.test_connection()
        wait_for(lambda: widget.test_button.isEnabled())
        self.assertIn("rejected the API key", widget.status.text())
        widget.token.setText(TOKEN)
        widget.catalog.setText("other-catalog")
        widget.save_settings()
        self.assertEqual(prefs["catalog"], "other-catalog")

    def test_dialog_scans_lets_the_user_choose_and_syncs(self) -> None:
        from calibre_plugin.dialog import SyncDialog

        self.add_book("Local one")
        self.fake.add_remote_entry("Remote one", {"application/epub+zip": epub("Remote one")})
        gui = FakeGui(self.db)
        action = SimpleNamespace(show_settings=lambda parent=None: None)
        dialog = SyncDialog(gui, QIcon(), action)
        dialog.show()  # showEvent starts the scan
        wait_for(lambda: dialog.worker is None and dialog.model.items)

        states = {item.title: (item.state, item.action) for item in dialog.model.items}
        self.assertEqual(
            states, {"Local one": (State.LOCAL_ONLY, Action.PUSH), "Remote one": (State.REMOTE_ONLY, Action.PULL)}
        )
        self.assertEqual(dialog.summary.text(), "1 to upload, 1 to download")
        self.assertEqual(dialog.chips["upload"].text(), "1 to upload")
        self.assertEqual(dialog.chips["download"].text(), "1 to download")
        self.assertIn("signed in as Ada Lovelace", dialog.header.subtitle())
        self.assertIs(dialog.stack.currentWidget(), dialog.plan_page)
        self.assertTrue(dialog.sync_button.isEnabled())

        # The user skips the download.
        row = next(i for i, item in enumerate(dialog.model.items) if item.title == "Remote one")
        dialog.model.set_action(row, Action.SKIP)
        self.assertEqual(dialog.summary.text(), "1 to upload, 0 to download")

        dialog.synchronize()
        wait_for(lambda: dialog.worker is None and dialog.model.results)
        self.assertEqual([r for r in dialog.model.results.values()], [(True, "done")])
        self.assertEqual(len(self.fake.entries), 2)
        self.assertEqual(len(self.db.all_book_ids()), 1, "the skipped entry must not be downloaded")
        self.assertEqual(gui.added, [])

        # Rescan: the upload is now in sync and hidden; the skipped entry is still offered.
        dialog.scan()
        wait_for(lambda: dialog.worker is None and not dialog.model.results)
        self.assertEqual(dialog.proxy.rowCount(), 1)
        dialog.chips["all"].click()
        self.assertEqual(dialog.proxy.rowCount(), 2)
        self.assertTrue(prefs["show_in_sync"], "All becomes the default for next time")
        dialog.chips["in_sync"].click()
        self.assertEqual([dialog.proxy.index(0, 1).data()], ["Local one"])
        dialog.chips["pending"].click()
        self.assertFalse(prefs["show_in_sync"])

        dialog.synchronize()
        wait_for(lambda: dialog.worker is None and dialog.model.results)
        self.assertEqual(gui.added, [1], "a downloaded book is announced to the library view")
        self.assertEqual(len(gui.view_ids), 1, "and registered with the view before the rows are inserted")
        self.assertIn(gui.view_ids[0], self.db.all_book_ids())
        self.assertIn("1 downloaded", dialog.progress_label.text())

        # Everything is in sync now: the window says so instead of showing an empty table.
        self.rescan(dialog)
        self.assertIs(dialog.stack.currentWidget(), dialog.message_page)
        self.assertIn("in sync", dialog.message_page.title.text())
        self.assertEqual(dialog.chips["in_sync"].text(), "2 in sync")
        dialog.message_page.button.click()  # "Show all books"
        self.assertIs(dialog.stack.currentWidget(), dialog.plan_page)
        self.assertEqual(dialog.proxy.rowCount(), 2)
        dialog.reject()

    def test_settings_menu_ignores_the_triggered_checked_flag(self) -> None:
        from calibre_plugin.ui import EvilFlowersAction

        parents: list[Any] = []
        gui = FakeGui(self.db)
        action = SimpleNamespace(gui=gui, interface_action_base_plugin=SimpleNamespace(do_user_config=parents.append))
        EvilFlowersAction.show_settings(action, False)  # what QAction.triggered passes
        EvilFlowersAction.show_settings(action)
        dialog_parent = QWidget()
        EvilFlowersAction.show_settings(action, dialog_parent)
        self.assertEqual(parents, [gui, gui, dialog_parent])

    def test_menu_hooks_reach_every_open_window(self) -> None:
        from calibre_plugin.ui import EvilFlowersAction

        calls: list[str] = []

        def window(name: str, visible: bool = True) -> SimpleNamespace:
            return SimpleNamespace(
                isVisible=lambda: visible,
                reject=lambda: calls.append(f"{name}.reject"),
                settings_changed=lambda: calls.append(f"{name}.settings_changed"),
            )

        action = SimpleNamespace(
            dialog=window("sync"), browse_dialog=window("browse", visible=False), highlights_dialog=window("highlights")
        )
        action.windows = lambda: EvilFlowersAction.windows(action)
        self.assertEqual(len(action.windows()), 2)
        EvilFlowersAction.apply_settings(action)
        EvilFlowersAction.close_windows(action)
        self.assertEqual(calls, ["sync.settings_changed", "sync.reject", "highlights.reject"])

    def test_first_run_shows_a_set_up_page_without_a_request(self) -> None:
        from calibre_plugin.dialog import SyncDialog

        prefs["token"] = ""
        opened: list[Any] = []
        dialog = SyncDialog(FakeGui(self.db), QIcon(), SimpleNamespace(show_settings=opened.append))
        dialog.show()
        self.assertIsNone(dialog.worker)
        self.assertEqual(dialog.phase, "setup")
        self.assertIs(dialog.stack.currentWidget(), dialog.message_page)
        self.assertEqual(dialog.message_page.button.text(), "Set up…")
        self.assertTrue(dialog.chip_bar.isHidden())
        self.assertTrue(dialog.banner.isHidden(), "a missing setting is not an error")
        self.assertFalse(dialog.sync_button.isEnabled())
        dialog.message_page.button.click()
        self.assertEqual(opened, [dialog])
        self.assertEqual(self.fake.requests, [])
        dialog.reject()

    def test_errors_show_inline_with_retry(self) -> None:
        prefs["token"] = "wrong"
        dialog = self.open_dialog()
        self.assertEqual(dialog.phase, "failed")
        self.assertFalse(dialog.banner.isHidden())
        self.assertIn("rejected the API key", dialog.banner.label.text())
        self.assertIs(dialog.stack.currentWidget(), dialog.message_page)
        self.assertEqual(dialog.message_page.button.text(), "Retry")
        self.assertTrue(dialog.banner.retry_button.isHidden(), "one Retry, on the page, not a second on the banner")
        self.assertIn(f"{self.fake.catalog_url_name} on 127.0.0.1", dialog.header.subtitle())

        prefs["token"] = TOKEN
        self.add_book("Local one")
        dialog.message_page.button.click()
        wait_for(lambda: dialog.worker is None and dialog.model.items)
        self.assertTrue(dialog.banner.isHidden())
        self.assertIs(dialog.stack.currentWidget(), dialog.plan_page)

    def test_chips_filter_rows_and_keep_rows_whose_action_changes(self) -> None:
        self.add_book("Local one")
        self.add_book("Local two")
        self.fake.add_remote_entry("Remote one", {"application/epub+zip": epub("Remote one")})
        dialog = self.open_dialog()
        self.assertEqual(dialog.chips["pending"].text(), "3 to review")
        self.assertFalse(dialog.chips["decision"].isEnabled(), "an empty group cannot be picked")

        dialog.chips["upload"].click()
        self.assertEqual(dialog.proxy.rowCount(), 2)
        dialog.model.set_action(self.row_of(dialog, "Local one"), Action.SKIP)
        self.assertEqual(dialog.chips["upload"].text(), "1 to upload")
        self.assertEqual(dialog.proxy.rowCount(), 2, "the row stays put until another group is picked")
        dialog.chips["download"].click()
        self.assertEqual([dialog.proxy.index(0, 1).data()], ["Remote one"])

    def test_conflict_panel_compares_both_sides_and_decides(self) -> None:
        book_id = self.add_book("Shared")
        dialog = self.open_dialog()
        self.sync(dialog)
        entry_id = self.db.field_for("uuid", book_id)
        self.fake.edit_entry(entry_id, title="Shared (catalog)", publisher="Remote Press")
        self.db.set_field("title", {book_id: "Shared (Calibre)"})
        self.rescan(dialog)

        [item] = dialog.model.items
        self.assertIs(item.state, State.CONFLICT)
        self.assertEqual(dialog.chips["decision"].text(), "1 needs a decision")
        self.assertTrue(dialog.details.isHidden(), "nothing is selected yet")
        dialog.table.selectRow(0)
        self.assertFalse(dialog.details.isHidden())
        title = next(diff for diff in dialog.details.diffs if diff.label == "Title")
        self.assertEqual((title.calibre, title.catalog), ("Shared (Calibre)", "Shared (catalog)"))
        self.assertNotIn("Authors", [diff.label for diff in dialog.details.diffs], "only differing fields")

        # Publisher and date come from the entry's detail, fetched in the background.
        wait_for(lambda: any(diff.label == "Publisher" for diff in dialog.details.diffs))
        publisher = next(diff for diff in dialog.details.diffs if diff.label == "Publisher")
        self.assertEqual((publisher.calibre, publisher.catalog), ("", "Remote Press"))

        dialog.details.buttons[Action.PULL].click()
        self.assertIs(dialog.model.items[0].action, Action.PULL)
        dialog.details.buttons[Action.PUSH].click()
        self.assertIs(dialog.model.items[0].action, Action.PUSH)
        dialog.model.set_action(0, Action.SKIP)  # e.g. from the Action column
        self.assertTrue(dialog.details.buttons[Action.SKIP].isChecked())

        dialog.table.clearSelection()
        self.assertTrue(dialog.details.isHidden())

    def test_closing_during_a_detail_lookup_does_not_wait_or_crash(self) -> None:
        import threading
        import time

        from calibre_plugin import dialog as dialog_module
        from calibre_plugin import worker

        book_id = self.add_book("Shared")
        dialog = self.open_dialog()
        self.sync(dialog)
        self.db.set_field("title", {book_id: "Shared (Calibre)"})
        self.fake.edit_entry(self.db.field_for("uuid", book_id), title="Shared (catalog)")
        self.rescan(dialog)

        release = threading.Event()
        self.addCleanup(release.set)

        class SlowClient:
            def get_entry(self, *_args: Any) -> Any:
                release.wait(20)
                raise RuntimeError("server went away")

        original = dialog_module.client_for
        dialog_module.client_for = lambda settings: SlowClient()
        self.addCleanup(setattr, dialog_module, "client_for", original)
        dialog.table.selectRow(0)  # starts the detail lookup, which hangs
        [task] = dialog._detail_tasks.values()
        self.assertIsNone(task.parent(), "a window's thread must not be its child")

        started = time.monotonic()
        dialog.reject()
        self.assertLess(time.monotonic() - started, 2, "closing never waits for a lookup")
        self.assertIn(task, worker._running, "the lookup lives on until it ends")
        release.set()
        wait_for(lambda: task not in worker._running)
        app().processEvents()
        self.assertEqual(dialog._details, {}, "its answer is dropped: the window had closed")

    def test_the_book_column_keeps_its_room_with_the_details_open(self) -> None:
        from calibre_plugin.dialog import BOOK_COLUMN, RESULT_COLUMN

        book_id = self.add_book("A book with a title long enough to need the room")
        dialog = self.open_dialog()
        self.assertTrue(dialog.table.isColumnHidden(RESULT_COLUMN), "no results yet: no Result column")
        self.sync(dialog)
        self.assertFalse(dialog.table.isColumnHidden(RESULT_COLUMN), "results are shown once there are some")
        self.db.set_field("title", {book_id: "Edited here"})
        self.fake.edit_entry(self.db.field_for("uuid", book_id), title="Edited there")
        self.rescan(dialog)
        dialog.resize(900, 650)
        dialog.table.selectRow(0)
        app().processEvents()
        self.assertFalse(dialog.details.isHidden())
        self.assertGreater(dialog.table.columnWidth(BOOK_COLUMN), 150)

    def test_in_sync_page_hides_empty_groups_and_the_summary(self) -> None:
        self.add_book("Local one")
        dialog = self.open_dialog()
        self.sync(dialog)
        self.rescan(dialog)
        self.assertIs(dialog.stack.currentWidget(), dialog.message_page)
        self.assertEqual(dialog.summary.text(), "", "nothing to do, so nothing to sum up")
        for key in ("upload", "download", "decision"):
            self.assertTrue(dialog.chips[key].isHidden(), key)
        self.assertFalse(dialog.chips["in_sync"].isHidden())

    def test_covers_load_lazily_for_local_and_remote_rows(self) -> None:
        from calibre_plugin.dialog import COVER_COLUMN

        book_id = self.add_book("Local one")
        self.db.set_cover({book_id: png("red")})
        with_cover = self.fake.add_remote_entry("Remote one", {}, cover=png("blue"))
        self.fake.add_remote_entry("Remote bare", {})
        dialog = self.open_dialog()
        for row in range(len(dialog.model.items)):
            dialog.model.data(dialog.model.index(row, COVER_COLUMN), Qt.ItemDataRole.DecorationRole)

        keys = [("local", book_id), ("remote", with_cover)]
        wait_for(lambda: all(key in dialog.model.covers for key in keys))
        for key in keys:
            self.assertFalse(dialog.model.covers[key].isNull(), key)
        self.assertEqual(len(dialog.model.covers), 2, "an entry without a cover is never requested")
        thumbnails = [path for method, path in self.fake.requests if "/thumbnails/" in path]
        self.assertEqual(thumbnails, [f"/data/v1/thumbnails/{with_cover}"])

    def test_action_combo_changes_the_row_and_closes(self) -> None:
        from qt.core import QComboBox

        from calibre_plugin.dialog import ACTION_COLUMN

        self.add_book("Local one")
        dialog = self.open_dialog()
        index = dialog.proxy.index(0, ACTION_COLUMN)
        dialog.table.edit(index)
        editor = dialog.table.findChild(QComboBox)
        self.assertIsNotNone(editor)
        self.assertEqual([editor.itemData(i) for i in range(editor.count())], [Action.PUSH, Action.SKIP])
        editor.setCurrentIndex(1)
        editor.activated.emit(1)
        self.assertIs(dialog.model.items[0].action, Action.SKIP)
        wait_for(lambda: not [w for w in dialog.table.findChildren(QComboBox) if w.isVisible()])
        self.assertEqual(dialog.summary.text(), "0 to upload, 0 to download")

    def test_compare_lists_only_the_fields_that_differ(self) -> None:
        from calibre_plugin.dialog import compare
        from calibre_plugin.remote.models import AcquisitionRef, EntryRef, Person
        from calibre_plugin.sync.local import LocalBook

        mi = Metadata("Same title", ["Ada Lovelace"])
        mi.tags = ["Maths", "history"]
        mi.languages = ["eng"]
        book = LocalBook(1, "u", "Same title", ("Ada Lovelace",), "", ("EPUB", "TXT"))
        entry = EntryRef(
            id=uuid.uuid4(),
            title="Same title",
            updated_at="",
            language_code="en",
            authors=(Person("Ada", "Lovelace"),),
            categories=("History", "maths"),
            acquisitions=(AcquisitionRef(uuid.uuid4(), "acquisition", "application/pdf"),),
            publisher="Press",
        )
        diffs = compare(mi, book, entry, detailed=False)
        self.assertEqual([(d.label, d.calibre, d.catalog) for d in diffs], [("Formats", "EPUB", "PDF")])
        detailed = compare(mi, book, entry, detailed=True)
        self.assertEqual([d.label for d in detailed], ["Publisher", "Formats"])


if __name__ == "__main__":
    unittest.main()
