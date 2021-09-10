from qt.core import QAbstractTableModel, Qt, QVariant


class OpdsEntriesModel(QAbstractTableModel):
    column_headers = [_("ID"), _("Title"), _("Author(s)"), _("Updated"), _("Synced")]

    def __init__(self, db=None):
        QAbstractTableModel.__init__(self)
        self.db = db
        self.books = []

    def headerData(self, section, orientation, role):
        if role != Qt.DisplayRole:
            return None
        if orientation == Qt.Vertical:
            return section + 1
        if section >= len(self.column_headers):
            return None
        return self.column_headers[section]

    def rowCount(self, parent):
        return len(self.books)

    def columnCount(self, parent):
        return len(self.column_headers)

    def data(self, index, role=Qt.DisplayRole):
        if role == Qt.DisplayRole:
            i = index.row()
            j = index.column()
            return '{0}'.format(self.datatable.iget_value(i, j))
        else:
            return QVariant()

    def flags(self, index):
        return Qt.ItemIsEnabled
