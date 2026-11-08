"""A stateful stand-in for the EvilFlowers Catalog API, served by stdlib ``http.server`` on localhost.

Grown from ``tests/fake_catalog.py`` in evilflowers-protocol-python (scripted replies)
into a small in-memory catalog: entries, acquisitions, covers, the response and
pagination envelopes, 409 on duplicate id or title, and RFC 7807 errors. It also
serves login and API keys, catalog and entry search with facets, categories,
thumbnails, the shelf, and the user-acquisition -> annotation -> item chain that
highlights are read through.

Downloads answer ``303 See Other`` to a second server on another port (standing
in for object storage), which fails any request that carries an ``Authorization``
header -- the token must never leave the catalog's origin.

Every list is paginated by ``page_size`` (2), whatever ``limit`` the client asks
for above it, so every listing exercises pagination.
"""

from __future__ import annotations

import base64
import json
import math
import threading
import uuid
from collections import Counter
from collections.abc import Callable
from dataclasses import dataclass, field
from datetime import UTC, datetime, timedelta
from email.parser import BytesParser
from email.policy import HTTP
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from typing import Any
from urllib.parse import parse_qs, urlsplit

TOKEN = "eyJhbGciOiJSUzI1NiJ9.ZmFrZS1jYXRhbG9nLXRva2Vu.c2lnbmF0dXJl"
USER_ID = "00000000-0000-0000-0000-000000000001"
USERNAME = "tester"
PASSWORD = "correct horse battery staple"
LANGUAGES = {"en": "eng", "sk": "slk", "de": "deu", "cs": "ces"}
ANNOTATION_KINDS = ("drawing", "highlight", "agent", "bookmarks")

_Reply = tuple[int, Any, dict[str, str]]


@dataclass
class Acquisition:
    id: str
    relation: str
    mime: str
    content: bytes


@dataclass
class Entry:
    id: str
    catalog_id: str
    data: dict[str, Any]
    updated_at: str
    acquisitions: list[Acquisition] = field(default_factory=list)
    cover: bytes | None = None


class _Server(ThreadingHTTPServer):
    daemon_threads = True
    request_queue_size = 128


