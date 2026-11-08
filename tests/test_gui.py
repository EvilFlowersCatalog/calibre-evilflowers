"""The settings widget and the sync dialog, driven headless (Qt offscreen) against the fake catalog."""

from __future__ import annotations

import os
import shutil
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from typing import Any

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

from calibre.ebooks.metadata.book.base import Metadata
from calibre.gui2 import Application
from calibre.library import db as open_library
from epub import epub
from fake_catalog import TOKEN, FakeCatalog
from qt.core import QEventLoop, QIcon, QTimer, QWidget

from calibre_plugin.settings import prefs
from calibre_plugin.sync.plan import Action, State

_app = None


def app() -> Any:
    global _app
    if _app is None:
        _app = Application([])
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
        saved = {key: prefs[key] for key in ("base_url", "token", "catalog", "direction")}
        self.addCleanup(lambda: [prefs.__setitem__(k, v) for k, v in saved.items()])
        prefs["base_url"], prefs["token"], prefs["catalog"] = self.fake.base_url, TOKEN, self.fake.catalog_url_name
        prefs["direction"] = "both"
        self.db = open_library(str(self.tmp / "lib")).new_api
        self.addCleanup(self.db.close)

    def add_book(self, title: str) -> int:
        path = self.tmp / f"{title}.epub"
        path.write_bytes(epub(title))
        [book_id], _ = self.db.add_books([(Metadata(title, ["A B"]), {"EPUB": str(path)})], run_hooks=False)
        return book_id

    def test_config_widget_round_trip_and_connection_test(self) -> None:
        from calibre_plugin.config import ConfigWidget

        widget = ConfigWidget()
        self.assertEqual(widget.current().catalog, self.fake.catalog_url_name)
        widget.test_connection()
        self.assertIn("Signed in as tester", widget.status.text())
        widget.token.setText("wrong")
        widget.test_connection()
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
        dialog.show_in_sync.setChecked(True)
        self.assertEqual(dialog.proxy.rowCount(), 2)
        dialog.show_in_sync.setChecked(False)

        dialog.synchronize()
        wait_for(lambda: dialog.worker is None and dialog.model.results)
        self.assertEqual(gui.added, [1], "a downloaded book is announced to the library view")
        self.assertEqual(len(gui.view_ids), 1, "and registered with the view before the rows are inserted")
        self.assertIn(gui.view_ids[0], self.db.all_book_ids())
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

    def test_missing_settings_are_reported_without_a_request(self) -> None:
        from calibre_plugin.dialog import SyncDialog

        prefs["token"] = ""
        dialog = SyncDialog(FakeGui(self.db), QIcon(), SimpleNamespace(show_settings=lambda parent=None: None))
        dialog.show()
        self.assertIsNone(dialog.worker)
        self.assertIn("token", dialog.progress.format())
        self.assertEqual(self.fake.requests, [])
        dialog.reject()


if __name__ == "__main__":
    unittest.main()
