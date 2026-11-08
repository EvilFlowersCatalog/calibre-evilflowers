"""CatalogClient endpoints and the sign-in helpers against the fake catalog."""

from __future__ import annotations

import shutil
import tempfile
import unittest
from pathlib import Path
from unittest import mock
from uuid import UUID, uuid4

from fake_catalog import PASSWORD, TOKEN, USERNAME, FakeCatalog

from calibre_plugin.remote import auth
from calibre_plugin.remote.client import CatalogClient
from calibre_plugin.vendor.evilflowers_protocol.errors import CatalogError, NotFound, Unauthorized

EPUB = "application/epub+zip"
PDF = "application/pdf"
JPEG = b"\xff\xd8\xff\xe0" + b"\x00" * 64
UA = "evilflowers-calibre-tests"


class _FakeTestCase(unittest.TestCase):
    def setUp(self) -> None:
        self.fake = FakeCatalog().start()
        self.addCleanup(self.fake.stop)
        self.client = CatalogClient(self.fake.base_url, TOKEN, retries=0, user_agent=UA)
        self.catalog_id = UUID(self.fake.catalog_id)


class AuthTests(_FakeTestCase):
    def test_sign_in_returns_a_working_key_and_drops_the_session(self) -> None:
        key = auth.sign_in(self.fake.origin, USERNAME, PASSWORD, key_name="Calibre on desk", user_agent=UA)
        self.assertEqual(key.name, "Calibre on desk")
        self.assertEqual(self.fake.api_keys[str(key.id)]["token"], key.token)
        self.assertEqual(self.fake.login_auth_headers, [None], "login must not send an Authorization header")
        self.assertNotIn(key.token, repr(key))
        me = CatalogClient(self.fake.origin, key.token, retries=0, user_agent=UA).me()
        self.assertEqual(me.username, USERNAME)

    def test_wrong_password_is_unauthorized_without_the_password(self) -> None:
        secret = "hunter2-very-secret"
        with self.assertRaises(Unauthorized) as caught:
            auth.sign_in(self.fake.base_url, USERNAME, secret, key_name="k", user_agent=UA)
        self.assertEqual(caught.exception.message, "Wrong username or password.")
        for text in (str(caught.exception), repr(caught.exception), repr(caught.exception.body)):
            self.assertNotIn(secret, text)
        self.assertEqual(self.fake.api_keys, {})

    def test_server_error_echoing_the_password_is_scrubbed(self) -> None:
        self.fake.login_failure = (500, {"title": "Boom", "detail": f"could not check {PASSWORD}"})
        with self.assertRaises(CatalogError) as caught:
            auth.login(self.fake.base_url, USERNAME, PASSWORD, user_agent=UA)
        self.assertEqual(caught.exception.status, 500)
        self.assertNotIn(PASSWORD, str(caught.exception))
        self.assertNotIn(PASSWORD, repr(caught.exception))
        self.assertIn("***", caught.exception.message)

    def test_key_name_is_truncated(self) -> None:
        access = auth.login(self.fake.base_url, USERNAME, PASSWORD, user_agent=UA)
        key = auth.create_api_key(self.fake.base_url, access, "x" * 150, user_agent=UA)
        self.assertEqual(key.name, "x" * 100)

    def test_create_api_key_with_a_bad_token_hides_it(self) -> None:
        with self.assertRaises(Unauthorized) as caught:
            auth.create_api_key(self.fake.base_url, "not-a-token", "k", user_agent=UA)
        self.assertNotIn("not-a-token", str(caught.exception))

    def test_default_key_name(self) -> None:
        with mock.patch("socket.gethostname", return_value="desk.local"):
            self.assertEqual(auth.default_key_name(), "Calibre on desk.local")
        with mock.patch("socket.gethostname", side_effect=OSError):
            self.assertEqual(auth.default_key_name(), "Calibre on this computer")

    def test_revoke_api_key_and_tolerate_404(self) -> None:
        key = auth.sign_in(self.fake.base_url, USERNAME, PASSWORD, key_name="k", user_agent=UA)
        self.client.revoke_api_key(key.id)
        self.assertEqual(self.fake.api_keys, {})
        self.client.revoke_api_key(key.id)  # already gone: no error
        with self.assertRaises(Unauthorized):
            CatalogClient(self.fake.base_url, key.token, retries=0, user_agent=UA).me()


