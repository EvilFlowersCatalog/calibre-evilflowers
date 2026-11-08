"""Theme helpers in a light and a dark palette, headless (Qt offscreen).

Runs in the suite once "test_theme" is listed in tests/run.py, or alone with
`scripts/dev.sh debug tests/test_theme.py`.
"""

from __future__ import annotations

import os
import sys
import unittest
from pathlib import Path
from typing import Any

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from calibre.gui2 import Application
from qt.core import QColor, QImage, QPalette, Qt

from calibre_plugin import theme
from calibre_plugin.sync.plan import State

_app = None


def app() -> Any:
    global _app
    if _app is None:
        _app = Application.instance() or Application([])
    return _app


def palette(window: QColor, base: QColor, text: QColor) -> QPalette:
    result = QPalette()
    result.setColor(QPalette.ColorRole.Window, window)
    result.setColor(QPalette.ColorRole.Base, base)
    result.setColor(QPalette.ColorRole.Text, text)
    result.setColor(QPalette.ColorRole.WindowText, text)
    return result


def ink_pixels(image: QImage) -> int:
    """Dark pixels on the header's paper third, where the title goes."""
    return sum(1 for x in range(10, 190, 2) for y in range(16, 80, 2) if image.pixelColor(x, y).lightness() < 90)


LIGHT = palette(QColor(239, 239, 239), QColor(255, 255, 255), QColor(0, 0, 0))
DARK = palette(QColor(45, 45, 45), QColor(30, 30, 30), QColor(230, 230, 230))


class ThemeTests(unittest.TestCase):
    def setUp(self) -> None:
        app()

    def test_icon_follows_the_theme(self) -> None:
        self.assertEqual(theme.icon_path(dark=False), "images/icon.png")
        self.assertEqual(theme.icon_path(dark=True), "images/icon-dark.png")
        self.assertIn(theme.icon_path(), ("images/icon.png", "images/icon-dark.png"))
        for dark in (False, True):
            self.assertFalse(theme.load_icon(dark).isNull())

    def test_dark_detection_reads_the_given_palette(self) -> None:
        self.assertFalse(theme.is_dark(LIGHT))
        self.assertTrue(theme.is_dark(DARK))

    def test_header_pixmap_is_scaled_to_the_requested_height(self) -> None:
        pixmap = theme.header_pixmap(60)
        self.assertFalse(pixmap.isNull())
        self.assertEqual(round(pixmap.height() / pixmap.devicePixelRatio()), 60)
        self.assertEqual(pixmap.width(), pixmap.height() * 4)  # 960x240 keeps its aspect

    def test_header_paints_the_image_and_the_title(self) -> None:
        header = theme.Header("EvilFlowers", "Sync your library")
        header.resize(600, 96)
        self.assertEqual(header.height(), 96)
        paper = theme._header_source().toImage().pixelColor(8, 120)
        self.assertGreater(theme.contrast(theme.INK, paper), 7)
        blank = theme.Header("")
        blank.resize(600, 96)
        self.assertEqual(ink_pixels(blank.grab().toImage()), 0, "the paper third is calm")
        self.assertGreater(ink_pixels(header.grab().toImage()), 50, "the title is drawn on the paper")
        header.set_title("Settings")
        header.set_subtitle("")
        self.assertEqual(header.title(), "Settings")
        header.grab()

    def test_every_state_has_a_tint_except_in_sync(self) -> None:
        for pal in (LIGHT, DARK):
            base = pal.color(QPalette.ColorRole.Base)
            for state in State:
                brush = theme.state_brush(state, pal)
                if state is State.IN_SYNC:
                    self.assertIsNone(brush)
                    continue
                self.assertIsNotNone(brush, state)
                self.assertEqual(brush.style(), Qt.BrushStyle.SolidPattern)
                color = brush.color()
                self.assertNotEqual(color, base, state)
                # Subtle: the row text stays as legible as on the plain base.
                text = pal.color(QPalette.ColorRole.Text)
                self.assertGreater(theme.contrast(text, color), 7, (state, color.name()))
            self.assertEqual(theme.state_color("LOCAL_ONLY", pal), theme.state_color(State.LOCAL_ONLY, pal))
            self.assertEqual(theme.state_color("upload", pal), theme.state_color(State.LOCAL_ONLY, pal))
            self.assertIsNone(theme.state_color("nonsense", pal))

    def test_chips_are_legible_in_both_themes(self) -> None:
        for pal in (LIGHT, DARK):
            for kind in ("upload", "download", "conflict", "skip", "in sync", "changed", "deleted", "unknown"):
                background, text, _border = theme.chip_colors(kind, pal)
                self.assertGreaterEqual(theme.contrast(text, background), 4.5, (kind, theme.is_dark(pal)))
                style = theme.chip_style(kind, pal)
                self.assertIn("background-color: #", style)
                self.assertIn(text.name(), style)
        self.assertNotEqual(theme.chip_style("upload", LIGHT), theme.chip_style("upload", DARK))

    def test_helper_text_meets_aa_contrast_on_the_window(self) -> None:
        for pal in (LIGHT, DARK):
            color = theme.helper_color(pal)
            window = pal.color(QPalette.ColorRole.Window)
            self.assertGreaterEqual(theme.contrast(color, window), 4.5, theme.is_dark(pal))
            self.assertNotEqual(color, pal.color(QPalette.ColorRole.WindowText), "still reads as secondary")
        label = theme.HelperLabel("Helps.")
        self.assertTrue(label.wordWrap())
        self.assertIn("color: #", label.styleSheet())

    def test_dark_chip_text_is_calm_not_neon(self) -> None:
        for kind in ("upload", "download", "changed", "conflict", "in sync"):
            _background, text, _border = theme.chip_colors(kind, DARK)
            self.assertLessEqual(text.hslSaturationF(), 0.6, (kind, text.name()))
            self.assertGreater(text.lightnessF(), 0.6, (kind, text.name()))

    def test_on_theme_change_connects_to_calibre(self) -> None:
        calls: list[int] = []
        self.assertTrue(theme.on_theme_change(lambda: calls.append(1)))
        app().palette_changed.emit()
        self.assertEqual(calls, [1])


if __name__ == "__main__":
    # calibre-debug -e does not register this script as sys.modules["__main__"].
    suite = unittest.TestLoader().loadTestsFromTestCase(ThemeTests)
    raise SystemExit(0 if unittest.TextTestRunner(verbosity=2).run(suite).wasSuccessful() else 1)
