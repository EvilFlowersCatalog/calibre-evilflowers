"""The settings page and the sign-in dialog, headless (Qt offscreen) against the fake catalog.

Runs in the suite once "test_settings_gui" is listed in tests/run.py after "test_gui",
or alone with `scripts/dev.sh debug tests/test_settings_gui.py`.
"""

from __future__ import annotations

import json
import os
import sys
import unittest
from pathlib import Path
from types import SimpleNamespace
from typing import Any

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
sys.path.insert(0, str(Path(__file__).resolve().parent))
sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from calibre.gui2 import Application
from fake_catalog import PASSWORD, TOKEN, USERNAME, FakeCatalog
from qt.core import QEventLoop, QTimer

from calibre_plugin.remote import auth
from calibre_plugin.settings import Direction, Settings, prefs

_app = None


def app() -> Any:
    global _app
    if _app is None:
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
        raise AssertionError("timed out")


def library_with_columns(**columns: str) -> Any:
    """Just enough of a Calibre db for the highlights column list: lookup -> datatype."""
    meta = {key: {"name": key.lstrip("#").title(), "datatype": kind} for key, kind in columns.items()}
    return SimpleNamespace(field_metadata=SimpleNamespace(custom_field_metadata=lambda: meta))


class SettingsGuiTests(unittest.TestCase):
    def setUp(self) -> None:
        app()
        self.fake = FakeCatalog().start()
        self.addCleanup(self.fake.stop)
        saved = {key: prefs[key] for key in prefs.defaults}
        self.addCleanup(lambda: [prefs.__setitem__(k, v) for k, v in saved.items()])
        prefs["base_url"], prefs["token"], prefs["catalog"] = self.fake.base_url, "", self.fake.catalog_url_name
        prefs["api_key_id"], prefs["username"] = "", ""
        prefs["shelf_only"], prefs["highlights_column"] = False, ""

    def stored(self) -> str:
        """Everything the plugin keeps on disk, as text: the prefs dict and the file itself."""
        on_disk = Path(prefs.file_path).read_text() if Path(prefs.file_path).exists() else ""
        return json.dumps(dict(prefs)) + on_disk

    def create_key(self) -> Any:
        return auth.sign_in(self.fake.base_url, USERNAME, PASSWORD, key_name="test", user_agent="tests")

    def sign_in_with_dialog(self, password: str) -> Any:
        from calibre_plugin.login import SignInDialog

        dialog = SignInDialog(None, self.fake.base_url)
        self.addCleanup(dialog.deleteLater)
        dialog.username.setText(USERNAME)
        dialog.password.setText(password)
        dialog.submit()
        self.assertFalse(dialog.password.isEnabled(), "inputs are disabled while signing in")
        self.assertTrue(dialog.busy.isVisibleTo(dialog))
        wait_for(lambda: dialog.task is None)
        return dialog

    def test_sign_in_keeps_the_key_and_never_the_password(self) -> None:
        from calibre_plugin.config import ConfigWidget

        dialog = self.sign_in_with_dialog(PASSWORD)
        self.assertIsNotNone(dialog.outcome)
        self.assertEqual(dialog.password.text(), "", "the password field is emptied when the dialog closes")
        base_url, key, username = dialog.outcome
        # The dialog was opened with the API address; the site address is what it shows and keeps.
        self.assertEqual(dialog.server.text(), self.fake.origin)
        self.assertEqual((base_url, username), (self.fake.origin, USERNAME))
        self.assertEqual(self.fake.api_keys[str(key.id)]["name"], auth.default_key_name())

        widget = ConfigWidget()
        widget.apply_sign_in(base_url, key, username)
        self.assertEqual(widget.base_url.text(), self.fake.origin, "the Server field shows the site, not …/api/v1")
        self.assertEqual(
            (prefs["token"], prefs["api_key_id"], prefs["username"], prefs["base_url"]),
            (key.token, str(key.id), USERNAME, self.fake.origin),
            "a sign-in is saved at once, without pressing OK",
        )
        stored = self.stored()
        self.assertNotIn(PASSWORD, stored)
        for access_token in self.fake.access_tokens:
            self.assertNotIn(access_token, stored, "the login's access token is not kept")
        self.assertNotIn("refresh-", stored)
        self.assertTrue(widget.account_card.isVisibleTo(widget))
        self.assertFalse(widget.sign_in_area.isVisibleTo(widget))
        self.assertIn(USERNAME, widget.account_label.text())

        # The catalog list loads with the new key and keeps the configured catalog chosen.
        self.fake.add_catalog("Another one", "another")
        widget.load_catalogs()
        wait_for(lambda: widget.catalog_picker.count() == 2 and widget.refresh_button.isEnabled())
        self.assertEqual(widget.catalog_picker.currentText(), "Calibre test catalog")
        self.assertEqual(widget.current().catalog, self.fake.catalog_url_name)
        self.assertEqual(widget.current().api_key_id, str(key.id))

    def test_wrong_password_is_shown_inline_and_the_field_is_cleared_on_cancel(self) -> None:
        dialog = self.sign_in_with_dialog("not the password")
        self.assertIsNone(dialog.outcome)
        self.assertTrue(dialog.error.isVisibleTo(dialog))
        self.assertEqual(dialog.error.text(), auth.WRONG_CREDENTIALS)
        self.assertNotIn("not the password", dialog.error.text())
        self.assertTrue(dialog.password.isEnabled(), "inputs come back after a failure")
        dialog.reject()
        self.assertEqual(dialog.password.text(), "")
        self.assertEqual(self.fake.api_keys, {})

    def test_empty_fields_are_caught_without_a_request(self) -> None:
        from calibre_plugin.login import SignInDialog

        dialog = SignInDialog(None, self.fake.base_url)
        dialog.submit()
        self.assertIsNone(dialog.task)
        self.assertIn("username and password", dialog.error.text())
        self.assertEqual(self.fake.requests, [])
        dialog.reject()

    def test_sign_out_revokes_the_key_it_created(self) -> None:
        from calibre_plugin.config import ConfigWidget

        key = self.create_key()
        widget = ConfigWidget()
        widget.apply_sign_in(self.fake.base_url, key, USERNAME)
        widget.sign_out()
        self.assertEqual((prefs["token"], prefs["api_key_id"], prefs["username"]), ("", "", ""))
        self.assertEqual(prefs["base_url"], self.fake.base_url, "the server stays for the next sign-in")
        self.assertTrue(widget.sign_in_area.isVisibleTo(widget))
        wait_for(lambda: str(key.id) not in self.fake.api_keys)

    def test_switching_account_revokes_the_previous_key(self) -> None:
        from calibre_plugin.config import ConfigWidget

        first, second = self.create_key(), self.create_key()
        widget = ConfigWidget()
        widget.apply_sign_in(self.fake.base_url, first, USERNAME)
        widget.apply_sign_in(self.fake.base_url, second, USERNAME)
        wait_for(lambda: str(first.id) not in self.fake.api_keys)
        self.assertIn(str(second.id), self.fake.api_keys)
        self.assertEqual(prefs["token"], second.token)

    def test_the_key_is_revoked_where_it_was_issued_never_at_an_edited_address(self) -> None:
        from calibre_plugin.config import ConfigWidget

        key = self.create_key()
        widget = ConfigWidget()
        widget.apply_sign_in(self.fake.base_url, key, USERNAME)
        widget.base_url.setText("https://elsewhere.invalid")  # typed, not signed in there
        widget.sign_out()
        wait_for(lambda: str(key.id) not in self.fake.api_keys)  # the DELETE reached the issuing server

        # Same after reopening the page: the saved server is the key's, not the field's.
        key = self.create_key()
        widget = ConfigWidget()
        widget.apply_sign_in(self.fake.base_url, key, USERNAME)
        widget = ConfigWidget()
        widget.base_url.setText("https://elsewhere.invalid")
        widget.sign_out()
        wait_for(lambda: str(key.id) not in self.fake.api_keys)

    def test_form_labels_have_their_fields_and_helpers_their_own_rows(self) -> None:
        from qt.core import QFormLayout, QLabel

        from calibre_plugin.config import ConfigWidget
        from calibre_plugin.theme import HelperLabel

        widget = ConfigWidget()
        captions = {label.text(): label for label in widget.findChildren(QLabel) if label.text().endswith(":")}
        self.assertIs(captions["&Catalog:"].buddy(), widget.catalog_picker, "an empty combo box is still a buddy")
        self.assertEqual(widget.catalog_picker.toolTip(), captions["&Catalog:"].toolTip())
        # Each helper is a form row of its own (nested under a field, QFormLayout cut it off).
        direct = [
            form.itemAt(row, QFormLayout.ItemRole.FieldRole).widget()
            for form in widget.findChildren(QFormLayout)
            for row in range(form.rowCount())
            if form.itemAt(row, QFormLayout.ItemRole.FieldRole) is not None
        ]
        helpers = [label for label in direct if isinstance(label, HelperLabel)]
        self.assertGreaterEqual(len(helpers), 8, "server, key, catalog and five sync fields")
        # Signed out: the catalog list cannot load, and the page says why.
        self.assertFalse(widget.refresh_button.isEnabled())
        self.assertIn("Sign in to choose", widget.catalog_status.text())

    def test_sign_out_of_a_pasted_key_clears_it_locally_only(self) -> None:
        from calibre_plugin.config import ConfigWidget

        prefs["token"] = TOKEN
        widget = ConfigWidget()
        self.assertTrue(widget.key_area.isVisibleTo(widget), "a pasted key stays visible and editable")
        self.assertIn("Using an API key", widget.account_label.text())
        before = len(self.fake.requests)
        wait_for(lambda: widget.refresh_button.isEnabled())  # the catalog list loads on open
        widget.sign_out()
        self.assertEqual(prefs["token"], "")
        self.assertIn("keeps working", widget.account_note.text())
        self.assertFalse(any(method == "DELETE" for method, _ in self.fake.requests[before:]))

    def test_key_toggle_reveals_the_api_key_field(self) -> None:
        from calibre_plugin.config import ConfigWidget

        widget = ConfigWidget()
        self.assertFalse(widget.key_area.isVisibleTo(widget))
        widget.toggle_key_area()
        self.assertTrue(widget.key_area.isVisibleTo(widget))
        widget.token.setText(TOKEN)
        widget.token.textEdited.emit(TOKEN)
        self.assertEqual(widget.current().api_key_id, "", "a pasted key is never revoked")

    def test_sync_settings_round_trip(self) -> None:
        from calibre_plugin.config import NO_HIGHLIGHTS, ConfigWidget

        db = library_with_columns(**{"#highlights": "comments", "#notes": "comments", "#rating2": "rating"})
        widget = ConfigWidget(db)
        lookups = [widget.highlights.itemData(i) for i in range(widget.highlights.count())]
        self.assertEqual(lookups, ["", "#highlights", "#notes"])
        self.assertEqual(widget.highlights.currentText(), NO_HIGHLIGHTS)
        widget.shelf_only.setChecked(True)
        widget.highlights.setCurrentIndex(1)
        widget.direction.setCurrentIndex(widget.direction.findData("pull"))
        widget.save_settings()

        settings = Settings.load()
        self.assertTrue(settings.shelf_only)
        self.assertEqual(settings.highlights_column, "#highlights")
        self.assertEqual(settings.direction, Direction.PULL)

        # Without a library to list columns from, the saved column is kept, not wiped.
        widget = ConfigWidget()
        self.assertEqual(widget.current().highlights_column, "#highlights")
        self.assertTrue(widget.current().shelf_only)

    def test_settings_defaults_and_positional_construction(self) -> None:
        settings = Settings("https://x", "t", "c", Direction.BOTH)
        self.assertEqual((settings.api_key_id, settings.username), ("", ""))
        self.assertFalse(settings.shelf_only)
        self.assertEqual(settings.highlights_column, "")
        self.assertFalse(settings.signed_in)
        self.assertEqual(Settings("https://x", "", "c").missing(), ["token"])


if __name__ == "__main__":
    # calibre-debug -e does not register this script as sys.modules["__main__"].
    suite = unittest.TestLoader().loadTestsFromTestCase(SettingsGuiTests)
    raise SystemExit(0 if unittest.TextTestRunner(verbosity=2).run(suite).wasSuccessful() else 1)
