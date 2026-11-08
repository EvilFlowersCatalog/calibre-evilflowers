"""Endpoint layer for the plugin, on the vendored ``evilflowers_protocol.transport``.

Mirrors upstream ``evilflowers_protocol.client.CatalogClient`` for the endpoints
both need, and adds what synchronisation needs on top: listing a catalog,
replacing files, and downloading files and covers.
"""

from __future__ import annotations

import json
import logging
import shutil
import urllib.error
import urllib.parse
import urllib.request
from collections.abc import Iterator
from pathlib import Path
from typing import Any
from uuid import UUID

from ..vendor.evilflowers_protocol.errors import CatalogError, Conflict, NotFound, Unauthorized, describe
from ..vendor.evilflowers_protocol.transport import Transport, _NoRedirect
from .models import (
    AcquisitionMeta,
    AcquisitionRef,
    AnnotationItemRef,
    AnnotationRef,
    CatalogRef,
    CategoryRef,
    EntryForm,
    EntryPage,
    EntryRef,
    Facets,
    Highlight,
    ShelfRecord,
    UserAcquisitionRef,
    UserRef,
)

__all__ = ["CatalogClient", "CatalogError", "Conflict", "NotFound", "Unauthorized", "api_root", "site_address"]

# Annotation kinds that mark passages of text; drawings and bookmarks carry none.
_HIGHLIGHT_KINDS = frozenset({"highlight", "agent"})

logger = logging.getLogger("evilflowers_protocol")

_MAX_REDIRECTS = 5


def api_root(url: str) -> str:
    """Normalise what a user types (``https://host`` or ``https://host/api/v1/``) to the API root.

    A site served under a path (``https://host/library``) gets ``/api/v1`` appended too, so
    :func:`site_address` and this function round-trip.
    """
    url = url.strip().rstrip("/")
    if not urllib.parse.urlsplit(url).path.endswith("/api/v1"):
        url = f"{url}/api/v1"
    return url


def site_address(url: str) -> str:
    """The address people know their site by: ``https://host/api/v1/`` becomes ``https://host``.

    This is what the settings show and store; :func:`api_root` adds the API path back.
    """
    url = url.strip().rstrip("/")
    if urllib.parse.urlsplit(url).path.endswith("/api/v1"):
        url = url[: -len("/api/v1")]
    return url


