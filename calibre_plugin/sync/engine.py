"""Scan a library and a catalog, then carry out a plan. No Qt: runs in a Calibre job thread or a test.

Rules the engine keeps:

* **Nothing is deleted** except a file this library uploaded itself and has since
  replaced or removed locally. Books and entries are never deleted.
* **A baseline is written only after a book's sync finished**, from what the
  server and the library report *afterwards*. A failed or aborted item keeps its
  old baseline and shows up again on the next scan.
* **Each item is independent.** One failure is reported and the run continues.
"""

from __future__ import annotations

import contextlib
import logging
import re
import tempfile
import threading
from collections.abc import Callable, Iterable
from dataclasses import dataclass, field, replace
from pathlib import Path
from typing import Any
from uuid import UUID

from ..remote.client import CatalogClient
from ..remote.models import MIME_BY_FORMAT, CatalogRef, EntryRef
from ..settings import Settings
from ..vendor.evilflowers_protocol.errors import CatalogError, Conflict, NotFound
from .local import last_modified, snapshot
from .mapping import entry_form, metadata_from_entry
from .plan import Action, PlanItem, build_plan
from .state import Baseline, FileBaseline, StateStore

Progress = Callable[[float, str], None]

_UNSAFE = re.compile(r"[^\w\-. ]+", re.UNICODE)


@dataclass(frozen=True, slots=True)
class Outcome:
    item: PlanItem
    ok: bool
    message: str = ""
    book_id: int | None = None
    entry_id: str | None = None
    created: bool = False


@dataclass(slots=True)
class Report:
    outcomes: list[Outcome] = field(default_factory=list)
    aborted: bool = False

    @property
    def failed(self) -> list[Outcome]:
        return [outcome for outcome in self.outcomes if not outcome.ok]

    def count(self, action: Action) -> int:
        return sum(1 for outcome in self.outcomes if outcome.ok and outcome.item.action is action)

    @property
    def touched_book_ids(self) -> set[int]:
        """Books a pull created or changed; the GUI refreshes these."""
        return {
            outcome.book_id
            for outcome in self.outcomes
            if outcome.ok and outcome.item.action is Action.PULL and outcome.book_id is not None
        }

    @property
    def created_book_ids(self) -> set[int]:
        return {outcome.book_id for outcome in self.outcomes if outcome.created and outcome.book_id is not None}

    def summary(self) -> str:
        parts = [f"{self.count(Action.PUSH)} uploaded", f"{self.count(Action.PULL)} downloaded"]
        if self.failed:
            parts.append(f"{len(self.failed)} failed")
        if self.aborted:
            parts.append("stopped early")
        return ", ".join(parts)


