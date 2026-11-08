"""HTTP transport for the EvilFlowers Catalog REST API, on stdlib :mod:`urllib` only.

This layer knows the backend's *conventions* but no endpoint and no model:

* bearer auth, JSON in and out, ``Accept: application/json``;
* the single-object envelope ``{"response": ...}`` and the list envelope
  ``{"items": [...], "metadata": {"page", "pages", ...}}``;
* RFC 7807 problem-detail error bodies, mapped onto :mod:`evilflowers_protocol.errors`;
* retries, streamed multipart uploads, logging.

Endpoints live one layer up (:mod:`evilflowers_protocol.client`) and models come from
the generated data layer (:mod:`evilflowers_protocol.schema`, via
:mod:`evilflowers_protocol.forms`). A change to the OpenAPI spec should never need a
change here.

Behaviour worth knowing:

* **Auth.** The token is kept out of ``repr()``, exception messages and log lines.
* **Retries.** Connection errors, timeouts and HTTP 502/503/504 are retried up to
  ``retries`` times, with exponential backoff and jitter. Nothing else is retried.
  A retried ``POST`` can repeat work the server already did.
* **Redirects** are not followed. Following one would forward the bearer token to
  whatever host the redirect names. A 3xx raises :class:`CatalogError`, which usually
  means ``base_url`` is wrong (for example ``http`` instead of ``https``).
* **Logging** goes to ``logging.getLogger("evilflowers_protocol")`` at DEBUG only:
  method, path, status and duration in ms.
* **Threads.** A :class:`Transport` holds no per-request state and can be shared.
"""

from __future__ import annotations

import http.client
import json
import logging
import random
import re
import secrets
import time
import urllib.error
import urllib.parse
import urllib.request
from collections.abc import Callable, Iterable, Iterator, Mapping
from dataclasses import dataclass
from email.message import Message
from pathlib import Path
from typing import IO, Any, Protocol, TypeVar

from ._version import __version__
from .errors import CatalogError, describe, error_for_status

__all__ = ["DEFAULT_USER_AGENT", "Model", "Payload", "Response", "Transport"]


# vendored: pydantic replaced by two structural protocols (Calibre bundles no pydantic).
class Model(Protocol):
    """A response model: builds itself from decoded JSON, raising KeyError/TypeError/ValueError on a mismatch."""

    @classmethod
    def from_dict(cls, data: Any) -> Any: ...


class Payload(Protocol):
    """A request body: serialises itself to a JSON-ready dict with unset fields left out."""

    def to_dict(self) -> dict[str, Any]: ...

logger = logging.getLogger("evilflowers_protocol")

DEFAULT_USER_AGENT = f"evilflowers-protocol/{__version__}"

_RETRY_STATUSES = frozenset({502, 503, 504})
_BACKOFF_BASE = 0.5
_BACKOFF_CAP = 30.0
CHUNK_SIZE = 64 * 1024
PAGE_LIMIT = 100
_MIME = re.compile(r"^[A-Za-z0-9!#$&^_.+-]+/[A-Za-z0-9!#$&^_.+-]+$")

_M = TypeVar("_M", bound=Model)
_Body = bytes | Iterable[bytes]


@dataclass(frozen=True, slots=True)
class Response:
    """A decoded response: ``body`` is parsed JSON, raw text, or None for an empty body."""

    method: str
    url: str
    status: int
    body: Any

    def unwrap(self) -> Any:
        """The payload inside the ``{"response": ...}`` envelope."""
        if not isinstance(self.body, dict) or "response" not in self.body:
            raise CatalogError(self.status, self.body, self.method, self.url, "missing 'response' envelope")
        return self.body["response"]

    def model(self, model: type[_M]) -> _M:
        """Validate the enveloped payload as ``model``."""
        return self.validate(model, self.unwrap())

    def validate(self, model: type[_M], data: Any) -> _M:
        """Validate ``data`` from this response as ``model``; a mismatch raises :class:`CatalogError`."""
        try:
            return model.from_dict(data)  # type: ignore[no-any-return]
        except (KeyError, TypeError, ValueError) as exc:
            message = f"unexpected response for {model.__name__}: {type(exc).__name__}: {exc}"
            raise CatalogError(self.status, self.body, self.method, self.url, message) from exc


class _NoRedirect(urllib.request.HTTPRedirectHandler):
    """Turn every 3xx into an ``HTTPError`` instead of following it."""

    def redirect_request(
        self,
        req: urllib.request.Request,
        fp: IO[bytes],
        code: int,
        msg: str,
        headers: Message,
        newurl: str,
    ) -> None:
        return None