class CatalogClient:
    """Client for one EvilFlowers Catalog instance; ``base_url`` is the API root (``https://host/api/v1``)."""

    def __init__(self, base_url: str, token: str, *, timeout: float = 60, retries: int = 3, user_agent: str) -> None:
        self._transport = Transport(api_root(base_url), token, timeout=timeout, retries=retries, user_agent=user_agent)
        self._token = token
        self._timeout = timeout
        self._user_agent = user_agent
        parts = urllib.parse.urlsplit(self._transport.base_url)
        if not parts.path.endswith("/api/v1"):
            raise ValueError(f"the API root must end with /api/v1, got {self._transport.base_url!r}")
        self._origin = f"{parts.scheme}://{parts.netloc}"
        self._data_root = f"{self._origin}{parts.path[: -len('/api/v1')]}/data/v1"

    def __repr__(self) -> str:
        return f"{type(self).__name__}(base_url={self._transport.base_url!r})"

    @property
    def transport(self) -> Transport:
        return self._transport

    # ------------------------------------------------------------------ identity

    def me(self) -> UserRef:
        """The user the token belongs to. Fails fast on a bad URL or token."""
        return self._transport.get("users/me", UserRef)

    def revoke_api_key(self, api_key_id: UUID) -> None:
        """Delete an API key. A key that is already gone (404) counts as revoked."""
        try:
            self._transport.request("DELETE", f"api_keys/{api_key_id}")
        except NotFound:
            logger.debug("API key %s was already gone", api_key_id)

    # ------------------------------------------------------------------ catalogs

    def list_catalogs(self) -> list[CatalogRef]:
        """Every catalog the token can read, sorted by title for a picker."""
        catalogs = self._transport.iter_pages("catalogs", CatalogRef)
        return sorted(catalogs, key=lambda catalog: (catalog.title.casefold(), catalog.url_name))

    def list_categories(self, catalog_id: UUID) -> dict[UUID, str]:
        """A label for each category of a catalog, to name the ids :meth:`facets` returns."""
        categories = self._transport.iter_pages("categories", CategoryRef, query={"catalog_id": str(catalog_id)})
        return {category.id: category.display_label for category in categories}

    def resolve_catalog(self, url_name_or_uuid: str) -> CatalogRef:
        """A catalog by UUID or exact ``url_name``. Raises :class:`NotFound` when none matches."""
        key = url_name_or_uuid.strip()
        if not key:
            raise ValueError("catalog must not be empty")
        try:
            catalog_id = UUID(key)
        except ValueError:
            pass
        else:
            return self._transport.get(f"catalogs/{catalog_id}", CatalogRef)
        # The url_name filter matches substrings, so keep only the exact match.
        for catalog in self._transport.iter_pages("catalogs", CatalogRef, query={"url_name": key}):
            if catalog.url_name == key:
                return catalog
        raise NotFound(
            None, None, "GET", self._transport.url("catalogs", {"url_name": key}), f"no catalog has url_name {key!r}"
        )

    # ------------------------------------------------------------------- entries

    def iter_entries(self, catalog_id: UUID) -> Iterator[EntryRef]:
        """Every entry of a catalog, in list form (no identifiers, publisher or published_at)."""
        return self._transport.iter_pages("entries", EntryRef, query={"catalog_id": str(catalog_id)})

    def search_entries(
        self,
        catalog_id: UUID,
        *,
        query: str = "",
        page: int = 1,
        limit: int = 30,
        language_code: str | None = None,
        category_id: UUID | None = None,
        order_by: str | None = None,
    ) -> EntryPage:
        """One page of a catalog's entries, for browsing.

        Every whitespace-separated word of ``query`` must match the title, summary,
        publisher, an author or a category. With a query and no ``order_by`` the
        server ranks by relevance. A page past the last raises :class:`NotFound`.
        """
        params = _search_params(catalog_id, query, language_code, category_id)
        params.update({"page": str(page), "limit": str(limit)})
        if order_by:
            params["order_by"] = order_by
        response = self._transport.request("GET", "entries", query=params)
        body = response.body
        if not isinstance(body, dict) or not isinstance(body.get("items"), list):
            raise CatalogError(response.status, body, response.method, response.url, "missing 'items' list")
        metadata = body.get("metadata") if isinstance(body.get("metadata"), dict) else {}
        items = [response.validate(EntryRef, item) for item in body["items"]]
        return EntryPage(
            items=items,
            page=_int(metadata.get("page"), page),
            pages=max(1, _int(metadata.get("pages"), 1)),
            total=_int(metadata.get("total"), len(items)),
        )

    def facets(
        self,
        catalog_id: UUID,
        *,
        query: str = "",
        language_code: str | None = None,
        category_id: UUID | None = None,
    ) -> Facets:
        """Language and category counts for the same search :meth:`search_entries` runs.

        Each dimension ignores its own filter, so the language list still offers the
        other languages while one is selected.
        """
        params = _search_params(catalog_id, query, language_code, category_id)
        return self._transport.get("entries/facets", Facets, query=params)

    def get_entry(self, catalog_id: UUID, entry_id: UUID) -> EntryRef:
        return self._transport.get(f"catalogs/{catalog_id}/entries/{entry_id}", EntryRef)

    def create_entry(self, catalog_id: UUID, entry: EntryForm) -> EntryRef:
        """Create an entry. Raises :class:`Conflict` on a duplicate id, title, ISBN or DOI."""
        return self._transport.send("POST", f"catalogs/{catalog_id}/entries", entry, EntryRef)

    def update_entry(self, catalog_id: UUID, entry_id: UUID, entry: EntryForm) -> EntryRef:
        return self._transport.send("PUT", f"catalogs/{catalog_id}/entries/{entry_id}", entry, EntryRef)

    # -------------------------------------------------------------- acquisitions

    def upload_acquisition(
        self, catalog_id: UUID, entry_id: UUID, path: Path, *, mime: str, relation: str = "acquisition"
    ) -> AcquisitionRef:
        """Attach a file to an entry. The file is streamed from disk.

        A retried POST can attach the file twice (see the transport docs); the
        caller re-reads the entry afterwards and records what is actually there.
        """
        return self._transport.upload(
            f"catalogs/{catalog_id}/entries/{entry_id}",
            Path(path),
            AcquisitionRef,
            file_field="content",
            mime=mime,
            fields={"metadata": json.dumps(AcquisitionMeta(relation=relation).to_dict())},
        )

    def delete_acquisition(self, acquisition_id: UUID) -> None:
        self._transport.request("DELETE", f"acquisitions/{acquisition_id}")

    def download_acquisition(self, acquisition_id: UUID, destination: Path) -> Path:
        """Stream a file (``GET /data/v1/acquisitions/{id}``) to ``destination``."""
        return self._download(f"{self._data_root}/acquisitions/{acquisition_id}", destination)

    def download_cover(self, entry_id: UUID, destination: Path) -> Path | None:
        """Stream an entry's cover to ``destination``; None when the entry has none."""
        try:
            return self._download(f"{self._data_root}/covers/{entry_id}", destination)
        except NotFound:
            return None

    def download_thumbnail(self, entry_id: UUID, destination: Path) -> Path | None:
        """Stream an entry's small cover to ``destination``; None when it has none.

        The server answers 500 instead of 404 for a missing cover, so both mean "none".
        """
        try:
            return self._download(f"{self._data_root}/thumbnails/{entry_id}", destination)
        except CatalogError as exc:
            if exc.status in (404, 500):
                return None
            raise

    # --------------------------------------------------------------------- shelf

    def list_shelf(self, catalog_id: UUID) -> list[ShelfRecord]:
        """The user's shelf records for one catalog.

        The server hides records for public catalogs the user has no membership in;
        ``EntryRef.shelf_record_id`` from a search shows those too.
        """
        return list(self._transport.iter_pages("shelf-records", ShelfRecord, query={"catalog_id": str(catalog_id)}))

    def add_to_shelf(self, entry_id: UUID) -> ShelfRecord:
        """Put an entry on the user's shelf. Adding it twice returns the existing record."""
        return self._transport.request("POST", "shelf-records", json_body={"entry_id": str(entry_id)}).model(
            ShelfRecord
        )

    def remove_from_shelf(self, shelf_record_id: UUID) -> None:
        """Take a record (by its own id, not the entry's) off the shelf. Already gone counts as done."""
        try:
            self._transport.request("DELETE", f"shelf-records/{shelf_record_id}")
        except NotFound:
            logger.debug("shelf record %s was already gone", shelf_record_id)

    # ---------------------------------------------------------------- highlights

    def list_highlights(self, entry: EntryRef, user_id: UUID) -> list[Highlight]:
        """The user's finished highlights (their own and an agent's) in any file of ``entry``.

        The API has no entry filter on annotations, so this walks entry -> its
        acquisitions -> the user's user-acquisitions -> annotations -> items.
        ``user_id`` is required: without it a superuser would get everyone's.
        ``entry`` must carry its acquisitions (a list or detail response does).
        """
        highlights: list[Highlight] = []
        for user_acquisition_id in self._user_acquisition_ids(entry, user_id):
            annotations = self._transport.iter_pages(
                "annotations", AnnotationRef, query={"user_acquisition_id": str(user_acquisition_id)}
            )
            for annotation in annotations:
                if annotation.kind not in _HIGHLIGHT_KINDS or annotation.status != "done":
                    continue
                items = self._transport.iter_pages(
                    "annotation-items", AnnotationItemRef, query={"annotation_id": str(annotation.id)}
                )
                highlights.extend(
                    Highlight(annotation.title, annotation.kind, item.page, item.text, item.content, item.created_at)
                    for item in items
                )
        return sorted(highlights, key=lambda highlight: (highlight.page, highlight.created_at))

    def _user_acquisition_ids(self, entry: EntryRef, user_id: UUID) -> list[UUID]:
        """The user's user-acquisition ids for every file of ``entry``, without duplicates, in order."""
        found: dict[UUID, None] = {}
        for acquisition in entry.acquisitions:
            records = self._transport.iter_pages(
                "user-acquisitions",
                UserAcquisitionRef,
                query={"user_id": str(user_id), "acquisition_id": str(acquisition.id)},
            )
            found.update((record.id, None) for record in records)
        return list(found)

    # ------------------------------------------------------------------ download

    def _download(self, url: str, destination: Path) -> Path:
        """GET ``url`` into ``destination``, following redirects by hand.

        The bearer token goes only to the catalog's own origin. A redirect to
        another host (object storage, a CDN) is followed without it.
        """
        opener = urllib.request.build_opener(_NoRedirect)
        for _hop in range(_MAX_REDIRECTS + 1):
            same_origin = _origin(url) == self._origin
            headers = {"User-Agent": self._user_agent}
            if same_origin:
                headers["Authorization"] = f"Bearer {self._token}"
            request = urllib.request.Request(url, headers=headers, method="GET")
            try:
                with opener.open(request, timeout=self._timeout) as response:
                    destination.parent.mkdir(parents=True, exist_ok=True)
                    with destination.open("wb") as handle:
                        shutil.copyfileobj(response, handle, 1024 * 1024)
                    logger.debug("GET %s -> %s", urllib.parse.urlsplit(url).path, response.status)
                    return destination
            except urllib.error.HTTPError as exc:
                location = exc.headers.get("Location") if 300 <= exc.code < 400 else None
                body = b""
                try:
                    body = exc.read()
                finally:
                    exc.close()
                if location:
                    url = urllib.parse.urljoin(url, location)
                    continue
                error_type = {401: Unauthorized, 403: Unauthorized, 404: NotFound}.get(exc.code, CatalogError)
                parsed = _problem(body)
                message = describe(exc.code, parsed).replace(self._token, "***")
                raise error_type(exc.code, parsed, "GET", _strip_query(url), message) from None
            except (urllib.error.URLError, OSError) as exc:
                raise CatalogError(None, None, "GET", _strip_query(url), str(getattr(exc, "reason", exc))) from exc
        raise CatalogError(None, None, "GET", _strip_query(url), f"more than {_MAX_REDIRECTS} redirects")


def _problem(body: bytes) -> Any:
    """An error body as a problem-detail dict when it is JSON; an HTML error page is dropped."""
    text = body.decode("utf-8", "replace").strip()
    try:
        return json.loads(text)
    except ValueError:
        return None if text.startswith("<") else text or None


def _search_params(catalog_id: UUID, query: str, language_code: str | None, category_id: UUID | None) -> dict[str, str]:
    """The filters ``GET /entries`` and ``GET /entries/facets`` share; unset ones are left out."""
    params = {"catalog_id": str(catalog_id)}
    if query.strip():
        params["query"] = query.strip()
    if language_code:
        params["language_code"] = language_code
    if category_id:
        params["category_id"] = str(category_id)
    return params


def _int(value: Any, default: int) -> int:
    return value if isinstance(value, int) and not isinstance(value, bool) else default


def _origin(url: str) -> str:
    parts = urllib.parse.urlsplit(url)
    return f"{parts.scheme}://{parts.netloc}"


def _strip_query(url: str) -> str:
    """Drop the query string, which for a presigned storage URL is a credential."""
    parts = urllib.parse.urlsplit(url)
    return urllib.parse.urlunsplit((parts.scheme, parts.netloc, parts.path, "", ""))
