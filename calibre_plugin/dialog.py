"""The sync window: scan, review what will happen to each book, adjust, synchronise.

Top to bottom: the header banner, an inline error banner, the count chips that filter
the table, then one page out of three (a message such as "set up" or "in sync", the
scanning indicator, or the plan table with its conflict panel), the progress row and
the buttons. Colours come from :mod:`theme`, so the window follows Calibre's theme.
"""

from __future__ import annotations

import contextlib
import shutil
import tempfile
from collections.abc import Callable
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from calibre.gui2 import error_dialog
from calibre.gui2.widgets2 import Dialog
from qt.core import (
    QAbstractItemView,
    QAbstractTableModel,
    QApplication,
    QButtonGroup,
    QColor,
    QComboBox,
    QDialogButtonBox,
    QFont,
    QFrame,
    QGridLayout,
    QHBoxLayout,
    QHeaderView,
    QIcon,
    QImage,
    QLabel,
    QMenu,
    QModelIndex,
    QPainter,
    QPalette,
    QPen,
    QPixmap,
    QPointF,
    QProgressBar,
    QPushButton,
    QRect,
    QRectF,
    QScrollArea,
    QSize,
    QSizePolicy,
    QSortFilterProxyModel,
    QSplitter,
    QStackedWidget,
    QStyle,
    QStyledItemDelegate,
    QStyleOptionViewItem,
    Qt,
    QTableView,
    QVBoxLayout,
    QWidget,
    pyqtSignal,
)

from . import theme
from .login import host_of
from .settings import Settings, prefs
from .sync.engine import Report
from .sync.local import LocalBook
from .sync.mapping import language_code, published_at
from .sync.plan import Action, PlanItem, State
from .worker import CoverLoader, SyncWorker, Task, client_for, disconnect_all, run_task, start_thread

COLUMNS = ("", "Book", "Status", "Action", "Result")
COVER_COLUMN, BOOK_COLUMN, STATUS_COLUMN, ACTION_COLUMN, RESULT_COLUMN = range(len(COLUMNS))
COVER_SIZE = QSize(30, 44)
ROW_HEIGHT = COVER_SIZE.height() + 8
DECISION_STATES = frozenset({State.CONFLICT, State.UNLINKED})

# What each state means, in the words the window uses: a short phrase and a tooltip.
STATUS_TEXT: dict[State, tuple[str, str]] = {
    State.IN_SYNC: ("In sync", "Nothing changed on either side since the last sync."),
    State.LOCAL_ONLY: ("New in Calibre", "Only in this library. Upload adds it to the catalog."),
    State.REMOTE_ONLY: ("New in the catalog", "Only in the catalog. Download adds it to this library."),
    State.LOCAL_CHANGED: ("Changed in Calibre", "Edited here since the last sync. Upload sends the changes."),
    State.REMOTE_CHANGED: ("Changed in the catalog", "Edited in the catalog since the last sync. Download gets it."),
    State.CONFLICT: ("Changed on both sides", "Edited here and in the catalog. Pick the version to keep."),
    State.UNLINKED: ("On both sides, never synced", "The same book is here and in the catalog. Pick one to keep."),
    State.LOCAL_DELETED: ("Deleted from Calibre", "Removed here after a sync. Download restores it."),
    State.REMOTE_DELETED: ("Deleted from the catalog", "Removed from the catalog after a sync. Upload adds it back."),
}
# Rows that need a decision come first, books in sync last.
STATE_ORDER = {
    state: rank
    for rank, state in enumerate(
        (
            State.CONFLICT,
            State.UNLINKED,
            State.LOCAL_ONLY,
            State.LOCAL_CHANGED,
            State.REMOTE_ONLY,
            State.REMOTE_CHANGED,
            State.LOCAL_DELETED,
            State.REMOTE_DELETED,
            State.IN_SYNC,
        )
    )
}
ACTION_TEXT = {Action.PUSH: "Upload", Action.PULL: "Download", Action.SKIP: "Skip"}
ACTION_KINDS = {Action.PUSH: "upload", Action.PULL: "download", Action.SKIP: "skip"}
ACTION_HELP = {
    Action.PUSH: "Send Calibre's version to the catalog.",
    Action.PULL: "Bring the catalog's version into Calibre.",
    Action.SKIP: "Leave this book alone this time.",
}


# ------------------------------------------------------------------------------- filters


@dataclass(frozen=True, slots=True)
class FilterChip:
    key: str
    kind: str  # a theme chip kind
    tooltip: str

    def label(self, count: int) -> str:
        if self.key == "all":
            return f"All {count}"
        if self.key == "pending":
            return f"{count} to review"
        if self.key == "upload":
            return f"{count} to upload"
        if self.key == "download":
            return f"{count} to download"
        if self.key == "decision":
            return f"{count} {'needs' if count == 1 else 'need'} a decision"
        return f"{count} in sync"


FILTERS = (
    FilterChip("pending", "changed", "Every book that is not in sync."),
    FilterChip("upload", "upload", "Books that will be sent to the catalog."),
    FilterChip("download", "download", "Books that will be brought into Calibre."),
    FilterChip("decision", "conflict", "Books changed on both sides, or never synced: pick a version."),
    FilterChip("in_sync", "in_sync", "Books that match the catalog."),
    FilterChip("all", "skip", "Every book in the library and the catalog."),
)


def matches(key: str, item: PlanItem) -> bool:
    """Whether a row belongs to the chip `key`."""
    if key == "pending":
        return item.state is not State.IN_SYNC
    if key == "upload":
        return item.action is Action.PUSH
    if key == "download":
        return item.action is Action.PULL
    if key == "decision":
        return item.state in DECISION_STATES
    if key == "in_sync":
        return item.state is State.IN_SYNC
    return True


def filter_counts(items: list[PlanItem]) -> dict[str, int]:
    return {chip.key: sum(1 for item in items if matches(chip.key, item)) for chip in FILTERS}


def summary_text(items: list[PlanItem]) -> str:
    counts = filter_counts(items)
    text = f"{counts['upload']} to upload, {counts['download']} to download"
    if counts["decision"]:
        decision = next(chip for chip in FILTERS if chip.key == "decision")
        text += f", {decision.label(counts['decision'])}"
    return text


# --------------------------------------------------------------------------- comparison


@dataclass(frozen=True, slots=True)
class FieldDiff:
    label: str
    calibre: str
    catalog: str


def _joined(values: Any) -> str:
    return ", ".join(sorted({str(v) for v in values or () if v}, key=str.casefold))


def _language_name(code: str | None) -> str:
    if not code:
        return ""
    try:
        from calibre.utils.localization import calibre_langcode_to_name, canonicalize_lang

        lang = canonicalize_lang(code)
        return calibre_langcode_to_name(lang) if lang else code
    except Exception:
        return code


