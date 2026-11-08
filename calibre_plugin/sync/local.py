"""A cheap snapshot of the local library: what the planner needs, read in bulk."""

from __future__ import annotations

from dataclasses import dataclass
from datetime import UTC, datetime
from typing import Any

from ..remote.models import MIME_BY_FORMAT


def iso(moment: datetime) -> str:
    """A ``last_modified`` value as a stable UTC ISO string (the baseline's comparison key)."""
    if moment.tzinfo is None:
        moment = moment.replace(tzinfo=UTC)
    return moment.astimezone(UTC).isoformat(timespec="microseconds")


@dataclass(frozen=True, slots=True)
class LocalBook:
    book_id: int
    uuid: str
    title: str
    authors: tuple[str, ...]
    last_modified: str
    formats: tuple[str, ...]

    @property
    def syncable_formats(self) -> tuple[str, ...]:
        """Formats the catalog can store (EPUB, PDF, MOBI)."""
        return tuple(fmt for fmt in self.formats if fmt in MIME_BY_FORMAT)


def snapshot(db: Any) -> dict[int, LocalBook]:
    """Every book in the library, keyed by book id. Five bulk reads, no per-book queries."""
    ids = db.all_book_ids()
    uuids = db.all_field_for("uuid", ids)
    titles = db.all_field_for("title", ids)
    authors = db.all_field_for("authors", ids)
    modified = db.all_field_for("last_modified", ids)
    formats = db.all_field_for("formats", ids)
    return {
        book_id: LocalBook(
            book_id=book_id,
            uuid=str(uuids[book_id]),
            title=titles[book_id],
            authors=tuple(authors[book_id] or ()),
            last_modified=iso(modified[book_id]),
            formats=tuple(fmt.upper() for fmt in formats[book_id] or ()),
        )
        for book_id in ids
    }


def last_modified(db: Any, book_id: int) -> str:
    return iso(db.field_for("last_modified", book_id))
