from qt.core import QDialog, QVBoxLayout, QPushButton, QMessageBox, QLabel, QFont, QHBoxLayout, QTableView, \
    QHeaderView, QAbstractItemView

from calibre_plugins.evilflowers.config import prefs
from calibre_plugins.evilflowers.model import OpdsEntriesModel


class EvilFlowersDialog(QDialog):

    def __init__(self, gui, icon, do_user_config):
        QDialog.__init__(self, gui)
        self.gui = gui
        self.do_user_config = do_user_config

        # The current database shown in the GUI
        # db is an instance of the class LibraryDatabase from db/legacy.py
        # This class has many, many methods that allow you to do a lot of
        # things. For most purposes you should use db.new_api, which has
        # a much nicer interface from db/cache.py
        self.db = gui.current_db.new_api
        self.model = OpdsEntriesModel(self.db)

        self.setWindowTitle('EvilFlowers Plugin')
        self.setWindowIcon(icon)
        self.resize(800, 600)

        self.l = QVBoxLayout()
        self.setLayout(self.l)

        self.label = QLabel(f"{prefs['catalog']} @ {prefs['base_url']}")
        self.label.setFont(QFont('Arial', 40))
        self.l.addWidget(self.label)

        # Table
        self.table = QTableView()
        self.table.setAlternatingRowColors(True)
        self.table.setModel(self.model)
        self.table.horizontalHeader().setSectionResizeMode(0, QHeaderView.Stretch)
        self.table.horizontalHeader().setSectionResizeMode(1, QHeaderView.Stretch)
        self.table.horizontalHeader().setSectionResizeMode(2, QHeaderView.Stretch)
        self.table.horizontalHeader().setSectionResizeMode(3, QHeaderView.Stretch)
        self.table.horizontalHeader().setSectionResizeMode(4, QHeaderView.Stretch)
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

    def about(self):
        text = get_resources('about.txt')
        QMessageBox.about(self, 'About the EvilFlowers plugin', text.decode('utf-8'))

    def sync(self):
        pass

    def config(self):
        self.do_user_config(parent=self)
        self.label.setText(prefs['base_url'])
