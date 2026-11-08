"""Exceptions raised by :mod:`evilflowers_protocol.transport` and the client built on it."""

from __future__ import annotations

from typing import Any

__all__ = ["CatalogError", "Conflict", "NotFound", "Unauthorized", "error_for_status"]

_MESSAGE_BODY_LIMIT = 500


class CatalogError(Exception):
    """A request to the catalog failed.

    ``status`` is the HTTP status, or None when no response arrived (connection
    error, timeout) or when the failure is client-side (for example, no catalog
    has the requested ``url_name``). ``body`` is the parsed JSON error body (an
    RFC 7807 problem detail on this backend), the raw text when the body is not
    JSON, or None.
    """

    def __init__(
        self,
        status: int | None,
        body: Any,
        method: str,
        url: str,
        message: str | None = None,
    ) -> None:
        self.status = status
        self.body = body
        self.method = method
        self.url = url
        self.message = message if message is not None else describe(status, body)
        super().__init__(self.message)

    def __str__(self) -> str:
        status = self.status if self.status is not None else "no response"
        return f"{self.method} {self.url} -> {status}: {self.message}"

    def __repr__(self) -> str:
        return (
            f"{type(self).__name__}(status={self.status!r}, method={self.method!r}, "
            f"url={self.url!r}, message={self.message!r})"
        )

    def __reduce__(self) -> tuple[Any, ...]:
        # Keep the exception picklable (process pools) despite the custom __init__.
        return (type(self), (self.status, self.body, self.method, self.url, self.message))


class Unauthorized(CatalogError):
    """HTTP 401 or 403: the token is missing, invalid, expired or lacks permission."""


class NotFound(CatalogError):
    """HTTP 404, or a lookup (such as ``CatalogClient.resolve_catalog``) that matched nothing."""


class Conflict(CatalogError):
    """HTTP 409. For entries: the catalog already has one with the same title, ISBN or DOI."""


def error_for_status(status: int) -> type[CatalogError]:
    """The exception class for an HTTP error status."""
    if status in (401, 403):
        return Unauthorized
    if status == 404:
        return NotFound
    if status == 409:
        return Conflict
    return CatalogError


def describe(status: int | None, body: Any) -> str:
    """A one-line message from a problem-detail body (title, detail, validation errors)."""
    if isinstance(body, dict):
        parts = [str(body[key]) for key in ("title", "detail") if body.get(key)]
        errors = body.get("validation_errors")
        if isinstance(errors, list):
            parts.extend(
                f"{'.'.join(map(str, item.get('path') or []))}: {item.get('message')}"
                for item in errors
                if isinstance(item, dict)
            )
        if parts:
            return "; ".join(parts)
    if isinstance(body, str) and body.strip():
        return body.strip()[:_MESSAGE_BODY_LIMIT]
    return f"HTTP {status}" if status is not None else "request failed"
