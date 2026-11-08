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
from .models import AcquisitionMeta, AcquisitionRef, CatalogRef, EntryForm, EntryRef, UserRef

__all__ = ["CatalogClient", "CatalogError", "Conflict", "NotFound", "Unauthorized", "api_root"]

logger = logging.getLogger("evilflowers_protocol")

_MAX_REDIRECTS = 5


def api_root(url: str) -> str:
    """Normalise what a user types (``https://host`` or ``https://host/api/v1/``) to the API root."""
    url = url.strip().rstrip("/")
    parts = urllib.parse.urlsplit(url)
    if parts.path in ("", "/"):
        url = f"{url}/api/v1"
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

    # ------------------------------------------------------------------ catalogs

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


def _origin(url: str) -> str:
    parts = urllib.parse.urlsplit(url)
    return f"{parts.scheme}://{parts.netloc}"


def _strip_query(url: str) -> str:
    """Drop the query string, which for a presigned storage URL is a credential."""
    parts = urllib.parse.urlsplit(url)
    return urllib.parse.urlunsplit((parts.scheme, parts.netloc, parts.path, "", ""))
