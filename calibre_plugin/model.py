from qt.core import QAbstractTableModel, Qt, QVariant, QCheckBox


# https://github.com/kovidgoyal/calibre/blob/eb78a761a99ac20a6364f85e12059fec6517d890/src/calibre/db/cache.py#L943
class EntriesModel(QAbstractTableModel):
    column_headers = [_("ID"), _("Title"), _("Author(s)"), _("Updated"), _("Synced")]
    _mapping = {
        'synced': _("Synced"),
        'id': _("ID"),
        'title': _("Title"),
        'author': _("Author(s)"),
        'updated': _("Updated"),
    }

    def __init__(self, db, client):
        super(EntriesModel, self).__init__()
        self._db = db
        self._client = client
        self._data = client.entries.list()

        for book_id in self._db.all_book_ids():
            book = self._db.get_metadata(book_id)
            print(book)

    def headerData(self, section, orientation, role):
        if role != Qt.DisplayRole:
            return None
        if orientation == Qt.Vertical:
            return section + 1
        if section >= len(self._mapping.keys()):
            return None
        return list(self._mapping.values())[section]

    def rowCount(self, parent):
        return len(self._data)

    def columnCount(self, parent):
        return len(self._mapping.keys())

    def data(self, index, role):
        if role != Qt.DisplayRole:
            return QVariant()

        row = index.row()
        column = list(self._mapping.keys())[index.column()]

        if column == 'author':
            return f"{self._data[row]['author']['name']} {self._data[row]['author']['surname']}"
        elif column == 'updated':
            return QVariant(self._data[row]['updated_at'])
        elif column == 'synced':
            return True

        return QVariant(self._data[row][column])

    def flags(self, index):
        return super().flags(index) | Qt.ItemIsUserCheckable
