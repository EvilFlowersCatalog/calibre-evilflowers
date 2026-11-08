"""End-to-end against a real catalog (``scripts/dev.sh test-dev``).

Uses $EVILFLOWERS_DEV_URL (default https://dev.evilflowers.elvira.stuba.sk),
$EVILFLOWERS_DEV_CATALOG (default calibre-sync-test, created when missing) and
$EVILFLOWERS_TOKEN. Writes only to that catalog. Every run uses unique titles,
so runs never collide with each other; entries are left behind (the plugin never
deletes), so point this at a throwaway catalog.
"""

from __future__ import annotations

import logging
import os
import shutil
import tempfile
import time
import unittest
from datetime import UTC, datetime
from pathlib import Path
from typing import Any
from uuid import UUID

from calibre.ebooks.metadata.book.base import Metadata
from calibre.library import db as open_library
from calibre.utils.img import create_canvas, image_to_data
from epub import epub

from calibre_plugin.remote.client import CatalogClient
from calibre_plugin.remote.models import CatalogRef
from calibre_plugin.settings import Settings
from calibre_plugin.sync.engine import SyncEngine
from calibre_plugin.sync.plan import State
from calibre_plugin.vendor.evilflowers_protocol.errors import NotFound

URL = os.environ.get("EVILFLOWERS_DEV_URL", "https://dev.evilflowers.elvira.stuba.sk")
CATALOG = os.environ.get("EVILFLOWERS_DEV_CATALOG", "calibre-sync-test")
TOKEN = os.environ.get("EVILFLOWERS_TOKEN", "")

if os.environ.get("VERBOSE"):
    logging.basicConfig(level=logging.DEBUG)


def ensure_catalog(client: CatalogClient) -> CatalogRef:
    try:
        return client.resolve_catalog(CATALOG)
    except NotFound:
        response = client.transport.request(
            "POST",
            "catalogs",
            json_body={"url_name": CATALOG, "title": "Calibre sync tests", "is_public": False},
        )
        return response.model(CatalogRef)


@unittest.skipUnless(TOKEN, "no EVILFLOWERS_TOKEN")
class DevCatalogTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls) -> None:
        cls.client = CatalogClient(URL, TOKEN, user_agent="evilflowers-calibre-tests")
        cls.user = cls.client.me()  # the first call the plugin's dialog and "Test connection" make
        cls.catalog = ensure_catalog(cls.client)

    def setUp(self) -> None:
        self.tmp = Path(tempfile.mkdtemp(prefix="evilflowers-dev-"))
        self.addCleanup(shutil.rmtree, self.tmp, True)
        self.run_id = time.strftime("%Y%m%d-%H%M%S")

    def library(self, name: str) -> Any:
        db = open_library(str(self.tmp / name)).new_api
        self.addCleanup(db.close)
        return db

    def engine(self, db: Any) -> SyncEngine:
        return SyncEngine(db, self.client, self.catalog, Settings(URL, TOKEN, CATALOG))

    def mine(self, items: list[Any]) -> list[Any]:
        """Only this run's books: the catalog also holds every earlier run's entries."""
        return [item for item in items if self.run_id in item.title]

    def test_round_trip(self) -> None:
        a = self.library("a")
        title = f"Calibre sync test {self.run_id}"
        mi = Metadata(title, ["Ján Novák", "Mary Shelley"])
        mi.languages = ["slk"]
        mi.tags = ["calibre-sync-test"]
        mi.comments = "<p>Created by the calibre-evilflowers test suite.</p>"
        mi.identifiers = {"isbn": f"978{int(time.time()) % 10_000_000_000:010d}"}
        mi.publisher = "T. Egerton"
        mi.pubdate = datetime(1813, 1, 28, tzinfo=UTC)
        source = self.tmp / "book.epub"
        source.write_bytes(epub(title))
        [book_id], _ = a.add_books([(mi, {"EPUB": str(source)})], run_hooks=False)
        a.set_cover({book_id: image_to_data(create_canvas(300, 450, "#336699"), fmt="JPEG")})
        uid = a.field_for("uuid", book_id)

        engine = self.engine(a)
        [item] = [i for i in engine.scan() if i.book is not None]
        self.assertEqual(item.state, State.LOCAL_ONLY)
        report = engine.apply([item])
        self.assertEqual([o.message for o in report.failed], [])

        remote = self.client.get_entry(self.catalog.id, UUID(uid))
        self.assertEqual(str(remote.id), uid)
        self.assertEqual([acq.calibre_format for acq in remote.acquisitions], ["EPUB"])
        [again] = [i for i in self.engine(a).scan() if i.book is not None]
        self.assertEqual(again.state, State.IN_SYNC, "a fresh push must scan as in sync")

        b = self.library("b")
        engine_b = self.engine(b)
        pulls = self.mine(engine_b.scan())
        self.assertEqual([(i.title, i.state) for i in pulls], [(title, State.REMOTE_ONLY)])
        report = engine_b.apply(pulls)
        self.assertEqual([o.message for o in report.failed], [])
        pulled = b.lookup_by_uuid(uid)
        self.assertIsNotNone(pulled)
        self.assertEqual(b.format(pulled, "EPUB"), epub(title))
        self.assertEqual(b.field_for("authors", pulled), ("Ján Novák", "Mary Shelley"))
        self.assertEqual(b.field_for("languages", pulled), ("slk",))
        self.assertTrue(b.cover(pulled), "the cover should come down with the book")
        self.assertEqual(b.field_for("publisher", pulled), "T. Egerton")
        self.assertEqual(b.field_for("pubdate", pulled).date().isoformat(), "1813-01-28")
        self.assertEqual(b.field_for("identifiers", pulled), mi.identifiers)
        self.assertEqual(self.mine([i for i in self.engine(b).scan()])[0].state, State.IN_SYNC)

        # A local edit travels back.
        a.set_field("comments", {book_id: "<p>edited in library a</p>"})
        engine = self.engine(a)
        [changed] = [i for i in engine.scan() if i.book is not None]
        self.assertEqual(changed.state, State.LOCAL_CHANGED)
        self.assertEqual(engine.apply([changed]).failed, [])
        [seen] = self.mine(self.engine(b).scan())
        self.assertEqual(seen.state, State.REMOTE_CHANGED)
