"""Sign in with a username and password, and trade the session for a long-lived API key.

Login cannot go through :class:`~..vendor.evilflowers_protocol.transport.Transport`:
it needs a token before there is one. These helpers use plain :mod:`urllib` with the
same conventions: JSON in and out, redirects never followed, problem-detail errors
mapped onto :mod:`..vendor.evilflowers_protocol.errors`.

What is kept: only the API key. The access token from ``POST /token`` lives in a
local variable for one request and is dropped; the refresh token is never read.
The password is never logged and never appears in an exception message.
"""

from __future__ import annotations

import json
import logging
import socket
import time
import urllib.error
import urllib.parse
import urllib.request
from typing import Any

from ..vendor.evilflowers_protocol.errors import CatalogError, Unauthorized, describe, error_for_status
from ..vendor.evilflowers_protocol.transport import _NoRedirect
from .client import _problem, api_root
from .models import ApiKeyCreated

__all__ = ["ApiKeyCreated", "create_api_key", "default_key_name", "login", "sign_in"]

logger = logging.getLogger("evilflowers_protocol")

_KEY_NAME_LIMIT = 100  # ApiKeyForm.name max_length on the server
WRONG_CREDENTIALS = "Wrong username or password."


def default_key_name() -> str:
    """A key name that tells the user, in the catalog's key list, which computer holds it."""
    try:
        host = socket.gethostname().strip()
    except OSError:
        host = ""
    return f"Calibre on {host or 'this computer'}"


def login(base_url: str, username: str, password: str, *, user_agent: str, timeout: float = 30) -> str:
    """``POST /token`` and return the short-lived access token.

    Raises :class:`Unauthorized` with :data:`WRONG_CREDENTIALS` on a 401.
    """
    body = _post(
        f"{api_root(base_url)}/token",
        {"username": username, "password": password},
        token=None,
        secrets=(password,),
        user_agent=user_agent,
        timeout=timeout,
        on_401=WRONG_CREDENTIALS,
    )
    token = body.get("access_token") if isinstance(body, dict) else None
    if not isinstance(token, str) or not token:
        raise CatalogError(200, None, "POST", f"{api_root(base_url)}/token", "the server sent no access token")
    return token


def create_api_key(
    base_url: str, access_token: str, name: str, *, user_agent: str, timeout: float = 30
) -> ApiKeyCreated:
    """``POST /api_keys`` as the user ``access_token`` belongs to. The reply holds the key's only copy."""
    name = name.strip()[:_KEY_NAME_LIMIT]
    url = f"{api_root(base_url)}/api_keys"
    body = _post(
        url,
        {"name": name},
        token=access_token,
        secrets=(access_token,),
        user_agent=user_agent,
        timeout=timeout,
    )
    try:
        created = ApiKeyCreated.from_dict(body)
    except (KeyError, TypeError, ValueError, AttributeError) as exc:
        raise CatalogError(
            201, None, "POST", url, f"unexpected response for ApiKeyCreated: {type(exc).__name__}"
        ) from None
    return created if created.name else ApiKeyCreated(created.id, created.token, name)


def sign_in(
    base_url: str, username: str, password: str, *, key_name: str, user_agent: str, timeout: float = 30
) -> ApiKeyCreated:
    """Log in and create an API key named ``key_name``; return the key.

    The access token is used for that one request and then discarded. Store
    ``result.token`` (and ``result.id``, to revoke it later), nothing else.
    """
    access_token = login(base_url, username, password, user_agent=user_agent, timeout=timeout)
    return create_api_key(base_url, access_token, key_name, user_agent=user_agent, timeout=timeout)


# ------------------------------------------------------------------------- helpers


def _post(
    url: str,
    payload: dict[str, Any],
    *,
    token: str | None,
    secrets: tuple[str, ...],
    user_agent: str,
    timeout: float,
    on_401: str | None = None,
) -> Any:
    """POST JSON once (no retries: a repeated POST could create a second key) and unwrap the envelope.

    ``secrets`` are scrubbed from any error message, in case the server echoes them.
    """
    headers = {"Accept": "application/json", "Content-Type": "application/json", "User-Agent": user_agent}
    if token:
        headers["Authorization"] = f"Bearer {token}"
    request = urllib.request.Request(url, data=json.dumps(payload).encode(), headers=headers, method="POST")
    opener = urllib.request.build_opener(_NoRedirect)
    path = urllib.parse.urlsplit(url).path
    started = time.monotonic()
    try:
        with opener.open(request, timeout=timeout) as response:
            status = response.status
            content = response.read()
    except urllib.error.HTTPError as exc:
        _log(path, exc.code, started)
        content = _read(exc)
        body = _problem(content)
        if exc.code == 401 and on_401:
            raise Unauthorized(401, None, "POST", url, on_401) from None
        if 300 <= exc.code < 400:
            message = "the server redirected the request; check the URL (https instead of http?)"
        else:
            message = _scrub(describe(exc.code, body), secrets)
        raise error_for_status(exc.code)(exc.code, None, "POST", url, message) from None
    except (urllib.error.URLError, OSError) as exc:
        _log(path, None, started)
        reason = _scrub(str(getattr(exc, "reason", exc)) or type(exc).__name__, secrets)
        raise CatalogError(None, None, "POST", url, reason) from None
    _log(path, status, started)
    try:
        decoded = json.loads(content) if content else None
    except ValueError:
        decoded = None
    if not isinstance(decoded, dict) or "response" not in decoded:
        raise CatalogError(status, None, "POST", url, "missing 'response' envelope")
    return decoded["response"]


def _read(exc: urllib.error.HTTPError) -> bytes:
    try:
        return exc.read()
    except OSError:
        return b""
    finally:
        exc.close()


def _scrub(text: str, secrets: tuple[str, ...]) -> str:
    for secret in secrets:
        if secret:
            text = text.replace(secret, "***")
    return text


def _log(path: str, status: int | None, started: float) -> None:
    elapsed_ms = (time.monotonic() - started) * 1000
    logger.debug("POST %s -> %s (%.0f ms)", path, status if status is not None else "no response", elapsed_ms)
