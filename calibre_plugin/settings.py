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
        )

    def missing(self) -> list[str]:
        """Names of required settings that are empty."""
        return [name for name in ("base_url", "token", "catalog") if not getattr(self, name)]
