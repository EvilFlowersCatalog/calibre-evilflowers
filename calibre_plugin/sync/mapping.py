"""Translate between Calibre metadata and catalog entries.

Only these fields travel, in both directions:

=================  ===========================
Calibre            Catalog entry
=================  ===========================
uuid               id (on create)
title              title
authors            authors (name + surname)
languages[0]       language_code (ISO 639-1 when one exists)
tags               categories (term = tag)
comments           summary (HTML)
publisher          publisher
pubdate            published_at (YYYY-MM-DD)
identifiers        identifiers (isbn, doi, google, dataverse_*)
cover              image (data URI)
=================  ===========================

Everything else in Calibre (series, rating, custom columns, ...) stays local and
is never cleared by a pull: :func:`metadata_from_entry` leaves those fields unset,
and ``set_metadata`` only writes the fields that are set.
"""

from __future__ import annotations

import base64
import re
from typing import Any
from uuid import UUID

from calibre.ebooks.metadata import author_to_author_sort
from calibre.ebooks.metadata.book.base import Metadata
from calibre.utils.date import is_date_undefined, parse_only_date
from calibre.utils.localization import canonicalize_lang, lang_as_iso639_1

from ..remote.models import CategoryForm, EntryForm, EntryRef, Person

# Identifier types the backend accepts on an entry; anything else gets a 422.
CATALOG_IDENTIFIERS = frozenset({"isbn", "doi", "google", "dataverse_pid", "dataverse_dataset_id"})
CATEGORY_SCHEME = "https://calibre-ebook.com/tags"
COVER_LIMIT = 5 * 1024 * 1024  # the backend rejects larger images
TITLE_LIMIT = 255
_YEAR = re.compile(r"^\s*(\d{4})\s*$")


def person(author: str) -> Person:
    """Split a Calibre author ("Ján Novák") into the catalog's name and surname."""
    sort = author_to_author_sort(author)
    if ", " in sort:
        surname, name = sort.split(", ", 1)
        return Person(name=name.strip(), surname=surname.strip())
    return Person(name="", surname=author.strip())


def language_code(mi: Metadata, default: str) -> str:
    for lang in mi.languages or ():
        return lang_as_iso639_1(lang) or lang
    return default


def published_at(mi: Metadata) -> str | None:
    if mi.pubdate is None or is_date_undefined(mi.pubdate):
        return None
    return mi.pubdate.date().isoformat()


def cover_data_uri(db: Any, book_id: int) -> str | None:
    """The book's cover as a JPEG data URI, shrunk if it exceeds the backend's limit."""
    data = db.cover(book_id)
    if not data:
        return None
    if len(data) > COVER_LIMIT:
        from calibre.utils.img import scale_image

        data = scale_image(data, width=1600, height=2400, compression_quality=85)[-1]
        if len(data) > COVER_LIMIT:
            return None
    mime = "image/png" if data[:8] == b"\x89PNG\r\n\x1a\n" else "image/jpeg"
    return f"data:{mime};base64,{base64.b64encode(data).decode('ascii')}"


def entry_form(db: Any, book_id: int, *, create: bool, default_language: str) -> EntryForm:
    """The entry body for a book. ``create`` adds the book's UUID as the entry id."""
    mi = db.get_metadata(book_id)
    return EntryForm(
        id=UUID(mi.uuid) if create else None,
        title=(mi.title or "")[:TITLE_LIMIT],
        language_code=language_code(mi, default_language),
        authors=tuple(person(author) for author in mi.authors or () if author),
        categories=tuple(CategoryForm(term=tag, label=tag, scheme=CATEGORY_SCHEME) for tag in mi.tags or ()),
        summary=mi.comments or None,
        publisher=mi.publisher or None,
        published_at=published_at(mi),
        identifiers={k: v for k, v in (mi.identifiers or {}).items() if k in CATALOG_IDENTIFIERS and v} or None,
        image=cover_data_uri(db, book_id),
    )


def _parse_published(value: str | None) -> Any:
    if not value:
        return None
    match = _YEAR.match(value)
    if match:
        # parse_only_date fills a bare year with today's month and day.
        value = f"{match.group(1)}-01-01"
    try:
        return parse_only_date(value)
    except (ValueError, TypeError):
        return None


def metadata_from_entry(entry: EntryRef, existing_identifiers: dict[str, str] | None = None) -> Metadata:
    """Calibre metadata for a (detailed) entry. Fields the catalog lacks stay unset."""
    authors = [author.full_name for author in entry.authors if author.full_name] or [_("Unknown")]
    mi = Metadata(entry.title, authors)
    mi.uuid = str(entry.id)
    lang = canonicalize_lang(entry.language_code) if entry.language_code else None
    if lang:
        mi.languages = [lang]
    mi.tags = list(entry.categories)
    if entry.summary:
        mi.comments = entry.summary
    if entry.publisher:
        mi.publisher = entry.publisher
    pubdate = _parse_published(entry.published_at)
    if pubdate is not None:
        mi.pubdate = pubdate
    identifiers = dict(existing_identifiers or {})
    identifiers.update(entry.identifiers)
    if identifiers:
        mi.identifiers = identifiers
    return mi
