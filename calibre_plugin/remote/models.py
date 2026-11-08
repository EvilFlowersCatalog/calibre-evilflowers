"""Wire models for the parts of the EvilFlowers Catalog API the plugin uses.

Plain dataclasses that implement the vendored transport's ``Model`` (``from_dict``)
and ``Payload`` (``to_dict``) protocols. They mirror the projections in upstream
``evilflowers_protocol.forms`` and add only the fields synchronisation needs.

Response models ignore unknown keys and tolerate nulls the spec marks required.
Request models drop ``None`` values so an unset field is never sent as ``null``.
"""

from __future__ import annotations

import hashlib
import json
from dataclasses import dataclass, field
from typing import Any
from uuid import UUID

# Formats the backend stores (``AcquisitionMIME`` in the OpenAPI spec), keyed by Calibre format name.
MIME_BY_FORMAT: dict[str, str] = {
    "EPUB": "application/epub+zip",
    "PDF": "application/pdf",
    "MOBI": "application/x-mobipocket-ebook",
}
FORMAT_BY_MIME: dict[str, str] = {mime: fmt for fmt, mime in MIME_BY_FORMAT.items()}


def _uuid(value: Any) -> UUID:
    return value if isinstance(value, UUID) else UUID(str(value))


def _drop_none(data: dict[str, Any]) -> dict[str, Any]:
    return {key: value for key, value in data.items() if value is not None}


# ------------------------------------------------------------------------------ responses


@dataclass(frozen=True, slots=True)
class CatalogRef:
    id: UUID
    url_name: str
    title: str

    @classmethod
    def from_dict(cls, data: Any) -> CatalogRef:
        return cls(id=_uuid(data["id"]), url_name=str(data["url_name"]), title=str(data["title"]))


@dataclass(frozen=True, slots=True)
class UserRef:
    id: UUID
    username: str

    @classmethod
    def from_dict(cls, data: Any) -> UserRef:
        return cls(id=_uuid(data["id"]), username=str(data.get("username") or ""))


@dataclass(frozen=True, slots=True)
class AcquisitionRef:
    id: UUID
    relation: str
    mime: str
    url: str | None = None

    @property
    def calibre_format(self) -> str | None:
        """The Calibre format name for this file, or None for a type Calibre sync ignores."""
        return FORMAT_BY_MIME.get(self.mime)

    @classmethod
    def from_dict(cls, data: Any) -> AcquisitionRef:
        return cls(
            id=_uuid(data["id"]),
            relation=str(data["relation"]),
            mime=str(data["mime"]),
            url=data.get("url"),
        )


@dataclass(frozen=True, slots=True)
class Person:
    name: str
    surname: str

    @property
    def full_name(self) -> str:
        return " ".join(part for part in (self.name, self.surname) if part)

    def to_dict(self) -> dict[str, Any]:
        return {"name": self.name, "surname": self.surname}

    @classmethod
    def from_dict(cls, data: Any) -> Person:
        return cls(name=str(data.get("name") or ""), surname=str(data.get("surname") or ""))