def compare(mi: Any, book: LocalBook, entry: Any, *, detailed: bool) -> list[FieldDiff]:
    """The synced fields that differ between a Calibre book and its catalog entry.

    ``mi`` is the book's Calibre Metadata. The catalog's list response carries no
    publisher or date, so those two are compared only when ``detailed`` (``entry`` came
    from the detail endpoint); otherwise they would look empty in the catalog.
    """
    pairs = [
        ("Title", mi.title or "", entry.title or ""),
        (
            "Authors",
            " & ".join(a for a in mi.authors or () if a),
            " & ".join(p.full_name for p in entry.authors if p.full_name),
        ),
        ("Tags", _joined(mi.tags), _joined(entry.categories)),
        ("Language", _language_name(language_code(mi, "")), _language_name(entry.language_code)),
    ]
    if detailed:
        pairs += [
            ("Publisher", mi.publisher or "", entry.publisher or ""),
            ("Published", published_at(mi) or "", (entry.published_at or "")[:10]),
        ]
    pairs.append(
        (
            "Formats",
            _joined(book.syncable_formats),
            _joined(acq.calibre_format for acq in entry.acquisitions),
        )
    )
    return [FieldDiff(*pair) for pair in pairs if pair[1].strip().casefold() != pair[2].strip().casefold()]


# ------------------------------------------------------------------------------- drawing


def app_palette() -> QPalette:
    """The application's palette. Widgets' own palettes pick up the stylesheets set on them,
    so deriving colours from those would compound on every theme refresh."""
    return QApplication.instance().palette()


def _mix(base: QColor, other: QColor, alpha: float) -> QColor:
    return QColor(
        round(base.red() * (1 - alpha) + other.red() * alpha),
        round(base.green() * (1 - alpha) + other.green() * alpha),
        round(base.blue() * (1 - alpha) + other.blue() * alpha),
    )


def muted(palette: QPalette) -> QColor:
    """Grey secondary text that still reads on the palette's Base colour.

    Always from the Active group: a widget that is disabled while it is styled (during a
    scan) would otherwise bake its disabled grey into the stylesheet.
    """
    active = QPalette.ColorGroup.Active
    return _mix(palette.color(active, QPalette.ColorRole.Text), palette.color(active, QPalette.ColorRole.Base), 0.42)


def error_color(palette: QPalette) -> QColor:
    return QColor(255, 130, 120) if theme.is_dark(palette) else QColor(180, 30, 20)


def placeholder_cover(size: QSize, dpr: float, palette: QPalette) -> QPixmap:
    """A plain book outline for rows without a cover (or whose cover is still loading)."""
    w, h = size.width(), size.height()
    pixmap = QPixmap(round(w * dpr), round(h * dpr))
    pixmap.setDevicePixelRatio(dpr)
    pixmap.fill(Qt.GlobalColor.transparent)
    base, text = palette.color(QPalette.ColorRole.Base), palette.color(QPalette.ColorRole.Text)
    painter = QPainter(pixmap)
    painter.setRenderHint(QPainter.RenderHint.Antialiasing)
    painter.setPen(QPen(_mix(base, text, 0.25), 1))
    painter.setBrush(_mix(base, text, 0.07))
    painter.drawRoundedRect(QRectF(0.5, 0.5, w - 1, h - 1), 3, 3)
    painter.setPen(QPen(_mix(base, text, 0.25), 1.5))
    for y in (h * 0.36, h * 0.48):
        painter.drawLine(QPointF(w * 0.25, y), QPointF(w * 0.75, y))
    painter.end()
    return pixmap


def dot_icon(color: QColor, size: int = 10) -> QIcon:
    pixmap = QPixmap(size * 2, size * 2)
    pixmap.setDevicePixelRatio(2)
    pixmap.fill(Qt.GlobalColor.transparent)
    painter = QPainter(pixmap)
    painter.setRenderHint(QPainter.RenderHint.Antialiasing)
    painter.setPen(Qt.PenStyle.NoPen)
    painter.setBrush(color)
    painter.drawEllipse(QRectF(1, 1, size - 2, size - 2))
    painter.end()
    return QIcon(pixmap)


def chip_button_style(kind: str, palette: QPalette | None = None) -> str:
    """A count chip: tinted like theme.chip_style, outlined in its text colour when selected."""
    palette = palette or app_palette()
    background, text, border = theme.chip_colors(kind, palette)
    base = palette.color(QPalette.ColorRole.Base)
    return (
        f"QPushButton {{ background-color: {background.name()}; color: {text.name()};"
        f" border: 1px solid {border.name()}; border-radius: 12px; padding: 3px 12px; }}"
        f" QPushButton:hover {{ border-color: {text.name()}; }}"
        f" QPushButton:checked {{ border: 2px solid {text.name()}; padding: 2px 11px; font-weight: 600; }}"
        # Disabled: outlined only. A filled pill in the Base colour reads as a heavy black
        # shape on a dark window, not as "nothing here".
        f" QPushButton:disabled {{ background-color: transparent; color: {theme.helper_color(palette).name()};"
        f" border-color: {_mix(base, palette.color(QPalette.ColorRole.Text), 0.2).name()}; }}"
    )


def _paint_cell_background(option: QStyleOptionViewItem, painter: QPainter, index: QModelIndex, delegate: Any) -> None:
    """The cell's selection and row tint, without its text or icon, which the caller draws."""
    opt = QStyleOptionViewItem(option)
    delegate.initStyleOption(opt, index)
    opt.text = ""
    opt.icon = QIcon()
    style = opt.widget.style() if opt.widget is not None else QApplication.style()
    style.drawControl(QStyle.ControlElement.CE_ItemViewItem, opt, painter, opt.widget)


def _text_color(option: QStyleOptionViewItem, role: QPalette.ColorRole = QPalette.ColorRole.Text) -> QColor:
    group = QPalette.ColorGroup.Active
    if not option.state & QStyle.StateFlag.State_Enabled:
        group = QPalette.ColorGroup.Disabled  # the table is disabled while a sync runs
    if option.state & QStyle.StateFlag.State_Selected:
        return option.palette.color(group, QPalette.ColorRole.HighlightedText)
    return option.palette.color(group, role)


# ---------------------------------------------------------------------------------- model


def cover_key(item: PlanItem) -> tuple[str, Any] | None:
    """Where a row's cover comes from: the Calibre book if there is one, else the entry."""
    if item.book is not None:
        return ("local", item.book.book_id)
    if item.entry is not None and (item.entry.thumbnail or item.entry.has_cover):
        return ("remote", str(item.entry.id))
    return None


