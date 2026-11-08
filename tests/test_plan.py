from __future__ import annotations

import unittest
from uuid import UUID, uuid4

from calibre_plugin.remote.models import AcquisitionRef, EntryRef
from calibre_plugin.settings import Direction
from calibre_plugin.sync.local import LocalBook
from calibre_plugin.sync.plan import Action, State, build_plan
from calibre_plugin.sync.state import Baseline, FileBaseline


def book(book_id: int, uid: str | None = None, modified: str = "L1", formats: tuple[str, ...] = ("EPUB",)) -> LocalBook:
    return LocalBook(book_id, uid or str(uuid4()), f"Book {book_id}", ("A",), modified, formats)


def entry(uid: str | None = None, updated: str = "R1", acquisitions: tuple[str, ...] = ()) -> EntryRef:
    acqs = tuple(AcquisitionRef(id=UUID(a), relation="acquisition", mime="application/epub+zip") for a in acquisitions)
    return EntryRef(id=UUID(uid) if uid else uuid4(), title="E", updated_at=updated, acquisitions=acqs)


def baseline(entry_id: str, modified: str = "L1", updated: str = "R1", acq: str | None = None) -> Baseline:
    acq = acq or str(uuid4())
    return Baseline("cat", entry_id, modified, updated, {"EPUB": FileBaseline("h", acq)}, (acq,))


class PlanTests(unittest.TestCase):
    def linked(self, *, local: str = "L1", remote: str = "R1") -> tuple[State, Action]:
        uid, acq = str(uuid4()), str(uuid4())
        b = book(1, uid, modified=local)
        e = entry(uid, updated=remote, acquisitions=(acq,))
        items = build_plan({1: b}, [e], {1: baseline(uid, acq=acq)}, frozenset({uid}))
        self.assertEqual(len(items), 1)
        return items[0].state, items[0].action

    def test_in_sync(self) -> None:
        self.assertEqual(self.linked(), (State.IN_SYNC, Action.SKIP))

    def test_local_changed_pushes(self) -> None:
        self.assertEqual(self.linked(local="L2"), (State.LOCAL_CHANGED, Action.PUSH))

    def test_remote_changed_pulls(self) -> None:
        self.assertEqual(self.linked(remote="R2"), (State.REMOTE_CHANGED, Action.PULL))

    def test_both_changed_is_a_conflict_left_to_the_user(self) -> None:
        self.assertEqual(self.linked(local="L2", remote="R2"), (State.CONFLICT, Action.SKIP))

    def test_new_on_each_side(self) -> None:
        items = build_plan({1: book(1)}, [entry()], {}, frozenset())
        self.assertEqual(
            sorted((i.state.name, i.action.name) for i in items), [("LOCAL_ONLY", "PUSH"), ("REMOTE_ONLY", "PULL")]
        )

    def test_same_uuid_never_synced_is_unlinked(self) -> None:
        uid = str(uuid4())
        [item] = build_plan({1: book(1, uid)}, [entry(uid)], {}, frozenset())
        self.assertEqual((item.state, item.action), (State.UNLINKED, Action.SKIP))
        self.assertEqual(item.with_action(Action.PULL).action, Action.PULL)

    def test_book_deleted_locally_is_not_pulled_back(self) -> None:
        uid = str(uuid4())
        [item] = build_plan({}, [entry(uid)], {}, frozenset({uid}))
        self.assertEqual((item.state, item.action), (State.LOCAL_DELETED, Action.SKIP))

    def test_entry_deleted_remotely_is_not_pushed_back(self) -> None:
        uid = str(uuid4())
        [item] = build_plan({1: book(1, uid)}, [], {1: baseline(uid)}, frozenset({uid}))
        self.assertEqual((item.state, item.action), (State.REMOTE_DELETED, Action.SKIP))
        with self.assertRaises(ValueError):
            item.with_action(Action.PULL)

    def test_new_remote_acquisition_is_a_remote_change(self) -> None:
        uid, acq = str(uuid4()), str(uuid4())
        e = entry(uid, acquisitions=(acq, str(uuid4())))
        [item] = build_plan({1: book(1, uid)}, [e], {1: baseline(uid, acq=acq)}, frozenset({uid}))
        self.assertEqual(item.state, State.REMOTE_CHANGED)

    def test_format_missing_from_baseline_is_a_local_change(self) -> None:
        uid, acq = str(uuid4()), str(uuid4())
        b = book(1, uid, formats=("EPUB", "PDF"))
        [item] = build_plan({1: b}, [entry(uid, acquisitions=(acq,))], {1: baseline(uid, acq=acq)}, frozenset({uid}))
        self.assertEqual(item.state, State.LOCAL_CHANGED)

    def test_direction_turns_defaults_into_skips(self) -> None:
        items = build_plan({1: book(1)}, [entry()], {}, frozenset(), Direction.PUSH)
        actions = {i.state: i.action for i in items}
        self.assertEqual(actions, {State.LOCAL_ONLY: Action.PUSH, State.REMOTE_ONLY: Action.SKIP})

    def test_copy_with_same_uuid_does_not_steal_the_link(self) -> None:
        uid, acq = str(uuid4()), str(uuid4())
        original, copy = book(1, uid), book(2, uid)
        items = build_plan(
            {1: original, 2: copy}, [entry(uid, acquisitions=(acq,))], {1: baseline(uid, acq=acq)}, frozenset({uid})
        )
        states = {i.book.book_id: i.state for i in items if i.book}
        self.assertEqual(states, {1: State.IN_SYNC, 2: State.LOCAL_ONLY})
        self.assertEqual(len(items), 2)

    def test_shelf_only_leaves_out_new_entries_off_the_shelf(self) -> None:
        shelved, other = entry(), entry()
        items = build_plan({}, [shelved, other], {}, frozenset(), shelf=frozenset({str(shelved.id)}))
        self.assertEqual([(i.entry_id, i.state) for i in items], [(str(shelved.id), State.REMOTE_ONLY)])

    def test_shelf_only_keeps_linked_unlinked_and_deleted_books(self) -> None:
        linked_uid, acq, same_uid, deleted_uid = str(uuid4()), str(uuid4()), str(uuid4()), str(uuid4())
        books = {1: book(1, linked_uid, modified="L2"), 2: book(2, same_uid)}
        entries = [entry(linked_uid, acquisitions=(acq,)), entry(same_uid), entry(deleted_uid)]
        items = build_plan(
            books, entries, {1: baseline(linked_uid, acq=acq)}, frozenset({linked_uid, deleted_uid}), shelf=frozenset()
        )
        states = {i.entry_id: i.state for i in items}
        self.assertEqual(
            states,
            {linked_uid: State.LOCAL_CHANGED, same_uid: State.UNLINKED, deleted_uid: State.LOCAL_DELETED},
        )

    def test_no_shelf_offers_every_entry(self) -> None:
        items = build_plan({}, [entry(), entry()], {}, frozenset(), shelf=None)
        self.assertEqual([i.state for i in items], [State.REMOTE_ONLY, State.REMOTE_ONLY])


if __name__ == "__main__":
    unittest.main()
