from qt.core import QDialog, QVBoxLayout, QPushButton, QMessageBox, QLabel, QFont, QHBoxLayout, QTableView, \
    QHeaderView, QAbstractItemView

from calibre_plugins.evilflowers.config import prefs
from calibre_plugins.evilflowers.model import EntriesModel
from calibre_plugins.evilflowers.client.api import ApiClient


class EvilFlowersDialog(QDialog):

    def __init__(self, gui, icon, plugin):
        QDialog.__init__(self, gui)
        self._gui = gui
        self._plugin = plugin
        self._db = gui.current_db.new_api
        self._client = ApiClient(plugin)

        self.model = EntriesModel(self._db, self._client)

        self.setWindowTitle('EvilFlowers Plugin')
        self.setWindowIcon(icon)
        self.resize(1000, 800)

        self.l = QVBoxLayout()
        self.setLayout(self.l)

        self.label = QLabel(f"{prefs['catalog']} @ {prefs['base_url']}")
        self.label.setFont(QFont('Arial', 40))
        self.l.addWidget(self.label)

        # Table
        self.table = QTableView()
        self.table.setAlternatingRowColors(True)
        self.table.setModel(self.model)
        self.table.setSelectionBehavior(QAbstractItemView.SelectRows)
        self.table.resizeColumnsToContents()
        self.l.addWidget(self.table)

        # Buttons
        button_layout = QHBoxLayout()

        self.about_button = QPushButton('About', self)
        self.about_button.clicked.connect(self.about)
        button_layout.addWidget(self.about_button)

        self.config_button = QPushButton('Config', self)
        self.config_button.clicked.connect(self.config)
        button_layout.addWidget(self.config_button)

        self.sync_button = QPushButton('Sync', self)
        self.sync_button.clicked.connect(self.sync)
        button_layout.addWidget(self.sync_button)

        self.l.addLayout(button_layout)

        self.reload()

    def about(self):
        text = get_resources('about.txt')
        QMessageBox.about(self, 'About the EvilFlowers plugin', text.decode('utf-8'))

    def sync(self):
        pass

    def config(self):
        self._plugin.do_user_config(parent=self)
        self.reload()

    def reload(self):
        try:
            status = self._client.status.status()
            self.label.setText(f"{prefs['catalog']} @ {status['instance']}")
        except Exception:
            pass
