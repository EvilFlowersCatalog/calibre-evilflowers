"""The catalog browser: search the catalog, see what is already here, download books and keep a shelf.

Every network call runs on a worker thread; the dialog only ever touches Qt objects.
Downloads go through the sync engine's own pull, so a book fetched here gets a
baseline and later syncs treat it like any other synced book.
"""

from __future__ import annotations

import hashlib
import logging
import os
import queue
import tempfile
import threading
from collections.abc import Callable, Iterable
from dataclasses import replace
from pathlib import Path
from typing import Any
from uuid import UUID

from calibre.gui2.widgets2 import Dialog
from qt.core import (
    QAbstractItemView,
    QAbstractTableModel,
    QAction,
    QApplication,
    QColor,
    QComboBox,
    QDialogButtonBox,
    QEvent,
    QGridLayout,
    QHBoxLayout,
    QHeaderView,
    QIcon,
    QLabel,
    QLineEdit,
    QMenu,
    QModelIndex,
    QPainter,
    QPalette,
    QPen,
    QPixmap,
    QProgressBar,
    QPushButton,
    QRectF,
    QSize,
    QStackedWidget,
    QStyle,
    QStyledItemDelegate,
    QStyleOptionViewItem,
    Qt,
    QTableView,
    QThread,
    QTimer,
    QVBoxLayout,
    pyqtSignal,
)

from .login import error_style, helper_label, host_of
from .remote.client import CatalogClient
from .remote.models import CatalogRef, EntryPage, EntryRef, Facets
from .settings import Settings
from .sync.engine import Report, SyncEngine
from .sync.plan import Action, PlanItem, State
from .sync.state import StateStore
from .theme import Header, chip_colors, on_theme_change
from .vendor.evilflowers_protocol.errors import CatalogError
from .worker import Task, connect, disconnect_all, run_task, start_thread, user_agent

log = logging.getLogger("evilflowers")

PAGE_SIZE = 30
SEARCH_DELAY_MS = 300
COVER_SIZE = QSize(48, 64)
COLUMNS = ("Title", "Authors", "Language", "Status")
BADGE_COLUMN = 3
BADGES_ROLE = Qt.ItemDataRole.UserRole + 1
IN_LIBRARY = ("In your library", "in sync")
ON_SHELF = ("On your shelf", "changed")
THUMBNAIL_CACHE = Path(tempfile.gettempdir()) / "evilflowers-thumbnails"

_open_dialog: BrowseDialog | None = None


def show_browse(gui: Any, icon: QIcon, action: Any) -> BrowseDialog:
    """Open the browser, or bring the open one to the front. This is the menu item's entry point."""
    global _open_dialog
    if _open_dialog is not None:
        if _open_dialog.isVisible():
            _open_dialog.raise_()
            _open_dialog.activateWindow()
            return _open_dialog
        _open_dialog.deleteLater()  # closed earlier: free it rather than keep one per opening
    _open_dialog = BrowseDialog(gui, icon, action)
    _open_dialog.show()
    return _open_dialog


# ------------------------------------------------------------------------- threads
#
# Every thread here is started with worker.start_thread (directly or through
# worker.run_task): it is nobody's child and stays referenced until it finishes, so
# closing the window never destroys a running thread.


class ThumbnailLoader(QThread):
    """Fetch small covers one at a time into an on-disk cache; ``loaded`` carries the path or "".

    Kept apart from worker.CoverLoader on purpose: this cache outlives the window and is
    keyed by the entry's ``updated_at``, and it hands over a path, not an image. The
    stop is the same: an Event checked before each fetch, so closing never drains the queue.
    """

    loaded = pyqtSignal(str, str)

    def __init__(self, client: CatalogClient, cache: Path = THUMBNAIL_CACHE) -> None:
        super().__init__()
        self._client = client
        self._cache = cache
        self._queue: queue.Queue[EntryRef | None] = queue.Queue()
        self._stop = threading.Event()

    def request(self, entry: EntryRef) -> None:
        self._queue.put(entry)

    def stop(self) -> None:
        self._stop.set()
        self._queue.put(None)  # wake it if it is waiting for work

    def run(self) -> None:
        while not self._stop.is_set():
            entry = self._queue.get()
            if entry is None or self._stop.is_set():
                break
            self.loaded.emit(str(entry.id), self._fetch(entry))

    def _fetch(self, entry: EntryRef) -> str:
        # Keyed by updated_at too, so a replaced cover is fetched again.
        stamp = hashlib.sha1(entry.updated_at.encode()).hexdigest()[:10]
        path = self._cache / f"{entry.id}-{stamp}.img"
        if path.is_file():
            return str(path)
        partial = path.with_suffix(f".{os.getpid()}.part")
        try:
            if self._client.download_thumbnail(entry.id, partial) is None:
                return ""
            os.replace(partial, path)
        except (CatalogError, OSError) as exc:
            log.warning(f"{entry.title}: thumbnail not loaded: {exc}")
            return ""
        finally:
            partial.unlink(missing_ok=True)
        return str(path)


