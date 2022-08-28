import datetime
import re
from collections import UserList
from enum import Enum
from typing import Optional
from uuid import UUID

from calibre.ebooks.metadata.book.base import Metadata

from qt.core import QAbstractTableModel, Qt, QVariant, QCheckBox

from calibre_plugins.evilflowers.client.requestor import RequestError
from calibre_plugins.evilflowers.config import prefs


class Entry:
    class EntryStatus(Enum):
        LOCAL = 'local'
        REMOTE = 'remote'
        SYNC = 'sync'
        UPDATED_REMOTE = 'updated_remote'
        UPDATED_LOCAL = 'updated_local'

    def __init__(self, status: EntryStatus, metadata: Metadata):
        self._metadata = metadata
        self.uuid = metadata.uuid
        self.status = status

    @property
    def authors(self) -> str:
        return ', '.join(self._metadata.authors)

    @property
    def title(self) -> str:
        return self._metadata.title

    @property
    def updated_at(self):
        return self._metadata.timestamp


class EntryList(UserList):
    def find_by_id(self, entry_id: UUID) -> Optional[Entry]:
        for i in self.data:
            if i.uuid == entry_id:
                return i
        return None

# https://github.com/kovidgoyal/calibre/blob/eb78a761a99ac20a6364f85e12059fec6517d890/src/calibre/db/cache.py#L943
class EntriesModel(QAbstractTableModel):
    column_headers = [_('UUID'), _("Status"), _("Title"), _("Author(s)"), _("Updated")]
    _mapping = {
        'uuid': _("UUID"),
        'status': _("Status"),
        'title': _("Title"),
        'authors': _("Author(s)"),
        'updated_at': _("Updated at"),
    }

    def __init__(self, db, client, progress_bar):
        super(EntriesModel, self).__init__()
        self._db = db
        self._client = client
        self._progress_bar = progress_bar
        self._data = EntryList()

        # Reading local database
        self._progress_bar.setValue(0)
        self._progress_bar.setFormat("Reading local database")
        local_book_ids = self._db.all_book_ids()
        self._progress_bar.setMaximum(len(local_book_ids))

        for book_id in local_book_ids:
            self._progress_bar.setValue(self._progress_bar.value() + 1)
            book = self._db.get_metadata(book_id)
            self._data.append(Entry(Entry.EntryStatus.LOCAL, book))

            print(type(book))
            print(f"\t{book.identifiers}")
            print(f"\t{book.languages}")
            print(f"\t{self._db.cover(book_id, as_path=True)}")
            for fmt in self._db.formats(book_id):
                print(f"\t{fmt}")
                print(f"\t{self._db.format(book_id, fmt, as_path=True)}")
                pass

        # Reading remote catalog
        try:
            self._progress_bar.setValue(0)
            self._progress_bar.setFormat("Downloading remote items")
            remote_items = client.entries.list(prefs['catalog_id'])
            self._progress_bar.setMaximum(len(remote_items))

            for item in remote_items:
                self._progress_bar.setValue(self._progress_bar.value() + 1)

                # Fix timezones
                updated_at = datetime.datetime.fromisoformat(
                    re.sub(r"((\.[0-9]+)?\+00:00|Z)$", "", item['updated_at'])
                )

                entry = self._data.find_by_id(item['id'])
                if entry:
                    if entry.updated_at > updated_at:
                        entry.status = entry.EntryStatus.UPDATED_LOCAL
                    elif entry.updated_at < updated_at:
                        entry.status = entry.EntryStatus.UPDATED_REMOTE
                    else:
                        entry.status = Entry.EntryStatus.SYNC
                else:
                    book = Metadata(
                        item['title'],
                        [item['author']['name'], item['author']['surname']]
                    )
                    book.uuid = item['id']
                    book.timestamp = updated_at
                    self._data.append(Entry(Entry.EntryStatus.REMOTE, book))
        except RequestError as e:
            self._progress_bar.setValue(0)
            self._progress_bar.setFormat(f"ERROR: {e}")
            self._data = EntryList()

        self._progress_bar.setValue(0)
        self._progress_bar.setFormat("Nothing to do")

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

        if column == 'status':
            return QVariant(self._data[row].status.value)
        elif column == 'updated_at':
            return QVariant(self._data[row].updated_at.strftime("%Y-%m-%d %H:%M:%S"))

        return QVariant(getattr(self._data[row], column))

    def entries(self) -> EntryList:
        return self._data

    def flags(self, index):
        return super().flags(index) | Qt.ItemIsUserCheckable
