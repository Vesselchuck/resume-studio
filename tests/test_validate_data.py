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
            {"type": "education", "heading": "Education",
             "items": [{"title": "BA"}]},
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

    # ── Link targets ──────────────────────────────────────────────
    # Chromium resolves a scheme-less href against dist/index.html, so
    # `example.com/me` reached the PDF as file:///…/dist/example.com/me,
    # a dead link carrying the project's local path, in /URI and in the
    # /Contents crop_pdf.describe_links writes from it.
    def test_contact_href_without_a_scheme_is_an_error(self):
        d = good_data()
        d["contact"] = {"rows": [{"value": "me", "href": "example.com/me"}]}
        self.rejects(d, "contact.rows[0]", "no scheme", "https://example.com/me")

    def test_details_href_without_a_scheme_is_an_error(self):
        d = good_data()
        d["sidebar"]["blocks"][0]["rows"][0]["href"] = "../notes.txt"
        self.rejects(d, "sidebar.blocks[0].rows[0]", "no scheme")

    def test_href_schemes_that_run_code_or_read_the_disk_are_errors(self):
        for href in ("javascript:alert(1)", "JavaScript:x", "file:///etc/passwd",
                     "data:text/html,x", "vbscript:x", "C:\\Users\\me\\cv.pdf"):
            with self.subTest(href=href):
                d = good_data()
                d["contact"] = {"rows": [{"value": "v", "href": href}]}
                self.rejects(d, "contact.rows[0]",
                             "path on this computer" if href.startswith("C:") else "not allowed")

    # Only https:, http:, mailto: and tel: are links: a viewer hands any
    # other scheme to whatever the reader's system registered for it.
    def test_href_schemes_outside_the_allowlist_are_errors(self):
        for href in ("about:blank", "blob:x", "smb://host/share",
                     "search-ms:query=x", "ms-msdt:/id", "ftp://x.example"):
            with self.subTest(href=href):
                d = good_data()
                d["contact"] = {"rows": [{"value": "v", "href": href}]}
                self.rejects(d, "contact.rows[0]", "not allowed")

    def test_href_with_nothing_after_the_scheme_is_an_error(self):
        for href, example in (("https:", "https://example.com/me"),
                              ("https://", "https://example.com/me"),
                              ("mailto:", "mailto:you@example.com"),
                              ("tel: ", "tel:+15550100")):
            with self.subTest(href=href):
                d = good_data()
                d["contact"] = {"rows": [{"value": "v", "href": href}]}
                self.rejects(d, "contact.rows[0]", "no address after", example)

    def test_host_and_port_reads_as_a_scheme_and_is_an_error(self):
        # `localhost:3000` parses as the scheme "localhost": a dead link.
        for href in ("localhost:3000", "example.com:8080/x"):
            with self.subTest(href=href):
                d = good_data()
                d["sidebar"]["blocks"][0]["rows"][0]["href"] = href
                self.rejects(d, "no scheme", f'"https://{href}"')

    def test_scheme_less_advice_is_the_address_it_most_likely_is(self):
        for href, fixed in (("//example.com/me", '"https://example.com/me"'),
                            ("you@example.com", '"mailto:you@example.com"'),
                            ("www.example.com", '"https://www.example.com"')):
            with self.subTest(href=href):
                d = good_data()
                d["contact"] = {"rows": [{"value": "v", "href": href}]}
                self.rejects(d, "no scheme", fixed)

    def test_ordinary_links_and_an_empty_href_pass(self):
        for href in ("https://example.com", "mailto:a@b.c", "tel:+15551234567",
                     "http://x.y/z", "HTTPS://Example.com", " https://x.y ", ""):
            with self.subTest(href=href):
                d = good_data()
                d["contact"] = {"rows": [{"value": "v", "href": href}]}
                d["sidebar"]["blocks"][0]["rows"][0]["href"] = href
                validate_data(d)  # should not raise