class Transport:
    """Sends requests to one catalog API root, e.g. ``https://host/api/v1``.

    ``retries`` counts extra attempts, so ``retries=3`` means at most 4 requests.
    """

    def __init__(
        self,
        base_url: str,
        token: str,
        *,
        timeout: float = 60,
        retries: int = 3,
        user_agent: str = DEFAULT_USER_AGENT,
    ) -> None:
        parts = urllib.parse.urlsplit(base_url)
        if parts.scheme not in ("http", "https") or not parts.netloc:
            raise ValueError(f"base_url must be an absolute http(s) URL, got {base_url!r}")
        if parts.query or parts.fragment:
            raise ValueError("base_url must not contain a query string or fragment")
        if not token or "\r" in token or "\n" in token:
            raise ValueError("token must be a non-empty single-line string")
        if retries < 0:
            raise ValueError("retries must be >= 0")
        if timeout <= 0:
            raise ValueError("timeout must be > 0")

        self.base_url = base_url.rstrip("/")
        self._token = token
        self._timeout = timeout
        self._retries = retries
        self._user_agent = user_agent
        self._opener = urllib.request.build_opener(_NoRedirect)
        self.sleep: Callable[[float], None] = time.sleep

    def __repr__(self) -> str:
        return f"{type(self).__name__}(base_url={self.base_url!r})"

    # -------------------------------------------------------------- conveniences

    def get(self, path: str, model: type[_M], *, query: Mapping[str, str] | None = None) -> _M:
        """``GET`` a single object and validate it as ``model``."""
        return self.request("GET", path, query=query).model(model)

    def send(self, method: str, path: str, payload: Payload, model: type[_M]) -> _M:
        """Send ``payload`` as JSON (unset fields omitted) and validate the reply as ``model``."""
        body = payload.to_dict()
        return self.request(method, path, json_body=body).model(model)

    def iter_pages(self, path: str, model: type[_M], *, query: Mapping[str, str] | None = None) -> Iterator[_M]:
        """Yield every item of a paginated list.

        Asks for page 1, then 2..pages. The backend answers 404 past the last page,
        so it never probes beyond ``metadata.pages``.
        """
        page_number = 1
        while True:
            page_query = {**(query or {}), "page": str(page_number), "limit": str(PAGE_LIMIT)}
            response = self.request("GET", path, query=page_query)
            body = response.body
            if not isinstance(body, dict) or not isinstance(body.get("items"), list):
                raise CatalogError(response.status, body, response.method, response.url, "missing 'items' list")
            metadata = body.get("metadata")
            pages = metadata.get("pages", 1) if isinstance(metadata, dict) else 1
            for item in body["items"]:
                yield response.validate(model, item)
            if not isinstance(pages, int) or page_number >= pages:
                return
            page_number += 1

    def upload(
        self,
        path: str,
        file_path: Path,
        model: type[_M],
        *,
        file_field: str,
        mime: str,
        fields: Mapping[str, str] | None = None,
    ) -> _M:
        """``POST`` a file as ``multipart/form-data`` and validate the reply as ``model``.

        ``fields`` are plain form fields sent before the file. The file is streamed
        from disk in chunks with a precomputed ``Content-Length``; it is never read
        into memory whole.
        """
        file_path = Path(file_path)
        if not file_path.is_file():
            raise FileNotFoundError(f"not a file: {file_path}")
        if not _MIME.match(mime):
            raise ValueError(f"invalid MIME type {mime!r}")
        size = file_path.stat().st_size
        boundary = f"evilflowers-{secrets.token_hex(16)}"
        head, tail = multipart_envelope(boundary, fields or {}, file_field, file_path.name, mime)
        response = self.request(
            "POST",
            path,
            body=lambda: stream_multipart(head, file_path, size, tail),
            headers={
                "Content-Type": f"multipart/form-data; boundary={boundary}",
                "Content-Length": str(len(head) + size + len(tail)),
            },
        )
        return response.model(model)

    # ------------------------------------------------------------------- core

    def url(self, path: str, query: Mapping[str, str] | None = None) -> str:
        """Join ``path`` onto the API root without doubling slashes."""
        url = f"{self.base_url}/{path.lstrip('/')}"
        if query:
            url = f"{url}?{urllib.parse.urlencode(query)}"
        return url

    def request(
        self,
        method: str,
        path: str,
        *,
        query: Mapping[str, str] | None = None,
        json_body: Any = None,
        body: Callable[[], _Body] | None = None,
        headers: Mapping[str, str] | None = None,
    ) -> Response:
        """Send one logical request, retrying transient failures.

        ``body`` is a factory, not a value: a streamed body is consumed by each
        attempt, so every retry needs a fresh one.
        """
        url = self.url(path, query)
        log_path = urllib.parse.urlsplit(url).path
        request_headers = {
            "Accept": "application/json",
            "Authorization": f"Bearer {self._token}",
            "User-Agent": self._user_agent,
        }
        if json_body is not None:
            payload = json.dumps(json_body).encode()
            request_headers["Content-Type"] = "application/json"
            body = lambda: payload
        if headers:
            request_headers.update(headers)

        attempt = 0
        while True:
            request = urllib.request.Request(
                url,
                data=body() if body is not None else None,
                headers=request_headers,
                method=method,
            )
            started = time.monotonic()
            try:
                with self._opener.open(request, timeout=self._timeout) as raw:
                    status: int = raw.status
                    content: bytes = raw.read()
            except urllib.error.HTTPError as exc:
                # Must come first: HTTPError subclasses URLError and OSError.
                content = _read_error_body(exc)
                self._log(method, log_path, exc.code, started)
                if exc.code in _RETRY_STATUSES and attempt < self._retries:
                    self._backoff(attempt, method, log_path, f"HTTP {exc.code}")
                    attempt += 1
                    continue
                error_body = _decode(content)
                message = self._scrub(describe(exc.code, error_body))
                raise error_for_status(exc.code)(exc.code, error_body, method, url, message) from None
            except (urllib.error.URLError, http.client.HTTPException, OSError) as exc:
                # OSError covers TimeoutError, ConnectionResetError and friends.
                self._log(method, log_path, None, started)
                reason = self._scrub(str(getattr(exc, "reason", exc)) or type(exc).__name__)
                if attempt < self._retries:
                    self._backoff(attempt, method, log_path, reason)
                    attempt += 1
                    continue
                message = f"no response after {attempt + 1} attempt(s): {reason}"
                raise CatalogError(None, None, method, url, message) from exc

            self._log(method, log_path, status, started)
            return Response(method, url, status, _decode(content))

    def _backoff(self, attempt: int, method: str, path: str, reason: str) -> None:
        delay = min(_BACKOFF_CAP, _BACKOFF_BASE * 2**attempt)
        delay = delay / 2 + random.uniform(0, delay / 2)  # jitter, not cryptography
        logger.debug(
            "%s %s failed (%s); retry %d/%d in %.2fs", method, path, reason, attempt + 1, self._retries, delay
        )
        self.sleep(delay)

    def _log(self, method: str, path: str, status: int | None, started: float) -> None:
        elapsed_ms = (time.monotonic() - started) * 1000
        logger.debug("%s %s -> %s (%.0f ms)", method, path, status if status is not None else "no response", elapsed_ms)

    def _scrub(self, text: str) -> str:
        return text.replace(self._token, "***")


