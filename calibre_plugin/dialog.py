"""The sync window: scan, review what will happen to each book, adjust, synchronise."""

from __future__ import annotations

from typing import Any

from calibre.gui2 import error_dialog
from calibre.gui2.widgets2 import Dialog
from qt.core import (
    QAbstractItemView,
    QAbstractTableModel,
    QCheckBox,
    QColor,
    QComboBox,
    QDialogButtonBox,
    QHBoxLayout,
    QHeaderView,
    QLabel,
    QMenu,
    QModelIndex,
    QProgressBar,
    QPushButton,
    QSortFilterProxyModel,
    QStyledItemDelegate,
    Qt,
    QTableView,
    QVBoxLayout,
)

from .settings import Settings, prefs
from .sync.engine import Report
from .sync.plan import Action, PlanItem, State
from .worker import SyncWorker

COLUMNS = ("Title", "Authors", "Status", "Action", "Result")
ACTION_COLUMN = 3
STATE_COLORS = {
    State.CONFLICT: QColor(200, 120, 0),
    State.UNLINKED: QColor(200, 120, 0),
    State.LOCAL_DELETED: QColor(128, 128, 128),
    State.REMOTE_DELETED: QColor(128, 128, 128),
}


class PlanModel(QAbstractTableModel):
    def __init__(self, parent: Any = None) -> None:
        super().__init__(parent)
        self.items: list[PlanItem] = []
        self.results: dict[int, tuple[bool, str]] = {}

    def set_items(self, items: list[PlanItem]) -> None:
        self.beginResetModel()
        self.items = sorted(items, key=lambda i: (i.state is State.IN_SYNC, i.state.name, i.title.casefold()))
        self.results = {}
        self.endResetModel()

    def set_action(self, row: int, action: Action) -> None:
        item = self.items[row]
        if action in item.choices and action is not item.action:
            self.items[row] = item.with_action(action)
            index = self.index(row, ACTION_COLUMN)
            self.dataChanged.emit(index, index)

    def set_report(self, report: Report) -> None:
        rows = {id(item): row for row, item in enumerate(self.items)}
        for outcome in report.outcomes:
            row = rows.get(id(outcome.item))
            if row is not None:
                self.results[row] = (outcome.ok, "done" if outcome.ok else outcome.message)
        self.dataChanged.emit(self.index(0, 4), self.index(len(self.items) - 1, 4))

    def rowCount(self, parent: QModelIndex | None = None) -> int:
        return 0 if parent is not None and parent.isValid() else len(self.items)

    def columnCount(self, parent: QModelIndex | None = None) -> int:
        return 0 if parent is not None and parent.isValid() else len(COLUMNS)

    def headerData(self, section: int, orientation: Qt.Orientation, role: int = Qt.ItemDataRole.DisplayRole) -> Any:
        if role == Qt.ItemDataRole.DisplayRole and orientation == Qt.Orientation.Horizontal:
            return COLUMNS[section]
        return None

    def flags(self, index: QModelIndex) -> Qt.ItemFlag:
        flags = super().flags(index)
        if index.column() == ACTION_COLUMN and len(self.items[index.row()].choices) > 1:
            flags |= Qt.ItemFlag.ItemIsEditable
        return flags

    def data(self, index: QModelIndex, role: int = Qt.ItemDataRole.DisplayRole) -> Any:
        if not index.isValid():
            return None
        item, column = self.items[index.row()], index.column()
        if role == Qt.ItemDataRole.DisplayRole:
            if column == 0:
                return item.title
            if column == 1:
                return " & ".join(item.authors)
            if column == 2:
                return item.state.value
            if column == ACTION_COLUMN:
                return item.action.value
            if column == 4:
                result = self.results.get(index.row())
                return result[1] if result else item.note
        if role == Qt.ItemDataRole.ToolTipRole and column == 4:
            result = self.results.get(index.row())
            return result[1] if result else item.note or None
        if role == Qt.ItemDataRole.ForegroundRole:
            if column == 4 and index.row() in self.results and not self.results[index.row()][0]:
                return QColor(200, 0, 0)
            if column == 2:
                return STATE_COLORS.get(item.state)
        if role == Qt.ItemDataRole.UserRole:
            return item
        return None


class InSyncFilter(QSortFilterProxyModel):
    def __init__(self, parent: Any = None) -> None:
        super().__init__(parent)
        self.show_in_sync = False

    def set_show_in_sync(self, show: bool) -> None:
        self.show_in_sync = show
        self.invalidateFilter()

    def filterAcceptsRow(self, source_row: int, source_parent: QModelIndex) -> bool:
        if self.show_in_sync:
            return True
        model = self.sourceModel()
        return model.items[source_row].state is not State.IN_SYNC or source_row in model.results


