"""Decide what to do with every book and every entry. Pure: no I/O, no Calibre imports.

Each side is compared with its own baseline (what it looked like right after the
last sync), never with the other side's clock. So a slow local clock or a server
that bumps ``updated_at`` for its own reasons cannot make the wrong side win.

=================  ==========================================  ============
State              Meaning                                     Default
=================  ==========================================  ============
IN_SYNC            neither side changed                        skip
LOCAL_ONLY         book never synced, no entry with its UUID   push
REMOTE_ONLY        entry this library has never seen           pull
LOCAL_CHANGED      book changed, entry did not                 push
REMOTE_CHANGED     entry changed, book did not                 pull
CONFLICT           both changed since the last sync            skip (choose)
UNLINKED           book and entry share a UUID, never synced   skip (choose)
LOCAL_DELETED      entry was synced here, the book is gone     skip (pull restores)
REMOTE_DELETED     book was synced, the entry is gone          skip (push re-creates)
=================  ==========================================  ============

Nothing is ever deleted. ``direction`` turns pushes or pulls into skips.

``shelf`` (the "only books on my shelf" setting) leaves out entries that would be
REMOTE_ONLY and are not on the user's shelf. They get no row at all rather than a
skipped one: a large catalog would otherwise bury the library under rows nobody
asked for. Linked books, entries that share a UUID with a local book and entries
this library synced before (LOCAL_DELETED) are never affected, so taking a book
off the shelf never stops it syncing.
"""

from __future__ import annotations

from collections.abc import Iterable, Mapping
from dataclasses import dataclass, replace
from enum import Enum

from ..remote.models import EntryRef
from ..settings import Direction
from .local import LocalBook
from .state import Baseline


class State(Enum):
    IN_SYNC = "in sync"
    LOCAL_ONLY = "new in Calibre"
    REMOTE_ONLY = "new in catalog"
    LOCAL_CHANGED = "changed in Calibre"
    REMOTE_CHANGED = "changed in catalog"
    CONFLICT = "changed on both sides"
    UNLINKED = "same book on both sides, never synced"
    LOCAL_DELETED = "deleted in Calibre"
    REMOTE_DELETED = "deleted in catalog"


class Action(Enum):
    SKIP = "skip"
    PUSH = "upload"
    PULL = "download"


# Which actions make sense in each state, default first.
CHOICES: dict[State, tuple[Action, ...]] = {
    State.IN_SYNC: (Action.SKIP, Action.PUSH, Action.PULL),
    State.LOCAL_ONLY: (Action.PUSH, Action.SKIP),
    State.REMOTE_ONLY: (Action.PULL, Action.SKIP),
    State.LOCAL_CHANGED: (Action.PUSH, Action.SKIP, Action.PULL),
    State.REMOTE_CHANGED: (Action.PULL, Action.SKIP, Action.PUSH),
    State.CONFLICT: (Action.SKIP, Action.PUSH, Action.PULL),
    State.UNLINKED: (Action.SKIP, Action.PUSH, Action.PULL),
    State.LOCAL_DELETED: (Action.SKIP, Action.PULL),
    State.REMOTE_DELETED: (Action.SKIP, Action.PUSH),
}


@dataclass(frozen=True, slots=True)
class PlanItem:
    state: State
    action: Action
    book: LocalBook | None = None
    entry: EntryRef | None = None
    baseline: Baseline | None = None
    note: str = ""

    @property
    def title(self) -> str:
        if self.book is not None:
            return self.book.title
        return self.entry.title if self.entry is not None else ""

    @property
    def authors(self) -> tuple[str, ...]:
        if self.book is not None:
            return self.book.authors
        return tuple(person.full_name for person in self.entry.authors) if self.entry is not None else ()

    @property
    def entry_id(self) -> str | None:
        if self.entry is not None:
            return str(self.entry.id)
        if self.baseline is not None:
            return self.baseline.entry_id
        return None

    @property
    def choices(self) -> tuple[Action, ...]:
        return CHOICES[self.state]

    def with_action(self, action: Action) -> PlanItem:
        if action not in self.choices:
            raise ValueError(f"{action.value} is not possible for a book that is {self.state.value}")
        return replace(self, action=action)


def _local_changed(book: LocalBook, baseline: Baseline) -> bool:
    if book.last_modified != baseline.local_modified:
        return True
    # A format added or removed always bumps last_modified; this also catches a
    # baseline written before an upload finished (the missing format is retried).
    return set(book.syncable_formats) != set(baseline.files)


def _remote_changed(entry: EntryRef, baseline: Baseline) -> bool:
    if entry.updated_at != baseline.remote_updated:
        return True
    return {str(acq.id) for acq in entry.acquisitions} != baseline.acquisition_ids


def _default(state: State, direction: Direction) -> Action:
    action = CHOICES[state][0]
    if (action is Action.PUSH and not direction.pushes) or (action is Action.PULL and not direction.pulls):
        return Action.SKIP
    return action


def _item(state: State, direction: Direction, **kwargs: object) -> PlanItem:
    return PlanItem(state=state, action=_default(state, direction), **kwargs)  # type: ignore[arg-type]


def build_plan(
    books: Mapping[int, LocalBook],
    entries: Iterable[EntryRef],
    baselines: Mapping[int, Baseline],
    known_entries: frozenset[str],
    direction: Direction = Direction.BOTH,
    shelf: frozenset[str] | None = None,
) -> list[PlanItem]:
    """One item per book and per entry, matched by baseline link first, then by UUID.

    ``shelf`` is the entry ids on the user's shelf, or None to offer every entry.
    """
    remote = {str(entry.id): entry for entry in entries}
    book_by_uuid = {book.uuid: book for book in books.values()}
    linked_by_baseline = {baseline.entry_id for book_id, baseline in baselines.items() if book_id in books}
    linked_entry_ids: set[str] = set()
    items: list[PlanItem] = []

    for book in books.values():
        baseline = baselines.get(book.book_id)
        if baseline is not None:
            linked_entry_ids.add(baseline.entry_id)
            entry = remote.get(baseline.entry_id)
            if entry is None:
                items.append(_item(State.REMOTE_DELETED, direction, book=book, baseline=baseline))
                continue
            local, upstream = _local_changed(book, baseline), _remote_changed(entry, baseline)
            if local and upstream:
                state = State.CONFLICT
            elif local:
                state = State.LOCAL_CHANGED
            elif upstream:
                state = State.REMOTE_CHANGED
            else:
                state = State.IN_SYNC
            items.append(_item(state, direction, book=book, entry=entry, baseline=baseline))
            continue

        entry = remote.get(book.uuid) if book.uuid not in linked_by_baseline else None
        if entry is not None:
            linked_entry_ids.add(book.uuid)
            items.append(_item(State.UNLINKED, direction, book=book, entry=entry))
        else:
            note = "" if book.syncable_formats else "no EPUB, PDF or MOBI: only metadata and cover upload"
            items.append(_item(State.LOCAL_ONLY, direction, book=book, note=note))

    for entry_id, entry in remote.items():
        if entry_id in linked_entry_ids or entry_id in book_by_uuid:
            continue
        state = State.LOCAL_DELETED if entry_id in known_entries else State.REMOTE_ONLY
        if state is State.REMOTE_ONLY and shelf is not None and entry_id not in shelf:
            continue  # not on the user's shelf: not offered for download
        items.append(_item(state, direction, entry=entry))

    return items