@dataclass(frozen=True, slots=True)
class EntryRef:
    """An entry as the list (``EntrySerializer.Base``) or detail endpoint returns it.

    The list response carries no ``identifiers``, ``publisher`` or ``published_at``;
    those stay empty there. ``updated_at`` is kept as the server's string so a
    baseline compares exactly, without timezone or precision round trips.

    The live API (checked on dev, 2026-11-08) differs from the spec on ``main``: it
    sends ``authors`` and ``image`` where the spec says ``entry_authors`` and
    ``image_url``. Both spellings are read. ``image`` is set even when the entry has
    no cover, so :attr:`has_cover` goes by ``image_mime``.

    ``updated_at`` also moves when anyone downloads a file (the server saves the
    popularity counter), so it only says *something* changed. :meth:`fingerprint`
    says whether anything synchronised did.
    """

    id: UUID
    title: str
    updated_at: str
    catalog_id: UUID | None = None
    summary: str | None = None
    language_code: str | None = None
    authors: tuple[Person, ...] = ()
    categories: tuple[str, ...] = ()
    identifiers: dict[str, str] = field(default_factory=dict)
    acquisitions: tuple[AcquisitionRef, ...] = ()
    publisher: str | None = None
    published_at: str | None = None
    image_mime: str | None = None

    @property
    def has_cover(self) -> bool:
        return bool(self.image_mime)

    def fingerprint(self) -> str:
        """A hash of every field sync carries. Only meaningful on a detail response.

        A cover replaced by another image of the same type does not change it; the
        API exposes no cover checksum.
        """
        canonical = [
            self.title,
            self.summary,
            self.language_code,
            [[person.name, person.surname] for person in self.authors],
            sorted(self.categories),
            sorted(self.identifiers.items()),
            self.publisher,
            self.published_at,
            sorted(str(acq.id) for acq in self.acquisitions),
            self.image_mime,
        ]
        return hashlib.sha256(json.dumps(canonical, ensure_ascii=False).encode()).hexdigest()

    @classmethod
    def from_dict(cls, data: Any) -> EntryRef:
        language = data.get("language") or {}
        identifiers = data.get("identifiers") or {}
        return cls(
            id=_uuid(data["id"]),
            title=str(data["title"]),
            updated_at=str(data["updated_at"]),
            catalog_id=_uuid(data["catalog_id"]) if data.get("catalog_id") else None,
            summary=data.get("summary"),
            language_code=language.get("alpha2") or language.get("alpha3") if isinstance(language, dict) else None,
            authors=tuple(Person.from_dict(item) for item in data.get("authors") or data.get("entry_authors") or ()),
            categories=tuple(str(item["term"]) for item in data.get("categories") or () if item.get("term")),
            identifiers={str(k): str(v) for k, v in identifiers.items() if v is not None}
            if isinstance(identifiers, dict)
            else {},
            acquisitions=tuple(AcquisitionRef.from_dict(item) for item in data.get("acquisitions") or ()),
            publisher=data.get("publisher"),
            published_at=data.get("published_at"),
            image_mime=data.get("image_mime"),
        )


# ------------------------------------------------------------------------------- requests


@dataclass(frozen=True, slots=True)
class CategoryForm:
    term: str
    label: str | None = None
    scheme: str | None = None

    def to_dict(self) -> dict[str, Any]:
        return _drop_none({"term": self.term, "label": self.label, "scheme": self.scheme})


@dataclass(frozen=True, slots=True)
class EntryForm:
    """Body of ``POST /catalogs/{c}/entries`` and ``PUT /catalogs/{c}/entries/{e}``.

    The backend has no partial update: ``title`` and ``language_code`` are required
    on both. ``id`` is only sent on create; it pins the entry to the Calibre book's UUID.
    ``image`` is the cover as a ``data:image/...;base64,...`` URI.
    """

    title: str
    language_code: str
    id: UUID | None = None
    authors: tuple[Person, ...] = ()
    categories: tuple[CategoryForm, ...] = ()
    summary: str | None = None
    publisher: str | None = None
    published_at: str | None = None
    identifiers: dict[str, str] | None = None
    image: str | None = None

    def to_dict(self) -> dict[str, Any]:
        return _drop_none(
            {
                "id": str(self.id) if self.id else None,
                "title": self.title,
                "language_code": self.language_code,
                "authors": [author.to_dict() for author in self.authors],
                "categories": [category.to_dict() for category in self.categories],
                "summary": self.summary,
                "publisher": self.publisher,
                "published_at": self.published_at,
                "identifiers": self.identifiers or None,
                "image": self.image,
            }
        )


@dataclass(frozen=True, slots=True)
class AcquisitionMeta:
    """The ``metadata`` form field of the multipart acquisition upload."""

    relation: str = "acquisition"

    def to_dict(self) -> dict[str, Any]:
        return {"relation": self.relation}
