"""Look and feel shared by every window: the icon, the header banner and state colours.

Everything here adapts to Calibre's light and dark themes. Colours are derived from the
current QPalette instead of being hard-coded, so they stay readable on any theme. Each
colour function takes an optional palette, which defaults to the application's.
"""

from __future__ import annotations

from collections.abc import Callable
from functools import cache
from pathlib import Path
from typing import Any

from qt.core import (
    QApplication,
    QBrush,
    QColor,
    QFont,
    QIcon,
    QLabel,
    QPainter,
    QPalette,
    QPixmap,
    QRect,
    QRectF,
    QSize,
    QSizePolicy,
    Qt,
    QWidget,
)

PLUGIN_NAME = "EvilFlowers"
ICON_LIGHT = "images/icon.png"
ICON_DARK = "images/icon-dark.png"
HEADER = "images/header.png"

# Dark ink for text on the header's paper. The paper is light in both themes, so the
# text colour is fixed rather than taken from the palette.
INK = QColor(43, 34, 26)
INK_SOFT = QColor(92, 78, 64)

# One accent hue per kind of row. Backgrounds blend these into the palette's Base colour.
ACCENTS = {
    "upload": QColor(52, 120, 246),
    "download": QColor(46, 160, 67),
    "changed": QColor(137, 87, 229),
    "conflict": QColor(230, 126, 0),
    "deleted": QColor(128, 128, 128),
    "skip": QColor(128, 128, 128),
    "in_sync": QColor(84, 130, 150),  # calm slate, so "in sync" never reads as "download"
}

# sync.plan.State names → accent kind. IN_SYNC gets no row tint: most rows are in sync
# and a tint on all of them would hide the ones that need attention.
STATE_KINDS = {
    "IN_SYNC": None,
    "LOCAL_ONLY": "upload",
    "REMOTE_ONLY": "download",
    "LOCAL_CHANGED": "changed",
    "REMOTE_CHANGED": "changed",
    "CONFLICT": "conflict",
    "UNLINKED": "conflict",
    "LOCAL_DELETED": "deleted",
    "REMOTE_DELETED": "deleted",
}


# --- resources ----------------------------------------------------------------


def _resource(name: str) -> bytes:
    """Bytes of a file shipped with the plugin.

    Calibre injects get_resources into each module loaded from the plugin ZIP. The test
    suite imports the package from the source tree, where it is missing, so fall back
    to reading the file next to this module.
    """
    loader = globals().get("get_resources")
    if loader is not None:
        data = loader(name)
        if data:
            return data
    path = Path(__file__).resolve().parent / name
    return path.read_bytes() if path.is_file() else b""


def _pixmap(name: str) -> QPixmap:
    pixmap = QPixmap()
    pixmap.loadFromData(_resource(name))
    return pixmap


# --- theme detection ------------------------------------------------------------


def _palette(palette: QPalette | None) -> QPalette:
    if palette is not None:
        return palette
    app = QApplication.instance()
    return app.palette() if app is not None else QPalette()


def is_dark(palette: QPalette | None = None) -> bool:
    """True when the palette (by default the application's) is a dark theme."""
    if palette is None:
        try:
            from calibre.gui2 import is_dark_theme

            return bool(is_dark_theme())
        except Exception:
            palette = _palette(None)
    check = getattr(palette, "is_dark_theme", None)  # Calibre patches this onto QPalette
    if callable(check):
        return bool(check())
    return palette.color(QPalette.ColorRole.Window).lightness() < 128


def on_theme_change(callback: Callable[[], Any]) -> bool:
    """Call `callback()` whenever the user switches Calibre's theme.

    Returns False, and does nothing, when the application has no palette_changed
    signal (it is Calibre's, not Qt's). Pass a bound method of a QObject rather than a
    lambda: PyQt then drops the connection when that object is deleted.
    """
    signal = getattr(QApplication.instance(), "palette_changed", None)
    if signal is None:
        return False
    signal.connect(callback)
    return True


# --- icon and header -----------------------------------------------------------


def icon_path(dark: bool | None = None) -> str:
    """The plugin icon for the theme. Calibre has no light/dark lookup for plugin ZIPs."""
    return ICON_DARK if (is_dark() if dark is None else dark) else ICON_LIGHT


def load_icon(dark: bool | None = None) -> QIcon:
    """The plugin icon for the current (or the given) theme."""
    name = icon_path(dark)
    loader = globals().get("get_icons")
    if loader is not None:
        return loader(name, PLUGIN_NAME)
    return QIcon(_pixmap(name))


@cache
def _header_source() -> QPixmap:
    return _pixmap(HEADER)


def _device_pixel_ratio() -> float:
    screen = QApplication.primaryScreen() if QApplication.instance() is not None else None
    return screen.devicePixelRatio() if screen is not None else 1.0