class FakeCatalog:
    def __init__(self) -> None:
        self.lock = threading.RLock()
        self.catalog_id = str(uuid.uuid4())
        self.catalog_url_name = "calibre-test"
        self.user_id = USER_ID
        self.user_name = "Ada"
        self.user_surname = "Lovelace"
        self.entries: dict[str, Entry] = {}
        self.extra_catalogs: list[dict[str, Any]] = []
        self.requests: list[tuple[str, str]] = []
        self._request_url = ""
        self.storage_auth_headers: list[str] = []
        # Login and API keys. Any issued access token or live key token authenticates like TOKEN.
        self.access_tokens: set[str] = set()
        self.api_keys: dict[str, dict[str, Any]] = {}
        self.login_auth_headers: list[str | None] = []
        self.login_failure: tuple[int, Any] | None = None  # force POST /token to fail with this reply
        # Per-user state.
        self.shelf: dict[str, str] = {}  # shelf record id -> entry id
        self.user_acquisitions: dict[str, dict[str, Any]] = {}
        self.annotations: dict[str, dict[str, Any]] = {}
        self.annotation_items: dict[str, dict[str, Any]] = {}
        self.page_size = 2  # small, so every listing exercises pagination
        self._clock = datetime(2026, 1, 1, tzinfo=UTC)
        self._api = _Server(("127.0.0.1", 0), _api_handler(self))
        self._storage = _Server(("127.0.0.1", 0), _storage_handler(self))
        self._threads = [
            threading.Thread(target=server.serve_forever, kwargs={"poll_interval": 0.02}, daemon=True)
            for server in (self._api, self._storage)
        ]

    # ----------------------------------------------------------------- lifecycle

    @property
    def origin(self) -> str:
        host, port = self._api.server_address[:2]
        return f"http://{host!s}:{port}"

    @property
    def base_url(self) -> str:
        return f"{self.origin}/api/v1"

    @property
    def storage_origin(self) -> str:
        host, port = self._storage.server_address[:2]
        return f"http://{host!s}:{port}"

    def start(self) -> FakeCatalog:
        for thread in self._threads:
            thread.start()
        return self

    def stop(self) -> None:
        for server in (self._api, self._storage):
            server.shutdown()
            server.server_close()

    # ------------------------------------------------------------- test helpers

    def tick(self) -> str:
        with self.lock:
            self._clock += timedelta(seconds=1, microseconds=123)
            return self._clock.isoformat().replace("+00:00", "Z")

    def edit_entry(self, entry_id: str, **fields: Any) -> None:
        """Change an entry as another client would."""
        with self.lock:
            entry = self.entries[entry_id]
            entry.data.update(fields)
            entry.updated_at = self.tick()

    def delete_entry(self, entry_id: str) -> None:
        with self.lock:
            del self.entries[entry_id]

    def add_remote_entry(self, title: str, files: dict[str, bytes], **fields: Any) -> str:
        """Create an entry as another client would; ``files`` maps MIME type to content.

        ``categories`` items are ``{"term", "label"}`` or ``{"term", "labels": {lang: label}}``.
        Pass ``cover=b"..."`` to give the entry a cover (and so a thumbnail).
        """
        with self.lock:
            entry_id = str(uuid.uuid4())
            cover = fields.pop("cover", None)
            data = {
                "title": title,
                "language_code": "en",
                "authors": [{"name": "Remote", "surname": "Author"}],
                **fields,
            }
            entry = Entry(entry_id, self.catalog_id, data, self.tick(), cover=cover)
            for mime, content in files.items():
                entry.acquisitions.append(Acquisition(str(uuid.uuid4()), "acquisition", mime, content))
            self.entries[entry_id] = entry
            return entry_id

    def add_catalog(self, title: str, url_name: str) -> str:
        """Another catalog the user can read (it holds no entries)."""
        with self.lock:
            catalog_id = str(uuid.uuid4())
            self.extra_catalogs.append({"id": catalog_id, "url_name": url_name, "title": title})
            return catalog_id

    def add_shelf_record(self, entry_id: str) -> str:
        with self.lock:
            record_id = str(uuid.uuid4())
            self.shelf[record_id] = entry_id
            return record_id

    def add_highlight(
        self,
        entry_id: str,
        page: int,
        text: str | None,
        *,
        content: str = "",
        kind: str = "highlight",
        status: str = "done",
        title: str = "My highlights",
        user_id: str | None = None,
        acquisition_index: int = 0,
    ) -> str:
        """Add one annotation item, creating its user acquisition and annotation on first use."""
        with self.lock:
            user_id = user_id or self.user_id
            acquisition_id = self.entries[entry_id].acquisitions[acquisition_index].id
            ua_id = next(
                (
                    key
                    for key, ua in self.user_acquisitions.items()
                    if ua["user_id"] == user_id and ua["acquisition_id"] == acquisition_id
                ),
                None,
            )
            if ua_id is None:
                ua_id = str(uuid.uuid4())
                self.user_acquisitions[ua_id] = {"user_id": user_id, "acquisition_id": acquisition_id}
            annotation_id = next(
                (
                    key
                    for key, ann in self.annotations.items()
                    if (ann["user_acquisition_id"], ann["kind"], ann["title"], ann["status"])
                    == (ua_id, kind, title, status)
                ),
                None,
            )
            if annotation_id is None:
                annotation_id = str(uuid.uuid4())
                self.annotations[annotation_id] = {
                    "user_acquisition_id": ua_id,
                    "kind": kind,
                    "title": title,
                    "status": status,
                    "created_at": self.tick(),
                }
            item_id = str(uuid.uuid4())
            self.annotation_items[item_id] = {
                "annotation_id": annotation_id,
                "page": page,
                "text": text,
                "content": content,
                "created_at": self.tick(),
            }
            return item_id

    def acquisition(self, acquisition_id: str) -> Acquisition | None:
        for entry in self.entries.values():
            for acq in entry.acquisitions:
                if acq.id == acquisition_id:
                    return acq
        return None

    def category_id(self, term: str, catalog_id: str | None = None) -> str:
        """A category's id, stable per catalog and term, as the server's would be."""
        return str(uuid.uuid5(uuid.NAMESPACE_OID, f"{catalog_id or self.catalog_id}:{term}"))

    # ----------------------------------------------------------- serialisation

    def serialize(self, entry: Entry, detailed: bool, *, shelf: bool = True) -> dict[str, Any]:
        data = entry.data
        result: dict[str, Any] = {
            "id": entry.id,
            "creator_id": USER_ID,
            "catalog_id": entry.catalog_id,
            "shelf_record_id": self._shelf_record_of(entry.id) if shelf else None,
            # Shaped like the live API (dev, 2026-11-08), not the spec on main: "authors"
            # instead of "entry_authors", "image"/"thumbnail" instead of "*_url", labels by language.
            "authors": [
                {"id": str(uuid.uuid4()), "name": a.get("name", ""), "surname": a.get("surname", "")}
                for a in data.get("authors") or []
            ],
            "categories": [
                {"id": self.category_id(c["term"], entry.catalog_id), "term": c["term"], "label": _labels(c)}
                for c in data.get("categories") or []
            ],
            "language": _language(data.get("language_code")),
            "feeds": [],
            "acquisitions": [
                {"id": a.id, "relation": a.relation, "mime": a.mime, "url": f"/data/v1/acquisitions/{a.id}"}
                for a in entry.acquisitions
            ],
            "popularity": 0,
            "title": data["title"],
            "summary": data.get("summary"),
            # Like the live server: without a cover, "image" and "thumbnail" hold the
            # URL of the request itself, not null. Only image_mime says there is none.
            "image": f"{self.origin}/data/v1/covers/{entry.id}" if entry.cover else self._request_url,
            "image_mime": "image/jpeg" if entry.cover else None,
            "thumbnail": f"{self.origin}/data/v1/thumbnails/{entry.id}" if entry.cover else self._request_url,
            "config": {},
            "citation": None,
            "touched_at": entry.updated_at,
            "created_at": entry.updated_at,
            "updated_at": entry.updated_at,
        }
        if detailed:
            result.update(
                {
                    "published_at": data.get("published_at"),
                    "publisher": data.get("publisher"),
                    "identifiers": data.get("identifiers") or {},
                }
            )
        return result

    def _shelf_record_of(self, entry_id: str) -> str | None:
        return next((record for record, entry in self.shelf.items() if entry == entry_id), None)

    def _user(self) -> dict[str, Any]:
        return {"id": self.user_id, "username": USERNAME, "name": self.user_name, "surname": self.user_surname}

    # ------------------------------------------------------------------ routing

    def route(self, method: str, path: str, query: dict[str, list[str]], headers: Any, body: bytes) -> _Reply:
        with self.lock:
            self.requests.append((method, path))
            self._request_url = f"{self.origin}{path}"
            parts = [part for part in path.split("/") if part]
            if parts == ["api", "v1", "token"] and method == "POST":
                return self._login(headers, body)
            if headers.get("Authorization") not in {f"Bearer {token}" for token in self._valid_tokens()}:
                return 401, {"title": "Unauthorized"}, {}

            if parts[:2] == ["data", "v1"]:
                return self._data(parts[2:])
            if parts[:2] != ["api", "v1"]:
                return 404, {"title": "Not found"}, {}
            return self._api_route(method, parts[2:], query, headers, body)

    def _api_route(
        self, method: str, parts: list[str], query: dict[str, list[str]], headers: Any, body: bytes
    ) -> _Reply:
        if parts == ["users", "me"]:
            return 200, {"response": {**self._user(), "is_superuser": False, "is_active": True}}, {}
        if parts == ["api_keys"] and method == "POST":
            return self._create_api_key(json.loads(body or b"{}"))
        if len(parts) == 2 and parts[0] == "api_keys" and method == "DELETE":
            if self.api_keys.pop(parts[1], None) is None:
                return 404, {"title": "ApiKey not found"}, {}
            return 204, None, {}
        if parts == ["catalogs"]:
            wanted = _one(query, "url_name")
            items = [c for c in (self._catalog(), *self.extra_catalogs) if wanted in c["url_name"]]
            return self._page(items, query)
        if parts == ["catalogs", self.catalog_id]:
            return 200, {"response": self._catalog()}, {}
        if parts == ["entries"] and method == "GET":
            return self._list(query)
        if parts == ["entries", "facets"] and method == "GET":
            return self._facets(query)
        if parts == ["categories"] and method == "GET":
            return self._categories(query)
        if parts[:1] == ["shelf-records"]:
            return self._shelf(method, parts[1:], query, body)
        if parts == ["user-acquisitions"] and method == "GET":
            return self._user_acquisitions(query)
        if parts == ["annotations"] and method == "GET":
            return self._annotations(query)
        if parts == ["annotation-items"] and method == "GET":
            return self._annotation_items(query)
        if len(parts) == 3 and parts[0] == "catalogs" and parts[2] == "entries" and method == "POST":
            return self._create(parts[1], json.loads(body))
        if len(parts) == 4 and parts[0] == "catalogs" and parts[2] == "entries":
            entry = self.entries.get(parts[3])
            if entry is None or entry.catalog_id != parts[1]:
                return 404, {"title": "Entry not found"}, {}
            if method == "GET":
                return 200, {"response": self.serialize(entry, True)}, {}
            if method == "PUT":
                return self._update(entry, json.loads(body))
            if method == "POST":
                return self._upload(entry, headers, body)
        if len(parts) == 2 and parts[0] == "acquisitions" and method == "DELETE":
            for entry in self.entries.values():
                for acq in entry.acquisitions:
                    if acq.id == parts[1]:
                        entry.acquisitions.remove(acq)
                        entry.updated_at = self.tick()
                        return 204, None, {}
            return 404, {"title": "Acquisition not found"}, {}
        return 404, {"title": f"no route for {method} /api/v1/{'/'.join(parts)}"}, {}

    def _valid_tokens(self) -> set[str]:
        return {TOKEN, *self.access_tokens, *(key["token"] for key in self.api_keys.values())}

    def _catalog(self) -> dict[str, Any]:
        return {"id": self.catalog_id, "url_name": self.catalog_url_name, "title": "Calibre test catalog"}

    def _page(self, items: list[Any], query: dict[str, list[str]]) -> _Reply:
        """The list envelope. A page past the last answers 404, like the server."""
        page = int(_one(query, "page") or 1)
        limit = max(1, min(int(_one(query, "limit") or self.page_size), self.page_size))
        pages = max(1, math.ceil(len(items) / limit))
        if page > pages:
            return 404, {"title": "Page out of range"}, {}
        chunk = items[(page - 1) * limit : page * limit]
        return (
            200,
            {"items": chunk, "metadata": {"page": page, "limit": limit, "pages": pages, "total": len(items)}},
            {},
        )

    # ------------------------------------------------------------ auth routes

    def _login(self, headers: Any, body: bytes) -> _Reply:
        self.login_auth_headers.append(headers.get("Authorization"))
        if self.login_failure is not None:
            status, payload = self.login_failure
            return status, payload, {}
        data = json.loads(body or b"{}")
        if not data.get("username") or not data.get("password"):
            return 422, {"title": "Validation error"}, {}
        if (data["username"], data["password"]) != (USERNAME, PASSWORD):
            # Echo the password, so a test proves the client never shows it.
            # The live server says only {"title": "Invalid credentials"}.
            return 401, {"title": "Invalid credentials", "detail": f"bad credentials {data['password']}"}, {}
        access = f"access-{uuid.uuid4().hex}"
        self.access_tokens.add(access)
        user = {**self._user(), "permissions": [], "catalog_permissions": {}}
        return (
            200,
            {"response": {"access_token": access, "refresh_token": f"refresh-{uuid.uuid4().hex}", "user": user}},
            {},
        )

    def _create_api_key(self, data: dict[str, Any]) -> _Reply:
        name = data.get("name")
        if name is not None and len(name) > 100:
            return (
                422,
                {"title": "Validation error", "validation_errors": [{"path": ["name"], "message": "too long"}]},
                {},
            )
        key_id = str(uuid.uuid4())
        token = f"apikey-{uuid.uuid4().hex}"
        now = self.tick()
        self.api_keys[key_id] = {"name": name, "token": token}
        record = {
            "id": key_id,
            "user_id": self.user_id,
            "name": name,
            "is_active": True,
            "last_seen_at": None,
            "created_at": now,
            "updated_at": now,
            "token": token,
        }
        return 201, {"response": record}, {}

    # ---------------------------------------------------------- entry routes

    def _matching_entries(self, query: dict[str, list[str]], *, ignore: str = "") -> list[Entry]:
        """Entries of ``catalog_id`` that pass the search filters, except the one named by ``ignore``."""
        catalog_id = _one(query, "catalog_id")
        words = _one(query, "query").casefold().split()
        languages = set(filter(None, _one(query, "language_code").split(",")))
        category_ids = set(filter(None, _one(query, "category_id").split(",")))
        matches = []
        for entry in self.entries.values():
            if entry.catalog_id != catalog_id:
                continue
            if any(word not in entry.data["title"].casefold() for word in words):
                continue
            if ignore != "language_code" and languages and _alpha2(entry.data.get("language_code")) not in languages:
                continue
            if ignore != "category_id" and category_ids and not category_ids & self._category_ids(entry):
                continue
            matches.append(entry)
        return matches

    def _category_ids(self, entry: Entry) -> set[str]:
        return {self.category_id(c["term"], entry.catalog_id) for c in entry.data.get("categories") or []}

    def _list(self, query: dict[str, list[str]]) -> _Reply:
        entries = self._matching_entries(query)
        order_by = _one(query, "order_by")
        if order_by.lstrip("-") == "title":
            entries.sort(key=lambda e: e.data["title"].casefold(), reverse=order_by.startswith("-"))
        elif order_by:
            return 400, {"title": f"unknown order_by {order_by}"}, {}
        else:
            entries.sort(key=lambda e: e.id)
        return self._page([self.serialize(entry, False) for entry in entries], query)

    def _facets(self, query: dict[str, list[str]]) -> _Reply:
        languages = Counter(
            _alpha2(e.data.get("language_code")) for e in self._matching_entries(query, ignore="language_code")
        )
        categories = Counter(
            category for e in self._matching_entries(query, ignore="category_id") for category in self._category_ids(e)
        )
        response = {
            "languages": [{"code": code, "count": n} for code, n in languages.most_common() if code],
            "categories": [{"id": category, "count": n} for category, n in categories.most_common()],
            "feeds": [],
            "availability": [],
            "years": {"min": None, "max": None},
        }
        return 200, {"response": response}, {}

    def _categories(self, query: dict[str, list[str]]) -> _Reply:
        catalog_id = _one(query, "catalog_id")
        found: dict[str, dict[str, Any]] = {}
        for entry in self.entries.values():
            if catalog_id and entry.catalog_id != catalog_id:
                continue
            for category in entry.data.get("categories") or []:
                category_id = self.category_id(category["term"], entry.catalog_id)
                found.setdefault(
                    category_id,
                    {
                        "id": category_id,
                        "term": category["term"],
                        "label": _labels(category),
                        "catalog_id": entry.catalog_id,
                        "scheme": None,
                    },
                )
        return self._page(sorted(found.values(), key=lambda c: c["term"]), query)

    # ---------------------------------------------------------- shelf routes

    def _shelf(self, method: str, rest: list[str], query: dict[str, list[str]], body: bytes) -> _Reply:
        if not rest and method == "GET":
            catalog_id, entry_id = _one(query, "catalog_id"), _one(query, "entry_id")
            items = [
                self._shelf_item(record_id, self.entries[eid])
                for record_id, eid in self.shelf.items()
                if eid in self.entries
                and (not catalog_id or self.entries[eid].catalog_id == catalog_id)
                and (not entry_id or eid == entry_id)
            ]
            return self._page(items, query)
        if not rest and method == "POST":
            entry_id = json.loads(body or b"{}").get("entry_id")
            if entry_id not in self.entries:
                return (
                    422,
                    {"title": "Validation error", "validation_errors": [{"path": ["entry_id"], "message": "invalid"}]},
                    {},
                )
            existing = self._shelf_record_of(entry_id)
            if existing:
                return 200, {"response": self._shelf_item(existing, self.entries[entry_id])}, {}
            record_id = self.add_shelf_record(entry_id)
            return 201, {"response": self._shelf_item(record_id, self.entries[entry_id])}, {}
        if len(rest) == 1 and method == "DELETE":
            if self.shelf.pop(rest[0], None) is None:
                return 404, {"title": "Not found"}, {}
            return 204, None, {}
        return 405, {"title": "Method not allowed"}, {}

    def _shelf_item(self, record_id: str, entry: Entry) -> dict[str, Any]:
        # Like the server: the nested entry carries no shelf_record_id.
        return {
            "id": record_id,
            "user_id": self.user_id,
            "entry": self.serialize(entry, False, shelf=False),
            "created_at": entry.updated_at,
            "updated_at": entry.updated_at,
        }

    # ------------------------------------------------------ annotation routes

    def _user_acquisitions(self, query: dict[str, list[str]]) -> _Reply:
        # Like a superuser's view: without user_id the list holds everyone's records.
        user_id, acquisition_id = _one(query, "user_id"), _one(query, "acquisition_id")
        items = []
        for ua_id, ua in self.user_acquisitions.items():
            if (user_id and ua["user_id"] != user_id) or (acquisition_id and ua["acquisition_id"] != acquisition_id):
                continue
            acq = self.acquisition(ua["acquisition_id"])
            entry = next((e for e in self.entries.values() if acq in e.acquisitions), None)
            items.append(
                {
                    "id": ua_id,
                    "type": "personal",
                    "range": None,
                    "user": {"id": ua["user_id"], "username": "someone", "name": "", "surname": ""},
                    "acquisition": {"relation": acq.relation, "mime": acq.mime, "url": None} if acq else None,
                    "url": f"{self.origin}/data/v1/user-acquisitions/{ua_id}",
                    "entry": self.serialize(entry, False) if entry else None,
                    "expire_at": None,
                    "created_at": self._clock.isoformat(),
                    "updated_at": self._clock.isoformat(),
                }
            )
        return self._page(items, query)

    def _annotations(self, query: dict[str, list[str]]) -> _Reply:
        kind = _one(query, "kind")
        if kind and kind not in ANNOTATION_KINDS:
            return 422, {"title": "Validation error"}, {}
        return self._filtered_page(
            self.annotations,
            query,
            lambda ann: (
                (
                    not _one(query, "user_acquisition_id")
                    or ann["user_acquisition_id"] == _one(query, "user_acquisition_id")
                )
                and (not kind or ann["kind"] == kind)
            ),
            lambda key, ann: {"id": key, **ann, "updated_at": ann["created_at"]},
        )

    def _annotation_items(self, query: dict[str, list[str]]) -> _Reply:
        annotation_id = _one(query, "annotation_id")
        return self._filtered_page(
            self.annotation_items,
            query,
            lambda item: not annotation_id or item["annotation_id"] == annotation_id,
            lambda key, item: {"id": key, **item, "quads": None, "updated_at": item["created_at"]},
        )

    def _filtered_page(
        self,
        rows: dict[str, dict[str, Any]],
        query: dict[str, list[str]],
        keep: Callable[[dict[str, Any]], bool],
        shape: Callable[[str, dict[str, Any]], dict[str, Any]],
    ) -> _Reply:
        return self._page([shape(key, row) for key, row in rows.items() if keep(row)], query)

    # ---------------------------------------------------------- write routes

    def _validate(self, data: dict[str, Any]) -> _Reply | None:
        missing = [key for key in ("title", "language_code") if not data.get(key)]
        if missing:
            return (
                422,
                {
                    "title": "Validation error",
                    "validation_errors": [{"path": [k], "message": "required"} for k in missing],
                },
                {},
            )
        if len(data["language_code"]) > 3:
            return 422, {"title": "Validation error"}, {}
        if data.get("image") and not str(data["image"]).startswith("data:image/"):
            return 422, {"title": "image must be a data URI"}, {}
        return None

    def _create(self, catalog_id: str, data: dict[str, Any]) -> _Reply:
        if catalog_id != self.catalog_id:
            return 404, {"title": "Catalog not found"}, {}
        invalid = self._validate(data)
        if invalid:
            return invalid
        entry_id = data.pop("id", None) or str(uuid.uuid4())
        if entry_id in self.entries:
            return 409, {"title": "Entry already exists", "type": "/conflict"}, {}
        if any(e.data["title"].casefold() == data["title"].casefold() for e in self.entries.values()):
            return 409, {"title": "Entry with this title already exists", "type": "/conflict"}, {}
        entry = Entry(entry_id, catalog_id, {}, self.tick())
        self._apply(entry, data)
        self.entries[entry_id] = entry
        return 201, {"response": self.serialize(entry, True)}, {}

    def _update(self, entry: Entry, data: dict[str, Any]) -> _Reply:
        invalid = self._validate(data)
        if invalid:
            return invalid
        self._apply(entry, data)
        entry.updated_at = self.tick()
        return 200, {"response": self.serialize(entry, True)}, {}

    def _apply(self, entry: Entry, data: dict[str, Any]) -> None:
        image = data.pop("image", None)
        if image:
            entry.cover = base64.b64decode(image.split(",", 1)[1])
        entry.data.update(data)

    def _upload(self, entry: Entry, headers: Any, body: bytes) -> _Reply:
        content_type = headers.get("Content-Type", "")
        message = BytesParser(policy=HTTP).parsebytes(f"Content-Type: {content_type}\r\n\r\n".encode() + body)
        fields: dict[str, Any] = {}
        content: tuple[bytes, str] | None = None
        for part in message.iter_parts():
            name = part.get_param("name", header="content-disposition")
            if part.get_filename():
                content = (part.get_payload(decode=True), part.get_content_type())
            else:
                fields[name] = part.get_payload(decode=True).decode()
        if content is None or "metadata" not in fields:
            return 422, {"title": "content and metadata are required"}, {}
        meta = json.loads(fields["metadata"])
        acq = Acquisition(str(uuid.uuid4()), meta.get("relation", "acquisition"), content[1], content[0])
        entry.acquisitions.append(acq)
        entry.updated_at = self.tick()
        return 201, {"response": {"id": acq.id, "relation": acq.relation, "mime": acq.mime, "url": None}}, {}

    def _data(self, parts: list[str]) -> _Reply:
        if len(parts) == 2 and parts[0] == "acquisitions" and self.acquisition(parts[1]):
            # Like the live server: a download saves the popularity counter, which moves updated_at.
            for entry in self.entries.values():
                if any(acq.id == parts[1] for acq in entry.acquisitions):
                    entry.updated_at = self.tick()
            return 303, None, {"Location": f"{self.storage_origin}/files/{parts[1]}?signature=s3cr3t"}
        if len(parts) == 2 and parts[0] == "covers":
            entry = self.entries.get(parts[1])
            if entry is not None and entry.cover:
                return 200, entry.cover, {"Content-Type": "image/jpeg"}
        if len(parts) == 2 and parts[0] == "thumbnails":
            entry = self.entries.get(parts[1])
            if entry is not None and entry.cover:
                return 200, entry.cover, {"Content-Type": "image/jpeg"}
            if entry is not None:
                # Like the live server's covers: an entry without an image answers 500, not 404.
                return 500, b"<html>Server Error</html>", {"Content-Type": "text/html"}
        return 404, {"title": "Not found"}, {}


