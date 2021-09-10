from qt.core import QCheckBox, QLineEdit, QGridLayout, QLabel, QWidget

from calibre.utils.config import JSONConfig


prefs = JSONConfig('plugins/evilflowers')

prefs.defaults["base_url"] = "http://localhost:8000"
prefs.defaults["catalog"] = "calibre"
prefs.defaults["token"] = ''


class ConfigWidget(QWidget):
    def __init__(self):
        QWidget.__init__(self)

        self.layout = QGridLayout()
        self.setLayout(self.layout)

        label_column_widths = []

        # Base URL
        # Label
        self.base_url_label = QLabel("EvilFlowers URL: ")
        self.layout.addWidget(self.base_url_label, 0, 0)
        label_column_widths.append(self.layout.itemAtPosition(0, 0).sizeHint().width())

        # Input
        self.base_url_input = QLineEdit(self)
        self.base_url_input.setText(prefs["base_url"])
        self.layout.addWidget(self.base_url_input, 0, 1)
        self.base_url_label.setBuddy(self.base_url_input)

        # Catalog
        # Label
        self.catalog_label = QLabel("OPDS Catalog name: ")
        self.layout.addWidget(self.catalog_label, 2, 0)
        label_column_widths.append(self.layout.itemAtPosition(2, 0).sizeHint().width())

        # Input
        self.catalog_input = QLineEdit(self)
        self.catalog_input.setText(prefs["catalog"])
        self.layout.addWidget(self.catalog_input, 2, 1)
        self.catalog_label.setBuddy(self.catalog_label)

        # Token
        # Label
        self.token_label = QLabel("Bearer token: ")
        self.layout.addWidget(self.token_label, 3, 0)
        label_column_widths.append(self.layout.itemAtPosition(3, 0).sizeHint().width())

        # Input
        self.token_input = QLineEdit(self)
        self.token_input.setText(prefs["token"])
        self.layout.addWidget(self.token_input, 3, 1)
        self.token_label.setBuddy(self.token_input)

        self.layout.setColumnMinimumWidth(1, max(label_column_widths) * 2)

    def save_settings(self):
        prefs["base_url"] = self.base_url_input.text()
        prefs["catalog"] = self.catalog_input.text()
        prefs["token"] = self.token_input.text()