class ActionDelegate(QStyledItemDelegate):
    def createEditor(self, parent: Any, option: Any, index: QModelIndex) -> QComboBox:
        editor = QComboBox(parent)
        item: PlanItem = index.data(Qt.ItemDataRole.UserRole)
        for action in item.choices:
            editor.addItem(action.value, action)
        editor.activated.connect(lambda _index: self.commitData.emit(editor))
        return editor

    def setEditorData(self, editor: QComboBox, index: QModelIndex) -> None:
        item: PlanItem = index.data(Qt.ItemDataRole.UserRole)
        editor.setCurrentIndex(max(0, editor.findData(item.action)))

    def setModelData(self, editor: QComboBox, model: Any, index: QModelIndex) -> None:
        source = model.mapToSource(index)
        model.sourceModel().set_action(source.row(), editor.currentData())


class SyncDialog(Dialog):
    def __init__(self, gui: Any, icon: Any, action: Any) -> None:
        self.gui = gui
        self.action = action
        self.worker: SyncWorker | None = None
        self.catalog: Any = None
        super().__init__("EvilFlowers sync", "evilflowers-sync-dialog", parent=gui)
        self.setWindowIcon(icon)
        self.setWindowFlag(Qt.WindowType.WindowMinMaxButtonsHint)

    def sizeHint(self) -> Any:
        hint = super().sizeHint()
        hint.setWidth(max(hint.width(), 1000))
        hint.setHeight(max(hint.height(), 650))
        return hint

    # ---------------------------------------------------------------- layout

    def setup_ui(self) -> None:
        layout = QVBoxLayout(self)
        self.header = QLabel(self)
        self.header.setTextFormat(Qt.TextFormat.RichText)
        layout.addWidget(self.header)

        self.progress = QProgressBar(self)
        self.progress.setRange(0, 1000)
        self.progress.setTextVisible(True)
        layout.addWidget(self.progress)

        self.model = PlanModel(self)
        self.proxy = InSyncFilter(self)
        self.proxy.setSourceModel(self.model)
        self.table = QTableView(self)
        self.table.setModel(self.proxy)
        self.table.setItemDelegateForColumn(ACTION_COLUMN, ActionDelegate(self.table))
        self.table.setSelectionBehavior(QAbstractItemView.SelectionBehavior.SelectRows)
        self.table.setEditTriggers(QAbstractItemView.EditTrigger.AllEditTriggers)
        self.table.setAlternatingRowColors(True)
        self.table.verticalHeader().hide()
        header = self.table.horizontalHeader()
        header.setSectionResizeMode(0, QHeaderView.ResizeMode.Stretch)
        for column in range(1, len(COLUMNS)):
            header.setSectionResizeMode(column, QHeaderView.ResizeMode.ResizeToContents)
        self.table.setContextMenuPolicy(Qt.ContextMenuPolicy.CustomContextMenu)
        self.table.customContextMenuRequested.connect(self.context_menu)
        layout.addWidget(self.table, 1)

        row = QHBoxLayout()
        self.show_in_sync = QCheckBox("Show books that are in sync", self)
        self.show_in_sync.setChecked(bool(prefs["show_in_sync"]))
        self.proxy.set_show_in_sync(self.show_in_sync.isChecked())
        self.show_in_sync.toggled.connect(self.toggle_in_sync)
        row.addWidget(self.show_in_sync)
        row.addStretch(1)
        self.summary = QLabel(self)
        row.addWidget(self.summary)
        layout.addLayout(row)

        self.bb = QDialogButtonBox(self)
        self.settings_button = self.bb.addButton("Settings…", QDialogButtonBox.ButtonRole.ActionRole)
        self.settings_button.clicked.connect(self.open_settings)
        self.scan_button = self.bb.addButton("Rescan", QDialogButtonBox.ButtonRole.ActionRole)
        self.scan_button.clicked.connect(self.scan)
        self.sync_button: QPushButton = self.bb.addButton("Synchronize", QDialogButtonBox.ButtonRole.AcceptRole)
        self.sync_button.setDefault(True)
        self.sync_button.clicked.connect(self.synchronize)
        self.stop_button = self.bb.addButton("Stop", QDialogButtonBox.ButtonRole.ActionRole)
        self.stop_button.clicked.connect(self.stop)
        close = self.bb.addButton(QDialogButtonBox.StandardButton.Close)
        close.clicked.connect(self.reject)
        layout.addWidget(self.bb)
        self.model.dataChanged.connect(self.update_summary)
        self.model.modelReset.connect(self.update_summary)
        self.set_busy(False)

    # ----------------------------------------------------------------- state

    def set_busy(self, busy: bool) -> None:
        for widget in (self.settings_button, self.scan_button, self.sync_button, self.table):
            widget.setEnabled(not busy)
        self.stop_button.setVisible(busy)
        self.stop_button.setEnabled(busy)
        if busy:
            self.progress.setValue(0)
        self.update_summary()

    def update_summary(self, *_args: Any) -> None:
        counts = {action: 0 for action in Action}
        for item in self.model.items:
            counts[item.action] += 1
        conflicts = sum(1 for item in self.model.items if item.state in (State.CONFLICT, State.UNLINKED))
        text = f"{counts[Action.PUSH]} to upload, {counts[Action.PULL]} to download"
        if conflicts:
            text += f", {conflicts} need a decision"
        self.summary.setText(text)
        self.sync_button.setEnabled(self.worker is None and bool(counts[Action.PUSH] or counts[Action.PULL]))

    def toggle_in_sync(self, show: bool) -> None:
        prefs["show_in_sync"] = show
        self.proxy.set_show_in_sync(show)

    def on_progress(self, fraction: float, message: str) -> None:
        self.progress.setValue(int(fraction * 1000))
        self.progress.setFormat(message)

    def settings_changed(self) -> None:
        if self.worker is None:
            self.scan()

    # --------------------------------------------------------------- actions

    def start(self, worker: SyncWorker) -> None:
        self.worker = worker
        worker.progress.connect(self.on_progress)
        worker.failed.connect(self.on_failed)
        worker.finished.connect(self.on_finished)
        self.set_busy(True)
        worker.start()

    def scan(self) -> None:
        settings = Settings.load()
        missing = settings.missing()
        self.header.setText(f"<b>{settings.catalog or '—'}</b> on {settings.base_url or '—'}")
        if missing:
            self.on_progress(0, f"Set {', '.join(missing)} in Settings first")
            self.model.set_items([])
            return
        worker = SyncWorker(self.gui.current_db.new_api, settings, parent=self)
        worker.scanned.connect(self.on_scanned)
        self.start(worker)

    def synchronize(self) -> None:
        items = [item for item in self.model.items if item.action is not Action.SKIP]
        if not items:
            return
        worker = SyncWorker(self.gui.current_db.new_api, Settings.load(), items=list(self.model.items), parent=self)
        worker.applied.connect(self.on_applied)
        self.start(worker)

    def stop(self) -> None:
        if self.worker is not None:
            self.worker.abort()
            self.stop_button.setEnabled(False)
            self.on_progress(self.progress.value() / 1000, "Stopping after the current book…")

    def on_scanned(self, items: list[PlanItem], catalog: Any) -> None:
        self.catalog = catalog
        settings = Settings.load()
        self.header.setText(f"<b>{catalog.title}</b> ({catalog.url_name}) on {settings.base_url}")
        self.model.set_items(items)

    def on_applied(self, report: Report) -> None:
        self.model.set_report(report)
        self.refresh_library(report)
        self.on_progress(1.0, report.summary())
        if report.failed:
            error_dialog(
                self,
                "Some books were not synchronised",
                f"{len(report.failed)} book(s) failed; see the Result column. The others were synchronised.",
                det_msg="\n".join(f"{o.item.title}: {o.message}" for o in report.failed),
                show=True,
            )

    def on_failed(self, message: str) -> None:
        self.on_progress(0, message)
        error_dialog(self, "EvilFlowers", message, show=True)

    def on_finished(self) -> None:
        worker, self.worker = self.worker, None
        if worker is not None:
            worker.deleteLater()
        self.set_busy(False)

    def refresh_library(self, report: Report) -> None:
        created = report.created_book_ids
        changed = report.touched_book_ids - created
        if created:
            # new_api.add_books does not touch the legacy view's id map, and the books
            # model only inserts rows for ids already in it; Calibre's own add action
            # registers the ids first in the same way.
            self.gui.current_db.data.books_added(sorted(created))
            self.gui.iactions["Add Books"].refresh_gui(len(created), recount=True)
        if changed:
            self.gui.library_view.model().refresh_ids(list(changed))
            self.gui.tags_view.recount()

    def context_menu(self, pos: Any) -> None:
        rows = sorted({self.proxy.mapToSource(index).row() for index in self.table.selectionModel().selectedRows()})
        if not rows:
            return
        menu = QMenu(self)
        for action in (Action.PUSH, Action.PULL, Action.SKIP):
            allowed = [row for row in rows if action in self.model.items[row].choices]
            entry = menu.addAction(f"{action.value.capitalize()} {len(allowed)} selected")
            entry.setEnabled(bool(allowed))
            entry.triggered.connect(lambda _=False, a=action, r=allowed: [self.model.set_action(row, a) for row in r])
        menu.exec(self.table.viewport().mapToGlobal(pos))

    def open_settings(self) -> None:
        self.action.show_settings(self)

    # -------------------------------------------------------------- lifecycle

    def showEvent(self, event: Any) -> None:
        super().showEvent(event)
        if not self.model.items and self.worker is None:
            self.scan()

    def reject(self) -> None:
        if self.worker is not None:
            self.worker.abort()
            self.worker.wait(30_000)
        super().reject()
