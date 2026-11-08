"""Runs a scan or a sync off the GUI thread and reports back through Qt signals."""

from __future__ import annotations

import logging
import threading
from typing import Any

from calibre.constants import __version__ as calibre_version
from qt.core import QThread, pyqtSignal

from .remote.client import CatalogClient
from .settings import Settings
from .sync.engine import SyncEngine
from .sync.plan import PlanItem
from .vendor.evilflowers_protocol.errors import CatalogError, NotFound, Unauthorized

log = logging.getLogger("evilflowers")


def user_agent() -> str:
    from . import EvilFlowersPlugin

    return f"evilflowers-calibre/{'.'.join(map(str, EvilFlowersPlugin.version))} calibre/{calibre_version}"


def connect(settings: Settings, *, timeout: float = 60, retries: int = 3) -> tuple[CatalogClient, Any]:
    """A client and the configured catalog. Raises :class:`CatalogError` with a readable message."""
    client = CatalogClient(settings.base_url, settings.token, timeout=timeout, retries=retries, user_agent=user_agent())
    client.me()
    try:
        catalog = client.resolve_catalog(settings.catalog)
    except NotFound as exc:
        raise CatalogError(
            404, None, "GET", exc.url, f"No catalog named {settings.catalog!r} is visible to this API key"
        ) from None
    return client, catalog


def describe_error(exc: BaseException) -> str:
    if isinstance(exc, Unauthorized):
        return "The server rejected the API key (expired, revoked or without access to this catalog)."
    if isinstance(exc, CatalogError):
        if exc.status is None:
            return f"Cannot reach the server: {exc.message}"
        return exc.message
    if isinstance(exc, ValueError):
        return str(exc)
    return f"{type(exc).__name__}: {exc}"


class SyncWorker(QThread):
    progress = pyqtSignal(float, str)
    scanned = pyqtSignal(object, object)  # list[PlanItem], CatalogRef
    applied = pyqtSignal(object)  # Report
    failed = pyqtSignal(str)

    def __init__(self, db: Any, settings: Settings, items: list[PlanItem] | None = None, parent: Any = None) -> None:
        super().__init__(parent)
        self._db = db
        self._settings = settings
        self._items = items
        self._abort = threading.Event()

    def abort(self) -> None:
        self._abort.set()

    def run(self) -> None:
        try:
            client, catalog = connect(self._settings)
            engine = SyncEngine(
                self._db,
                client,
                catalog,
                self._settings,
                log=log,
                abort=self._abort,
                progress=lambda fraction, message: self.progress.emit(fraction, message),
            )
            if self._items is None:
                self.scanned.emit(engine.scan(), catalog)
            else:
                self.applied.emit(engine.apply(self._items))
        except InterruptedError:
            self.failed.emit("Stopped.")
        except Exception as exc:
            log.exception("EvilFlowers sync failed")
            self.failed.emit(describe_error(exc))