class IdentityAndCatalogTests(_FakeTestCase):
    def test_me_has_a_display_name(self) -> None:
        me = self.client.me()
        self.assertEqual((me.name, me.surname, me.display_name), ("Ada", "Lovelace", "Ada Lovelace"))
        self.fake.user_name = self.fake.user_surname = ""
        self.assertEqual(self.client.me().display_name, USERNAME)

    def test_list_catalogs_reads_every_page_sorted_by_title(self) -> None:
        self.fake.add_catalog("Zoology", "zoo")
        self.fake.add_catalog("art history", "art")
        titles = [catalog.title for catalog in self.client.list_catalogs()]
        self.assertEqual(titles, ["art history", "Calibre test catalog", "Zoology"])


class SearchTests(_FakeTestCase):
    def setUp(self) -> None:
        super().setUp()
        self.ids = {
            title: self.fake.add_remote_entry(title, {EPUB: b"e"}, **fields)
            for title, fields in {
                "Dune": {"categories": [{"term": "scifi", "labels": {"sk": "Vedecká fantastika"}}]},
                "Dune Messiah": {"categories": [{"term": "scifi", "labels": {"sk": "Vedecká fantastika"}}]},
                "Children of Dune": {"language_code": "sk", "categories": [{"term": "classic", "label": "Classics"}]},
                "Emma": {"categories": [{"term": "classic", "label": "Classics"}]},
                "Persuasion": {"cover": JPEG},
            }.items()
        }

    def test_search_entries_paginates(self) -> None:
        first = self.client.search_entries(self.catalog_id, query="dune", limit=30)
        self.assertEqual((first.page, first.pages, first.total), (1, 2, 3))
        self.assertEqual(len(first.items), self.fake.page_size)
        second = self.client.search_entries(self.catalog_id, query="dune", page=2)
        self.assertEqual(second.page, 2)
        titles = {entry.title for entry in first.items + second.items}
        self.assertEqual(titles, {"Dune", "Dune Messiah", "Children of Dune"})
        with self.assertRaises(NotFound):
            self.client.search_entries(self.catalog_id, query="dune", page=3)

    def test_search_entries_filters_and_orders(self) -> None:
        page = self.client.search_entries(self.catalog_id, query="dune", language_code="sk")
        self.assertEqual([entry.title for entry in page.items], ["Children of Dune"])
        classic = UUID(self.fake.category_id("classic"))
        page = self.client.search_entries(self.catalog_id, category_id=classic, order_by="-title")
        self.assertEqual([entry.title for entry in page.items], ["Emma", "Children of Dune"])

    def test_search_entries_reads_shelf_and_thumbnail(self) -> None:
        record = self.fake.add_shelf_record(self.ids["Persuasion"])
        [entry] = self.client.search_entries(self.catalog_id, query="persuasion").items
        self.assertEqual(entry.shelf_record_id, UUID(record))
        self.assertTrue(entry.thumbnail and entry.thumbnail.endswith(f"/thumbnails/{entry.id}"))
        [other] = self.client.search_entries(self.catalog_id, query="emma").items
        self.assertIsNone(other.shelf_record_id)
        self.assertIsNone(other.thumbnail)  # the server sends the request's URL here; it is not a thumbnail

    def test_shelf_and_thumbnail_stay_out_of_the_fingerprint(self) -> None:
        [entry] = self.client.search_entries(self.catalog_id, query="persuasion").items
        before = entry.fingerprint()
        self.fake.add_shelf_record(self.ids["Persuasion"])
        [shelved] = self.client.search_entries(self.catalog_id, query="persuasion").items
        self.assertEqual(shelved.fingerprint(), before)

    def test_facets_count_and_ignore_their_own_filter(self) -> None:
        facets = self.client.facets(self.catalog_id)
        self.assertEqual(dict(facets.languages), {"en": 4, "sk": 1})
        scifi, classic = UUID(self.fake.category_id("scifi")), UUID(self.fake.category_id("classic"))
        self.assertEqual(dict(facets.categories), {scifi: 2, classic: 2})
        narrowed = self.client.facets(self.catalog_id, query="dune", language_code="sk")
        self.assertEqual(dict(narrowed.languages), {"en": 2, "sk": 1})
        self.assertEqual(dict(narrowed.categories), {classic: 1})

    def test_list_categories_prefers_english_then_any_label(self) -> None:
        labels = self.client.list_categories(self.catalog_id)
        self.assertEqual(
            labels,
            {
                UUID(self.fake.category_id("scifi")): "Vedecká fantastika",
                UUID(self.fake.category_id("classic")): "Classics",
            },
        )


