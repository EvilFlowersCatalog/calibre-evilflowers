from calibre.customize import InterfaceActionBase


class EvilFlowers(InterfaceActionBase):
    name = 'EvilFlowers OPDS client'
    description = 'Calibre plugin for bi-directional synchronisation with EvilFlowers OPDS server.'
    supported_platforms = ['windows', 'osx', 'linux']
    author = 'Jakub Dubec'
    version = (0, 1, 0)
    minimum_calibre_version = (5, 0, 0)

    actual_plugin = 'calibre_plugins.evilflowers.ui:EvilFlowersInterfacePlugin'

    def is_customizable(self):
        return True

    def config_widget(self):
        from calibre_plugins.evilflowers.config import ConfigWidget

        return ConfigWidget()

    def save_settings(self, config_widget):
        config_widget.save_settings()

        ac = self.actual_plugin_
        if ac is not None:
            ac.apply_settings()
