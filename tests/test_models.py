from __future__ import annotations

import unittest
from uuid import UUID, uuid4

from calibre_plugin.remote.client import api_root
from calibre_plugin.remote.models import CategoryForm, EntryForm, EntryRef, Person
from calibre_plugin.sync.state import Baseline, FileBaseline
from calibre_plugin.vendor.evilflowers_protocol.errors import CatalogError
from calibre_plugin.vendor.evilflowers_protocol.transport import Response


class ModelTests(unittest.TestCase):
    def test_entry_ref_tolerates_nulls_and_unknown_keys(self) -> None:
        entry = EntryRef.from_dict(
            {
                "id": "6c5d2c3e-9b51-4b5a-9a3e-0c0a3b3f1e11",
                "title": "T",
                "updated_at": "2026-01-01T00:00:00.123456Z",
                "summary": None,
                "language": None,
                "identifiers": {"isbn": "123", "doi": None},
                "acquisitions": None,
                "entry_authors": [{"id": str(uuid4()), "name": "Ján", "surname": "Novák"}],
                "brand_new_field": 1,
            }
        )
        self.assertEqual(entry.identifiers, {"isbn": "123"})
        self.assertEqual(entry.acquisitions, ())
        self.assertIsNone(entry.language_code)
        self.assertEqual(entry.authors[0].full_name, "Ján Novák")

    def test_bad_response_becomes_catalog_error(self) -> None:
        response = Response("GET", "http://x/api/v1/e", 200, {"response": {"title": "no id"}})
        with self.assertRaises(CatalogError) as caught:
            response.model(EntryRef)
        self.assertIn("EntryRef", str(caught.exception))

    def test_entry_form_drops_unset_fields(self) -> None:
        form = EntryForm(
            title="T",
            language_code="en",
            id=UUID("6c5d2c3e-9b51-4b5a-9a3e-0c0a3b3f1e11"),
            authors=(Person("A", "B"),),
            categories=(CategoryForm(term="x"),),
        )
        self.assertEqual(
            form.to_dict(),
            {
                "id": "6c5d2c3e-9b51-4b5a-9a3e-0c0a3b3f1e11",
                "title": "T",
                "language_code": "en",
                "authors": [{"name": "A", "surname": "B"}],
                "categories": [{"term": "x"}],
            },
        )

    def test_api_root(self) -> None:
        self.assertEqual(api_root("https://h.example"), "https://h.example/api/v1")
        self.assertEqual(api_root(" https://h.example/api/v1/ "), "https://h.example/api/v1")

    def test_baseline_round_trip_and_unknown_version(self) -> None:
        baseline = Baseline("c", "e", "l", "r", {"EPUB": FileBaseline("h", "a")}, ("a", "b"))
        self.assertEqual(Baseline.from_json(baseline.to_json()), baseline)
        self.assertIsNone(Baseline.from_json({"v": 99}))
        self.assertIsNone(Baseline.from_json("garbage"))


if __name__ == "__main__":
    unittest.main()
