"""The toolbar button and menu."""

from __future__ import annotations

from typing import Any

from calibre.gui2 import info_dialog
from calibre.gui2.actions import InterfaceAction
from qt.core import QToolButton, QWidget

from .settings import migrate
from .theme import load_icon, on_theme_change

PLUGIN_NAME = "EvilFlowers"


class EvilFlowersAction(InterfaceAction):
    name = PLUGIN_NAME
    action_spec = (PLUGIN_NAME, None, "Sync this library with an EvilFlowers catalog", None)
    action_type = "current"
    popup_type = QToolButton.ToolButtonPopupMode.MenuButtonPopup
    action_add_menu = True
    action_menu_clone_qaction = "Synchronize…"

    def genesis(self) -> None:
        migrate()
        self.dialog: Any = None
        self.browse_dialog: Any = None
        self.highlights_dialog: Any = None  # set and cleared by highlights_dialog.show_highlights
        self.icon = load_icon()
        self.qaction.setIcon(self.icon)
        on_theme_change(self.refresh_icon)
        self.qaction.triggered.connect(self.show_dialog)
        menu = self.qaction.menu()
        # QAction.triggered passes a `checked` bool; lambdas keep it out of the slots' arguments.
        self.create_menu_action(
            menu,
            "evilflowers-browse",
            "Browse catalog…",
            "search.png",
            description="Search the catalog, download books and manage your shelf",
            triggered=lambda *_: self.show_browse(),
        )
        self.create_menu_action(
            menu,
            "evilflowers-highlights",
            "Highlights for selected book…",
            "highlight.png",
            description="Show what you highlighted in the selected books on EvilFlowers",
            triggered=lambda *_: self.show_highlights(),
        )
        menu.addSeparator()
        self.create_menu_action(
            menu, "evilflowers-settings", "Settings…", "config.png", triggered=lambda *_: self.show_settings()
        )
        self.create_menu_action(
            menu, "evilflowers-about", "About EvilFlowers", "help.png", triggered=lambda *_: self.about()
        )

    def windows(self) -> list[Any]:
        """The plugin's windows that are currently open."""
        candidates = (self.dialog, self.browse_dialog, self.highlights_dialog)
        return [window for window in candidates if window is not None and window.isVisible()]

    def refresh_icon(self) -> None:
        """Swap to the light or dark icon after the user switches Calibre's theme."""
        self.icon = load_icon()
        self.qaction.setIcon(self.icon)
        for window in self.windows():
            window.setWindowIcon(self.icon)

    def show_dialog(self) -> None:
        if self.dialog is not None and self.dialog.isVisible():
            self.dialog.raise_()
            self.dialog.activateWindow()
            return
        from .dialog import SyncDialog

        self.dialog = SyncDialog(self.gui, self.icon, self)
        self.dialog.finished.connect(self._dialog_closed)
        self.dialog.show()

    def _dialog_closed(self, _result: int) -> None:
        if self.dialog is not None:
            self.dialog.deleteLater()
        self.dialog = None

    def show_browse(self) -> None:
        from .browse import show_browse

        self.browse_dialog = show_browse(self.gui, self.icon, self)

    def show_highlights(self) -> None:
        from .highlights_dialog import show_highlights

        show_highlights(self.gui, self.icon, self)

    def show_settings(self, parent: Any = None) -> None:
        if not isinstance(parent, QWidget):  # e.g. a signal's `checked` flag
            parent = self.gui
        self.interface_action_base_plugin.do_user_config(parent)

    def apply_settings(self) -> None:
        # The highlights window reads the settings once per selection; reopening it picks them up.
        for window in (self.dialog, self.browse_dialog):
            if window is not None and window.isVisible():
                window.settings_changed()

    def about(self) -> None:
        text = get_resources("about.txt").decode("utf-8")
        info_dialog(self.gui, f"About {PLUGIN_NAME}", text, show=True, show_copy_button=False)

    def close_windows(self) -> None:
        for window in self.windows():
            window.reject()

    def library_changed(self, db: Any) -> None:
        # Plans, "in your library" badges and selected books all belong to the previous
        # library; never apply them to the new one.
        self.close_windows()

    def shutting_down(self) -> bool:
        self.close_windows()
        # Windows close without waiting; at exit, give their requests (short timeouts,
        # stopped where they can be) a moment to end rather than be torn down mid-run.
        from .worker import wait_for_threads

        wait_for_threads(10_000)
        return True
