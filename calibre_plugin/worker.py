"""Runs a scan or a sync off the GUI thread and reports back through Qt signals."""

from __future__ import annotations

import contextlib
import logging
import queue
import threading
import time
from collections.abc import Callable
from pathlib import Path
from typing import Any
from uuid import UUID

from calibre.constants import __version__ as calibre_version
from qt.core import QImage, QSize, Qt, QThread, pyqtSignal

from .remote.client import CatalogClient
from .remote.models import UserRef
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
    client, catalog, _user = connect_as(settings, timeout=timeout, retries=retries)
    return client, catalog


def connect_as(settings: Settings, *, timeout: float = 60, retries: int = 3) -> tuple[CatalogClient, Any, UserRef]:
    """Like :func:`connect`, plus the signed-in user, so a window can say who it syncs as."""
    client = CatalogClient(settings.base_url, settings.token, timeout=timeout, retries=retries, user_agent=user_agent())
    user = client.me()
    try:
        catalog = client.resolve_catalog(settings.catalog)
    except NotFound as exc:
        raise CatalogError(
            404, None, "GET", exc.url, f"No catalog named {settings.catalog!r} is visible to this API key"
        ) from None
    return client, catalog, user


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


# --- thread lifetime -----------------------------------------------------------------
#
# A QThread destroyed while it runs aborts the whole process, and the user may close a
# window at any moment. So no background thread here is a child of a window: each one
# is held in this set from start() until it has finished, and windows only disconnect
# from it (and ask it to stop) when they close.
_running: set[QThread] = set()


def start_thread(thread: QThread) -> None:
    """Start ``thread`` and keep it referenced until it finishes, whatever happens to its window."""
    if thread not in _running:
        _running.add(thread)
        thread.finished.connect(lambda: _running.discard(thread))
    QThread.start(thread)  # not thread.start(): Task.start() comes back here


def wait_for_threads(timeout_ms: int = 30_000) -> bool:
    """Block until every background thread has finished, at most ``timeout_ms`` in all. True if all did."""
    deadline = time.monotonic() + timeout_ms / 1000
    for thread in list(_running):
        thread.wait(max(0, int((deadline - time.monotonic()) * 1000)))
    return not any(thread.isRunning() for thread in list(_running))


def disconnect_all(*signals: Any) -> None:
    """Drop every connection of these signals; one with none connected is fine."""
    for signal in signals:
        with contextlib.suppress(TypeError, RuntimeError):
            signal.disconnect()


class Task(QThread):
    """Run one blocking call off the GUI thread and report back with a signal.

    Failures are reported as text, never as the exception: a traceback keeps every
    frame's locals alive, and on a sign-in one of those is the password.
    """

    succeeded = pyqtSignal(object)
    failed = pyqtSignal(str)

    def __init__(self, fn: Callable[[], Any], describe: Callable[[BaseException], str] = describe_error) -> None:
        super().__init__()
        self._fn: Callable[[], Any] | None = fn
        self._describe = describe

    def start(self) -> None:  # type: ignore[override]
        start_thread(self)

    def run(self) -> None:
        fn, self._fn = self._fn, None  # the closure may hold a password: drop it after one call
        message = None
        try:
            result = fn() if fn is not None else None
        except Exception as exc:
            message = self._describe(exc)
            log.warning(f"EvilFlowers background task failed: {message}")
            exc.__traceback__ = None
        finally:
            del fn
        if message is None:
            self.succeeded.emit(result)
        else:
            self.failed.emit(message)

    def disconnect_all(self) -> None:
        """Stop reporting to whoever started it: its window has closed."""
        disconnect_all(self.succeeded, self.failed)


def run_task(
    fn: Callable[[], Any],
    *,
    on_success: Callable[[Any], Any] | None = None,
    on_failure: Callable[[str], Any] | None = None,
    describe: Callable[[BaseException], str] = describe_error,
) -> Task:
    """Start a :class:`Task`. Prefer bound methods of a QObject: Qt drops them with the object."""
    task = Task(fn, describe)
    if on_success is not None:
        task.succeeded.connect(on_success)
    if on_failure is not None:
        task.failed.connect(on_failure)
    task.start()
    return task


class SyncWorker(QThread):
    progress = pyqtSignal(float, str)
    connected = pyqtSignal(object, object)  # CatalogRef, UserRef: before the scan or sync starts
    scanned = pyqtSignal(object, object)  # list[PlanItem], CatalogRef
    applied = pyqtSignal(object)  # Report
    failed = pyqtSignal(str)
    stopped = pyqtSignal()  # the user stopped it; not an error

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
            client, catalog, user = connect_as(self._settings)
            self.connected.emit(catalog, user)
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
            self.stopped.emit()
        except Exception as exc:
            log.exception("EvilFlowers sync failed")
            self.failed.emit(describe_error(exc))


class CoverLoader(QThread):
    """Loads small covers off the GUI thread, one at a time, in the order they were asked for.

    A key is ``("local", book_id)`` for a Calibre cover (``Cache.cover`` is a locked
    read, safe from any thread) or ``("remote", entry_id)`` for a catalog thumbnail,
    downloaded into ``directory``. Each key answers once through :attr:`loaded`, with a
    null QImage when there is no cover. QImage, unlike QPixmap, may cross threads.
    """

    loaded = pyqtSignal(object, object)  # key, QImage

    def __init__(self, db: Any, settings: Settings, directory: Path, size: QSize, parent: Any = None) -> None:
        super().__init__(parent)
        self._db = db
        self._settings = settings
        self._directory = directory
        self._size = size
        self._queue: queue.Queue[tuple[str, Any] | None] = queue.Queue()
        self._stop = threading.Event()

    def request(self, key: tuple[str, Any]) -> None:
        self._queue.put(key)
        if not self.isRunning() and not self._stop.is_set():
            start_thread(self)

    def stop(self) -> None:
        self._stop.set()
        self._queue.put(None)

    def run(self) -> None:
        client: CatalogClient | None = None
        while not self._stop.is_set():
            key = self._queue.get()
            if key is None or self._stop.is_set():
                break
            kind, ident = key
            try:
                if kind == "local":
                    data = self._db.cover(ident)
                else:
                    if client is None:  # short timeout: a slow thumbnail must not hold up closing
                        settings = self._settings
                        client = CatalogClient(
                            settings.base_url, settings.token, timeout=10, retries=0, user_agent=user_agent()
                        )
                    path = client.download_thumbnail(UUID(str(ident)), self._directory / f"{ident}.img")
                    data = path.read_bytes() if path is not None else None
            except Exception as exc:  # a missing cover is never worth more than a log line
                log.debug(f"cover {key}: {exc}")
                data = None
            self.loaded.emit(key, self._scaled(data))

    def _scaled(self, data: bytes | None) -> QImage:
        image = QImage()
        if data and image.loadFromData(data):
            return image.scaled(
                self._size, Qt.AspectRatioMode.KeepAspectRatio, Qt.TransformationMode.SmoothTransformation
            )
        return QImage()


def client_for(settings: Settings, *, timeout: float = 30) -> CatalogClient:
    """A client for quick lookups. It makes no request until it is used."""
    return CatalogClient(settings.base_url, settings.token, timeout=timeout, retries=1, user_agent=user_agent())
