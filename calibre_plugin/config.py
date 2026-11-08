"""Settings page (Preferences → Plugins → EvilFlowers → Customize).

Three sections: who you are (Account), what you sync with (Catalog) and how (Sync).
Signing in and out take effect at once, not when the page is saved: both create or
delete a key on the server, and Cancel on the preferences window cannot undo that.
"""

from __future__ import annotations

from html import escape
from typing import Any

from qt.core import (
    QApplication,
    QCheckBox,
    QComboBox,
    QFormLayout,
    QFrame,
    QGroupBox,
    QHBoxLayout,
    QLabel,
    QLineEdit,
    QPushButton,
    QScrollArea,
    QSize,
    Qt,
    QVBoxLayout,
    QWidget,
)

from . import theme
from .login import (
    SERVER_HELP,
    add_field,
    error_style,
    helper_label,
    host_of,
    normalize_server,
    revoke_key,
    sign_in_dialog,
)
from .remote.client import CatalogClient
from .remote.models import ApiKeyCreated, CatalogRef
from .settings import Direction, Settings, clear_account, save_account
from .worker import connect_as, run_task, user_agent

DIRECTIONS = [
    (Direction.BOTH, "Both ways"),
    (Direction.PUSH, "Upload only (Calibre → catalog)"),
    (Direction.PULL, "Download only (catalog → Calibre)"),
]
RELATIONS = [
    ("acquisition", "Acquisition (for signed-in readers)"),
    ("open-access", "Open access (anyone who sees the catalog)"),
]

CATALOG_HELP = "Which catalog on that server this library syncs with."
DIRECTION_HELP = "Upload only never changes your library; Download only never changes the catalog."
RELATION_HELP = "Acquisition: only signed-in readers can download. Open access: anyone who can see the catalog."
LANGUAGE_HELP = "Used for books without a language — the catalog requires one."
SHELF_HELP = "Skip catalog books you haven't put on your shelf in EvilFlowers."
HIGHLIGHTS_HELP = "Where to copy highlights you made in the EvilFlowers reader. Read-only; never sent back."
TOKEN_HELP = "Paste a key from your EvilFlowers profile. Use one you can revoke."
NO_HIGHLIGHTS = "Don't copy"
SIGN_IN_NOTE = "Sign in to choose from your catalogs."
REFRESH_NOTE = "Press Refresh to choose from the catalogs this key can see."


def long_text_columns(db: Any = None) -> list[tuple[str, str]]:
    """``(lookup name, display name)`` of the library's long-text custom columns, by name.

    ``db`` defaults to the library open in Calibre. Without a GUI (tests, calibre-debug)
    there is no such library, and the list is empty.
    """
    if db is None:
        try:
            from calibre.gui2.ui import get_gui

            gui = get_gui()
            db = gui.current_db if gui is not None else None
        except Exception:
            db = None
    try:
        columns = db.field_metadata.custom_field_metadata() if db is not None else {}
    except Exception:
        return []
    found = [(key, meta.get("name") or key) for key, meta in columns.items() if meta.get("datatype") == "comments"]
    return sorted(found, key=lambda column: column[1].casefold())


class CatalogPicker(QComboBox):
    """An editable combo box: pick a catalog by title, or type its url_name when offline.

    Items show the title and hold the url_name, which is what gets saved.
    """

    def __init__(self, value: str, parent: QWidget | None = None) -> None:
        super().__init__(parent)
        self.setEditable(True)
        self.setInsertPolicy(QComboBox.InsertPolicy.NoInsert)
        self.lineEdit().setPlaceholderText("Catalog name, e.g. my-library")
        self.set_value(value)

    def value(self) -> str:
        text = self.currentText().strip()
        index = self.findText(text, Qt.MatchFlag.MatchExactly)
        return (self.itemData(index) or text) if index >= 0 else text

    def set_value(self, value: str) -> None:
        index = self.findData(value)
        if index >= 0:
            self.setCurrentIndex(index)
        else:
            self.setEditText(value)

    def set_catalogs(self, catalogs: list[CatalogRef]) -> None:
        """Replace the list, keeping the chosen catalog even when it is not in the new one."""
        current = self.value()
        self.clear()
        for catalog in catalogs:
            self.addItem(catalog.title, catalog.url_name)
            self.setItemData(self.count() - 1, catalog.url_name, Qt.ItemDataRole.ToolTipRole)
        self.set_value(current)


