"""Read a user's highlights from the catalog and render them for a person to read. No Qt.

Import only, one way. The catalog anchors a highlight to a page and a set of
rectangles on that page; Calibre's viewer anchors its own to EPUB CFIs. Neither
maps onto the other, so highlights are never written into Calibre's annotations.
They are shown, copied, or saved as HTML into a long-text custom column the user
picked, which is the only thing this module ever writes.
"""

from __future__ import annotations

import html
import threading
from collections.abc import Iterable, Mapping
from dataclasses import dataclass, field, replace
from typing import Any
from uuid import UUID

from ..remote.client import CatalogClient
from ..remote.models import CatalogRef, EntryRef, Highlight
from ..vendor.evilflowers_protocol.errors import NotFound
from .local import last_modified
from .state import BOOK_DATA_NAME, Baseline, StateStore

UNTITLED = "Highlights"
NO_TEXT = "(no text)"
# Custom column types that hold free text long enough for a list of passages.
TEXT_COLUMN_TYPES = frozenset({"comments"})


@dataclass(frozen=True, slots=True)
class BookHighlights:
    """One selected book and what the catalog holds for it.

    ``entry_id`` is None when the book is not in the catalog. ``error`` is set when
    the book is in the catalog but reading its highlights failed.
    """

    book_id: int
    title: str
    entry_id: str | None = None
    highlights: list[Highlight] = field(default_factory=list)
    error: str = ""

    @property
    def linked(self) -> bool:
        return self.entry_id is not None


# ------------------------------------------------------------------------- fetching


def fetch_highlights(client: CatalogClient, entry: EntryRef, user_id: UUID) -> list[Highlight]:
    """The user's finished highlights in ``entry``, by page. ``entry`` must carry its acquisitions."""
    return client.list_highlights(entry, user_id)


def collect(
    db: Any,
    client: CatalogClient,
    catalog: CatalogRef,
    book_ids: Iterable[int],
    user_id: UUID,
    *,
    abort: threading.Event | None = None,
) -> list[BookHighlights]:
    """Highlights for each book, in the order given. One failing book does not stop the rest.

    A book is linked by its sync baseline when it has one, else by its UUID (a book
    synced from or to the catalog keeps the entry id as its UUID).
    """
    links = {book_id: baseline.entry_id for book_id, baseline in StateStore(db, str(catalog.id)).baselines().items()}
    result: list[BookHighlights] = []
    for book_id in book_ids:
        if abort is not None and abort.is_set():
            raise InterruptedError("stopped")
        title = db.field_for("title", book_id) or ""
        entry_id = links.get(book_id) or str(db.field_for("uuid", book_id) or "")
        try:
            entry = client.get_entry(catalog.id, UUID(entry_id))
        except (NotFound, ValueError):
            result.append(BookHighlights(book_id, title))
            continue
        try:
            highlights = fetch_highlights(client, entry, user_id)
        except Exception as exc:  # shown next to that book; the others still load
            result.append(BookHighlights(book_id, title, entry_id, error=str(exc)))
            continue
        result.append(BookHighlights(book_id, title, entry_id, highlights))
    return result


# ------------------------------------------------------------------------ rendering


def _groups(highlights: Iterable[Highlight]) -> list[tuple[str, list[Highlight]]]:
    """Highlights grouped by annotation title, groups in order of their first highlight.

    The client returns highlights sorted by page, so groups read front to back and
    each group keeps that order.
    """
    groups: dict[str, list[Highlight]] = {}
    for highlight in highlights:
        groups.setdefault(highlight.annotation_title.strip() or UNTITLED, []).append(highlight)
    return list(groups.items())


def _passage(highlight: Highlight) -> str:
    """The marked text. ``content`` stands in only when ``text`` is empty."""
    return " ".join((highlight.text or highlight.content or "").split())


def _line(highlight: Highlight) -> str:
    passage = _passage(highlight)
    return f"p. {highlight.page} — “{passage}”" if passage else f"p. {highlight.page} — {NO_TEXT}"


def render_html(highlights: Iterable[Highlight]) -> str:
    """Highlights as HTML for a comments column or a viewer. Every piece of text is escaped."""
    parts: list[str] = []
    for title, items in _groups(highlights):
        parts.append(f"<h3>{html.escape(title)}</h3>")
        parts.append("<ul>")
        parts.extend(f"<li>{html.escape(_line(item))}</li>" for item in items)
        parts.append("</ul>")
    return "\n".join(parts)


def render_text(highlights: Iterable[Highlight]) -> str:
    """Highlights as plain text for the clipboard: a title line, then one line per passage."""
    blocks = ["\n".join([title, *(f"  {_line(item)}" for item in items)]) for title, items in _groups(highlights)]
    return "\n\n".join(blocks)


def render_books_html(books: Iterable[BookHighlights]) -> str:
    """Several books in one page, each under its title. Books without highlights are left out."""
    sections = [
        f"<h2>{html.escape(book.title)}</h2>\n{render_html(book.highlights)}" for book in books if book.highlights
    ]
    return "\n".join(sections)


def render_books_text(books: Iterable[BookHighlights]) -> str:
    sections = [f"{book.title}\n\n{render_text(book.highlights)}" for book in books if book.highlights]
    return "\n\n\n".join(sections)


# --------------------------------------------------------------------------- saving


def text_column_name(db: Any, column: str) -> str | None:
    """The display name of ``column`` when it is a long-text custom column, else None."""
    column = column.strip()
    if not column.startswith("#"):
        return None
    try:
        meta = db.field_metadata[column]
    except KeyError:
        return None
    if meta.get("datatype") not in TEXT_COLUMN_TYPES:
        return None
    return str(meta.get("name") or column)


def save_to_column(db: Any, column: str, html_by_book: Mapping[int, str]) -> None:
    """Write rendered highlights into a custom column.

    Writing any field bumps a book's ``last_modified``, which would make every
    synced book look edited in Calibre and upload it again. So a book that was in
    sync before the write gets its baseline moved to the new ``last_modified``.
    A book that already had local edits keeps its old baseline and still uploads.
    """
    if not html_by_book:
        return
    if text_column_name(db, column) is None:
        raise ValueError(f"{column} is not a long-text custom column in this library")
    book_ids = list(html_by_book)
    before = {book_id: last_modified(db, book_id) for book_id in book_ids}
    db.set_field(column.strip(), dict(html_by_book))
    raw = db.get_custom_book_data(BOOK_DATA_NAME, book_ids)
    restamped: dict[int, Any] = {}
    for book_id, data in raw.items():
        baseline = Baseline.from_json(data)
        if baseline is not None and baseline.local_modified == before.get(book_id):
            restamped[book_id] = replace(baseline, local_modified=last_modified(db, book_id)).to_json()
    if restamped:
        db.add_custom_book_data(BOOK_DATA_NAME, restamped)