class FileAndShelfTests(_FakeTestCase):
    def setUp(self) -> None:
        super().setUp()
        self.tmp = Path(tempfile.mkdtemp(prefix="evilflowers-client-"))
        self.addCleanup(shutil.rmtree, self.tmp, True)

    def test_download_thumbnail(self) -> None:
        with_cover = UUID(self.fake.add_remote_entry("Covered", {}, cover=JPEG))
        without = UUID(self.fake.add_remote_entry("Bare", {}))
        path = self.client.download_thumbnail(with_cover, self.tmp / "t.jpg")
        self.assertEqual(path.read_bytes(), JPEG)
        self.assertIsNone(self.client.download_thumbnail(without, self.tmp / "none.jpg"))  # server answers 500
        self.assertIsNone(self.client.download_thumbnail(uuid4(), self.tmp / "missing.jpg"))  # 404

    def test_shelf_round_trip(self) -> None:
        entry_id = UUID(self.fake.add_remote_entry("Shelved", {}))
        added = self.client.add_to_shelf(entry_id)
        self.assertEqual(added.entry_id, entry_id)
        self.assertEqual(self.client.add_to_shelf(entry_id).id, added.id, "adding twice keeps one record")
        for i in range(3):
            self.client.add_to_shelf(UUID(self.fake.add_remote_entry(f"More {i}", {})))
        shelf = self.client.list_shelf(self.catalog_id)
        self.assertEqual(len(shelf), 4)  # over two pages
        self.assertIn(added.id, {record.id for record in shelf})
        self.assertEqual(self.client.list_shelf(uuid4()), [])
        self.client.remove_from_shelf(added.id)
        self.client.remove_from_shelf(added.id)  # already gone: no error
        self.assertNotIn(added.id, {record.id for record in self.client.list_shelf(self.catalog_id)})

    def test_add_unknown_entry_to_shelf_fails(self) -> None:
        with self.assertRaises(CatalogError) as caught:
            self.client.add_to_shelf(uuid4())
        self.assertEqual(caught.exception.status, 422)


class HighlightTests(_FakeTestCase):
    def test_list_highlights_walks_the_chain(self) -> None:
        entry_id = self.fake.add_remote_entry("Annotated", {EPUB: b"e", PDF: b"p"})
        other_id = self.fake.add_remote_entry("Elsewhere", {EPUB: b"e"})
        self.fake.add_highlight(entry_id, 12, "late", content="c1")
        self.fake.add_highlight(entry_id, 3, "early")
        self.fake.add_highlight(entry_id, 12, "same page, later", title="Agent notes", kind="agent")
        self.fake.add_highlight(entry_id, 5, "in the pdf", acquisition_index=1)
        self.fake.add_highlight(entry_id, 1, "pending agent", kind="agent", status="pending")
        self.fake.add_highlight(entry_id, 1, None, kind="drawing", content="{}")
        self.fake.add_highlight(entry_id, 1, "someone else", user_id=str(uuid4()))
        self.fake.add_highlight(other_id, 1, "other book")

        entry = self.client.get_entry(self.catalog_id, UUID(entry_id))
        highlights = self.client.list_highlights(entry, self.client.me().id)

        self.assertEqual(
            [(h.page, h.text, h.kind) for h in highlights],
            [(3, "early", "highlight"), (5, "in the pdf", "highlight"), (12, "late", "highlight"),
             (12, "same page, later", "agent")],
        )  # fmt: skip
        self.assertEqual(highlights[2].content, "c1")
        self.assertEqual(highlights[3].annotation_title, "Agent notes")

    def test_entry_without_files_has_no_highlights(self) -> None:
        entry = self.client.get_entry(self.catalog_id, UUID(self.fake.add_remote_entry("Empty", {})))
        self.assertEqual(self.client.list_highlights(entry, self.client.me().id), [])


if __name__ == "__main__":
    unittest.main()