class PlanModel(QAbstractTableModel):
    def __init__(self, parent: Any = None) -> None:
        super().__init__(parent)
        self.items: list[PlanItem] = []
        self.results: dict[int, tuple[bool, str]] = {}
        self.covers: dict[tuple[str, Any], QPixmap] = {}
        self.request_cover: Callable[[tuple[str, Any]], None] | None = None
        self.dpr = 1.0
        self._pending: set[tuple[str, Any]] = set()
        self._rows_by_key: dict[tuple[str, Any], list[int]] = {}
        self._placeholder: QPixmap | None = None
        self._dots: dict[State, QIcon] = {}

    def set_items(self, items: list[PlanItem]) -> None:
        self.beginResetModel()
        self.items = sorted(items, key=lambda i: (STATE_ORDER[i.state], i.title.casefold()))
        self.results = {}
        self._rows_by_key = {}
        for row, item in enumerate(self.items):
            key = cover_key(item)
            if key is not None:
                self._rows_by_key.setdefault(key, []).append(row)
        self.endResetModel()

    def clear_covers(self) -> None:
        self.covers.clear()
        self._pending.clear()

    def set_cover(self, key: tuple[str, Any], image: QImage) -> None:
        self._pending.discard(key)
        pixmap = QPixmap.fromImage(image) if not image.isNull() else QPixmap()
        if not pixmap.isNull():
            pixmap.setDevicePixelRatio(self.dpr)
        self.covers[key] = pixmap
        for row in self._rows_by_key.get(key, ()):
            index = self.index(row, COVER_COLUMN)
            self.dataChanged.emit(index, index)

    def refresh_theme(self) -> None:
        self._placeholder = None
        if self.items:
            self.dataChanged.emit(self.index(0, 0), self.index(len(self.items) - 1, len(COLUMNS) - 1))

    def placeholder(self) -> QPixmap:
        if self._placeholder is None:
            self._placeholder = placeholder_cover(COVER_SIZE, self.dpr, QApplication.instance().palette())
        return self._placeholder

    def cover(self, item: PlanItem) -> QPixmap:
        """The row's cover if loaded; otherwise ask for it once and show the placeholder."""
        key = cover_key(item)
        if key is None:
            return self.placeholder()
        pixmap = self.covers.get(key)
        if pixmap is not None:
            return pixmap if not pixmap.isNull() else self.placeholder()
        if key not in self._pending and self.request_cover is not None:
            self._pending.add(key)
            self.request_cover(key)
        return self.placeholder()

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
        if self.items:
            self.dataChanged.emit(self.index(0, RESULT_COLUMN), self.index(len(self.items) - 1, RESULT_COLUMN))

    def rowCount(self, parent: QModelIndex | None = None) -> int:
        return 0 if parent is not None and parent.isValid() else len(self.items)

    def columnCount(self, parent: QModelIndex | None = None) -> int:
        return 0 if parent is not None and parent.isValid() else len(COLUMNS)

    def headerData(self, section: int, orientation: Qt.Orientation, role: int = Qt.ItemDataRole.DisplayRole) -> Any:
        if orientation == Qt.Orientation.Horizontal:
            if role == Qt.ItemDataRole.DisplayRole:
                return COLUMNS[section]
            if role == Qt.ItemDataRole.ToolTipRole and section == ACTION_COLUMN:
                return "What Synchronize will do. Click to change it."
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
        if role == Qt.ItemDataRole.UserRole:
            return item
        if role == Qt.ItemDataRole.BackgroundRole:
            return theme.state_brush(item.state)
        if role == Qt.ItemDataRole.DisplayRole:
            return self._display(item, index.row(), column)
        if role == Qt.ItemDataRole.ToolTipRole:
            return self._tooltip(item, index.row(), column)
        if role == Qt.ItemDataRole.DecorationRole:
            if column == COVER_COLUMN:
                return self.cover(item)
            if column == STATUS_COLUMN:
                return self._dot(item.state)
        if role == Qt.ItemDataRole.ForegroundRole and column == RESULT_COLUMN:
            result = self.results.get(index.row())
            if result is not None and not result[0]:
                return error_color(QApplication.instance().palette())
        return None

    def _display(self, item: PlanItem, row: int, column: int) -> Any:
        if column == BOOK_COLUMN:
            return item.title
        if column == STATUS_COLUMN:
            return STATUS_TEXT[item.state][0]
        if column == ACTION_COLUMN:
            return item.action.value
        if column == RESULT_COLUMN:
            result = self.results.get(row)
            if result is None:
                return item.note
            return "Done" if result[0] else result[1]
        return None

    def _tooltip(self, item: PlanItem, row: int, column: int) -> str | None:
        if column == BOOK_COLUMN:
            return f"{item.title}\n{' & '.join(item.authors)}" if item.authors else item.title
        if column == STATUS_COLUMN:
            return STATUS_TEXT[item.state][1]
        if column == ACTION_COLUMN:
            return ACTION_HELP[item.action] + (" Click to change." if len(item.choices) > 1 else "")
        if column == RESULT_COLUMN:
            result = self.results.get(row)
            return result[1] if result else item.note or None
        return None

    def _dot(self, state: State) -> QIcon | None:
        kind = theme.STATE_KINDS.get(state.name) or "in_sync"
        if state not in self._dots:
            self._dots[state] = dot_icon(theme.ACCENTS[kind])
        return self._dots[state]


class PlanFilter(QSortFilterProxyModel):
    """Shows the rows of one chip. A row with a result stays visible, so the outcome can be read."""

    def __init__(self, parent: Any = None) -> None:
        super().__init__(parent)
        self.mode = "pending"
        # Changing a row's action must not make it vanish from under the user's cursor;
        # the filter is applied again only when a chip is picked or the plan reloads.
        self.setDynamicSortFilter(False)

    def set_mode(self, mode: str) -> None:
        self.mode = mode
        self.invalidateFilter()

    def filterAcceptsRow(self, source_row: int, source_parent: QModelIndex) -> bool:
        model = self.sourceModel()
        return matches(self.mode, model.items[source_row]) or source_row in model.results


# ------------------------------------------------------------------------------ delegates


class CoverDelegate(QStyledItemDelegate):
    def paint(self, painter: QPainter, option: QStyleOptionViewItem, index: QModelIndex) -> None:
        _paint_cell_background(option, painter, index, self)
        pixmap = index.data(Qt.ItemDataRole.DecorationRole)
        if isinstance(pixmap, QPixmap) and not pixmap.isNull():
            size = pixmap.deviceIndependentSize().toSize()
            target = QRect(0, 0, size.width(), size.height())
            target.moveCenter(option.rect.center())
            painter.drawPixmap(target, pixmap)