def _one(query: dict[str, list[str]], name: str) -> str:
    return (query.get(name) or [""])[0]


def _labels(category: dict[str, Any]) -> dict[str, str]:
    return category.get("labels") or {"en": category.get("label", category["term"])}


def _alpha2(code: str | None) -> str:
    if not code:
        return ""
    return code if len(code) == 2 else next((k for k, v in LANGUAGES.items() if v == code), "")


def _language(code: str | None) -> dict[str, Any] | None:
    if not code:
        return None
    alpha3 = LANGUAGES.get(code, code if len(code) == 3 else "und")
    return {
        "id": str(uuid.uuid5(uuid.NAMESPACE_OID, alpha3)),
        "name": alpha3,
        "alpha2": _alpha2(code),
        "alpha3": alpha3,
    }


def _send(handler: BaseHTTPRequestHandler, status: int, body: Any, headers: dict[str, str]) -> None:
    payload = body if isinstance(body, bytes) else b"" if body is None else json.dumps(body).encode()
    handler.send_response(status)
    handler.send_header("Content-Type", headers.pop("Content-Type", "application/json"))
    for name, value in headers.items():
        handler.send_header(name, value)
    handler.send_header("Content-Length", str(len(payload)))
    handler.end_headers()
    handler.wfile.write(payload)