class TestSharedIdsAndEmptyHeadings(unittest.TestCase):
    def test_a_job_id_that_is_also_a_block_id_is_an_error(self):
        # Both become an HTML id on the same page.
        d = good_data()
        d["mainColumn"][1]["jobs"][0]["id"] = "key-skills"
        with self.assertRaises(SchemaError) as ctx:
            validate_data(d)
        self.assertIn("mainColumn[experience].jobs[0]", str(ctx.exception))
        self.assertIn("sidebar block's id", str(ctx.exception))

    def test_a_list_of_only_groups_warns_for_each(self):
        d = good_data()
        d["sidebar"]["blocks"][1]["items"] = [{"group": "A"}, {"group": "B"}]
        warnings = validate_data(d)
        self.assertEqual(len(warnings), 2)
        self.assertIn("sidebar.blocks[1].items[0]: group 'A' has no entries", warnings[0])
        self.assertIn("sidebar.blocks[1].items[1]: group 'B' has no entries", warnings[1])

    def test_a_group_with_lines_under_it_is_quiet(self):
        d = good_data()
        d["sidebar"]["blocks"][1]["items"] = [{"group": "A"}, "x", {"group": "B"}, "y"]
        self.assertEqual(validate_data(d), [])

    def test_an_empty_education_list_warns_that_the_heading_prints_alone(self):
        d = good_data()
        d["mainColumn"][2]["items"] = []
        self.assertEqual(validate_data(d), [
            "mainColumn[education]: 'items' is empty, so the 'Education' "
            "heading prints with nothing under it"])