def header_pixmap(height: int = 96) -> QPixmap:
    """The header image `height` logical pixels tall, sharp on high-DPI screens."""
    dpr = _device_pixel_ratio()
    source = _header_source()
    if source.isNull():
        return QPixmap()
    scaled = source.scaledToHeight(round(height * dpr), Qt.TransformationMode.SmoothTransformation)
    scaled.setDevicePixelRatio(dpr)
    return scaled


class Header(QWidget):
    """The banner at the top of a window: the EvilFlowers painting with a title on its paper.

    The image covers the whole widget, keeping its aspect ratio. It is anchored left so
    the calm paper third, where the title sits, is always visible.
    """

    def __init__(self, title: str, subtitle: str = "", parent: QWidget | None = None, height: int = 96) -> None:
        super().__init__(parent)
        self._title = title
        self._subtitle = subtitle
        self._height = height
        self.setSizePolicy(QSizePolicy.Policy.Expanding, QSizePolicy.Policy.Fixed)
        self.setFixedHeight(height)
        self.setAccessibleName(title)

    def set_title(self, title: str) -> None:
        self._title = title
        self.setAccessibleName(title)
        self.update()

    def set_subtitle(self, subtitle: str) -> None:
        self._subtitle = subtitle
        self.update()

    def title(self) -> str:
        return self._title

    def subtitle(self) -> str:
        return self._subtitle

    def sizeHint(self) -> QSize:
        return QSize(480, self._height)

    def minimumSizeHint(self) -> QSize:
        return QSize(240, self._height)

    def paintEvent(self, _event: Any) -> None:
        painter = QPainter(self)
        painter.setRenderHint(QPainter.RenderHint.SmoothPixmapTransform)
        painter.setRenderHint(QPainter.RenderHint.Antialiasing)
        self._paint_background(painter)
        self._paint_text(painter)
        painter.end()

    def _paint_background(self, painter: QPainter) -> None:
        source = _header_source()
        if source.isNull():
            painter.fillRect(self.rect(), QColor(240, 234, 222))
            return
        # Cover: scale so the image fills both dimensions, then crop the overflow.
        w, h = max(self.width(), 1), max(self.height(), 1)
        scale = max(w / source.width(), h / source.height())
        crop_w, crop_h = w / scale, h / scale
        top = (source.height() - crop_h) / 2  # centred vertically, anchored left
        painter.drawPixmap(QRectF(self.rect()), source, QRectF(0, top, crop_w, crop_h))

    def _paint_text(self, painter: QPainter) -> None:
        pad = max(12, self.height() // 5)
        area = QRect(pad, 0, max(self.width() // 2, 200), self.height())
        title_font = QFont(self.font())
        title_font.setPointSizeF(title_font.pointSizeF() * 1.6)
        title_font.setBold(True)
        if not self._subtitle:
            painter.setFont(title_font)
            painter.setPen(INK)
            painter.drawText(area, Qt.AlignmentFlag.AlignLeft | Qt.AlignmentFlag.AlignVCenter, self._title)
            return
        mid = self.height() // 2
        painter.setFont(title_font)
        painter.setPen(INK)
        painter.drawText(
            QRect(area.left(), 0, area.width(), mid + 2),
            Qt.AlignmentFlag.AlignLeft | Qt.AlignmentFlag.AlignBottom,
            self._title,
        )
        painter.setFont(QFont(self.font()))
        painter.setPen(INK_SOFT)
        painter.drawText(
            QRect(area.left(), mid + 4, area.width(), self.height() - mid - 4),
            Qt.AlignmentFlag.AlignLeft | Qt.AlignmentFlag.AlignTop | Qt.TextFlag.TextWordWrap,
            self._subtitle,
        )


# --- state colours ------------------------------------------------------------------


def _blend(base: QColor, accent: QColor, alpha: float) -> QColor:
    """`accent` laid over `base` at opacity `alpha`, as an opaque colour."""
    return QColor(
        round(base.red() * (1 - alpha) + accent.red() * alpha),
        round(base.green() * (1 - alpha) + accent.green() * alpha),
        round(base.blue() * (1 - alpha) + accent.blue() * alpha),
    )


def _luminance(color: QColor) -> float:
    def channel(value: int) -> float:
        c = value / 255
        return c / 12.92 if c <= 0.03928 else ((c + 0.055) / 1.055) ** 2.4

    return 0.2126 * channel(color.red()) + 0.7152 * channel(color.green()) + 0.0722 * channel(color.blue())


def contrast(a: QColor, b: QColor) -> float:
    """WCAG contrast ratio between two colours, from 1 (none) to 21."""
    hi, lo = sorted((_luminance(a), _luminance(b)), reverse=True)
    return (hi + 0.05) / (lo + 0.05)


def _state_kind(state: Any) -> str | None:
    """Accept a sync.plan.State, its name ("LOCAL_ONLY") or a kind ("upload", "in sync")."""
    key = getattr(state, "name", state)
    if not isinstance(key, str):
        return None
    if key in STATE_KINDS:
        return STATE_KINDS[key]
    kind = _kind(key)
    return kind if kind in ACCENTS and kind != "in_sync" else None


def _kind(kind: str) -> str:
    return "_".join(kind.strip().lower().replace("-", " ").split())


def state_color(state: Any, palette: QPalette | None = None) -> QColor | None:
    """A subtle row background for a plan state, or None for rows that need no tint.

    The accent hue is blended into the palette's Base colour, a little stronger on dark
    themes where a low-alpha tint would otherwise vanish.
    """
    kind = _state_kind(state)
    if kind is None:
        return None
    palette = _palette(palette)
    alpha = 0.22 if is_dark(palette) else 0.13
    return _blend(palette.color(QPalette.ColorRole.Base), ACCENTS[kind], alpha)


def state_brush(state: Any, palette: QPalette | None = None) -> QBrush | None:
    """state_color() as a QBrush, ready for Qt.ItemDataRole.BackgroundRole."""
    color = state_color(state, palette)
    return QBrush(color) if color is not None else None


def _soft_light(accent: QColor) -> QColor:
    """The accent as light text for a dark theme: same hue, light, and never neon.

    QColor.lighter() scales the value channel, which turns a saturated green into a
    glaring #4aff6c. Fixing lightness and capping saturation in HSL keeps every kind
    equally calm.
    """
    hue = max(accent.hslHueF(), 0.0)  # -1 for greys
    return QColor.fromHslF(hue, min(accent.hslSaturationF(), 0.55), 0.76)


def helper_color(palette: QPalette | None = None) -> QColor:
    """Grey for helper and secondary text on the window background, at least 4.5:1 (WCAG AA).

    The palette's text colour is blended towards the window colour only as far as that
    contrast allows, so it reads as secondary without going faint on dark themes.
    """
    palette = _palette(palette)
    active = QPalette.ColorGroup.Active
    text = palette.color(active, QPalette.ColorRole.WindowText)
    window = palette.color(active, QPalette.ColorRole.Window)
    alpha = 0.45
    color = _blend(text, window, alpha)
    while alpha > 0 and contrast(color, window) < 4.5:
        alpha = max(0.0, alpha - 0.05)
        color = _blend(text, window, alpha)
    return color


class HelperLabel(QLabel):
    """The small grey line that says what a field or a control is for.

    Its colour comes from :func:`helper_color` and follows Calibre's theme switches.
    The application palette is used, never the label's own: a widget's palette picks
    up the stylesheet set on it.
    """

    def __init__(self, text: str = "", parent: QWidget | None = None, *, scale: float = 0.9) -> None:
        super().__init__(text, parent)
        self.setWordWrap(True)
        font = QFont(self.font())
        font.setPointSizeF(max(font.pointSizeF() * scale, 8))
        self.setFont(font)
        self.refresh_theme()
        on_theme_change(self.refresh_theme)

    def refresh_theme(self) -> None:
        self.setStyleSheet(f"color: {helper_color().name()};")


def chip_colors(kind: str, palette: QPalette | None = None) -> tuple[QColor, QColor, QColor]:
    """(background, text, border) for a pill label of `kind`.

    The text is the accent darkened on light themes and lightened on dark ones; if that
    is not legible on the chip it falls back to the palette's own text colour.
    """
    palette = _palette(palette)
    accent = ACCENTS.get(_kind(kind), ACCENTS["skip"])
    dark = is_dark(palette)
    background = _blend(palette.color(QPalette.ColorRole.Base), accent, 0.30 if dark else 0.16)
    text = _soft_light(accent) if dark else accent.darker(170)
    if contrast(text, background) < 4.5:
        text = palette.color(QPalette.ColorRole.Text)
    border = _blend(background, accent, 0.5)
    return background, text, border


def chip_style(kind: str, palette: QPalette | None = None) -> str:
    """A stylesheet for a small pill/badge label: upload, download, conflict, skip, in sync.

    Use it on the label itself: `label.setStyleSheet(chip_style("upload"))`. Unknown kinds
    get the neutral skip look. Re-apply it from on_theme_change() so the chip follows
    the theme.
    """
    background, text, border = chip_colors(kind, palette)
    return (
        f"background-color: {background.name()}; color: {text.name()};"
        f" border: 1px solid {border.name()}; border-radius: 8px;"
        " padding: 1px 8px; font-weight: 600;"
    )
