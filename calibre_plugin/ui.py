"""The toolbar button and menu."""

from __future__ import annotations

from typing import Any

from calibre.gui2 import info_dialog
from calibre.gui2.actions import InterfaceAction
from qt.core import QToolButton, QWidget

from .settings import migrate

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
        self.icon = get_icons("images/icon.png", PLUGIN_NAME)
        self.qaction.setIcon(self.icon)
        self.qaction.triggered.connect(self.show_dialog)
        menu = self.qaction.menu()
        # QAction.triggered passes a `checked` bool; lambdas keep it out of the slots' arguments.
        self.create_menu_action(
            menu, "evilflowers-settings", "Settings…", "config.png", triggered=lambda *_: self.show_settings()
        )
        self.create_menu_action(
            menu, "evilflowers-about", "About EvilFlowers", "help.png", triggered=lambda *_: self.about()
        )

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

    def show_settings(self, parent: Any = None) -> None:
        if not isinstance(parent, QWidget):  # e.g. a signal's `checked` flag
            parent = self.gui
        self.interface_action_base_plugin.do_user_config(parent)

    def apply_settings(self) -> None:
        if self.dialog is not None:
            self.dialog.settings_changed()

    def about(self) -> None:
        text = get_resources("about.txt").decode("utf-8")
        info_dialog(self.gui, f"About {PLUGIN_NAME}", text, show=True, show_copy_button=False)

    def library_changed(self, db: Any) -> None:
        # The plan belongs to the previous library; never apply it to the new one.
        if self.dialog is not None:
            self.dialog.reject()

    def shutting_down(self) -> bool:
        if self.dialog is not None:
            self.dialog.reject()
        return True
