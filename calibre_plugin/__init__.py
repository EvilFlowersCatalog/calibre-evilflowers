"""EvilFlowers: keep a Calibre library in sync with an EvilFlowers catalog.

No Qt imports here: Calibre loads this module in worker processes too. The GUI
lives in ``ui.py`` (``actual_plugin``) and the settings widget in ``config.py``.
"""

from calibre.customize import InterfaceActionBase


class EvilFlowersPlugin(InterfaceActionBase):
    name = "EvilFlowers"
    description = (
        "Two-way sync between this Calibre library and an EvilFlowers catalog: books, metadata, covers and files."
    )
    supported_platforms = ("windows", "osx", "linux")
    author = "Jakub Dubec"
    version = (2, 1, 0)
    minimum_calibre_version = (7, 0, 0)  # Qt 6 + Python 3.11

    actual_plugin = "calibre_plugins.evilflowers.ui:EvilFlowersAction"

    def is_customizable(self) -> bool:
        return True

    def config_widget(self):  # type: ignore[no-untyped-def]
        from calibre_plugins.evilflowers.config import ConfigWidget

        return ConfigWidget()

    def save_settings(self, config_widget) -> None:  # type: ignore[no-untyped-def]
        config_widget.save_settings()
        action = self.actual_plugin_
        if action is not None:
            action.apply_settings()