def _download(
    db: Any, client: CatalogClient, catalog: CatalogRef, entries: list[EntryRef], abort: threading.Event
) -> tuple[Report, int]:
    """Pull ``entries`` into the library with the sync engine, as a sync would for "new in catalog".

    Returns the report and how many entries were already here and so left alone.

    A full scan would list the whole catalog just to find these few, so the plan
    items are built here. Only entries with no book in the library may come in:
    the engine would otherwise add a second book with the same UUID. What is in the
    library is read again here, on this thread, right before the pull: the window's
    badges may be older than a sync or an add made since.
    """
    present = local_entry_ids(db, catalog.id)
    fresh = [entry for entry in entries if str(entry.id).lower() not in present]
    items = [PlanItem(state=State.REMOTE_ONLY, action=Action.PULL, entry=entry) for entry in fresh]
    report = SyncEngine(db, client, catalog, Settings.load(), log=log, abort=abort).apply(items)
    return report, len(entries) - len(fresh)


def local_entry_ids(db: Any, catalog_id: UUID | None) -> frozenset[str]:
    """Entry ids that already have a book here: every book's UUID, plus books linked by a baseline."""
    uuids = db.all_field_for("uuid", db.all_book_ids())
    ids = {str(value).lower() for value in uuids.values() if value}
    if catalog_id is not None:
        ids.update(baseline.entry_id.lower() for baseline in StateStore(db, str(catalog_id)).baselines().values())
    return frozenset(ids)


def language_name(code: str) -> str:
    """ "Slovak" for "sk"; the code itself when Calibre does not know it."""
    try:
        from calibre.utils.localization import get_language

        return get_language(code) or code
    except Exception:
        return code


# --------------------------------------------------------------------------- model


class EntryModel(QAbstractTableModel):
    """One row per catalog entry, with its cover, and badges for what this library and shelf hold."""

    def __init__(self, parent: Any = None) -> None:
        super().__init__(parent)
        self.entries: list[EntryRef] = []
        self.local_ids: frozenset[str] = frozenset()
        self.covers: dict[str, QIcon] = {}
        self.placeholder = QIcon()

    def set_entries(self, entries: Iterable[EntryRef]) -> None:
        self.beginResetModel()
        self.entries = list(entries)
        self.endResetModel()

    def append(self, entries: list[EntryRef]) -> None:
        known = {entry.id for entry in self.entries}
        fresh = [entry for entry in entries if entry.id not in known]
        if fresh:
            self.beginInsertRows(QModelIndex(), len(self.entries), len(self.entries) + len(fresh) - 1)
            self.entries.extend(fresh)
            self.endInsertRows()

    def replace_entry(self, entry: EntryRef) -> None:
        for row, current in enumerate(self.entries):
            if current.id == entry.id:
                self.entries[row] = entry
                self.dataChanged.emit(self.index(row, 0), self.index(row, len(COLUMNS) - 1))

    def set_local_ids(self, ids: frozenset[str]) -> None:
        self.local_ids = ids
        self._changed_column(BADGE_COLUMN)

    def set_cover(self, entry_id: str, icon: QIcon) -> None:
        self.covers[entry_id] = icon
        for row, entry in enumerate(self.entries):
            if str(entry.id) == entry_id:
                self.dataChanged.emit(self.index(row, 0), self.index(row, 0))

    def _changed_column(self, column: int) -> None:
        if self.entries:
            self.dataChanged.emit(self.index(0, column), self.index(len(self.entries) - 1, column))

    def in_library(self, entry: EntryRef) -> bool:
        return str(entry.id).lower() in self.local_ids

    def badges(self, entry: EntryRef) -> list[tuple[str, str]]:
        """(text, theme chip kind) pairs for the badge column."""
        result = []
        if self.in_library(entry):
            result.append(IN_LIBRARY)
        if entry.shelf_record_id is not None:
            result.append(ON_SHELF)
        return result

    def rowCount(self, parent: QModelIndex | None = None) -> int:
        return 0 if parent is not None and parent.isValid() else len(self.entries)

    def columnCount(self, parent: QModelIndex | None = None) -> int:
        return 0 if parent is not None and parent.isValid() else len(COLUMNS)

    def headerData(self, section: int, orientation: Qt.Orientation, role: int = Qt.ItemDataRole.DisplayRole) -> Any:
        if role == Qt.ItemDataRole.DisplayRole and orientation == Qt.Orientation.Horizontal:
            return COLUMNS[section]
        return None

    def data(self, index: QModelIndex, role: int = Qt.ItemDataRole.DisplayRole) -> Any:
        if not index.isValid():
            return None
        entry, column = self.entries[index.row()], index.column()
        if role == Qt.ItemDataRole.DisplayRole:
            return self._display(entry, column)
        if role == Qt.ItemDataRole.DecorationRole and column == 0:
            return self.covers.get(str(entry.id), self.placeholder)
        if role == Qt.ItemDataRole.ToolTipRole and column == 0:
            return _tooltip(entry)
        if role == BADGES_ROLE:
            return self.badges(entry)
        if role == Qt.ItemDataRole.UserRole:
            return entry
        return None

    def _display(self, entry: EntryRef, column: int) -> str:
        if column == 0:
            return entry.title
        if column == 1:
            return ", ".join(person.full_name for person in entry.authors)
        if column == 2:
            return language_name(entry.language_code) if entry.language_code else ""
        return " · ".join(text for text, _kind in self.badges(entry))