# ---------------------------------------------------------------------- helpers


def _decode(content: bytes) -> Any:
    if not content:
        return None
    try:
        return json.loads(content)
    except (ValueError, UnicodeDecodeError):
        return content.decode("utf-8", errors="replace")


def _read_error_body(exc: urllib.error.HTTPError) -> bytes:
    try:
        return exc.read()
    except (OSError, http.client.HTTPException):
        return b""
    finally:
        exc.close()


def _quote(value: str) -> str:
    return "".join(ch for ch in value if ch not in "\r\n").replace('"', "%22").replace("\\", "_")


def multipart_envelope(
    boundary: str, fields: Mapping[str, str], file_field: str, filename: str, mime: str
) -> tuple[bytes, bytes]:
    """Everything before and after the file bytes in a multipart body."""
    head = "".join(
        f'--{boundary}\r\nContent-Disposition: form-data; name="{_quote(name)}"\r\n\r\n{value}\r\n'
        for name, value in fields.items()
    )
    head += (
        f"--{boundary}\r\n"
        f'Content-Disposition: form-data; name="{_quote(file_field)}"; filename="{_quote(filename)}"\r\n'
        f"Content-Type: {mime}\r\n"
        "\r\n"
    )
    return head.encode(), f"\r\n--{boundary}--\r\n".encode()


def stream_multipart(head: bytes, path: Path, size: int, tail: bytes) -> Iterator[bytes]:
    """Yield ``head``, then exactly ``size`` bytes of the file in chunks, then ``tail``."""
    yield head
    remaining = size
    with path.open("rb") as handle:
        while remaining > 0:
            chunk = handle.read(min(CHUNK_SIZE, remaining))
            if not chunk:
                raise OSError(f"{path.name} shrank while it was being uploaded")
            remaining -= len(chunk)
            yield chunk
    yield tail