class SyncEngine:
    def __init__(
        self,
        db: Any,
        client: CatalogClient,
        catalog: CatalogRef,
        settings: Settings,
        *,
        log: logging.Logger | Any | None = None,
        abort: threading.Event | None = None,
        progress: Progress | None = None,
    ) -> None:
        self._db = db
        self._client = client
        self._catalog = catalog
        self._settings = settings
        self._log = log or logging.getLogger("evilflowers")
        self._abort = abort or threading.Event()
        self._progress = progress or (lambda _fraction, _message: None)
        self._store = StateStore(db, str(catalog.id))

    # ------------------------------------------------------------------- scan

    def scan(self) -> list[PlanItem]:
        self._progress(0.0, f"Reading catalog {self._catalog.title}")
        entries: list[EntryRef] = []
        for entry in self._client.iter_entries(self._catalog.id):
            if self._abort.is_set():
                raise InterruptedError("scan aborted")
            entries.append(entry)
        self._progress(0.4, "Reading library")
        books = snapshot(self._db)
        baselines = self._store.baselines()
        # Backfill: every linked book's entry counts as known, so deleting the book later is detected.
        self._store.remember(baseline.entry_id for baseline in baselines.values())
        baselines = self._absorb_touches(entries, baselines)
        shelf = self._shelf(entries) if self._settings.shelf_only else None
        plan = build_plan(books, entries, baselines, self._store.known_entries(), self._settings.direction, shelf)
        message = f"{len(books)} books, {len(entries)} entries"
        if shelf is not None:
            planned = {item.entry_id for item in plan}
            hidden = sum(1 for entry in entries if str(entry.id) not in planned)
            if hidden:
                message += f", {hidden} not on your shelf"
        self._progress(1.0, message)
        return plan

    def _shelf(self, entries: list[EntryRef]) -> frozenset[str]:
        """Entry ids on the user's shelf.

        The entry list marks shelved entries itself (``shelf_record_id``), for every
        shelf record of the user, so that costs no request. ``GET /shelf-records``
        is only asked when no entry is marked: a server that does not send the field
        must not make shelf-only sync hide everything. It hides records of public
        catalogs the user is not a member of, so it is the fallback, not the source.
        """
        shelved = {str(entry.id) for entry in entries if entry.shelf_record_id is not None}
        if not shelved:
            shelved = {str(record.entry_id) for record in self._client.list_shelf(self._catalog.id)}
        return frozenset(shelved)

    def _absorb_touches(self, entries: list[EntryRef], baselines: dict[int, Baseline]) -> dict[int, Baseline]:
        """Tell real remote edits from ``updated_at`` bumps that changed nothing synced.

        Any file download bumps an entry's ``updated_at``. For each linked entry whose
        ``updated_at`` moved, fetch its detail; when the fingerprint still matches the
        baseline, record the new ``updated_at`` so the entry stays in sync.
        """
        by_id = {str(entry.id): entry for entry in entries}
        moved = [
            (book_id, baseline)
            for book_id, baseline in baselines.items()
            if baseline.remote_fingerprint
            and baseline.entry_id in by_id
            and by_id[baseline.entry_id].updated_at != baseline.remote_updated
        ]
        result = dict(baselines)
        for index, (book_id, baseline) in enumerate(moved):
            if self._abort.is_set():
                raise InterruptedError("scan aborted")
            self._progress(0.5 + 0.5 * index / len(moved), f"Checking {by_id[baseline.entry_id].title}")
            detail = self._client.get_entry(self._catalog.id, UUID(baseline.entry_id))
            if detail.fingerprint() == baseline.remote_fingerprint:
                refreshed = replace(baseline, remote_updated=detail.updated_at)
                self._store.save(book_id, refreshed)
                result[book_id] = refreshed
                by_id[baseline.entry_id] = replace(by_id[baseline.entry_id], updated_at=detail.updated_at)
        entries[:] = [by_id.get(str(entry.id), entry) for entry in entries]
        return result

    # ------------------------------------------------------------------ apply

    def apply(self, items: Iterable[PlanItem]) -> Report:
        todo = [item for item in items if item.action is not Action.SKIP]
        report = Report()
        for index, item in enumerate(todo):
            if self._abort.is_set():
                report.aborted = True
                break
            verb = "Uploading" if item.action is Action.PUSH else "Downloading"
            self._progress(index / max(len(todo), 1), f"{verb} {item.title}")
            try:
                outcome = self._push(item) if item.action is Action.PUSH else self._pull(item)
            except CatalogError as exc:
                self._log.error(f"{item.title}: {exc}")
                outcome = Outcome(item, ok=False, message=_explain(exc))
            except Exception as exc:  # one broken book must not stop the run
                self._log.exception(f"{item.title}: unexpected error")
                outcome = Outcome(item, ok=False, message=f"{type(exc).__name__}: {exc}")
            report.outcomes.append(outcome)
        self._store.remember(outcome.entry_id for outcome in report.outcomes if outcome.ok and outcome.entry_id)
        self._progress(1.0, report.summary())
        return report

    # ------------------------------------------------------------------- push

    def _push(self, item: PlanItem) -> Outcome:
        book = item.book
        if book is None:
            raise ValueError("nothing to upload: the book is not in this library")
        catalog_id = self._catalog.id
        modified_before = last_modified(self._db, book.book_id)

        entry_id: UUID
        if item.entry is not None:
            entry_id = item.entry.id
            form = entry_form(self._db, book.book_id, create=False, default_language=self._settings.default_language)
            self._client.update_entry(catalog_id, entry_id, form)
            remote_acquisitions = {str(acq.id) for acq in item.entry.acquisitions}
        else:
            form = entry_form(self._db, book.book_id, create=True, default_language=self._settings.default_language)
            try:
                entry_id = self._client.create_entry(catalog_id, form).id
            except Conflict as conflict:
                # An earlier run may have created the entry and failed before writing the
                # baseline. Anything else (same title, ISBN or DOI) is a real conflict.
                try:
                    existing = self._client.get_entry(catalog_id, UUID(book.uuid))
                except NotFound:
                    raise conflict from None
                entry_id = existing.id
                update = entry_form(
                    self._db, book.book_id, create=False, default_language=self._settings.default_language
                )
                self._client.update_entry(catalog_id, entry_id, update)
            remote_acquisitions = set()

        previous = item.baseline.files if item.baseline is not None else {}
        files: dict[str, FileBaseline] = {}
        with tempfile.TemporaryDirectory(prefix="evilflowers-") as tmp:
            for fmt in book.syncable_formats:
                sha256 = self._db.format_hash(book.book_id, fmt)
                old = previous.get(fmt)
                if old is not None and old.sha256 == sha256 and old.acquisition_id in remote_acquisitions:
                    files[fmt] = old
                    continue
                path = Path(tmp) / f"{_filename(book.title)}.{fmt.lower()}"
                self._db.copy_format_to(book.book_id, fmt, str(path))
                uploaded = self._client.upload_acquisition(
                    catalog_id, entry_id, path, mime=MIME_BY_FORMAT[fmt], relation=self._settings.relation
                )
                files[fmt] = FileBaseline(sha256=sha256, acquisition_id=str(uploaded.id))
                if old is not None and old.acquisition_id in remote_acquisitions:
                    self._delete_acquisition(old.acquisition_id)
        for fmt, old in previous.items():
            if fmt not in files and old.acquisition_id in remote_acquisitions:
                self._delete_acquisition(old.acquisition_id)  # the format was removed in Calibre

        final = self._client.get_entry(catalog_id, entry_id)
        self._store.save(book.book_id, self._baseline(final, modified_before, files))
        return Outcome(item, ok=True, book_id=book.book_id, entry_id=str(entry_id))

    def _delete_acquisition(self, acquisition_id: str) -> None:
        with contextlib.suppress(NotFound):  # already gone is fine
            self._client.delete_acquisition(UUID(acquisition_id))

    # ------------------------------------------------------------------- pull

    def _pull(self, item: PlanItem) -> Outcome:
        if item.entry is None:
            raise ValueError("nothing to download: the entry is not in the catalog")
        entry = self._client.get_entry(self._catalog.id, item.entry.id)
        book_id = item.book.book_id if item.book is not None else None
        previous = item.baseline.files if item.baseline is not None else {}
        local_formats = set(item.book.formats) if item.book is not None else set()

        # One file per Calibre format; the first acquisition of each type wins.
        wanted: dict[str, str] = {}
        for acquisition in entry.acquisitions:
            fmt = acquisition.calibre_format
            if fmt is not None and fmt not in wanted:
                wanted[fmt] = str(acquisition.id)

        with tempfile.TemporaryDirectory(prefix="evilflowers-") as tmp:
            downloads: dict[str, str] = {}
            for fmt, acquisition_id in wanted.items():
                old = previous.get(fmt)
                if old is not None and old.acquisition_id == acquisition_id and fmt in local_formats:
                    continue  # same file as last time
                path = Path(tmp) / f"{entry.id}.{fmt.lower()}"
                self._client.download_acquisition(UUID(acquisition_id), path)
                downloads[fmt] = str(path)
            cover = self._download_cover(entry, Path(tmp))

            created = book_id is None
            if book_id is None:
                mi = metadata_from_entry(entry)
                if cover:
                    mi.cover_data = ("jpeg", cover)
                ids, _duplicates = self._db.add_books(
                    [(mi, downloads)], add_duplicates=True, preserve_uuid=True, run_hooks=False
                )
                book_id = ids[0]
            else:
                existing = self._db.field_for("identifiers", book_id) or {}
                # An entry without authors must not replace real local ones with "Unknown".
                self._db.set_metadata(book_id, metadata_from_entry(entry, existing), set_authors=bool(entry.authors))
                for fmt, path in downloads.items():
                    self._db.add_format(book_id, fmt, path, replace=True, run_hooks=False)
                if cover:
                    self._db.set_cover({book_id: cover})

        files = {
            fmt: FileBaseline(sha256=self._db.format_hash(book_id, fmt), acquisition_id=acquisition_id)
            for fmt, acquisition_id in wanted.items()
            if fmt in {f.upper() for f in self._db.formats(book_id)}
        }
        self._store.save(book_id, self._baseline(entry, last_modified(self._db, book_id), files))
        return Outcome(item, ok=True, book_id=book_id, entry_id=str(entry.id), created=created)

    def _download_cover(self, entry: EntryRef, tmp: Path) -> bytes | None:
        """The entry's cover, or None. A cover that fails to download never fails the book."""
        if not entry.has_cover:
            return None
        try:
            path = self._client.download_cover(entry.id, tmp / "cover")
        except CatalogError as exc:
            self._log.warning(f"{entry.title}: cover not downloaded: {exc}")
            return None
        return path.read_bytes() if path is not None else None

    # ---------------------------------------------------------------- helpers

    def _baseline(self, entry: EntryRef, local_modified: str, files: dict[str, FileBaseline]) -> Baseline:
        return Baseline(
            catalog_id=str(self._catalog.id),
            entry_id=str(entry.id),
            local_modified=local_modified,
            remote_updated=entry.updated_at,
            files=files,
            acquisitions=tuple(sorted(str(acq.id) for acq in entry.acquisitions)),
            remote_fingerprint=entry.fingerprint(),
        )


def _filename(title: str) -> str:
    return (_UNSAFE.sub("_", title).strip(" ._") or "book")[:80]


def _explain(exc: CatalogError) -> str:
    if isinstance(exc, Conflict):
        return f"the catalog already has an entry with this title, ISBN or DOI ({exc.message})"
    return exc.message
