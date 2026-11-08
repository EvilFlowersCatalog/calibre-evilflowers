"""A stateful stand-in for the EvilFlowers Catalog API, served by stdlib ``http.server`` on localhost.

Grown from ``tests/fake_catalog.py`` in evilflowers-protocol-python (scripted replies)
into a small in-memory catalog: entries, acquisitions, covers, the response and
pagination envelopes, 409 on duplicate id or title, and RFC 7807 errors.

Downloads answer ``303 See Other`` to a second server on another port (standing
in for object storage), which fails any request that carries an ``Authorization``
header -- the token must never leave the catalog's origin.
"""

from __future__ import annotations

import base64
import json
import math
import threading
import uuid
from dataclasses import dataclass, field
from datetime import UTC, datetime, timedelta
from email.parser import BytesParser
from email.policy import HTTP
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from typing import Any
from urllib.parse import parse_qs, urlsplit

TOKEN = "eyJhbGciOiJSUzI1NiJ9.ZmFrZS1jYXRhbG9nLXRva2Vu.c2lnbmF0dXJl"
LANGUAGES = {"en": "eng", "sk": "slk", "de": "deu", "cs": "ces"}


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
        self.entries: dict[str, Entry] = {}
        self.requests: list[tuple[str, str]] = []
        self.storage_auth_headers: list[str] = []
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
        """Create an entry as another client would; ``files`` maps MIME type to content."""
        with self.lock:
            entry_id = str(uuid.uuid4())
            data = {
                "title": title,
                "language_code": "en",
                "authors": [{"name": "Remote", "surname": "Author"}],
                **fields,
            }
            entry = Entry(entry_id, self.catalog_id, data, self.tick())
            for mime, content in files.items():
                entry.acquisitions.append(Acquisition(str(uuid.uuid4()), "acquisition", mime, content))
            self.entries[entry_id] = entry
            return entry_id

    def acquisition(self, acquisition_id: str) -> Acquisition | None:
        for entry in self.entries.values():
            for acq in entry.acquisitions:
                if acq.id == acquisition_id:
                    return acq
        return None

    # ----------------------------------------------------------- serialisation

    def serialize(self, entry: Entry, detailed: bool) -> dict[str, Any]:
        data = entry.data
        code = data.get("language_code")
        language = None
        if code:
            alpha3 = LANGUAGES.get(code, code if len(code) == 3 else "und")
            alpha2 = code if len(code) == 2 else next((k for k, v in LANGUAGES.items() if v == code), "")
            language = {
                "id": str(uuid.uuid5(uuid.NAMESPACE_OID, alpha3)),
                "name": alpha3,
                "alpha2": alpha2,
                "alpha3": alpha3,
            }
        result: dict[str, Any] = {
            "id": entry.id,
            "creator_id": "00000000-0000-0000-0000-000000000001",
            "catalog_id": entry.catalog_id,
            # Shaped like the live API (dev, 2026-11-08), not the spec on main: "authors"
            # instead of "entry_authors", "image"/"thumbnail" instead of "*_url", labels by language.
            "authors": [
                {"id": str(uuid.uuid4()), "name": a.get("name", ""), "surname": a.get("surname", "")}
                for a in data.get("authors") or []
            ],
            "categories": [
                {"id": str(uuid.uuid4()), "term": c["term"], "label": {"en": c.get("label", c["term"])}}
                for c in data.get("categories") or []
            ],
            "language": language,
            "feeds": [],
            "acquisitions": [
                {"id": a.id, "relation": a.relation, "mime": a.mime, "url": f"/data/v1/acquisitions/{a.id}"}
                for a in entry.acquisitions
            ],
            "popularity": 0,
            "title": data["title"],
            "summary": data.get("summary"),
            "image": f"{self.origin}/data/v1/covers/{entry.id}" if entry.cover else None,
            "image_mime": "image/jpeg" if entry.cover else None,
            "thumbnail": None,
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

    # ------------------------------------------------------------------ routing

    def route(
        self, method: str, path: str, query: dict[str, list[str]], headers: Any, body: bytes
    ) -> tuple[int, Any, dict[str, str]]:
        with self.lock:
            self.requests.append((method, path))
            if headers.get("Authorization") != f"Bearer {TOKEN}":
                return 401, {"title": "Unauthorized"}, {}
            parts = [part for part in path.split("/") if part]

            if parts[:2] == ["data", "v1"]:
                return self._data(parts[2:])
            if parts[:2] != ["api", "v1"]:
                return 404, {"title": "Not found"}, {}
            parts = parts[2:]

            if parts == ["users", "me"]:
                return 200, {"response": {"id": "00000000-0000-0000-0000-000000000001", "username": "tester"}}, {}
            if parts == ["catalogs"]:
                wanted = (query.get("url_name") or [""])[0]
                items = [self._catalog()] if wanted in self.catalog_url_name else []
                return 200, {"items": items, "metadata": {"page": 1, "limit": 100, "pages": 1, "total": len(items)}}, {}
            if parts == ["catalogs", self.catalog_id]:
                return 200, {"response": self._catalog()}, {}
            if parts == ["entries"] and method == "GET":
                return self._list(query)
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
            return 404, {"title": f"no route for {method} {path}"}, {}

    def _catalog(self) -> dict[str, Any]:
        return {"id": self.catalog_id, "url_name": self.catalog_url_name, "title": "Calibre test catalog"}

    def _list(self, query: dict[str, list[str]]) -> tuple[int, Any, dict[str, str]]:
        catalog_id = (query.get("catalog_id") or [""])[0]
        page = int((query.get("page") or ["1"])[0])
        entries = sorted((e for e in self.entries.values() if e.catalog_id == catalog_id), key=lambda e: e.id)
        pages = max(1, math.ceil(len(entries) / self.page_size))
        if page > pages:
            return 404, {"title": "Page out of range"}, {}
        chunk = entries[(page - 1) * self.page_size : page * self.page_size]
        items = [self.serialize(entry, False) for entry in chunk]
        return (
            200,
            {
                "items": items,
                "metadata": {"page": page, "limit": self.page_size, "pages": pages, "total": len(entries)},
            },
            {},
        )

    def _validate(self, data: dict[str, Any]) -> tuple[int, Any, dict[str, str]] | None:
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

    def _create(self, catalog_id: str, data: dict[str, Any]) -> tuple[int, Any, dict[str, str]]:
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

    def _update(self, entry: Entry, data: dict[str, Any]) -> tuple[int, Any, dict[str, str]]:
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

    def _upload(self, entry: Entry, headers: Any, body: bytes) -> tuple[int, Any, dict[str, str]]:
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

    def _data(self, parts: list[str]) -> tuple[int, Any, dict[str, str]]:
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
        return 404, {"title": "Not found"}, {}


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
