"""
Tests for build.validate_data — schema validation.

Pin down each error message so future changes to the validator are
intentional. Mutations apply to a known-good fixture; each test
asserts that exactly the expected violation fires.
"""

import sys
import unittest
from pathlib import Path

ROOT = Path(__file__).parent.parent
sys.path.insert(0, str(ROOT / "build"))

from build import validate_data, SchemaError


def good_data():
    """A minimal-but-valid resume.yml structure for use in tests."""
    return {
        "name": {"first": "Gaius", "last": "Caesar"},
        "meta": {"description": "Test resume", "maxPages": 10},
        "sidebar": {
            "blocks": [
                {"id": "details", "type": "details", "heading": "Details",
                 "rows": [{"label": "X", "value": "Y"}]},
                {"id": "key-skills", "type": "list", "heading": "Skills",
                 "items": ["A", "B"]},
            ],
        },
        "mainColumn": [
            {"type": "summary", "heading": "Summary", "text": "Hello"},
            {"type": "experience", "heading": "Work", "jobs": [
                {"id": "job-one", "title": "A", "bullets": ["x", "y"]},
                {"id": "job-two", "title": "B", "gap": True},
            ]},
            {"type": "education", "heading": "Education", "items": []},
        ],
    }


class TestValidateData(unittest.TestCase):
    def test_good_data_passes(self):
        validate_data(good_data())  # should not raise

    # ── Top-level keys ────────────────────────────────────────────
    def test_missing_top_level(self):
        d = good_data()
        del d["meta"]
        with self.assertRaises(SchemaError) as ctx:
            validate_data(d)
        self.assertIn("meta", str(ctx.exception))

    # ── Name ──────────────────────────────────────────────────────
    def test_name_missing_first(self):
        d = good_data()
        del d["name"]["first"]
        with self.assertRaises(SchemaError) as ctx:
            validate_data(d)
        self.assertIn("name.first", str(ctx.exception))

    # ── Role ──────────────────────────────────────────────────────
    def test_role_omitted_passes(self):
        d = good_data()
        d.pop("role", None)
        validate_data(d)  # should not raise

    def test_role_not_string(self):
        d = good_data()
        d["role"] = 123
        with self.assertRaises(SchemaError) as ctx:
            validate_data(d)
        self.assertIn("role", str(ctx.exception))

    # ── Contact ───────────────────────────────────────────────────
    def test_contact_omitted_passes(self):
        d = good_data()
        d.pop("contact", None)
        validate_data(d)  # should not raise

    def test_contact_null_passes(self):
        d = good_data()
        d["contact"] = None  # explicit `contact:` with no value (YAML null)
        validate_data(d)  # should not raise — same as omitted

    def test_contact_not_mapping(self):
        d = good_data()
        d["contact"] = "123 Main St"
        with self.assertRaises(SchemaError) as ctx:
            validate_data(d)
        self.assertIn("contact", str(ctx.exception))

    def test_contact_address_not_string(self):
        d = good_data()
        d["contact"] = {"address": 123, "rows": []}
        with self.assertRaises(SchemaError) as ctx:
            validate_data(d)
        self.assertIn("address", str(ctx.exception))

    def test_contact_rows_missing(self):
        d = good_data()
        d["contact"] = {"address": "Somewhere"}  # no rows
        with self.assertRaises(SchemaError) as ctx:
            validate_data(d)
        self.assertIn("rows", str(ctx.exception))

    def test_contact_rows_not_list(self):
        d = good_data()
        d["contact"] = {"rows": "not a list"}
        with self.assertRaises(SchemaError) as ctx:
            validate_data(d)
        self.assertIn("rows", str(ctx.exception))

    def test_contact_row_not_mapping(self):
        d = good_data()
        d["contact"] = {"rows": ["just a string"]}
        with self.assertRaises(SchemaError) as ctx:
            validate_data(d)
        self.assertIn("contact.rows[0]", str(ctx.exception))

    def test_contact_row_value_missing(self):
        d = good_data()
        d["contact"] = {"rows": [{"href": "tel:+10000000000"}]}
        with self.assertRaises(SchemaError) as ctx:
            validate_data(d)
        self.assertIn("value", str(ctx.exception))

    def test_contact_row_value_empty(self):
        d = good_data()
        d["contact"] = {"rows": [{"value": ""}]}
        with self.assertRaises(SchemaError) as ctx:
            validate_data(d)
        self.assertIn("value", str(ctx.exception))

    def test_contact_row_value_not_string(self):
        d = good_data()
        d["contact"] = {"rows": [{"value": 42}]}
        with self.assertRaises(SchemaError) as ctx:
            validate_data(d)
        self.assertIn("value", str(ctx.exception))

    def test_contact_row_href_not_string(self):
        d = good_data()
        d["contact"] = {"rows": [{"value": "x", "href": 42}]}
        with self.assertRaises(SchemaError) as ctx:
            validate_data(d)
        self.assertIn("href", str(ctx.exception))

    def test_contact_rows_empty_passes(self):
        d = good_data()
        d["contact"] = {"address": "Somewhere", "rows": []}
        validate_data(d)  # should not raise — empty rows is valid

    # ── Meta ──────────────────────────────────────────────────────
    def test_meta_max_pages_missing(self):
        d = good_data()
        del d["meta"]["maxPages"]
        with self.assertRaises(SchemaError) as ctx:
            validate_data(d)
        self.assertIn("maxPages", str(ctx.exception))

    def test_meta_max_pages_zero(self):
        d = good_data()
        d["meta"]["maxPages"] = 0
        with self.assertRaises(SchemaError) as ctx:
            validate_data(d)
        self.assertIn("maxPages", str(ctx.exception))

    def test_meta_max_pages_string(self):
        d = good_data()
        d["meta"]["maxPages"] = "10"
        with self.assertRaises(SchemaError) as ctx:
            validate_data(d)
        self.assertIn("maxPages", str(ctx.exception))

    def test_meta_lang_omitted_passes(self):
        # lang is optional; resolve_lang() falls back to a default.
        d = good_data()
        d["meta"].pop("lang", None)  # ensure absent regardless of fixture
        validate_data(d)  # should not raise

    def test_meta_lang_not_string(self):
        # A non-string would crash resolve_lang() on .strip() with
        # AttributeError; validator must catch it up-front.
        d = good_data()
        d["meta"]["lang"] = ["en-US"]
        with self.assertRaises(SchemaError) as ctx:
            validate_data(d)
        self.assertIn("lang", str(ctx.exception))

    # ── Sidebar ───────────────────────────────────────────────────
    def test_sidebar_must_be_mapping(self):
        d = good_data()
        d["sidebar"] = []  # old shape (list)
        with self.assertRaises(SchemaError) as ctx:
            validate_data(d)
        self.assertIn("sidebar", str(ctx.exception))

    def test_sidebar_blocks_empty(self):
        d = good_data()
        d["sidebar"]["blocks"] = []
        with self.assertRaises(SchemaError) as ctx:
            validate_data(d)
        self.assertIn("non-empty", str(ctx.exception))

    def test_sidebar_block_missing_id(self):
        d = good_data()
        del d["sidebar"]["blocks"][0]["id"]
        with self.assertRaises(SchemaError) as ctx:
            validate_data(d)
        self.assertIn("id", str(ctx.exception))

    def test_sidebar_block_id_not_kebab(self):
        d = good_data()
        d["sidebar"]["blocks"][0]["id"] = "Not_KebabCase"
        with self.assertRaises(SchemaError) as ctx:
            validate_data(d)
        self.assertIn("kebab-case", str(ctx.exception))

    def test_sidebar_duplicate_block_id(self):
        d = good_data()
        d["sidebar"]["blocks"][1]["id"] = "details"  # collide
        with self.assertRaises(SchemaError) as ctx:
            validate_data(d)
        self.assertIn("duplicate", str(ctx.exception))

    def test_sidebar_invalid_block_type(self):
        d = good_data()
        d["sidebar"]["blocks"][0]["type"] = "bogus"
        with self.assertRaises(SchemaError) as ctx:
            validate_data(d)
        self.assertIn("bogus", str(ctx.exception))

    def test_sidebar_block_missing_heading(self):
        d = good_data()
        del d["sidebar"]["blocks"][0]["heading"]
        with self.assertRaises(SchemaError) as ctx:
            validate_data(d)
        self.assertIn("heading", str(ctx.exception))

    # ── Main column ───────────────────────────────────────────────
    def test_main_column_invalid_type(self):
        d = good_data()
        d["mainColumn"][0]["type"] = "preamble"
        with self.assertRaises(SchemaError) as ctx:
            validate_data(d)
        self.assertIn("preamble", str(ctx.exception))

    def test_main_column_duplicate_type(self):
        d = good_data()
        d["mainColumn"].append({"type": "summary", "heading": "X", "text": "y"})
        with self.assertRaises(SchemaError) as ctx:
            validate_data(d)
        self.assertIn("multiple", str(ctx.exception))

    def test_main_column_missing_required(self):
        d = good_data()
        d["mainColumn"] = [s for s in d["mainColumn"] if s["type"] != "experience"]
        with self.assertRaises(SchemaError) as ctx:
            validate_data(d)
        self.assertIn("experience", str(ctx.exception))

    # ── Jobs ──────────────────────────────────────────────────────
    def test_job_missing_id(self):
        d = good_data()
        del d["mainColumn"][1]["jobs"][0]["id"]
        with self.assertRaises(SchemaError) as ctx:
            validate_data(d)
        self.assertIn("id", str(ctx.exception))

    def test_job_id_not_kebab(self):
        d = good_data()
        d["mainColumn"][1]["jobs"][0]["id"] = "JOB ONE"
        with self.assertRaises(SchemaError) as ctx:
            validate_data(d)
        self.assertIn("kebab-case", str(ctx.exception))

    def test_job_duplicate_id(self):
        d = good_data()
        d["mainColumn"][1]["jobs"][1]["id"] = "job-one"
        with self.assertRaises(SchemaError) as ctx:
            validate_data(d)
        self.assertIn("duplicate", str(ctx.exception))

    def test_regular_job_missing_bullets(self):
        d = good_data()
        del d["mainColumn"][1]["jobs"][0]["bullets"]
        with self.assertRaises(SchemaError) as ctx:
            validate_data(d)
        self.assertIn("bullets", str(ctx.exception))

    def test_regular_job_empty_bullets(self):
        d = good_data()
        d["mainColumn"][1]["jobs"][0]["bullets"] = []
        with self.assertRaises(SchemaError) as ctx:
            validate_data(d)
        self.assertIn("bullets", str(ctx.exception))

    def test_gap_job_no_bullets_required(self):
        # Gap entries don't need bullets; should pass.
        d = good_data()
        validate_data(d)  # gap job is jobs[1], already valid

    def test_jobs_list_empty(self):
        d = good_data()
        d["mainColumn"][1]["jobs"] = []
        with self.assertRaises(SchemaError) as ctx:
            validate_data(d)
        self.assertIn("non-empty", str(ctx.exception))


if __name__ == "__main__":
    unittest.main()