class ConfigWidget(QWidget):
    def __init__(self, db: Any = None) -> None:
        super().__init__()
        settings = Settings.load()
        self._api_key_id = settings.api_key_id
        self._username = settings.username
        # The server the signed-in key belongs to. Revoking always goes there, never to
        # whatever the Server field says now: the key must not be sent to another host.
        self._key_base_url = settings.base_url if settings.api_key_id else ""
        self._show_key_area = False
        self._loading_catalogs = False
        self._testing = False

        layout = QVBoxLayout(self)
        self.header = theme.Header("EvilFlowers", "Keep this library in sync with a catalog.", self)
        layout.addWidget(self.header)
        # The sections scroll on a short screen; Calibre's customize dialog does not.
        self.body = QWidget(self)
        sections = QVBoxLayout(self.body)
        sections.setContentsMargins(0, 0, 8, 0)  # room for an overlay scroll bar
        sections.addWidget(self._account_section(settings))
        sections.addWidget(self._catalog_section(settings))
        sections.addWidget(self._sync_section(settings, db))
        sections.addStretch(1)
        self.scroll = QScrollArea(self)
        self.scroll.setWidgetResizable(True)
        self.scroll.setFrameShape(QFrame.Shape.NoFrame)
        self.scroll.setHorizontalScrollBarPolicy(Qt.ScrollBarPolicy.ScrollBarAlwaysOff)
        self.scroll.setWidget(self.body)
        layout.addWidget(self.scroll, 1)

        self.refresh_account()
        if settings.base_url and settings.token:
            self.load_catalogs()
        else:
            self._catalog_note(SIGN_IN_NOTE)

    def sizeHint(self) -> QSize:
        """Big enough to show every section without scrolling, if the screen allows it.

        A QScrollArea alone asks for a small fixed size, so the dialog would open cramped.
        """
        body = self.body.sizeHint()
        width = max(body.width() + self.scroll.verticalScrollBar().sizeHint().width(), 640)
        height = self.header.height() + body.height() + 24
        screen = self.screen() or QApplication.primaryScreen()
        if screen is not None:
            height = min(height, int(screen.availableGeometry().height() * 0.8))
        return QSize(width, height)

    def minimumSizeHint(self) -> QSize:
        return QSize(min(self.body.minimumSizeHint().width(), 560), self.header.height() + 220)

    # ------------------------------------------------------------------ sections

    def _account_section(self, settings: Settings) -> QGroupBox:
        box = QGroupBox("Account", self)
        column = QVBoxLayout(box)
        form = QFormLayout()
        # Older versions stored the API address (…/api/v1); show the site people know.
        self.base_url = QLineEdit(normalize_server(settings.base_url), box)
        self.base_url.setPlaceholderText("https://elvira.stuba.sk")
        self.base_url.textChanged.connect(self.refresh_account)
        add_field(form, "&Server:", self.base_url, SERVER_HELP)
        column.addLayout(form)

        # Signed in: who, where, and the two ways out.
        self.account_card = QFrame(box)
        self.account_card.setFrameShape(QFrame.Shape.StyledPanel)
        card = QHBoxLayout(self.account_card)
        self.account_label = QLabel(self.account_card)
        self.account_label.setWordWrap(True)
        self.sign_out_button = QPushButton("Sign out", self.account_card)
        self.sign_out_button.setToolTip("Forget this account on this computer and revoke its key on the server.")
        self.sign_out_button.clicked.connect(self.sign_out)
        self.switch_button = QPushButton("Switch account", self.account_card)
        self.switch_button.setToolTip("Sign in as someone else. The current key is revoked.")
        self.switch_button.clicked.connect(self.sign_in)
        card.addWidget(self.account_label, 1)
        card.addWidget(self.switch_button)
        card.addWidget(self.sign_out_button)
        column.addWidget(self.account_card)

        # Not signed in: one obvious button, and the old way for those who need it.
        self.sign_in_area = QWidget(box)
        row = QHBoxLayout(self.sign_in_area)
        row.setContentsMargins(0, 0, 0, 0)
        self.sign_in_button = QPushButton("Sign in…", self.sign_in_area)
        self.sign_in_button.setToolTip("Sign in with your EvilFlowers username and password.")
        font = self.sign_in_button.font()
        font.setBold(True)
        self.sign_in_button.setFont(font)
        self.sign_in_button.setMinimumHeight(self.sign_in_button.sizeHint().height() + 8)
        self.sign_in_button.clicked.connect(self.sign_in)
        row.addWidget(self.sign_in_button)
        row.addStretch(1)
        column.addWidget(self.sign_in_area)

        self.key_toggle = QLabel(box)
        self.key_toggle.setToolTip("For a key you created yourself in your EvilFlowers profile.")
        self.key_toggle.linkActivated.connect(lambda _link: self.toggle_key_area())
        column.addWidget(self.key_toggle)

        self.key_area = self._key_area(settings, box)
        column.addWidget(self.key_area)

        self.account_note = QLabel(box)
        self.account_note.setWordWrap(True)
        self.account_note.setVisible(False)
        column.addWidget(self.account_note)
        column.addWidget(
            helper_label(
                "The key is stored in Calibre's settings folder in plain text, like other plugin "
                "credentials. Your password is never stored.",
                box,
            )
        )
        return box

    def _key_area(self, settings: Settings, parent: QWidget) -> QWidget:
        area = QWidget(parent)
        form = QFormLayout(area)
        form.setContentsMargins(0, 0, 0, 0)
        self.token = QLineEdit(settings.token, area)
        self.token.setEchoMode(QLineEdit.EchoMode.Password)
        self.token.setPlaceholderText("API key from your EvilFlowers profile")
        self.token.textEdited.connect(self._token_edited)
        self.token.textChanged.connect(self.refresh_account)
        show = QPushButton("Show", area)
        show.setCheckable(True)
        show.setToolTip("Show or hide the key.")
        show.toggled.connect(
            lambda on: self.token.setEchoMode(QLineEdit.EchoMode.Normal if on else QLineEdit.EchoMode.Password)
        )
        row = QHBoxLayout()
        row.addWidget(self.token, 1)
        row.addWidget(show)
        add_field(form, "API &key:", row, TOKEN_HELP, buddy=self.token)

        self.test_button = QPushButton("&Test connection", area)
        self.test_button.setToolTip("Check the server, the key and the catalog.")
        self.test_button.clicked.connect(self.test_connection)
        self.status = QLabel(area)
        self.status.setWordWrap(True)
        self.status.setTextInteractionFlags(Qt.TextInteractionFlag.TextSelectableByMouse)
        row = QHBoxLayout()
        row.addWidget(self.test_button)
        row.addWidget(self.status, 1)
        form.addRow(row)
        return area

    def _catalog_section(self, settings: Settings) -> QGroupBox:
        box = QGroupBox("Catalog", self)
        form = QFormLayout(box)
        self.catalog_picker = CatalogPicker(settings.catalog, box)
        self.catalog = self.catalog_picker.lineEdit()  # text()/setText() of the chosen catalog
        self.refresh_button = QPushButton("Refresh", box)
        self.refresh_button.setToolTip("Load the list of catalogs from the server again.")
        self.refresh_button.clicked.connect(self.load_catalogs)
        row = QHBoxLayout()
        row.addWidget(self.catalog_picker, 1)
        row.addWidget(self.refresh_button)
        add_field(form, "&Catalog:", row, CATALOG_HELP, buddy=self.catalog_picker)
        self.catalog_status = helper_label("", box)
        self.catalog_status.setVisible(False)
        form.addRow(self.catalog_status)
        return box

    def _sync_section(self, settings: Settings, db: Any) -> QGroupBox:
        box = QGroupBox("Sync", self)
        form = QFormLayout(box)

        self.direction = QComboBox(box)
        for direction, label in DIRECTIONS:
            self.direction.addItem(label, direction.value)
        self.direction.setCurrentIndex(max(0, self.direction.findData(settings.direction.value)))
        add_field(form, "&Direction:", self.direction, DIRECTION_HELP)

        self.relation = QComboBox(box)
        for value, label in RELATIONS:
            self.relation.addItem(label, value)
        self.relation.setCurrentIndex(max(0, self.relation.findData(settings.relation)))
        add_field(form, "Uploaded &files are:", self.relation, RELATION_HELP)

        self.language = QLineEdit(settings.default_language, box)
        self.language.setMaxLength(3)
        self.language.setMaximumWidth(80)
        add_field(form, "Default &language:", self.language, LANGUAGE_HELP)

        self.shelf_only = QCheckBox("Only download books on my &shelf", box)
        self.shelf_only.setChecked(settings.shelf_only)
        add_field(form, "", self.shelf_only, SHELF_HELP)

        self.highlights = QComboBox(box)
        self.highlights.addItem(NO_HIGHLIGHTS, "")
        for lookup, name in long_text_columns(db):
            self.highlights.addItem(f"{name} ({lookup})", lookup)
        if settings.highlights_column and self.highlights.findData(settings.highlights_column) < 0:
            # Keep a column this library lacks (or that can't be listed now) rather than lose it on save.
            self.highlights.addItem(settings.highlights_column, settings.highlights_column)
        self.highlights.setCurrentIndex(max(0, self.highlights.findData(settings.highlights_column)))
        add_field(form, "Copy &highlights into:", self.highlights, HIGHLIGHTS_HELP)
        return box

    # ------------------------------------------------------------------- account

    def refresh_account(self) -> None:
        """Show the card when there is a key, the sign-in button when there is none."""
        has_key = bool(self.token.text().strip())
        host = escape(host_of(self.base_url.text()) or "this server")
        if self._username:
            self.account_label.setText(f"Signed in as <b>{escape(self._username)}</b> on {host}")
            self.switch_button.setText("Switch account")
            self.switch_button.setToolTip("Sign in as someone else. The current key is revoked.")
        else:
            self.account_label.setText(f"Using an API key on {host}")
            self.switch_button.setText("Sign in…")
            self.switch_button.setToolTip("Sign in with your username and password instead of this key.")
        self.refresh_button.setEnabled(has_key and not self._loading_catalogs)
        if has_key and self.catalog_status.text() == SIGN_IN_NOTE:  # a key was pasted
            self._catalog_note(REFRESH_NOTE)
        elif not has_key and self.catalog_status.text() == REFRESH_NOTE:
            self._catalog_note(SIGN_IN_NOTE)
        self.account_card.setVisible(has_key)
        self.sign_in_area.setVisible(not has_key)
        pasted = has_key and not self._api_key_id
        self.key_toggle.setVisible(not has_key)
        self.key_area.setVisible(pasted or (not has_key and self._show_key_area))
        link = "Hide API key" if self._show_key_area else "Use an API key instead"
        self.key_toggle.setText(f'<a href="#key">{link}</a>')

    def toggle_key_area(self) -> None:
        self._show_key_area = not self._show_key_area
        self.refresh_account()
        if self._show_key_area:
            self.token.setFocus()

    def _token_edited(self, _text: str) -> None:
        # A typed or pasted key is not the one "Sign in" created: never revoke it, and
        # don't claim to know whose it is.
        self._api_key_id = ""
        self._username = ""

    def sign_in(self) -> None:
        """Open the sign-in dialog (also "Switch account")."""
        outcome = sign_in_dialog(self, self.base_url.text())
        if outcome is not None:
            self.apply_sign_in(*outcome)

    def apply_sign_in(self, base_url: str, key: ApiKeyCreated, username: str) -> None:
        """Keep the new key (saved at once) and revoke the one it replaces, if the plugin made it."""
        previous = (self._key_base_url, self.token.text().strip(), self._api_key_id)
        self._api_key_id = str(key.id)
        self._key_base_url = base_url
        self._username = username
        self.base_url.setText(base_url)
        self.token.setText(key.token)
        save_account(base_url, key.token, self._api_key_id, username)
        self._show_key_area = False
        self.refresh_account()
        self.show_note(f"Signed in as {username}.")
        if all(previous) and previous[2] != self._api_key_id:
            self._revoke(*previous)
        self.load_catalogs()

    def sign_out(self) -> None:
        """Forget the key here at once; revoke it on the server in the background."""
        base_url, token, api_key_id = self._key_base_url, self.token.text().strip(), self._api_key_id
        self._api_key_id = ""
        self._key_base_url = ""
        self._username = ""
        self.token.clear()
        clear_account()
        self.refresh_account()
        self._catalog_note(SIGN_IN_NOTE)
        if base_url and token and api_key_id:
            self.show_note("Signed out.")
            self._revoke(base_url, token, api_key_id)
        else:
            self.show_note("Signed out. A key you pasted keeps working until you delete it in EvilFlowers.")

    def _revoke(self, base_url: str, token: str, api_key_id: str) -> None:
        run_task(lambda: revoke_key(base_url, token, api_key_id), on_failure=self._revoke_failed)

    def _revoke_failed(self, message: str) -> None:
        self.show_note(
            f"Signed out here, but the server couldn't revoke the key ({message.rstrip('.')}). "
            "You can delete it in your EvilFlowers profile.",
            error=True,
        )

    def show_note(self, text: str, *, error: bool = False) -> None:
        self.account_note.setStyleSheet(error_style() if error else "")
        self.account_note.setText(text)
        self.account_note.setVisible(bool(text))

    # ------------------------------------------------------------------- catalog

    def load_catalogs(self) -> None:
        """Fill the catalog list from the server in the background; keep the choice on failure."""
        base_url, token = normalize_server(self.base_url.text()), self.token.text().strip()
        if not base_url or not token:
            self._catalog_note(SIGN_IN_NOTE)
            return
        self._loading_catalogs = True
        self.refresh_button.setEnabled(False)
        self._catalog_note("Loading catalogs…")

        def fetch() -> list[CatalogRef]:
            client = CatalogClient(base_url, token, timeout=15, retries=0, user_agent=user_agent())
            return client.list_catalogs()

        run_task(fetch, on_success=self._catalogs_loaded, on_failure=self._catalogs_failed)

    def _catalogs_done(self) -> None:
        self._loading_catalogs = False
        self.refresh_button.setEnabled(bool(self.token.text().strip()))

    def _catalogs_loaded(self, catalogs: list[CatalogRef]) -> None:
        self._catalogs_done()
        self.catalog_picker.set_catalogs(catalogs)
        if not catalogs:
            self._catalog_note("This account can't see any catalogs yet.")
        elif not self.catalog_picker.value() and len(catalogs) == 1:
            self.catalog_picker.setCurrentIndex(0)
            self._catalog_note("")
        else:
            self._catalog_note("")

    def _catalogs_failed(self, message: str) -> None:
        self._catalogs_done()
        self._catalog_note(f"Couldn't load the catalogs: {message} You can type the catalog's name instead.")

    def _catalog_note(self, text: str) -> None:
        self.catalog_status.setText(text)
        self.catalog_status.setVisible(bool(text))

    # ------------------------------------------------------------------- results

    def current(self) -> Settings:
        return Settings(
            base_url=normalize_server(self.base_url.text()),
            token=self.token.text().strip(),
            catalog=self.catalog_picker.value(),
            direction=Direction(self.direction.currentData()),
            default_language=self.language.text().strip() or "en",
            relation=self.relation.currentData(),
            api_key_id=self._api_key_id,
            username=self._username,
            shelf_only=self.shelf_only.isChecked(),
            highlights_column=self.highlights.currentData() or "",
        )

    def test_connection(self) -> None:
        """Check the pasted key off the GUI thread, with a short timeout and no retries."""
        settings = self.current()
        missing = settings.missing()
        if missing:
            self._test_result(f"Fill in: {', '.join(missing)}", error=True)
            return
        if self._testing:
            return
        self._testing = True
        self.test_button.setEnabled(False)
        self._test_result("Checking…")

        def check() -> str:
            _client, catalog, user = connect_as(settings, timeout=15, retries=0)
            return f"✓ Signed in as {user.username}; catalog “{catalog.title}”."

        # Bound methods, not lambdas: Qt drops them if the page is closed before the answer.
        run_task(check, on_success=self._tested, on_failure=self._test_failed)

    def _tested(self, message: str, *, error: bool = False) -> None:
        self._testing = False
        self.test_button.setEnabled(True)
        self._test_result(message, error=error)

    def _test_failed(self, message: str) -> None:
        self._tested(f"✗ {message}", error=True)

    def _test_result(self, message: str, *, error: bool = False) -> None:
        self.status.setStyleSheet(error_style() if error else "")
        self.status.setText(message)

    def save_settings(self) -> None:
        self.current().save()
