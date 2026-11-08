"""Plugin preferences, without any Qt import so the sync engine and tests can use them.

Stored by Calibre in ``<config dir>/plugins/evilflowers.json``. The token is kept
there in plain text, like every other Calibre plugin credential; Calibre offers
plugins no keychain API.
"""

from __future__ import annotations

from dataclasses import dataclass
from enum import Enum

from calibre.utils.config import JSONConfig

prefs = JSONConfig("plugins/evilflowers")
prefs.defaults["base_url"] = ""
prefs.defaults["token"] = ""
prefs.defaults["catalog"] = ""
prefs.defaults["direction"] = "both"
prefs.defaults["default_language"] = "en"
prefs.defaults["relation"] = "acquisition"
prefs.defaults["show_in_sync"] = False
# Set by "Sign in": the id of the key the plugin created, so "Sign out" can revoke it.
# Empty when the user pasted a key; the plugin then never revokes it.
prefs.defaults["api_key_id"] = ""
prefs.defaults["username"] = ""  # display only; never used to authenticate
prefs.defaults["shelf_only"] = False
prefs.defaults["highlights_column"] = ""  # lookup name of a long-text column, "" = don't copy

# The account fields one sign-in or sign-out changes together.
_ACCOUNT_KEYS = ("base_url", "token", "api_key_id", "username")


def migrate() -> None:
    """Carry settings over from plugin 0.x, which stored ``api_key``, ``catalog_id`` and ``read_only``."""
    if "api_key" in prefs or "catalog_id" in prefs:
        if not prefs.get("token") and prefs.get("api_key"):
            prefs["token"] = prefs["api_key"]
        if not prefs.get("catalog") and prefs.get("catalog_id"):
            prefs["catalog"] = prefs["catalog_id"]
        if prefs.get("read_only") and "direction" not in prefs:
            prefs["direction"] = Direction.PULL.value
        for key in ("api_key", "catalog_id", "read_only"):
            if key in prefs:
                del prefs[key]  # JSONConfig commits on __delitem__, not on pop()


def save_account(base_url: str, token: str, api_key_id: str, username: str) -> None:
    """Store a new sign-in at once, not when the settings page is saved.

    The key already exists on the server; if the user then cancelled the settings
    page, the only copy of its token would be lost and the key left orphaned.
    """
    values = (base_url.strip(), token.strip(), api_key_id.strip(), username.strip())
    for key, value in zip(_ACCOUNT_KEYS, values, strict=True):
        prefs[key] = value


def clear_account() -> None:
    """Forget the key and who it belongs to; the server address stays for the next sign-in."""
    for key in _ACCOUNT_KEYS[1:]:
        prefs[key] = ""


class Direction(Enum):
    BOTH = "both"
    PUSH = "push"  # Calibre -> catalog only
    PULL = "pull"  # catalog -> Calibre only

    @property
    def pushes(self) -> bool:
        return self in (Direction.BOTH, Direction.PUSH)

    @property
    def pulls(self) -> bool:
        return self in (Direction.BOTH, Direction.PULL)


@dataclass(frozen=True, slots=True)
class Settings:
    base_url: str
    token: str
    catalog: str
    direction: Direction = Direction.BOTH
    default_language: str = "en"
    relation: str = "acquisition"
    # New fields go last, with defaults: tests and scripts build Settings positionally.
    api_key_id: str = ""
    username: str = ""
    shelf_only: bool = False
    highlights_column: str = ""

    @property
    def signed_in(self) -> bool:
        """True when the key came from "Sign in" (so it can be revoked), not from pasting."""
        return bool(self.token and self.api_key_id)

    @classmethod
    def load(cls) -> Settings:
        try:
            direction = Direction(prefs["direction"])
        except ValueError:
            direction = Direction.BOTH
        return cls(
            base_url=prefs["base_url"].strip(),
            token=prefs["token"].strip(),
            catalog=prefs["catalog"].strip(),
            direction=direction,
            default_language=prefs["default_language"].strip() or "en",
            relation=prefs["relation"],
            api_key_id=str(prefs["api_key_id"] or "").strip(),
            username=str(prefs["username"] or "").strip(),
            shelf_only=bool(prefs["shelf_only"]),
            highlights_column=str(prefs["highlights_column"] or "").strip(),
        )

    def save(self) -> None:
        """Write every field back to the preferences."""
        prefs["base_url"] = self.base_url
        prefs["token"] = self.token
        prefs["api_key_id"] = self.api_key_id
        prefs["username"] = self.username
        prefs["catalog"] = self.catalog
        prefs["direction"] = self.direction.value
        prefs["relation"] = self.relation
        prefs["default_language"] = self.default_language
        prefs["shelf_only"] = self.shelf_only
        prefs["highlights_column"] = self.highlights_column

    def missing(self) -> list[str]:
        """Names of required settings that are empty."""
        return [name for name in ("base_url", "token", "catalog") if not getattr(self, name)]
