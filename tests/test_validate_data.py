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
                {"id": "job-one", "title": "A", "date": "2020 – 2021",
                 "bullets": ["x", "y"]},
                {"id": "job-two", "title": "B", "date": "2019", "gap": True},
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


class TestValidateDataGaps(unittest.TestCase):
    """
    Shapes that used to reach the template and die there (a Jinja
    UndefinedError or TypeError), or render wrong output, now stop in
    the validator with a message that says where and what to write.
    """

    def job(self, d, i=0):
        return d["mainColumn"][1]["jobs"][i]

    def rejects(self, d, *needles):
        with self.assertRaises(SchemaError) as ctx:
            validate_data(d)
        msg = str(ctx.exception)
        for needle in needles:
            self.assertIn(needle, msg)
        return msg

    def test_good_data_has_no_warnings(self):
        self.assertEqual(validate_data(good_data()), [])

    # ── Jobs ──────────────────────────────────────────────────────
    def test_job_without_title(self):
        d = good_data()
        del self.job(d)["title"]
        self.rejects(d, "mainColumn[experience].jobs[0]", "'title'")

    def test_job_without_date(self):
        d = good_data()
        del self.job(d)["date"]
        self.rejects(d, "mainColumn[experience].jobs[0]", "'date'",
                     "Mar 2019")

    def test_job_null_date(self):
        d = good_data()
        self.job(d)["date"] = None
        self.rejects(d, "'date'", "empty")

    def test_job_without_datetime_is_fine(self):
        """The template prints the date text and drops the attribute."""
        d = good_data()
        self.assertNotIn("datetime", self.job(d))
        validate_data(d)

    def test_integer_datetime_and_date_are_accepted(self):
        """`datetime: 2021` is what people write; it always rendered."""
        d = good_data()
        self.job(d)["datetime"] = 2021
        self.job(d)["date"] = 2021
        validate_data(d)

    def test_datetime_must_be_text(self):
        d = good_data()
        self.job(d)["datetime"] = ["2021"]
        self.rejects(d, "'datetime'", "list")

    def test_gap_entry_with_bullets(self):
        d = good_data()
        self.job(d, 1)["bullets"] = ["did things"]
        self.rejects(d, "jobs[1]", "gap", "delete its 'bullets'")

    def test_gap_entry_with_empty_bullets_is_fine(self):
        d = good_data()
        self.job(d, 1)["bullets"] = []
        validate_data(d)

    def test_bullet_read_as_mapping(self):
        """`- Led migration: cut costs 30%` is a one-key mapping."""
        d = good_data()
        self.job(d)["bullets"] = [{"Led migration": "cut costs 30%"}]
        msg = self.rejects(d, "jobs[0].bullets[0]", "quotes")
        self.assertIn('"Led migration: cut costs 30%"', msg)

    def test_empty_bullet(self):
        d = good_data()
        self.job(d)["bullets"] = ["x", None]
        self.rejects(d, "jobs[0].bullets[1]", "empty")

    def test_blank_bullet(self):
        d = good_data()
        self.job(d)["bullets"] = ["   "]
        self.rejects(d, "jobs[0].bullets[0]", "blank")

    def test_bullets_as_string(self):
        d = good_data()
        self.job(d)["bullets"] = "one long line"
        self.rejects(d, "bullets")

    def test_unknown_job_key_is_an_error_because_the_schema_is_closed(self):
        """job has additionalProperties:false in resume.schema.json."""
        d = good_data()
        self.job(d)["locaton"] = "Rome"
        self.rejects(d, "mainColumn[experience].jobs[0]", "'locaton'",
                     "did you mean 'location'")

    # ── Sections ──────────────────────────────────────────────────
    def test_section_without_heading(self):
        d = good_data()
        del d["mainColumn"][1]["heading"]
        self.rejects(d, "mainColumn[experience]", "'heading'")

    def test_summary_without_text(self):
        d = good_data()
        del d["mainColumn"][0]["text"]
        self.rejects(d, "mainColumn[summary]", "'text'")

    def test_summary_text_as_list(self):
        d = good_data()
        d["mainColumn"][0]["text"] = ["one", "two"]
        self.rejects(d, "mainColumn[summary]", "list")

    def test_education_without_items(self):
        d = good_data()
        del d["mainColumn"][2]["items"]
        self.rejects(d, "mainColumn[education]", "'items'")

    def test_education_item_needs_only_a_title(self):
        d = good_data()
        d["mainColumn"][2]["items"] = [{"title": "BA"}]
        validate_data(d)

    def test_education_item_without_title(self):
        d = good_data()
        d["mainColumn"][2]["items"] = [{"subtitle": "x"}]
        self.rejects(d, "mainColumn[education].items[0]", "'title'")

    def test_education_item_as_string(self):
        """A bare string would render str.title as a bound method."""
        d = good_data()
        d["mainColumn"][2]["items"] = ["BA in History"]
        self.rejects(d, "mainColumn[education].items[0]", "mapping")

    def test_unknown_section_key_warns(self):
        d = good_data()
        d["mainColumn"][0]["txt"] = "typo"
        self.assertEqual(
            validate_data(d),
            ["unknown key 'txt' in mainColumn[summary] — ignored "
             "(did you mean 'text'?)"],
        )

    # ── Sidebar blocks ────────────────────────────────────────────
    def test_list_block_without_items(self):
        d = good_data()
        del d["sidebar"]["blocks"][1]["items"]
        self.rejects(d, "sidebar.blocks[1].items", "'items'")

    def test_list_block_items_as_string(self):
        """`items: "Latin, Greek"` rendered one <li> per character."""
        d = good_data()
        d["sidebar"]["blocks"][1]["items"] = "Latin, Greek"
        self.rejects(d, "sidebar.blocks[1].items", "must be a list",
                     "'Latin, Greek'")

    def test_list_block_empty_items(self):
        d = good_data()
        d["sidebar"]["blocks"][1]["items"] = []
        self.rejects(d, "sidebar.blocks[1].items", "empty list")

    def test_details_block_empty_rows(self):
        d = good_data()
        d["sidebar"]["blocks"][0]["rows"] = []
        self.rejects(d, "sidebar.blocks[0].rows", "empty list")

    def test_details_block_without_rows(self):
        d = good_data()
        del d["sidebar"]["blocks"][0]["rows"]
        self.rejects(d, "sidebar.blocks[0].rows")

    def test_list_item_mapping_without_group(self):
        """`- Latin: native` is a mapping with no 'group'."""
        d = good_data()
        d["sidebar"]["blocks"][1]["items"] = [{"Latin": "native"}]
        msg = self.rejects(d, "sidebar.blocks[1].items[0]", "quotes")
        self.assertIn('"Latin: native"', msg)
        self.assertIn("group", msg)

    def test_list_item_group_with_extra_key(self):
        d = good_data()
        d["sidebar"]["blocks"][1]["items"] = [{"group": "A", "note": "x"}]
        self.rejects(d, "sidebar.blocks[1].items[0]", "'note'")

    def test_list_item_empty(self):
        d = good_data()
        d["sidebar"]["blocks"][1]["items"] = ["A", None]
        self.rejects(d, "sidebar.blocks[1].items[1]", "empty")

    def test_list_item_group_empty(self):
        d = good_data()
        d["sidebar"]["blocks"][1]["items"] = [{"group": None}, "A"]
        self.rejects(d, "sidebar.blocks[1].items[0]", "'group'")

    def test_details_row_label_must_be_text(self):
        d = good_data()
        d["sidebar"]["blocks"][0]["rows"] = [{"label": ["X"], "value": "Y"}]
        self.rejects(d, "sidebar.blocks[0].rows[0]", "'label'")

    def test_details_row_value_must_be_text(self):
        d = good_data()
        d["sidebar"]["blocks"][0]["rows"] = [{"label": "X", "value": None}]
        self.rejects(d, "sidebar.blocks[0].rows[0]", "'value'")

    def test_details_row_missing_label(self):
        d = good_data()
        d["sidebar"]["blocks"][0]["rows"] = [{"value": "Y"}]
        self.rejects(d, "sidebar.blocks[0].rows[0]", "'label'")

    def test_block_with_the_other_types_content_key_warns(self):
        d = good_data()
        d["sidebar"]["blocks"][0]["items"] = ["stray"]
        self.assertEqual(
            validate_data(d),
            ["unknown key 'items' in sidebar.blocks[0] — ignored "
             "(a details block reads 'rows')"],
        )

    # ── Meta / top level ──────────────────────────────────────────
    def test_max_pages_true_is_rejected(self):
        """bool is an int subclass; `maxPages: true` passed as 1."""
        d = good_data()
        d["meta"]["maxPages"] = True
        self.rejects(d, "maxPages", "whole number")

    def test_max_pages_leading_zero_hint(self):
        """`maxPages: 02` is now the string '02' (see _yaml_loader)."""
        d = good_data()
        d["meta"]["maxPages"] = "02"
        self.rejects(d, "maxPages", "maxPages: 2")

    def test_meta_lang_null_is_fine(self):
        d = good_data()
        d["meta"]["lang"] = None
        validate_data(d)

    def test_unknown_meta_key_warns(self):
        d = good_data()
        d["meta"]["maxpages"] = 3
        self.assertEqual(
            validate_data(d),
            ["unknown key 'maxpages' in meta — ignored "
             "(did you mean 'maxPages'?)"],
        )

    def test_unknown_top_level_key_warns(self):
        d = good_data()
        d["skills"] = ["x"]
        self.assertEqual(validate_data(d),
                         ["unknown key 'skills' in the top level — ignored"])

    def test_unknown_name_key_is_an_error(self):
        d = good_data()
        d["name"]["middle"] = "Julius"
        self.rejects(d, "name", "'middle'")

    def test_unknown_contact_key_is_an_error(self):
        d = good_data()
        d["contact"] = {"rows": [], "phone": "1"}
        self.rejects(d, "contact", "'phone'")


if __name__ == "__main__":
    unittest.main()
