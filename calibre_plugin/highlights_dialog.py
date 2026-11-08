"""The highlights window: what the user marked in the EvilFlowers reader, for the selected books.

Read-only towards the catalog. Highlights load on a worker thread; the only write is
the optional "Save to <column>", which puts the rendered HTML into a long-text
custom column of this library.
"""

from __future__ import annotations

import contextlib
import html
import logging
import threading
from typing import Any

from calibre.gui2 import info_dialog
from calibre.gui2.widgets2 import Dialog
from qt.core import (
    QApplication,
    QDialogButtonBox,
    QIcon,
    QLabel,
    QMimeData,
    QPushButton,
    Qt,
    QTextBrowser,
    QVBoxLayout,
)

from .login import error_style
from .settings import Settings
from .sync.highlights import (
    BookHighlights,
    collect,
    render_books_html,
    render_books_text,
    render_html,
    save_to_column,
    text_column_name,
)
from .theme import Header, helper_color, on_theme_change
from .worker import Task, connect_as, run_task

log = logging.getLogger("evilflowers")

TITLE = "Highlights from EvilFlowers"
SUBTITLE = "Passages you marked in the EvilFlowers reader. Read-only: nothing here changes the catalog."
LOADING = "Loading highlights…"
EMPTY = "No highlights for this book yet — highlight passages in the EvilFlowers reader and they'll show up here."
EMPTY_MANY = (
    "No highlights for these books yet — highlight passages in the EvilFlowers reader and they'll show up here."
)
NOT_LINKED = "This book isn't in the EvilFlowers catalog, so it has no highlights there. Sync it first."
NOT_LINKED_MANY = "These books aren't in the EvilFlowers catalog, so they have no highlights there. Sync them first."
COPY_TIP = "Copy these highlights as text, ready to paste anywhere."
SAVE_TIP = "Write these highlights into the “{name}” column of each book. Only this library changes."
SAVE_HELP = "Save replaces what the “{name}” column holds for these books."


# The page's look: book titles stand out, group names are small grey labels, and the
# passages have room to breathe. Applied to the viewer only; the HTML saved into a
# column stays plain.
PAGE_STYLE = """
h2 {{ margin-top: 18px; margin-bottom: 4px; }}
h3 {{ font-size: small; font-weight: 600; color: {muted}; margin-top: 10px; margin-bottom: 2px; }}
ul {{ margin-top: 0px; }}
li {{ margin-bottom: 5px; }}
.empty {{ color: {muted}; }}
"""


def _centered(message: str, heading: str = "") -> str:
    """A calm, centred message for an empty page, under the book's title when there is one."""
    # Qt's rich text has no vertical centring (and drops a first heading's top margin);
    # a cell's top padding puts the message about a third of the way down.
    title = f"<h2 align='center'>{html.escape(heading)}</h2>" if heading else ""
    return (
        "<table width='100%' border='0' cellspacing='0' cellpadding='0'>"
        "<tr><td align='center' style='padding-top: 110px; padding-left: 48px; padding-right: 48px;'>"
        f"{title}<p class='empty' align='center'>{html.escape(message)}</p>"
        "</td></tr></table>"
    )


def load_highlights(db: Any, settings: Settings, book_ids: list[int], abort: threading.Event) -> list[BookHighlights]:
    """Connect, find who the user is, and read each book's highlights. Runs off the GUI thread.

    Short timeout and no retries: a hanging server must not keep the window waiting
    for minutes. The user can always open the window again.
    """
    client, catalog, user = connect_as(settings, timeout=20, retries=0)
    return collect(db, client, catalog, book_ids, user.id, abort=abort)


