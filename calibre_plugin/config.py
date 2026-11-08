"""Settings page (Preferences → Plugins → EvilFlowers → Customize)."""

from __future__ import annotations

from qt.core import (
    QApplication,
    QComboBox,
    QCursor,
    QFormLayout,
    QHBoxLayout,
    QLabel,
    QLineEdit,
    QPushButton,
    Qt,
    QWidget,
)

from .settings import Direction, Settings, prefs
from .worker import connect, describe_error

DIRECTIONS = [
    (Direction.BOTH, "Both ways"),
    (Direction.PUSH, "Upload only (Calibre → catalog)"),
    (Direction.PULL, "Download only (catalog → Calibre)"),
]
RELATIONS = [
    ("acquisition", "Acquisition (for logged-in readers)"),
    ("open-access", "Open access (anyone who sees the catalog)"),
]


class ConfigWidget(QWidget):
    def __init__(self) -> None:
        super().__init__()
        form = QFormLayout(self)

        self.base_url = QLineEdit(prefs["base_url"], self)
        self.base_url.setPlaceholderText("https://evilflowers.example.org")
        form.addRow("&Server:", self.base_url)

        self.token = QLineEdit(prefs["token"], self)
        self.token.setEchoMode(QLineEdit.EchoMode.Password)
        self.token.setPlaceholderText("API key from your EvilFlowers profile")
        show = QPushButton("Show", self)
        show.setCheckable(True)
        show.toggled.connect(
            lambda on: self.token.setEchoMode(QLineEdit.EchoMode.Normal if on else QLineEdit.EchoMode.Password)
        )
        row = QHBoxLayout()
        row.addWidget(self.token, 1)
        row.addWidget(show)
        form.addRow("API &key:", row)

        self.catalog = QLineEdit(prefs["catalog"], self)
        self.catalog.setPlaceholderText("url_name or UUID of the catalog")
        form.addRow("&Catalog:", self.catalog)

        self.direction = QComboBox(self)
        for direction, label in DIRECTIONS:
            self.direction.addItem(label, direction.value)
        self.direction.setCurrentIndex(max(0, self.direction.findData(prefs["direction"])))
        form.addRow("&Direction:", self.direction)

        self.relation = QComboBox(self)
        for value, label in RELATIONS:
            self.relation.addItem(label, value)
        self.relation.setCurrentIndex(max(0, self.relation.findData(prefs["relation"])))
        form.addRow("Uploaded &files are:", self.relation)

        self.language = QLineEdit(prefs["default_language"], self)
        self.language.setMaxLength(3)
        self.language.setToolTip("Used for books without a language; the catalog requires one.")
        form.addRow("Default &language:", self.language)

        test = QPushButton("&Test connection", self)
        test.clicked.connect(self.test_connection)
        self.status = QLabel(self)
        self.status.setWordWrap(True)
        self.status.setTextInteractionFlags(Qt.TextInteractionFlag.TextSelectableByMouse)
        row = QHBoxLayout()
        row.addWidget(test)
        row.addWidget(self.status, 1)
        form.addRow(row)

        note = QLabel(
            "The API key is stored in Calibre's configuration folder in plain text, "
            "like other plugin credentials. Use a key you can revoke.",
            self,
        )
        note.setWordWrap(True)
        form.addRow(note)

    def current(self) -> Settings:
        return Settings(
            base_url=self.base_url.text().strip(),
            token=self.token.text().strip(),
            catalog=self.catalog.text().strip(),
            direction=Direction(self.direction.currentData()),
            default_language=self.language.text().strip() or "en",
            relation=self.relation.currentData(),
        )

    def test_connection(self) -> None:
        settings = self.current()
        missing = settings.missing()
        if missing:
            self.status.setText(f"Fill in: {', '.join(missing)}")
            return
        QApplication.setOverrideCursor(QCursor(Qt.CursorShape.WaitCursor))
        try:
            client, catalog = connect(settings, timeout=15, retries=0)
            user = client.me()
            self.status.setText(f"✓ Signed in as {user.username}; catalog “{catalog.title}”.")
        except Exception as exc:
            self.status.setText(f"✗ {describe_error(exc)}")
        finally:
            QApplication.restoreOverrideCursor()

    def save_settings(self) -> None:
        settings = self.current()
        prefs["base_url"] = settings.base_url
        prefs["token"] = settings.token
        prefs["catalog"] = settings.catalog
        prefs["direction"] = settings.direction.value
        prefs["relation"] = settings.relation
        prefs["default_language"] = settings.default_language