class TestAuditedEdges(unittest.TestCase):
    """Shapes that crashed the validator, slipped past it into broken
    output, or were refused although empty means not set."""

    def rejects(self, d, *needles):
        with self.assertRaises(SchemaError) as ctx:
            validate_data(d)
        msg = str(ctx.exception)
        for needle in needles:
            self.assertIn(needle, msg)
        return msg

    # ── An href a browser does not strip ──────────────────────────
    # Chromium keeps a leading no-break space, and its HTML parser turns
    # U+0000 into U+FFFD; either way it read the href as relative, and
    # the PDF linked to file:///…/dist/%C2%A0…. The build strips them,
    # says so, and renders the link without them. It used to refuse
    # them, which rejected a file that built in 1.0.0.
    def test_href_with_leading_invisible_characters_is_stripped(self):
        for space in ("\u00a0", "\u3000", "\u2028", "\u2003", "\0", "\u200b",
                      "\ufeff", "\u2060"):
            with self.subTest(space=hex(ord(space))):
                d = good_data()
                d["contact"] = {"rows": [{"value": "v",
                                          "href": f"{space}https://example.com/me"}]}
                warnings = validate_data(d)
                self.assertEqual(d["contact"]["rows"][0]["href"],
                                 "https://example.com/me")
                self.assertEqual(len(warnings), 1, warnings)
                self.assertIn("contact.rows[0]: 'href' starts with invisible", warnings[0])
                self.assertIn(f"U+{ord(space):04X}", warnings[0])
                d = good_data()
                d["sidebar"]["blocks"][0]["rows"][0]["href"] = f"{space}{space}mailto:a@b.c"
                warnings = validate_data(d)
                self.assertEqual(d["sidebar"]["blocks"][0]["rows"][0]["href"],
                                 "mailto:a@b.c")
                self.assertEqual(len(warnings), 1, warnings)
                self.assertIn("sidebar.blocks[0].rows[0]: 'href'", warnings[0])

    def test_href_with_invisible_characters_after_it_is_stripped(self):
        d = good_data()
        d["contact"] = {"rows": [{"value": "v",
                                  "href": "https://example.com/me\u00a0\u200b"}]}
        warnings = validate_data(d)
        self.assertEqual(d["contact"]["rows"][0]["href"], "https://example.com/me")
        self.assertIn("U+00A0, U+200B", warnings[0])

    def test_href_u0000_inside_is_stripped(self):
        d = good_data()
        d["contact"] = {"rows": [{"value": "v", "href": "https://example.com/a\0b"}]}
        warnings = validate_data(d)
        self.assertEqual(d["contact"]["rows"][0]["href"], "https://example.com/ab")
        self.assertEqual(len(warnings), 1, warnings)
        self.assertIn("contact.rows[0]: 'href' contains U+0000", warnings[0])

    def test_href_that_browsers_strip_themselves_is_cleaned_quietly(self):
        # U+0001 to U+0020: what the build always stripped without a word.
        d = good_data()
        d["contact"] = {"rows": [{"value": "v", "href": "\t https://example.com\n"}]}
        self.assertEqual(validate_data(d), [])
        self.assertEqual(d["contact"]["rows"][0]["href"], "https://example.com")

    def test_href_with_no_scheme_once_stripped_is_still_an_error(self):
        for href in ("\u00a0", "\u200bexample.com", "\0ht\0tps://example.com"):
            with self.subTest(href=ascii(href)):
                d = good_data()
                d["contact"] = {"rows": [{"value": "v", "href": href}]}
                self.rejects(d, "contact.rows[0]", "has no scheme")

    def test_mailto_with_nothing_visible_after_it_prints_as_text(self):
        # `mailto:\u200b` passed the allowlist and built a link to nobody.
        for href in ("mailto:\u200b", "tel:\u00a0\u2060", "MAILTO:\0"):
            with self.subTest(href=ascii(href)):
                d = good_data()
                d["contact"] = {"rows": [{"value": "v", "href": href}]}
                warnings = validate_data(d)
                self.assertIsNone(d["contact"]["rows"][0]["href"])
                self.assertEqual(len(warnings), 1, warnings)
                self.assertIn("contact.rows[0]: 'href' has nothing visible after",
                              warnings[0])

    def test_href_with_what_a_browser_strips_still_passes(self):
        # U+0000 to U+0020: the URL parser drops them, so the link works.
        for href in (" https://example.com", "\thttps://example.com\n",
                     "\x01https://example.com"):
            with self.subTest(href=repr(href)):
                d = good_data()
                d["contact"] = {"rows": [{"value": "v", "href": href}]}
                validate_data(d)  # should not raise

    # ── A type that is not a string ───────────────────────────────
    def test_block_type_as_list_or_mapping_is_a_schema_error(self):
        for value in (["list"], {"list": 1}):
            with self.subTest(type=value):
                d = good_data()
                d["sidebar"]["blocks"][1]["type"] = value
                self.rejects(d, "sidebar.blocks[1]", "not in ['details', 'list']")

    def test_section_type_as_list_or_mapping_is_a_schema_error(self):
        for value in (["summary"], {"summary": None}):
            with self.subTest(type=value):
                d = good_data()
                d["mainColumn"][0]["type"] = value
                self.rejects(d, "mainColumn entry 1", "not in")

    # ── Empty means not set ───────────────────────────────────────
    def test_gap_entry_with_bullets_left_empty_passes(self):
        d = good_data()
        d["mainColumn"][1]["jobs"][1]["bullets"] = None
        self.assertEqual(validate_data(d), [])

    def test_empty_optional_header_fields_pass(self):
        cases = {
            "role": lambda d: d.update(role=None),
            "contact.address": lambda d: d.update(contact={"address": None, "rows": []}),
            "contact row href": lambda d: d.update(
                contact={"rows": [{"value": "v", "href": None}]}),
            "details row href": lambda d: d["sidebar"]["blocks"][0]["rows"][0]
                .update(href=None),
            "empty role": lambda d: d.update(role=""),
            "empty address": lambda d: d.update(contact={"address": "", "rows": []}),
        }
        for label, mutate in cases.items():
            with self.subTest(case=label):
                d = good_data()
                mutate(d)
                self.assertEqual(validate_data(d), [])

    # ── Blank text the template would print as an empty element ───
    # Warned about, not refused: each of these built in 1.0.0, and a
    # stricter build must not reject a file that built.
    def test_name_with_both_parts_blank_is_warned_about(self):
        for first, last in (("", ""), (" ", "\t"), ("\u3000", "")):
            with self.subTest(first=first, last=last):
                d = good_data()
                d["name"] = {"first": first, "last": last}
                warnings = validate_data(d)
                self.assertEqual(len(warnings), 1, warnings)
                self.assertIn("'name.first' and 'name.last' are both blank",
                              warnings[0])

    def test_name_with_one_part_blank_passes(self):
        # One name is still a name: the heading, title and file name all
        # come out right from the part that is there.
        for first, last in (("Gaius", ""), ("", "Caesar")):
            with self.subTest(first=first, last=last):
                d = good_data()
                d["name"] = {"first": first, "last": last}
                validate_data(d)  # should not raise

    def test_blank_header_and_heading_text_is_warned_about(self):
        # A blank role, address or contact line is left out, so the header
        # prints no empty element for it; a blank heading prints as it did.
        cases = {
            "role": (lambda d: d.update(role="  "), "'role' is blank",
                     lambda d: d["role"] is None),
            "contact.address": (lambda d: d.update(contact={"address": " ", "rows": []}),
                                "'contact.address' is blank",
                                lambda d: d["contact"]["address"] is None),
            "contact row value": (lambda d: d.update(contact={"rows": [
                                      {"value": "   ", "href": "tel:+15550100"},
                                      {"value": "kept"}]}),
                                  "contact.rows[0]: 'value' is blank",
                                  lambda d: d["contact"]["rows"] == [{"value": "kept"}]),
            "sidebar heading": (lambda d: d["sidebar"]["blocks"][1].update(heading=" "),
                                "sidebar.blocks[1]: 'heading' is blank",
                                lambda d: d["sidebar"]["blocks"][1]["heading"] == " "),
        }
        for label, (mutate, needle, rendered) in cases.items():
            with self.subTest(case=label):
                d = good_data()
                mutate(d)
                warnings = validate_data(d)
                self.assertEqual(len(warnings), 1, warnings)
                self.assertIn(needle, warnings[0])
                self.assertTrue(rendered(d), "what the build renders")

    def test_empty_contact_value_and_heading_are_still_errors(self):
        # Refused in 1.0.0 too: an empty string, not one of spaces.
        d = good_data()
        d["contact"] = {"rows": [{"value": ""}]}
        self.rejects(d, "contact.rows[0]: 'value' must be a non-empty string")
        d = good_data()
        d["sidebar"]["blocks"][1]["heading"] = ""
        self.rejects(d, "sidebar.blocks[1]: 'heading' must be a non-empty string")

    # ── meta.lang reaches /Lang and the XMP ───────────────────────
    def test_lang_that_is_not_a_language_tag_is_warned_about(self):
        # A locale name (en_US.UTF-8, C) built in 1.0.0: warned about,
        # and the documents are marked en-US rather than with it.
        from build import resolve_lang
        for lang in ("en\x01US", "en US", "e", "en-", "en--US", "<en>", "en-US\u00a0x",
                     "q-private", "en-abcdefghi", "en_US.UTF-8", "C", "en\u2011US"):
            with self.subTest(lang=ascii(lang)):
                d = good_data()
                d["meta"]["lang"] = lang
                warnings = validate_data(d)
                self.assertEqual(len(warnings), 1, warnings)
                self.assertIn("'meta.lang'", warnings[0])
                self.assertIn("is not a language tag", warnings[0])
                self.assertEqual(resolve_lang(d), "en-US")

    def test_language_tags_pass(self):
        for lang in ("en", "en-US", "EN-us", "en_GB", "zh-Hant-TW", "de-CH-1996",
                     "sr-Latn-RS", "en-150", "en-x-gb", "x-private", "i-klingon",
                     " en-US ", "", "  "):
            with self.subTest(lang=lang):
                d = good_data()
                d["meta"]["lang"] = lang
                validate_data(d)  # should not raise


if __name__ == "__main__":
    unittest.main()