def _api_handler(fake: FakeCatalog) -> type[BaseHTTPRequestHandler]:
    class Handler(BaseHTTPRequestHandler):
        def _serve(self) -> None:
            length = int(self.headers.get("Content-Length") or 0)
            body = self.rfile.read(length) if length else b""
            url = urlsplit(self.path)
            status, payload, headers = fake.route(self.command, url.path, parse_qs(url.query), self.headers, body)
            _send(self, status, payload, dict(headers))

        do_GET = do_POST = do_PUT = do_DELETE = _serve

        def log_message(self, format: str, *args: Any) -> None:
            return

    return Handler


def _storage_handler(fake: FakeCatalog) -> type[BaseHTTPRequestHandler]:
    class Handler(BaseHTTPRequestHandler):
        def do_GET(self) -> None:
            if self.headers.get("Authorization"):
                fake.storage_auth_headers.append(self.headers["Authorization"])
                _send(self, 400, {"title": "storage got an Authorization header"}, {})
                return
            url = urlsplit(self.path)
            acq = fake.acquisition(url.path.rsplit("/", 1)[-1])
            if acq is None or parse_qs(url.query).get("signature") != ["s3cr3t"]:
                _send(self, 403, b"denied", {"Content-Type": "text/plain"})
                return
            _send(self, 200, acq.content, {"Content-Type": acq.mime})

        def log_message(self, format: str, *args: Any) -> None:
            return

    return Handler