def _tooltip(entry: EntryRef) -> str:
    summary = (entry.summary or "").strip()
    if len(summary) > 400:
        summary = summary[:400].rsplit(" ", 1)[0] + "…"
    return f"{entry.title}\n\n{summary}" if summary else entry.title


class BadgeDelegate(QStyledItemDelegate):
    """Draw the badge column as theme-coloured pills instead of plain text."""

    def paint(self, painter: QPainter, option: QStyleOptionViewItem, index: QModelIndex) -> None:
        opt = QStyleOptionViewItem(option)
        self.initStyleOption(opt, index)
        opt.text = ""
        style = opt.widget.style() if opt.widget is not None else QApplication.style()
        style.drawControl(QStyle.ControlElement.CE_ItemViewItem, opt, painter, opt.widget)
        metrics = opt.fontMetrics
        height = metrics.height() + 4
        x = opt.rect.left() + 6
        painter.save()
        painter.setRenderHint(QPainter.RenderHint.Antialiasing)
        for text, kind in index.data(BADGES_ROLE) or ():
            background, foreground, border = chip_colors(kind)
            rect = QRectF(x, opt.rect.center().y() - height / 2, metrics.horizontalAdvance(text) + 16, height)
            painter.setPen(QPen(border))
            painter.setBrush(background)
            painter.drawRoundedRect(rect, height / 2, height / 2)
            painter.setPen(foreground)
            painter.drawText(rect, Qt.AlignmentFlag.AlignCenter, text)
            x += int(rect.width()) + 6
        painter.restore()

    def sizeHint(self, option: QStyleOptionViewItem, index: QModelIndex) -> QSize:
        hint = super().sizeHint(option, index)
        badges = index.data(BADGES_ROLE) or ()
        # Each pill is text + 16 wide with 6 between; 6 before the first and 12 after the last,
        # so the last pill never touches the scroll bar.
        width = sum(option.fontMetrics.horizontalAdvance(text) + 22 for text, _kind in badges) + 12
        return QSize(max(width, 60), hint.height())


def _placeholder(palette: QPalette) -> QIcon:
    """A plain book-shaped tile shown while a cover loads, and for books without one."""
    pixmap = QPixmap(COVER_SIZE)
    pixmap.fill(QColor(0, 0, 0, 0))
    painter = QPainter(pixmap)
    painter.setPen(QPen(palette.color(QPalette.ColorRole.Mid)))
    painter.setBrush(palette.color(QPalette.ColorRole.AlternateBase))
    painter.drawRoundedRect(QRectF(4.5, 0.5, COVER_SIZE.width() - 9, COVER_SIZE.height() - 1), 3, 3)
    painter.end()
    return QIcon(pixmap)


