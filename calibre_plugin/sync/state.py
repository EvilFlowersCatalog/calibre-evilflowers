"""What each side looked like after the last successful sync of a book.

Two stores, both inside the library's ``metadata.db`` so the state travels with
the library and never with the Calibre profile:

* **Per book** (``add_custom_book_data``): a :class:`Baseline` linking the book to a
  remote entry, with the local ``last_modified`` and the remote ``updated_at``
  seen right after the sync, and which acquisition holds which format.
* **Per library** (``set_pref``): the entry ids of each catalog this library has
  ever been in sync with. An id in that set without a local book means the book
  was deleted locally, so it is not pulled back as if it were new.

Neither write touches a book's ``last_modified``, so recording state never makes
a book look edited.
"""

from __future__ import annotations

from collections.abc import Iterable
from dataclasses import asdict, dataclass, field
from typing import Any

BOOK_DATA_NAME = "evilflowers_sync"
KNOWN_ENTRIES_PREF = "evilflowers_known_entries"
SCHEMA_VERSION = 1


@dataclass(frozen=True, slots=True)
class FileBaseline:
    """A format as it was uploaded or downloaded: content hash and the acquisition holding it."""

    sha256: str
    acquisition_id: str


@dataclass(frozen=True, slots=True)
class Baseline:
    catalog_id: str
    entry_id: str
    local_modified: str  # ISO 8601, from the book's last_modified
    remote_updated: str  # the entry's updated_at string, verbatim
    # Calibre format -> the file this library synced for it. Only these are ever replaced or removed.
    files: dict[str, FileBaseline] = field(default_factory=dict)
    # Every acquisition the entry had after the sync, including ones Calibre does not own.
    acquisitions: tuple[str, ...] = ()
    # EntryRef.fingerprint() after the sync: tells a real edit from a download bumping updated_at.
    remote_fingerprint: str = ""

    @property
    def acquisition_ids(self) -> frozenset[str]:
        return frozenset(self.acquisitions)

    def to_json(self) -> dict[str, Any]:
        data = asdict(self)
        data["acquisitions"] = list(self.acquisitions)
        return {"v": SCHEMA_VERSION, **data}

    @classmethod
    def from_json(cls, data: Any) -> Baseline | None:
        if not isinstance(data, dict) or data.get("v") != SCHEMA_VERSION:
            return None
        try:
            return cls(
                catalog_id=str(data["catalog_id"]),
                entry_id=str(data["entry_id"]),
                local_modified=str(data["local_modified"]),
                remote_updated=str(data["remote_updated"]),
                files={
                    str(fmt): FileBaseline(sha256=str(item["sha256"]), acquisition_id=str(item["acquisition_id"]))
                    for fmt, item in (data.get("files") or {}).items()
                },
                acquisitions=tuple(str(item) for item in data.get("acquisitions") or ()),
                remote_fingerprint=str(data.get("remote_fingerprint") or ""),
            )
        except (KeyError, TypeError):
            return None


class StateStore:
    """Reads and writes sync state in a library (``db`` is ``Cache``, i.e. ``new_api``)."""

    def __init__(self, db: Any, catalog_id: str) -> None:
        self._db = db
        self._catalog_id = catalog_id

    def baselines(self) -> dict[int, Baseline]:
        """Baselines of every book linked to this catalog, by book id."""
        raw = self._db.get_custom_book_data(BOOK_DATA_NAME)
        result: dict[int, Baseline] = {}
        for book_id, data in raw.items():
            baseline = Baseline.from_json(data)
            if baseline is not None and baseline.catalog_id == self._catalog_id:
                result[book_id] = baseline
        return result

    def save(self, book_id: int, baseline: Baseline) -> None:
        """Store one book's baseline. Callers batch :meth:`remember` for the entry ids."""
        self._db.add_custom_book_data(BOOK_DATA_NAME, {book_id: baseline.to_json()})

    def known_entries(self) -> frozenset[str]:
        known = self._db.pref(KNOWN_ENTRIES_PREF, {}) or {}
        return frozenset(known.get(self._catalog_id, ()))

    def remember(self, entry_ids: Iterable[str]) -> None:
        known = dict(self._db.pref(KNOWN_ENTRIES_PREF, {}) or {})
        current = set(known.get(self._catalog_id, ()))
        new = current | set(entry_ids)
        if new != current:
            known[self._catalog_id] = sorted(new)
            self._db.set_pref(KNOWN_ENTRIES_PREF, known)

    def forget(self, entry_ids: Iterable[str]) -> None:
        known = dict(self._db.pref(KNOWN_ENTRIES_PREF, {}) or {})
        current = set(known.get(self._catalog_id, ()))
        new = current - set(entry_ids)
        if new != current:
            known[self._catalog_id] = sorted(new)
            self._db.set_pref(KNOWN_ENTRIES_PREF, known)