class HighlightsDialog(Dialog):
    def __init__(self, gui: Any, book_ids: list[int], *, icon: QIcon | None = None, settings: Any = None) -> None:
        self.gui = gui
        self.book_ids = list(book_ids)
        self.settings = settings if settings is not None else Settings.load()
        self.books: list[BookHighlights] = []
        self.worker: Task | None = None  # the load in flight
        self._abort = threading.Event()
        super().__init__(TITLE, "evilflowers-highlights-dialog", parent=gui)
        if icon is not None:
            self.setWindowIcon(icon)
        self.setWindowFlag(Qt.WindowType.WindowMinMaxButtonsHint)

    def sizeHint(self) -> Any:
        hint = super().sizeHint()
        hint.setWidth(max(hint.width(), 640))
        hint.setHeight(max(hint.height(), 560))
        return hint

    # ---------------------------------------------------------------- layout

    def setup_ui(self) -> None:
        layout = QVBoxLayout(self)
        self.header = Header(TITLE, SUBTITLE, self)
        layout.addWidget(self.header)

        self.browser = QTextBrowser(self)
        self.browser.setOpenLinks(False)
        layout.addWidget(self.browser, 1)

        self.status = QLabel(self)
        self.status.setWordWrap(True)
        self.status.setTextInteractionFlags(Qt.TextInteractionFlag.TextSelectableByMouse)
        self._status_error = False
        self._page: str | None = None
        layout.addWidget(self.status)
        self.refresh_theme()
        on_theme_change(self.refresh_theme)

        self.bb = QDialogButtonBox(self)
        self.copy_button: QPushButton = self.bb.addButton("Copy", QDialogButtonBox.ButtonRole.ActionRole)
        self.copy_button.setToolTip(COPY_TIP)
        self.copy_button.clicked.connect(self.copy)
        self.column_name = self._column_name()
        self.save_button: QPushButton | None = None
        if self.column_name is not None:
            self.save_button = self.bb.addButton(f"Save to {self.column_name}", QDialogButtonBox.ButtonRole.ActionRole)
            self.save_button.setToolTip(SAVE_TIP.format(name=self.column_name))
            self.save_button.clicked.connect(self.save)
        close = self.bb.addButton(QDialogButtonBox.StandardButton.Close)
        close.clicked.connect(self.reject)
        layout.addWidget(self.bb)
        self.set_busy(False)

    def _column_name(self) -> str | None:
        column = self.settings.highlights_column
        if not column:
            return None
        return text_column_name(self.gui.current_db.new_api, column)

    # ----------------------------------------------------------------- state

    @property
    def has_highlights(self) -> bool:
        return any(book.highlights for book in self.books)

    def set_busy(self, busy: bool) -> None:
        enabled = not busy and self.has_highlights
        self.copy_button.setEnabled(enabled)
        if self.save_button is not None:
            self.save_button.setEnabled(enabled)

    def load(self) -> None:
        missing = self.settings.missing()
        if missing:
            self.show_message(f"Set {', '.join(missing)} in Settings first.")
            return
        self.books = []
        self.show_message(LOADING)
        db, settings, book_ids, abort = self.gui.current_db.new_api, self.settings, self.book_ids, self._abort
        # Not a child of this window: it outlives a closed window instead of being destroyed mid-request.
        self.worker = run_task(
            lambda: load_highlights(db, settings, book_ids, abort), on_success=self.on_loaded, on_failure=self.on_failed
        )
        self.set_busy(True)

    def show_message(self, message: str) -> None:
        self._set_page(_centered(message))
        self.set_status("")

    def on_loaded(self, books: list[BookHighlights]) -> None:
        self.worker = None
        self.books = books
        self._set_page(self.render())
        self.set_status(self.describe())
        self.set_busy(False)

    def on_failed(self, message: str) -> None:
        self.worker = None
        self.show_message(message)
        self.set_busy(False)

    def set_status(self, text: str, *, error: bool = False) -> None:
        """The line under the page: grey, or red for an error, readable on any theme."""
        self._status_error = error
        self.status.setText(text)
        self._style_status()

    def _style_status(self) -> None:
        self.status.setStyleSheet(error_style() if self._status_error else f"color: {helper_color().name()};")

    def _set_page(self, page: str) -> None:
        self._page = page
        self.browser.setHtml(page)

    def refresh_theme(self) -> None:
        """Colours for the page and the status line; the default stylesheet applies on setHtml."""
        self.browser.document().setDefaultStyleSheet(PAGE_STYLE.format(muted=helper_color().name()))
        self._style_status()
        if self._page is not None:
            self.browser.setHtml(self._page)

    # ------------------------------------------------------------- rendering

    def render(self) -> str:
        """The page shown: every book's highlights, or why there are none."""
        many = len(self.books) > 1
        heading = f"{len(self.books)} books" if many else (self.books[0].title if self.books else "")
        if self.has_highlights:
            return render_books_html(self.books)
        if self.books and not any(book.linked for book in self.books):
            return _centered(NOT_LINKED_MANY if many else NOT_LINKED, heading)
        errors = [book for book in self.books if book.error]
        if errors:
            return _centered(errors[0].error, heading)
        return _centered(EMPTY_MANY if many else EMPTY, heading)

    def describe(self) -> str:
        """One grey line under the page: counts, and the books that could not show anything."""
        if not self.has_highlights:
            return ""
        count = sum(len(book.highlights) for book in self.books)
        lines = [f"{count} highlight{'s' if count != 1 else ''}."]
        empty = [book.title for book in self.books if book.linked and not book.error and not book.highlights]
        if empty:
            lines.append(f"No highlights yet: {', '.join(empty)}.")
        missing = [book.title for book in self.books if not book.linked]
        if missing:
            lines.append(f"Not in the catalog: {', '.join(missing)}.")
        failed = [book.title for book in self.books if book.error]
        if failed:
            lines.append(f"Could not load: {', '.join(failed)}.")
        if self.column_name is not None:
            lines.append(SAVE_HELP.format(name=self.column_name))
        return " ".join(lines)

    # --------------------------------------------------------------- actions

    def copy(self) -> None:
        if not self.has_highlights:
            return
        data = QMimeData()
        data.setText(render_books_text(self.books))
        data.setHtml(render_books_html(self.books))
        QApplication.clipboard().setMimeData(data)
        self.set_status("Copied.")

    def save(self) -> None:
        column = self.settings.highlights_column
        html_by_book = {book.book_id: render_html(book.highlights) for book in self.books if book.highlights}
        if not html_by_book or self.column_name is None:
            return
        db = self.gui.current_db.new_api
        try:
            save_to_column(db, column, html_by_book)
        except Exception as exc:
            log.exception("EvilFlowers: saving highlights failed")
            self.set_status(f"Could not save the highlights: {exc}", error=True)
            return
        self._refresh_library(list(html_by_book))
        books = len(html_by_book)
        self.set_status(f"Saved to {self.column_name} for {books} book{'s' if books != 1 else ''}.")

    def _refresh_library(self, book_ids: list[int]) -> None:
        """Repaint the changed rows in Calibre's book list, if there is one."""
        with contextlib.suppress(AttributeError):
            self.gui.library_view.model().refresh_ids(book_ids)

    # -------------------------------------------------------------- lifecycle

    def showEvent(self, event: Any) -> None:
        super().showEvent(event)
        if not self.books and self.worker is None:
            self.load()

    def reject(self) -> None:
        """Close at once. A load still running only reads; it stops before the next book."""
        self._abort.set()
        if self.worker is not None:
            self.worker.disconnect_all()
            self.worker = None
        super().reject()


def show_highlights(gui: Any, icon: QIcon | None, action: Any) -> HighlightsDialog | None:
    """Menu entry point: open the highlights of the books selected in the library.

    One window at a time: opening it again replaces the previous one with the new
    selection. The window is kept on ``action`` so it is not garbage-collected.
    """
    book_ids = list(gui.library_view.get_selected_ids())
    if not book_ids:
        info_dialog(gui, TITLE, "Select one or more books first.", show=True, show_copy_button=False)
        return None
    previous = getattr(action, "highlights_dialog", None)
    if previous is not None:
        previous.reject()
    dialog = HighlightsDialog(gui, book_ids, icon=icon)
    dialog.setAttribute(Qt.WidgetAttribute.WA_DeleteOnClose)
    dialog.finished.connect(lambda _result: setattr(action, "highlights_dialog", None))
    action.highlights_dialog = dialog
    dialog.show()
    return dialog