class BookDelegate(QStyledItemDelegate):
    """The title, with the authors on a second, grey line."""

    def paint(self, painter: QPainter, option: QStyleOptionViewItem, index: QModelIndex) -> None:
        _paint_cell_background(option, painter, index, self)
        item: PlanItem = index.data(Qt.ItemDataRole.UserRole)
        rect = option.rect.adjusted(6, 4, -6, -4)
        metrics = option.fontMetrics
        line = metrics.height()
        top = rect.top() + max(0, (rect.height() - 2 * line - 2) // 2)
        painter.save()
        title_font = QFont(option.font)
        title_font.setWeight(QFont.Weight.DemiBold)
        painter.setFont(title_font)
        painter.setPen(_text_color(option))
        title_rect = QRect(rect.left(), top, rect.width(), line)
        painter.drawText(
            title_rect,
            Qt.AlignmentFlag.AlignLeft | Qt.AlignmentFlag.AlignVCenter,
            painter.fontMetrics().elidedText(item.title, Qt.TextElideMode.ElideRight, rect.width()),
        )
        authors = " & ".join(item.authors) or "Unknown author"
        painter.setFont(option.font)
        selected = option.state & QStyle.StateFlag.State_Selected
        painter.setPen(_text_color(option) if selected else muted(option.palette))
        painter.drawText(
            QRect(rect.left(), top + line + 2, rect.width(), line),
            Qt.AlignmentFlag.AlignLeft | Qt.AlignmentFlag.AlignVCenter,
            metrics.elidedText(authors, Qt.TextElideMode.ElideRight, rect.width()),
        )
        painter.restore()


class ActionDelegate(QStyledItemDelegate):
    """Draws the action as a coloured chip; editing it opens a combo box of the allowed actions."""

    def paint(self, painter: QPainter, option: QStyleOptionViewItem, index: QModelIndex) -> None:
        _paint_cell_background(option, painter, index, self)
        item: PlanItem = index.data(Qt.ItemDataRole.UserRole)
        background, text, border = theme.chip_colors(ACTION_KINDS[item.action], option.palette)
        label = ACTION_TEXT[item.action] + ("  ▾" if len(item.choices) > 1 else "")
        font = QFont(option.font)
        font.setWeight(QFont.Weight.DemiBold)
        painter.save()
        painter.setFont(font)
        metrics = painter.fontMetrics()
        height = metrics.height() + 6
        width = min(metrics.horizontalAdvance(label) + 22, option.rect.width() - 12)
        chip = QRect(option.rect.left() + 6, 0, width, height)
        chip.moveTop(option.rect.center().y() - height // 2)
        painter.setRenderHint(QPainter.RenderHint.Antialiasing)
        painter.setPen(QPen(border, 1))
        painter.setBrush(background)
        painter.drawRoundedRect(QRectF(chip).adjusted(0.5, 0.5, -0.5, -0.5), height / 2, height / 2)
        painter.setPen(text)
        painter.drawText(chip, Qt.AlignmentFlag.AlignCenter, label)
        painter.restore()

    def createEditor(self, parent: Any, option: Any, index: QModelIndex) -> QComboBox:
        editor = QComboBox(parent)
        item: PlanItem = index.data(Qt.ItemDataRole.UserRole)
        for action in item.choices:
            editor.addItem(ACTION_TEXT[action], action)
            editor.setItemData(editor.count() - 1, ACTION_HELP[action], Qt.ItemDataRole.ToolTipRole)
        editor.setStyleSheet(
            "QComboBox { " + theme.chip_style(ACTION_KINDS[item.action]) + " } QComboBox::drop-down { border: none; }"
        )
        editor.setToolTip("Choose what Synchronize does with this book.")
        editor.activated.connect(lambda _index: self._commit(editor))
        return editor

    def _commit(self, editor: QComboBox) -> None:
        self.commitData.emit(editor)
        self.closeEditor.emit(editor, QStyledItemDelegate.EndEditHint.NoHint)

    def setEditorData(self, editor: QComboBox, index: QModelIndex) -> None:
        item: PlanItem = index.data(Qt.ItemDataRole.UserRole)
        editor.setCurrentIndex(max(0, editor.findData(item.action)))

    def setModelData(self, editor: QComboBox, model: Any, index: QModelIndex) -> None:
        source = model.mapToSource(index)
        model.sourceModel().set_action(source.row(), editor.currentData())

    def updateEditorGeometry(self, editor: QWidget, option: QStyleOptionViewItem, index: QModelIndex) -> None:
        height = editor.sizeHint().height()
        rect = QRect(option.rect.left() + 2, 0, option.rect.width() - 4, height)
        rect.moveTop(option.rect.center().y() - height // 2)
        editor.setGeometry(rect)


# -------------------------------------------------------------------------------- widgets


def helper_label(text: str, parent: QWidget | None = None) -> QLabel:
    """A small grey line that explains the control above or beside it."""
    return theme.HelperLabel(text, parent)


def style_helper(label: QLabel) -> None:
    """Grey secondary text for a plain label (the comparison grid's field names and "(none)")."""
    label.setStyleSheet(f"color: {theme.helper_color(app_palette()).name()};")


class MessagePage(QWidget):
    """A centred message with one button: first run, all in sync, a failed or stopped scan."""

    def __init__(self, parent: QWidget | None = None) -> None:
        super().__init__(parent)
        layout = QVBoxLayout(self)
        layout.addStretch(1)
        self.title = QLabel(self)
        self.title.setAlignment(Qt.AlignmentFlag.AlignCenter)
        font = QFont(self.title.font())
        font.setPointSizeF(font.pointSizeF() * 1.4)
        font.setBold(True)
        self.title.setFont(font)
        layout.addWidget(self.title)
        self.text = QLabel(self)
        self.text.setAlignment(Qt.AlignmentFlag.AlignCenter)
        self.text.setWordWrap(True)
        self.text.setMaximumWidth(520)
        row = QHBoxLayout()
        row.addStretch(1)
        row.addWidget(self.text)
        row.addStretch(1)
        layout.addLayout(row)
        self.button = QPushButton(self)
        self.button.clicked.connect(self._clicked)
        row = QHBoxLayout()
        row.addStretch(1)
        row.addWidget(self.button)
        row.addStretch(1)
        layout.addSpacing(8)
        layout.addLayout(row)
        layout.addStretch(2)
        self._callback: Callable[[], Any] | None = None
        self._kind: str | None = None

    def configure(
        self,
        title: str,
        text: str,
        button: str = "",
        callback: Callable[[], Any] | None = None,
        *,
        tooltip: str = "",
        kind: str | None = None,
    ) -> None:
        self.title.setText(title)
        self.text.setText(text)
        self.button.setText(button)
        self.button.setToolTip(tooltip)
        self.button.setVisible(bool(button))
        self._callback = callback
        self._kind = kind
        self.refresh_theme()

    def refresh_theme(self) -> None:
        color = theme.chip_colors(self._kind, app_palette())[1] if self._kind else None
        self.title.setStyleSheet(f"color: {color.name()};" if color is not None else "")
        self.text.setStyleSheet(f"color: {theme.helper_color(app_palette()).name()};")

    def _clicked(self) -> None:
        if self._callback is not None:
            self._callback()


class LoadingPage(QWidget):
    def __init__(self, parent: QWidget | None = None) -> None:
        super().__init__(parent)
        layout = QVBoxLayout(self)
        layout.addStretch(1)
        self.busy = QProgressBar(self)
        self.busy.setRange(0, 0)  # indeterminate: the scan cannot say how far it is
        self.busy.setTextVisible(False)
        self.busy.setFixedWidth(260)
        row = QHBoxLayout()
        row.addStretch(1)
        row.addWidget(self.busy)
        row.addStretch(1)
        layout.addLayout(row)
        self.title = QLabel("Comparing your library with the catalog…", self)
        self.title.setAlignment(Qt.AlignmentFlag.AlignCenter)
        layout.addWidget(self.title)
        self.detail = helper_label("", self)
        self.detail.setAlignment(Qt.AlignmentFlag.AlignCenter)
        layout.addWidget(self.detail)
        self.cancel = QPushButton("Cancel", self)
        self.cancel.setToolTip("Stop comparing. Nothing has been changed yet.")
        row = QHBoxLayout()
        row.addStretch(1)
        row.addWidget(self.cancel)
        row.addStretch(1)
        layout.addSpacing(8)
        layout.addLayout(row)
        layout.addStretch(2)


class Banner(QFrame):
    """An inline error with a Retry button, instead of a modal message box."""

    retry = pyqtSignal()

    def __init__(self, parent: QWidget | None = None) -> None:
        super().__init__(parent)
        self.setObjectName("evilflowersBanner")
        layout = QHBoxLayout(self)
        layout.setContentsMargins(10, 6, 6, 6)
        self.label = QLabel(self)
        self.label.setWordWrap(True)
        self.label.setTextInteractionFlags(Qt.TextInteractionFlag.TextSelectableByMouse)
        layout.addWidget(self.label, 1)
        self.details_button = QPushButton("Details…", self)
        self.details_button.setToolTip("Show the full error for each book.")
        self.details_button.clicked.connect(self._show_details)
        layout.addWidget(self.details_button)
        self.retry_button = QPushButton("Retry", self)
        self.retry_button.setToolTip("Compare the library with the catalog again.")
        self.retry_button.clicked.connect(self.retry.emit)
        layout.addWidget(self.retry_button)
        self.close_button = QPushButton("Dismiss", self)
        self.close_button.setToolTip("Hide this message.")
        self.close_button.clicked.connect(self.hide)
        layout.addWidget(self.close_button)
        self._details = ""
        self.refresh_theme()
        self.hide()

    def show_message(self, text: str, details: str = "", *, retry: bool = True) -> None:
        """Show an error. ``retry=False`` when the page below already offers its own Retry."""
        self.label.setText(text)
        self._details = details
        self.details_button.setVisible(bool(details))
        self.retry_button.setVisible(retry)
        self.show()

    def _show_details(self) -> None:
        error_dialog(self, "EvilFlowers", self.label.text(), det_msg=self._details, show=True)

    def refresh_theme(self) -> None:
        background, text, border = theme.chip_colors("conflict", app_palette())
        self.setStyleSheet(
            f"#evilflowersBanner {{ background-color: {background.name()}; border: 1px solid {border.name()};"
            f" border-radius: 6px; }} #evilflowersBanner QLabel {{ color: {text.name()}; }}"
        )


class DetailsPanel(QFrame):
    """Side-by-side comparison for a book that needs a decision, with one button per choice."""

    chosen = pyqtSignal(object)  # Action

    def __init__(self, parent: QWidget | None = None) -> None:
        super().__init__(parent)
        self.setFrameShape(QFrame.Shape.StyledPanel)
        # Wide enough that a title fits on one or two lines in each column; the table keeps its
        # own minimum, so the Book column stays readable too.
        self.setMinimumWidth(320)
        self.setMaximumWidth(620)
        outer = QVBoxLayout(self)
        self.heading = QLabel(self)
        self.heading.setWordWrap(True)
        font = QFont(self.heading.font())
        font.setPointSizeF(font.pointSizeF() * 1.15)
        font.setBold(True)
        self.heading.setFont(font)
        outer.addWidget(self.heading)
        self.explain = helper_label("", self)
        outer.addWidget(self.explain)

        scroll = QScrollArea(self)
        scroll.setWidgetResizable(True)
        scroll.setFrameShape(QFrame.Shape.NoFrame)
        self.grid_host = QWidget(scroll)
        self.grid = QGridLayout(self.grid_host)
        self.grid.setContentsMargins(0, 6, 0, 6)
        self.grid.setColumnStretch(1, 1)
        self.grid.setColumnStretch(2, 1)
        self.grid.setHorizontalSpacing(12)
        self.grid.setVerticalSpacing(6)
        scroll.setWidget(self.grid_host)
        outer.addWidget(scroll, 1)
        self.note = helper_label("", self)
        outer.addWidget(self.note)

        self.buttons: dict[Action, QPushButton] = {}
        self.group = QButtonGroup(self)
        self.group.setExclusive(True)
        for action, text, tip in (
            (Action.PUSH, "Keep Calibre version", "Upload this book; the catalog gets Calibre's fields and files."),
            (Action.PULL, "Keep catalog version", "Download this book; Calibre gets the catalog's fields and files."),
            (Action.SKIP, "Skip", "Change nothing this time; the book is offered again next sync."),
        ):
            button = QPushButton(text, self)
            button.setCheckable(True)
            button.setToolTip(tip)
            button.clicked.connect(lambda _=False, a=action: self.chosen.emit(a))
            self.group.addButton(button)
            self.buttons[action] = button
            outer.addWidget(button)
        self.help = helper_label("Your choice is applied when you press Synchronize.", self)
        outer.addWidget(self.help)
        self.item: PlanItem | None = None
        self.diffs: list[FieldDiff] = []

    def show_item(self, item: PlanItem, diffs: list[FieldDiff], note: str) -> None:
        self.item = item
        self.diffs = diffs
        self.heading.setText(item.title)
        self.explain.setText(STATUS_TEXT[item.state][1])
        self._fill(diffs)
        self.note.setText(note)
        self.note.setVisible(bool(note))
        self.set_choice(item.action)
        self.refresh_theme()

    def set_choice(self, action: Action) -> None:
        self.buttons[action].setChecked(True)

    def _fill(self, diffs: list[FieldDiff]) -> None:
        while self.grid.count():
            widget = self.grid.takeAt(0).widget()
            if widget is not None:
                widget.deleteLater()
        if not diffs:
            label = QLabel("Title, authors, tags, language and formats match.", self.grid_host)
            label.setWordWrap(True)
            self.grid.addWidget(label, 0, 0, 1, 3)
            self._pack(1)
            return
        for column, text in ((1, "Calibre"), (2, "Catalog")):
            header = QLabel(f"<b>{text}</b>", self.grid_host)
            self.grid.addWidget(header, 0, column)
        for row, diff in enumerate(diffs, start=1):
            name = QLabel(diff.label, self.grid_host)
            name.setProperty("evilflowers_helper", True)
            self.grid.addWidget(name, row, 0, Qt.AlignmentFlag.AlignTop)
            for column, value in ((1, diff.calibre), (2, diff.catalog)):
                cell = QLabel(value or "(none)", self.grid_host)
                cell.setWordWrap(True)
                cell.setTextInteractionFlags(Qt.TextInteractionFlag.TextSelectableByMouse)
                if not value:
                    cell.setProperty("evilflowers_helper", True)
                self.grid.addWidget(cell, row, column, Qt.AlignmentFlag.AlignTop)
        self._pack(len(diffs) + 1)

    def _pack(self, rows: int) -> None:
        """Keep the rows together at the top; the empty space goes below them."""
        for row in range(self.grid.rowCount()):
            self.grid.setRowStretch(row, 0)
        self.grid.setRowStretch(rows, 1)

    def refresh_theme(self) -> None:
        for label in self.findChildren(QLabel):
            if label.property("evilflowers_helper"):
                style_helper(label)
        for action, button in self.buttons.items():
            background, text, _border = theme.chip_colors(ACTION_KINDS[action], app_palette())
            button.setStyleSheet(
                f"QPushButton:checked {{ background-color: {background.name()}; color: {text.name()};"
                f" border: 2px solid {text.name()}; border-radius: 4px; padding: 4px; font-weight: 600; }}"
            )


# --------------------------------------------------------------------------------- dialog


class SyncDialog(Dialog):
    def __init__(self, gui: Any, icon: Any, action: Any) -> None:
        self.gui = gui
        self.action = action
        self.worker: SyncWorker | None = None
        self.catalog: Any = None
        self.phase = "idle"  # idle, setup, loading, failed, stopped, ready
        self._scanning = False
        self._loader: CoverLoader | None = None
        self._detail_tasks: dict[str, Task] = {}  # entry id -> its detail request in flight
        self._details: dict[str, Any] = {}  # entry id -> detailed EntryRef, or an error message
        self._thumbs: Path | None = None
        super().__init__("EvilFlowers sync", "evilflowers-sync-dialog", parent=gui)
        self.setWindowIcon(icon)
        self.setWindowFlag(Qt.WindowType.WindowMinMaxButtonsHint)
        theme.on_theme_change(self.refresh_theme)

    def sizeHint(self) -> Any:
        hint = super().sizeHint()
        hint.setWidth(max(hint.width(), 1060))
        hint.setHeight(max(hint.height(), 700))
        return hint

    # ---------------------------------------------------------------- layout

    def setup_ui(self) -> None:
        layout = QVBoxLayout(self)
        self.header = theme.Header("EvilFlowers", "", self)
        layout.addWidget(self.header)
        self.banner = Banner(self)
        self.banner.retry.connect(self.scan)
        layout.addWidget(self.banner)
        layout.addWidget(self._build_chips())

        self.stack = QStackedWidget(self)
        self.message_page = MessagePage(self.stack)
        self.loading_page = LoadingPage(self.stack)
        self.loading_page.cancel.clicked.connect(self.stop)
        self.plan_page = self._build_plan_page()
        for page in (self.message_page, self.loading_page, self.plan_page):
            self.stack.addWidget(page)
        layout.addWidget(self.stack, 1)
        layout.addWidget(self._build_progress_row())

        row = QHBoxLayout()
        self.summary = QLabel(self)
        self.summary.setToolTip("What Synchronize will do with the books in the table.")
        row.addWidget(self.summary, 1)
        self.bb = QDialogButtonBox(self)
        self.settings_button = self.bb.addButton("Settings…", QDialogButtonBox.ButtonRole.ActionRole)
        self.settings_button.setToolTip("Server, sign-in and catalog.")
        self.settings_button.clicked.connect(self.open_settings)
        self.scan_button = self.bb.addButton("Rescan", QDialogButtonBox.ButtonRole.ActionRole)
        self.scan_button.setToolTip("Compare the library with the catalog again.")
        self.scan_button.clicked.connect(self.scan)
        self.sync_button: QPushButton = self.bb.addButton("Synchronize", QDialogButtonBox.ButtonRole.AcceptRole)
        self.sync_button.setToolTip("Upload and download the books as shown in the Action column.")
        self.sync_button.setDefault(True)
        self.sync_button.clicked.connect(self.synchronize)
        close = self.bb.addButton(QDialogButtonBox.StandardButton.Close)
        close.clicked.connect(self.reject)
        row.addWidget(self.bb)
        layout.addLayout(row)

        self.model.dataChanged.connect(self.on_data_changed)
        self.model.modelReset.connect(self.update_summary)
        self.model.modelReset.connect(self.update_result_column)
        self.refresh_theme()
        self.set_busy(False)

    def _build_chips(self) -> QWidget:
        self.chip_bar = QWidget(self)
        column = QVBoxLayout(self.chip_bar)
        column.setContentsMargins(0, 4, 0, 0)
        row = QHBoxLayout()
        row.setSpacing(6)
        self.chip_group = QButtonGroup(self.chip_bar)
        self.chip_group.setExclusive(True)
        self.chips: dict[str, QPushButton] = {}
        for chip in FILTERS:
            button = QPushButton(chip.label(0), self.chip_bar)
            button.setCheckable(True)
            button.setToolTip(chip.tooltip)
            button.setCursor(Qt.CursorShape.PointingHandCursor)
            button.setSizePolicy(QSizePolicy.Policy.Maximum, QSizePolicy.Policy.Fixed)
            button.clicked.connect(lambda _=False, key=chip.key: self.set_filter(key, remember=True))
            self.chip_group.addButton(button)
            self.chips[chip.key] = button
            row.addWidget(button)
        row.addStretch(1)
        column.addLayout(row)
        self.chip_help = helper_label(
            "Click a group to show only those books. Right-click selected rows to change several at once.",
            self.chip_bar,
        )
        column.addWidget(self.chip_help)
        self.chip_bar.hide()
        return self.chip_bar

    def _build_plan_page(self) -> QWidget:
        self.model = PlanModel(self)
        self.model.request_cover = self.request_cover
        self.proxy = PlanFilter(self)
        self.proxy.setSourceModel(self.model)
        self.proxy.mode = "all" if prefs["show_in_sync"] else "pending"

        self.splitter = QSplitter(Qt.Orientation.Horizontal, self)
        self.table = QTableView(self.splitter)
        self.table.setModel(self.proxy)
        self.table.setItemDelegateForColumn(COVER_COLUMN, CoverDelegate(self.table))
        self.table.setItemDelegateForColumn(BOOK_COLUMN, BookDelegate(self.table))
        self.table.setItemDelegateForColumn(ACTION_COLUMN, ActionDelegate(self.table))
        self.table.setSelectionBehavior(QAbstractItemView.SelectionBehavior.SelectRows)
        self.table.setEditTriggers(QAbstractItemView.EditTrigger.AllEditTriggers)
        self.table.setShowGrid(False)
        self.table.setWordWrap(False)
        self.table.verticalHeader().hide()
        self.table.verticalHeader().setSectionResizeMode(QHeaderView.ResizeMode.Fixed)
        self.table.verticalHeader().setDefaultSectionSize(ROW_HEIGHT)
        header = self.table.horizontalHeader()
        header.setSectionResizeMode(COVER_COLUMN, QHeaderView.ResizeMode.Fixed)
        header.resizeSection(COVER_COLUMN, COVER_SIZE.width() + 14)
        header.setSectionResizeMode(BOOK_COLUMN, QHeaderView.ResizeMode.Stretch)
        header.setSectionResizeMode(STATUS_COLUMN, QHeaderView.ResizeMode.ResizeToContents)
        header.setSectionResizeMode(ACTION_COLUMN, QHeaderView.ResizeMode.Fixed)
        header.resizeSection(ACTION_COLUMN, 120)
        header.setSectionResizeMode(RESULT_COLUMN, QHeaderView.ResizeMode.Interactive)
        header.resizeSection(RESULT_COLUMN, 180)
        self.table.setColumnHidden(RESULT_COLUMN, True)  # shown once a row has a note or a result
        self.table.setMinimumWidth(500)  # the Book column never shrinks to a couple of letters
        self.table.setContextMenuPolicy(Qt.ContextMenuPolicy.CustomContextMenu)
        self.table.customContextMenuRequested.connect(self.context_menu)
        self.table.selectionModel().selectionChanged.connect(self.update_details)
        self.proxy.modelReset.connect(self.update_details)

        self.details = DetailsPanel(self.splitter)
        self.details.chosen.connect(self.choose_for_selected)
        self.details.hide()
        self.splitter.addWidget(self.table)
        self.splitter.addWidget(self.details)
        self.splitter.setStretchFactor(0, 3)
        self.splitter.setStretchFactor(1, 2)
        self.splitter.setChildrenCollapsible(False)
        return self.splitter

    def _build_progress_row(self) -> QWidget:
        self.progress_row = QWidget(self)
        row = QHBoxLayout(self.progress_row)
        row.setContentsMargins(0, 0, 0, 0)
        self.progress = QProgressBar(self.progress_row)
        self.progress.setRange(0, 1000)
        self.progress.setTextVisible(True)
        self.progress.setFixedWidth(220)
        row.addWidget(self.progress)
        self.progress_label = QLabel(self.progress_row)
        self.progress_label.setSizePolicy(QSizePolicy.Policy.Ignored, QSizePolicy.Policy.Preferred)
        row.addWidget(self.progress_label, 1)
        self.stop_button = QPushButton("Cancel", self.progress_row)
        self.stop_button.setToolTip("Stop after the current book. Books already done stay synchronised.")
        self.stop_button.clicked.connect(self.stop)
        row.addWidget(self.stop_button)
        self.progress_row.hide()
        return self.progress_row

    # ----------------------------------------------------------------- theme

    def refresh_theme(self) -> None:
        """Re-derive every colour from the current palette after Calibre's theme changes."""
        palette = app_palette()
        for chip in FILTERS:
            self.chips[chip.key].setStyleSheet(chip_button_style(chip.kind, palette))
        for label in self.findChildren(QLabel):
            if label.property("evilflowers_helper"):
                style_helper(label)
        self.banner.refresh_theme()
        self.message_page.refresh_theme()
        self.details.refresh_theme()
        self.model.refresh_theme()
        self.header.update()
        self.table.viewport().update()

    # ----------------------------------------------------------------- state

    def set_busy(self, busy: bool) -> None:
        for widget in (self.settings_button, self.scan_button, self.table, self.details, self.chip_bar):
            widget.setEnabled(not busy)
        syncing = busy and not self._scanning
        if syncing:
            self.progress.setValue(0)
            self.progress_label.setText("")
            self.progress_row.show()
        self.stop_button.setVisible(syncing)
        self.stop_button.setEnabled(syncing)
        self.update_summary()

    def update_view(self) -> None:
        """Pick the page for the current phase; a "ready" plan with nothing to review says so."""
        ready = self.phase == "ready"
        self.chip_bar.setVisible(ready and bool(self.model.items))
        if self.phase == "loading":
            self.stack.setCurrentWidget(self.loading_page)
        elif ready and self.proxy.mode == "pending" and self.proxy.rowCount() == 0:
            if self.model.items:
                self.message_page.configure(
                    "Your library is in sync ✓",
                    f"All {len(self.model.items)} books match the catalog. There is nothing to do.",
                    "Show all books",
                    lambda: self.set_filter("all"),
                    tooltip="List every book, including the ones in sync.",
                    kind="in_sync",
                )
            else:
                self.message_page.configure(
                    "Nothing to sync yet",
                    "Your library and the catalog are both empty. Add a book on either side, then rescan.",
                    "Rescan",
                    self.scan,
                    tooltip="Compare the library with the catalog again.",
                )
            self.stack.setCurrentWidget(self.message_page)
        elif ready:
            self.stack.setCurrentWidget(self.plan_page)
        else:
            self.stack.setCurrentWidget(self.message_page)

    def update_summary(self, *_args: Any) -> None:
        counts = filter_counts(self.model.items)
        for chip in FILTERS:
            button = self.chips[chip.key]
            button.setText(chip.label(counts[chip.key]))
            usable = bool(counts[chip.key]) or chip.key in ("pending", "all") or button.isChecked()
            button.setEnabled(usable)
            button.setVisible(usable)  # an empty group is noise, not a choice
        self.chips[self.proxy.mode].setChecked(True)
        # Nothing to say when every book is in sync: the page above already says so.
        everything_in_sync = all(item.state is State.IN_SYNC for item in self.model.items)
        self.summary.setText("" if everything_in_sync else summary_text(self.model.items))
        self.sync_button.setEnabled(self.worker is None and bool(counts["upload"] or counts["download"]))

    def update_result_column(self, *_args: Any) -> None:
        """The Result column only takes room once there is something in it."""
        model = self.model
        self.table.setColumnHidden(RESULT_COLUMN, not (model.results or any(item.note for item in model.items)))

    def on_data_changed(self, top_left: QModelIndex, bottom_right: QModelIndex, *_args: Any) -> None:
        if top_left.column() <= ACTION_COLUMN <= bottom_right.column():
            self.update_summary()
            if self.details.item is not None and self.details.isVisible():
                row = self._details_row()
                if row is not None:
                    self.details.set_choice(self.model.items[row].action)

    def set_filter(self, key: str, *, remember: bool = False) -> None:
        """Show only the rows of one chip. "All" and "to review" also set the default for next time."""
        if remember and key in ("all", "pending"):
            prefs["show_in_sync"] = key == "all"
        self.proxy.set_mode(key)
        self.chips[key].setChecked(True)
        self.update_summary()
        self.update_view()

    def on_progress(self, fraction: float, message: str) -> None:
        self.progress.setValue(int(fraction * 1000))
        self.progress_label.setText(message)
        self.loading_page.detail.setText(message)

    def settings_changed(self) -> None:
        if self.worker is None:
            self.scan()

    # --------------------------------------------------------------- actions

    def start(self, worker: SyncWorker) -> None:
        self.worker = worker
        worker.progress.connect(self.on_progress)
        worker.connected.connect(self.on_connected)
        worker.failed.connect(self.on_failed)
        worker.stopped.connect(self.on_stopped)
        worker.finished.connect(self.on_finished)
        self.set_busy(True)
        start_thread(worker)  # no parent: it must outlive this window if the window goes first

    def scan(self) -> None:
        if self.worker is not None:
            return
        settings = Settings.load()
        self.banner.hide()
        self.progress_row.hide()
        self._retire_loader()
        self.model.clear_covers()
        self._details.clear()
        self.model.set_items([])
        if settings.missing():
            self.phase = "setup"
            self.header.set_subtitle("Not connected yet")
            self.message_page.configure(
                "Connect to your catalog",
                "Sign in to your EvilFlowers server and pick a catalog. This window then compares it with your books.",
                "Set up…",
                self.open_settings,
                tooltip="Open the plugin settings.",
            )
            self.update_view()
            self.update_summary()
            return
        # Until the server answers there is no catalog title, only the configured name.
        where = f"{settings.catalog} on {host_of(settings.base_url)}"
        self.header.set_subtitle(self._subtitle(where, settings.username))
        self.phase = "loading"
        self._scanning = True
        self.loading_page.detail.setText("")
        self.update_view()
        worker = SyncWorker(self.gui.current_db.new_api, settings)
        worker.scanned.connect(self.on_scanned)
        self.start(worker)

    def synchronize(self) -> None:
        items = [item for item in self.model.items if item.action is not Action.SKIP]
        if not items or self.worker is not None:
            return
        self.banner.hide()
        self._scanning = False
        worker = SyncWorker(self.gui.current_db.new_api, Settings.load(), items=list(self.model.items))
        worker.applied.connect(self.on_applied)
        self.start(worker)

    def stop(self) -> None:
        if self.worker is not None:
            self.worker.abort()
            self.stop_button.setEnabled(False)
            self.loading_page.cancel.setEnabled(False)
            self.on_progress(self.progress.value() / 1000, "Stopping after the current book…")

    @staticmethod
    def _subtitle(catalog: str, user: str) -> str:
        return f"{catalog} · signed in as {user}" if user else catalog

    def on_connected(self, catalog: Any, user: Any) -> None:
        self.catalog = catalog
        self.header.set_subtitle(self._subtitle(catalog.title, user.display_name))

    def on_scanned(self, items: list[PlanItem], catalog: Any) -> None:
        self.catalog = catalog
        self.phase = "ready"
        self.model.set_items(items)
        self.proxy.invalidateFilter()

    def on_applied(self, report: Report) -> None:
        self.model.set_report(report)
        self.update_result_column()
        self.refresh_library(report)
        self.on_progress(1.0, report.summary())
        if report.failed:
            count = len(report.failed)
            self.banner.show_message(
                f"{count} {'book was' if count == 1 else 'books were'} not synchronised. The others were."
                " Hover the Result column for the reason.",
                details="\n".join(f"{o.item.title}: {o.message}" for o in report.failed),
            )

    def on_failed(self, message: str) -> None:
        if self._scanning:
            # The page offers Retry; Settings… is in the button bar. No second Retry on the banner.
            self.banner.show_message(message, retry=False)
            self.phase = "failed"
            self.message_page.configure(
                "Couldn't compare yet",
                "Fix the problem above, then try again. Nothing was changed.",
                "Retry",
                self.scan,
                tooltip="Compare the library with the catalog again.",
            )
        else:
            self.banner.show_message(message)
            self.on_progress(self.progress.value() / 1000, f"Stopped: {message}")

    def on_stopped(self) -> None:
        if self._scanning:
            self.phase = "stopped"
            self.message_page.configure(
                "Comparison stopped",
                "Nothing was changed.",
                "Compare again",
                self.scan,
                tooltip="Compare the library with the catalog again.",
            )

    def on_finished(self) -> None:
        worker, self.worker = self.worker, None
        if worker is not None:
            worker.deleteLater()
        self._scanning = False
        self.loading_page.cancel.setEnabled(True)
        if self.phase == "loading":  # finished without a result signal; should not happen
            self.phase = "failed"
        self.set_busy(False)
        self.update_view()

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

    def selected_rows(self) -> list[int]:
        return sorted({self.proxy.mapToSource(index).row() for index in self.table.selectionModel().selectedRows()})

    def context_menu(self, pos: Any) -> None:
        rows = self.selected_rows()
        if not rows:
            return
        menu = QMenu(self)
        for action in (Action.PUSH, Action.PULL, Action.SKIP):
            allowed = [row for row in rows if action in self.model.items[row].choices]
            entry = menu.addAction(f"{ACTION_TEXT[action]} {len(allowed)} selected")
            entry.setToolTip(ACTION_HELP[action])
            entry.setEnabled(bool(allowed))
            entry.triggered.connect(lambda _=False, a=action, r=allowed: [self.model.set_action(row, a) for row in r])
        menu.exec(self.table.viewport().mapToGlobal(pos))

    def open_settings(self) -> None:
        self.action.show_settings(self)

    # --------------------------------------------------------------- details

    def _details_row(self) -> int | None:
        rows = self.selected_rows()
        if len(rows) != 1 or self.model.items[rows[0]].state not in DECISION_STATES:
            return None
        return rows[0]

    def update_details(self, *_args: Any) -> None:
        """Show the comparison panel while exactly one row that needs a decision is selected."""
        row = self._details_row()
        if row is None:
            self.details.item = None
            self.details.hide()
            return
        item = self.model.items[row]
        entry_id = str(item.entry.id)
        detail = self._details.get(entry_id)
        if detail is None:
            self._fetch_detail(item)
            entry, detailed, note = item.entry, False, "Loading publisher and date from the catalog…"
        elif isinstance(detail, str):
            entry, detailed, note = item.entry, False, f"Publisher and date could not be loaded: {detail}"
        else:
            entry, detailed, note = detail, True, ""
        try:
            mi = self.gui.current_db.new_api.get_metadata(item.book.book_id)
            diffs = compare(mi, item.book, entry, detailed=detailed)
        except Exception as exc:  # the book may have been deleted since the scan
            diffs, note = [], f"This book could not be read from the library: {exc}"
        self.details.show_item(item, diffs, note)
        if not self.details.isVisible():
            self.details.show()
            self._size_details()

    def _size_details(self) -> None:
        """Give the comparison panel about 40% of the width when it opens; the user can drag it."""
        total = sum(self.splitter.sizes()) or self.splitter.width()
        panel = max(self.details.minimumWidth(), min(self.details.maximumWidth(), int(total * 0.4)))
        self.splitter.setSizes([max(total - panel, self.table.minimumWidth()), panel])

    def _fetch_detail(self, item: PlanItem) -> None:
        """Fetch an entry's detail (publisher, date) once, off the GUI thread."""
        entry_id = str(item.entry.id)
        if self.catalog is None or entry_id in self._detail_tasks:
            return
        settings, catalog_id, uuid = Settings.load(), self.catalog.id, item.entry.id
        # The task is not a child of this window and outlives it; reject() disconnects these lambdas.
        self._detail_tasks[entry_id] = run_task(
            lambda: client_for(settings).get_entry(catalog_id, uuid),
            on_success=lambda entry, key=entry_id: self._detail_arrived(key, entry),
            on_failure=lambda message, key=entry_id: self._detail_arrived(key, message),
        )

    def _detail_arrived(self, entry_id: str, detail: Any) -> None:
        self._detail_tasks.pop(entry_id, None)
        self._details[entry_id] = detail
        if self.details.item is not None and self.details.item.entry_id == entry_id:
            self.update_details()

    def choose_for_selected(self, action: Action) -> None:
        row = self._details_row()
        if row is not None:
            self.model.set_action(row, action)

    # ---------------------------------------------------------------- covers

    def request_cover(self, key: tuple[str, Any]) -> None:
        if self._loader is None:
            if self._thumbs is None:
                self._thumbs = Path(tempfile.mkdtemp(prefix="evilflowers-thumbs-"))
            dpr = self.model.dpr
            size = QSize(round(COVER_SIZE.width() * dpr), round(COVER_SIZE.height() * dpr))
            # No parent: worker.start_thread keeps it alive until it ends, even after this window.
            self._loader = CoverLoader(self.gui.current_db.new_api, Settings.load(), self._thumbs, size)
            self._loader.loaded.connect(self.model.set_cover)
        self._loader.request(key)

    def _retire_loader(self) -> None:
        """Stop the cover loader without waiting: it may be in the middle of a download."""
        loader, self._loader = self._loader, None
        if loader is not None:
            disconnect_all(loader.loaded)
            loader.stop()

    # -------------------------------------------------------------- lifecycle

    def showEvent(self, event: Any) -> None:
        super().showEvent(event)
        dpr = self.devicePixelRatioF() or 1.0
        if dpr != self.model.dpr:  # covers are drawn at the screen's resolution
            self.model.dpr = dpr
            self.model.refresh_theme()
        if not self.model.items and self.worker is None:
            self.scan()

    def reject(self) -> None:
        if self.worker is not None:
            # A sync writes to the library: give it a chance to stop after the current book,
            # so nothing changes once the window is gone. It is not this window's child, so
            # if it takes longer it simply finishes on its own.
            self.worker.abort()
            self.worker.wait(30_000)
            disconnect_all(
                self.worker.progress,
                self.worker.connected,
                self.worker.scanned,
                self.worker.applied,
                self.worker.failed,
                self.worker.stopped,
            )
            # Only this window's slot: worker.start_thread has its own on `finished`.
            with contextlib.suppress(TypeError, RuntimeError):
                self.worker.finished.disconnect(self.on_finished)
            self.worker = None
        self._retire_loader()
        for task in self._detail_tasks.values():  # lookups need no waiting; their answers are dropped
            task.disconnect_all()
        self._detail_tasks.clear()
        if self._thumbs is not None:
            shutil.rmtree(self._thumbs, ignore_errors=True)
            self._thumbs = None
        super().reject()