def _cover_icon(path: str) -> QIcon | None:
    pixmap = QPixmap(path)
    if pixmap.isNull():
        return None
    return QIcon(
        pixmap.scaled(COVER_SIZE, Qt.AspectRatioMode.KeepAspectRatio, Qt.TransformationMode.SmoothTransformation)
    )


# -------------------------------------------------------------------------- dialog


class BrowseDialog(Dialog):
    """Search the configured catalog and act on the books found. Non-modal."""

    def __init__(self, gui: Any, icon: QIcon, action: Any) -> None:
        self.gui = gui
        self.action = action
        self.client: CatalogClient | None = None
        self.catalog: CatalogRef | None = None
        self.category_labels: dict[UUID, str] = {}
        self.page = 0
        self.pages = 0
        self.total = 0
        self._depth = 0  # requests in flight
        self._filling_combos = False  # set while filters are refilled, so that refill starts no search
        self._generation = 0
        self._tasks: set[Task] = set()
        self._thumbnails: ThumbnailLoader | None = None
        self._requested: set[str] = set()
        self._closed = False  # set by reject(): answers that arrive later are dropped
        self._pulls = Settings.load().direction.pulls  # "Upload only" promises never to change the library
        self._abort = threading.Event()  # stops a download after the current book when the window closes
        super().__init__("Browse EvilFlowers catalog", "evilflowers-browse-dialog", parent=gui)
        self.setWindowIcon(icon)
        self.setWindowFlag(Qt.WindowType.WindowMinMaxButtonsHint)
        self.setModal(False)
        on_theme_change(self.theme_changed)

    def sizeHint(self) -> QSize:
        hint = super().sizeHint()
        return QSize(max(hint.width(), 960), max(hint.height(), 680))

    # ---------------------------------------------------------------- layout

    def setup_ui(self) -> None:
        layout = QVBoxLayout(self)
        self.header = Header("Browse catalog", "Find books in the catalog and bring them into Calibre.", self)
        layout.addWidget(self.header)
        layout.addLayout(self._filters())
        layout.addLayout(self._status_row())

        self.model = EntryModel(self)
        self.model.placeholder = _placeholder(self.palette())
        self.table = QTableView(self)
        self.table.setModel(self.model)
        self.table.setItemDelegateForColumn(BADGE_COLUMN, BadgeDelegate(self.table))
        self.table.setSelectionBehavior(QAbstractItemView.SelectionBehavior.SelectRows)
        self.table.setSelectionMode(QAbstractItemView.SelectionMode.ExtendedSelection)
        self.table.setEditTriggers(QAbstractItemView.EditTrigger.NoEditTriggers)
        self.table.setIconSize(COVER_SIZE)
        self.table.setWordWrap(True)
        self.table.setAlternatingRowColors(True)
        self.table.verticalHeader().hide()
        self.table.verticalHeader().setDefaultSectionSize(COVER_SIZE.height() + 8)
        header = self.table.horizontalHeader()
        header.setSectionResizeMode(0, QHeaderView.ResizeMode.Stretch)
        header.setSectionResizeMode(1, QHeaderView.ResizeMode.Interactive)
        header.resizeSection(1, 200)
        header.setSectionResizeMode(2, QHeaderView.ResizeMode.ResizeToContents)
        header.setSectionResizeMode(BADGE_COLUMN, QHeaderView.ResizeMode.ResizeToContents)
        self.table.selectionModel().selectionChanged.connect(self.update_actions)
        self.table.setContextMenuPolicy(Qt.ContextMenuPolicy.CustomContextMenu)
        self.table.customContextMenuRequested.connect(self.context_menu)

        self.empty = helper_label("", self)
        self.empty.setFont(self.font())
        self.empty.setAlignment(Qt.AlignmentFlag.AlignCenter)
        self.stack = QStackedWidget(self)
        self.stack.addWidget(self.table)
        self.stack.addWidget(self.empty)
        layout.addWidget(self.stack, 1)

        layout.addLayout(self._paging_row())
        layout.addWidget(self._buttons())
        self.update_actions()

    def _filters(self) -> QGridLayout:
        grid = QGridLayout()
        self.search = QLineEdit(self)
        self.search.setPlaceholderText("Search by title, author, summary, publisher or category")
        self.search.setClearButtonEnabled(True)
        self.language = QComboBox(self)
        self.category = QComboBox(self)
        self.language.addItem("All languages", None)
        self.category.addItem("All categories", None)
        # (widget, the short line under it, the fuller tooltip)
        fields = (
            (
                self.search,
                "Every word must match. Press Enter to search at once.",
                "Every word must match. Press Enter to search right away.",
            ),
            (self.language, "Filter by language.", "Show only books in this language."),
            (self.category, "Filter by category.", "Show only books in this category."),
        )
        for column, (widget, help_text, tooltip) in enumerate(fields):
            widget.setToolTip(tooltip)
            grid.addWidget(widget, 0, column)
            helper = helper_label(help_text, self)
            helper.setToolTip(tooltip)
            grid.addWidget(helper, 1, column, Qt.AlignmentFlag.AlignTop)
        grid.setColumnStretch(0, 3)
        grid.setColumnStretch(1, 1)
        grid.setColumnStretch(2, 1)

        self.debounce = QTimer(self)
        self.debounce.setSingleShot(True)
        self.debounce.setInterval(SEARCH_DELAY_MS)
        self.debounce.timeout.connect(self.run_search)
        self.search.textChanged.connect(lambda _text: self.debounce.start())
        self.search.returnPressed.connect(self.run_search)
        self.language.currentIndexChanged.connect(self.filters_changed)
        self.category.currentIndexChanged.connect(self.filters_changed)
        return grid

    def _status_row(self) -> QHBoxLayout:
        row = QHBoxLayout()
        self.status = QLabel(self)
        self.status.setWordWrap(True)
        self.status.setTextInteractionFlags(Qt.TextInteractionFlag.TextSelectableByMouse)
        self.spinner = QProgressBar(self)
        self.spinner.setRange(0, 0)
        self.spinner.setMaximumWidth(120)
        self.spinner.setMaximumHeight(12)
        self.spinner.setTextVisible(False)
        self.spinner.hide()
        row.addWidget(self.status, 1)
        row.addWidget(self.spinner)
        return row

    def _paging_row(self) -> QHBoxLayout:
        row = QHBoxLayout()
        self.count = helper_label("", self)
        self.more_button = QPushButton("Load more", self)
        self.more_button.setToolTip("Show the next page of results.")
        self.more_button.clicked.connect(self.load_more)
        row.addWidget(self.count, 1)
        row.addWidget(self.more_button)
        return row

    def _buttons(self) -> QDialogButtonBox:
        bb = QDialogButtonBox(self)
        role = QDialogButtonBox.ButtonRole.ActionRole
        self.download_button = bb.addButton("Download to library", role)
        self.download_button.setToolTip("Add the selected books to this Calibre library.")
        self.download_button.clicked.connect(self.download)
        self.shelve_button = bb.addButton("Add to shelf", role)
        self.shelve_button.setToolTip("Keep the selected books on your shelf in the catalog.")
        self.shelve_button.clicked.connect(self.add_to_shelf)
        self.unshelve_button = bb.addButton("Remove from shelf", role)
        self.unshelve_button.setToolTip("Take the selected books off your shelf. Nothing is deleted.")
        self.unshelve_button.clicked.connect(self.remove_from_shelf)
        self.settings_button = bb.addButton("Settings…", role)
        self.settings_button.setToolTip("Server, sign-in and catalog.")
        self.settings_button.clicked.connect(self.open_settings)
        close = bb.addButton(QDialogButtonBox.StandardButton.Close)
        close.clicked.connect(self.reject)
        return bb

    # ----------------------------------------------------------------- state

    def selected_entries(self) -> list[EntryRef]:
        rows = sorted({index.row() for index in self.table.selectionModel().selectedRows()})
        return [self.model.entries[row] for row in rows]

    def update_actions(self, *_args: Any) -> None:
        selected = self.selected_entries()
        idle = not self.busy and self.client is not None
        self.download_button.setEnabled(self._pulls and idle and any(not self.model.in_library(e) for e in selected))
        self.download_button.setToolTip(
            "Add the selected books to this Calibre library."
            if self._pulls
            else "Sync is set to Upload only, which never changes your library. Change it in Settings to download."
        )
        self.shelve_button.setEnabled(idle and any(e.shelf_record_id is None for e in selected))
        self.unshelve_button.setEnabled(idle and any(e.shelf_record_id is not None for e in selected))
        self.more_button.setVisible(self.page < self.pages)
        self.more_button.setEnabled(idle)

    @property
    def busy(self) -> bool:
        return self._depth > 0

    def set_busy(self, busy: bool, message: str = "") -> None:
        """Count running requests, so a search that ends mid-download does not re-enable Download."""
        self._depth = self._depth + 1 if busy else max(0, self._depth - 1)
        self.spinner.setVisible(self.busy)
        if busy:
            self.show_status(message)
        self.update_actions()

    def show_status(self, message: str, *, error: bool = False) -> None:
        self.status.setText(message)
        self.status.setStyleSheet(error_style() if error else "")

    def show_results_or_empty(self) -> None:
        if self.model.entries:
            self.stack.setCurrentWidget(self.table)
            self.count.setText(f"Showing {len(self.model.entries)} of {self.total}")
            return
        query = self.search.text().strip()
        filtered = self.language.currentData() is not None or self.category.currentData() is not None
        if query:
            text = f"No books match “{query}”."
        elif filtered:
            text = "No books match these filters."
        else:
            text = "This catalog has no books yet."
        self.empty.setText(text + (" Try fewer words or other filters." if query or filtered else ""))
        self.stack.setCurrentWidget(self.empty)
        self.count.setText("")

    def theme_changed(self) -> None:
        self.model.placeholder = _placeholder(self.palette())
        self.table.viewport().update()

    # ------------------------------------------------------------- threading

    def run_task(self, work: Callable[[], Any], done: Callable[[Any], None]) -> Task:
        """Run ``work`` off the GUI thread; ``done`` (a bound method) gets the result, a failure shows inline."""
        self._tasks = {task for task in self._tasks if not task.isFinished()}
        task = run_task(work, on_success=done, on_failure=self.on_failed)
        self._tasks.add(task)
        return task

    def on_failed(self, message: str) -> None:
        if self._closed:
            return
        self.set_busy(False)
        self.show_status(message, error=True)

    # ------------------------------------------------------------ connecting

    def connect_catalog(self) -> None:
        settings = Settings.load()
        missing = settings.missing()
        self.client = None
        self._pulls = settings.direction.pulls
        if missing:
            self.show_status("Add the server, API key and catalog in Settings first.", error=True)
            self.model.set_entries([])
            self.update_actions()
            return
        self.set_busy(True, f"Connecting to {host_of(settings.base_url)}…")

        def work() -> tuple[CatalogClient, CatalogRef, dict[UUID, str]]:
            client, catalog = connect(settings, timeout=30, retries=1)
            try:
                labels = client.list_categories(catalog.id)
            except CatalogError as exc:  # the browser still works, with unnamed categories
                log.warning(f"categories not loaded: {exc}")
                labels = {}
            return client, catalog, labels

        self.run_task(work, self.on_connected)

    def on_connected(self, result: tuple[CatalogClient, CatalogRef, dict[UUID, str]]) -> None:
        if self._closed:  # the window closed while connecting: start no loader and no search
            return
        self.client, self.catalog, self.category_labels = result
        settings = Settings.load()
        self.header.set_title(f"Browse {self.catalog.title}")
        self.header.set_subtitle(f"Find books on {host_of(settings.base_url)} and bring them into Calibre.")
        self.refresh_local_ids()
        self.start_thumbnails(settings)
        self.set_busy(False)
        self.run_search()

    def start_thumbnails(self, settings: Settings) -> None:
        self.stop_thumbnails()
        # A client of its own: the search client is used from other threads at the same time.
        # Short timeout, no retries: closing the window waits for the thumbnail in flight.
        client = CatalogClient(settings.base_url, settings.token, timeout=10, retries=0, user_agent=user_agent())
        self._thumbnails = ThumbnailLoader(client)
        self._thumbnails.loaded.connect(self.on_thumbnail)
        self._requested.clear()
        start_thread(self._thumbnails)

    def stop_thumbnails(self) -> None:
        """Stop without waiting: at most the thumbnail in flight still finishes, and is dropped."""
        if self._thumbnails is not None:
            disconnect_all(self._thumbnails.loaded)
            self._thumbnails.stop()
            self._thumbnails = None

    def refresh_local_ids(self) -> None:
        catalog_id = self.catalog.id if self.catalog is not None else None
        self.model.set_local_ids(local_entry_ids(self.gui.current_db.new_api, catalog_id))

    def settings_changed(self) -> None:
        """Reconnect after the user saved new settings (call it from the action's apply_settings)."""
        self.connect_catalog()

    # --------------------------------------------------------------- search

    def filters(self) -> dict[str, Any]:
        query = self.search.text().strip()
        category = self.category.currentData()  # kept as str: QComboBox.findData compares plain values only
        return {
            "query": query,
            "language_code": self.language.currentData(),
            "category_id": UUID(category) if category else None,
            # The server ranks by relevance only when there is a query; otherwise keep a stable order.
            "order_by": None if query else "title",
        }

    def filters_changed(self, *_args: Any) -> None:
        if not self._filling_combos:
            self.run_search()

    def run_search(self) -> None:
        self.debounce.stop()
        self.fetch_page(1)

    def load_more(self) -> None:
        if self.page < self.pages:
            self.fetch_page(self.page + 1)

    def fetch_page(self, page: int) -> None:
        if self.client is None or self.catalog is None:
            return
        self._generation += 1
        generation, client, catalog_id, filters = self._generation, self.client, self.catalog.id, self.filters()
        facet_filters = {key: value for key, value in filters.items() if key != "order_by"}

        def work() -> tuple[int, EntryPage, Facets | None]:
            result = client.search_entries(catalog_id, page=page, limit=PAGE_SIZE, **filters)
            facets = client.facets(catalog_id, **facet_filters) if page == 1 else None
            return generation, result, facets

        self.set_busy(True, "Searching…" if page == 1 else "Loading more…")
        self.run_task(work, self.on_page)

    def on_page(self, result: tuple[int, EntryPage, Facets | None]) -> None:
        generation, page, facets = result
        if self._closed:
            return
        self.set_busy(False)
        if generation != self._generation:
            return  # a newer search has started; this answer is stale
        if page.page == 1:
            self.model.set_entries(page.items)
        else:
            self.model.append(page.items)
        self.page, self.pages, self.total = page.page, page.pages, page.total
        if facets is not None:
            self.fill_facets(facets)
        self.request_thumbnails(page.items)
        if not self.busy:
            self.show_status("")
        self.update_actions()
        self.show_results_or_empty()

    def fill_facets(self, facets: Facets) -> None:
        languages = [(code, f"{language_name(code)} ({count})") for code, count in facets.languages]
        categories = [
            (str(category_id), f"{self.category_labels.get(category_id, 'Unnamed category')} ({count})")
            for category_id, count in facets.categories
        ]
        categories.sort(key=lambda item: item[1].casefold())
        self._filling_combos = True
        try:
            _refill(self.language, "All languages", languages)
            _refill(self.category, "All categories", categories)
        finally:
            self._filling_combos = False

    # ------------------------------------------------------------ thumbnails

    def request_thumbnails(self, entries: Iterable[EntryRef]) -> None:
        if self._thumbnails is None:
            return
        for entry in entries:
            key = str(entry.id)
            if entry.has_cover and key not in self._requested:
                self._requested.add(key)
                self._thumbnails.request(entry)

    def on_thumbnail(self, entry_id: str, path: str) -> None:
        # QPixmap must be built on the GUI thread, so the loader only hands over a path.
        icon = _cover_icon(path) if path else None
        if icon is not None:
            self.model.set_cover(entry_id, icon)

    # --------------------------------------------------------------- actions

    def download(self) -> None:
        if not self._pulls:
            return
        entries = [entry for entry in self.selected_entries() if not self.model.in_library(entry)]
        if not entries or self.client is None or self.catalog is None:
            return
        db, client, catalog, abort = self.gui.current_db.new_api, self.client, self.catalog, self._abort
        noun = "book" if len(entries) == 1 else "books"
        self.set_busy(True, f"Downloading {len(entries)} {noun}…")
        self.run_task(lambda: _download(db, client, catalog, entries, abort), self.on_downloaded)

    def on_downloaded(self, result: tuple[Report, int]) -> None:
        if self._closed:
            # Closed meanwhile (perhaps for a library switch): this report belongs to the
            # library the download started in, so it must not touch the book list now.
            return
        report, already_here = result
        self.set_busy(False)
        refresh_library(self.gui, report)
        self.refresh_local_ids()
        done = report.count(Action.PULL)
        noun = "book" if done == 1 else "books"
        skipped = ""
        if already_here:
            skipped = f" {already_here} {'was' if already_here == 1 else 'were'} already in your library."
        if report.failed:
            reasons = "; ".join(f"{o.item.title}: {o.message}" for o in report.failed)
            self.show_status(f"Downloaded {done}, {len(report.failed)} failed. {reasons}{skipped}", error=True)
        elif done == 0 and already_here:
            self.show_status(f"Nothing to download:{skipped}")
        else:
            self.show_status(f"Downloaded {done} {noun} to your library.{skipped}")
        self.update_actions()

    def add_to_shelf(self) -> None:
        entries = [entry for entry in self.selected_entries() if entry.shelf_record_id is None]
        if not entries or self.client is None:
            return
        client = self.client

        def work() -> list[EntryRef]:
            return [replace(entry, shelf_record_id=client.add_to_shelf(entry.id).id) for entry in entries]

        self.set_busy(True, "Adding to your shelf…")
        self.run_task(work, self.on_shelf_changed)

    def remove_from_shelf(self) -> None:
        entries = [entry for entry in self.selected_entries() if entry.shelf_record_id is not None]
        if not entries or self.client is None:
            return
        client = self.client

        def work() -> list[EntryRef]:
            for entry in entries:
                client.remove_from_shelf(entry.shelf_record_id)
            return [replace(entry, shelf_record_id=None) for entry in entries]

        self.set_busy(True, "Removing from your shelf…")
        self.run_task(work, self.on_shelf_changed)

    def on_shelf_changed(self, entries: list[EntryRef]) -> None:
        if self._closed:
            return
        for entry in entries:
            self.model.replace_entry(entry)
        self.set_busy(False)
        self.show_status("Shelf updated.")

    def context_menu(self, pos: Any) -> None:
        if not self.selected_entries():
            return
        menu = QMenu(self)
        for button in (self.download_button, self.shelve_button, self.unshelve_button):
            item = QAction(button.text(), menu)
            item.setEnabled(button.isEnabled())
            item.triggered.connect(button.click)
            menu.addAction(item)
        menu.exec(self.table.viewport().mapToGlobal(pos))

    def open_settings(self) -> None:
        self.action.show_settings(self)

    # ------------------------------------------------------------- lifecycle

    def showEvent(self, event: Any) -> None:
        super().showEvent(event)
        if self._closed:  # shown again after a close: start over
            self._closed, self._abort, self._depth, self.client = False, threading.Event(), 0, None
        if self.client is None and not self.busy:
            self.connect_catalog()

    def changeEvent(self, event: Any) -> None:
        super().changeEvent(event)
        # Back from the sync window or the library: books may have arrived since, so
        # re-read which entries are here (download() checks again on its own thread).
        if (
            event.type() == QEvent.Type.ActivationChange
            and self.isActiveWindow()
            and self.catalog is not None
            and not self._closed
        ):
            self.refresh_local_ids()
            self.update_actions()

    def reject(self) -> None:
        """Close at once, never waiting on the network.

        Running requests are not this window's children (worker.start_thread holds
        them), so they finish on their own; their answers are dropped. A download stops
        after the book it is on.
        """
        self._closed = True
        self.debounce.stop()
        self._generation += 1
        self._abort.set()
        self.stop_thumbnails()
        for task in self._tasks:
            task.disconnect_all()
        self._tasks.clear()
        super().reject()


def refresh_library(gui: Any, report: Report) -> None:
    """Show new and changed books in Calibre's book list (same steps as the sync window)."""
    created = report.created_book_ids
    changed = report.touched_book_ids - created
    if created:
        # new_api.add_books does not touch the legacy view's id map; register the ids first.
        gui.current_db.data.books_added(sorted(created))
        gui.iactions["Add Books"].refresh_gui(len(created), recount=True)
    if changed:
        gui.library_view.model().refresh_ids(list(changed))
        gui.tags_view.recount()


def _refill(combo: QComboBox, everything: str, items: list[tuple[Any, str]]) -> None:
    """Replace a filter's choices with fresh counts, keeping the current choice selected."""
    current, current_text = combo.currentData(), combo.currentText()
    combo.clear()
    combo.addItem(everything, None)
    for value, text in items:
        combo.addItem(text, value)
    if current is not None:
        index = combo.findData(current)
        if index < 0:  # no match left, but it stays the active filter
            combo.addItem(current_text.rsplit(" (", 1)[0] + " (0)", current)
            index = combo.count() - 1
        combo.setCurrentIndex(index)
