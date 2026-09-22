"""
Tests for build_letter — the letter data layer.

Covers:
  • validate_data — schema validation (name, role, contact, meta,
    letter.body and the optional letter fields).
  • resolve_letter — the stamped date, the split body and recipient,
    and the signature taken from `name`, without mutating the input.
  • format_date — how the stamped date is written for a given lang.
  • derive_pdf_metadata — title/author/subject/keywords/lang/data_source.
  • load_data — YAML loader with RESUME_DATA_SOURCE precedence, mirroring
    test_load_data.py for the resume.

Path constants are patched to a temp dir for the load_data tests so the
suite isn't sensitive to the real data files. stdout/stderr are silenced
because load_data emits c.ok_pair / c.err / c.detail.
"""

import contextlib
import datetime
import io
import os
import sys
import tempfile
import unittest
from pathlib import Path
from unittest import mock

ROOT = Path(__file__).parent.parent
sys.path.insert(0, str(ROOT / "build"))

import build_letter as clb  # noqa: E402
from build import SchemaError  # noqa: E402
from _env_contract import ENV_RESUME_DATA_SOURCE, ENV_LETTER_DATA_FILE  # noqa: E402


@contextlib.contextmanager
def silenced():
    with contextlib.redirect_stdout(io.StringIO()), \
         contextlib.redirect_stderr(io.StringIO()):
        yield


def good_data():
    """A minimal-but-valid letter structure for use in tests."""
    return {
        "name": {"first": "Gaius", "last": "Caesar"},
        "role": "Staff Engineer",
        "contact": {
            "address": "Somewhere",
            "rows": [{"value": "gaius@example.com", "href": "mailto:gaius@example.com"}],
        },
        "meta": {"description": "Cover letter of Gaius Caesar.", "lang": "en-US"},
        "letter": {
            "recipient": ["Hiring Team", "Acme Inc."],
            "body": [
                "Dear Hiring Team,",
                "First paragraph.",
                "Second **bold** paragraph.",
                "Sincerely,",
            ],
        },
    }


MINIMAL_YAML = """\
name: {first: Gaius, last: Caesar}
letter:
  body:
    - Hello there.
"""


class TestValidateData(unittest.TestCase):
    def test_good_data_passes(self):
        clb.validate_data(good_data())  # should not raise

    def test_minimal_data_passes(self):
        # Only the two required keys, body the only required letter field.
        clb.validate_data({
            "name": {"first": "A", "last": "B"},
            "letter": {"body": ["Hi."]},
        })

    # ── Top-level ─────────────────────────────────────────────────
    def test_missing_name(self):
        d = good_data()
        del d["name"]
        with self.assertRaises(SchemaError) as ctx:
            clb.validate_data(d)
        self.assertIn("name", str(ctx.exception))

    def test_missing_letter(self):
        d = good_data()
        del d["letter"]
        with self.assertRaises(SchemaError) as ctx:
            clb.validate_data(d)
        self.assertIn("letter", str(ctx.exception))

    # ── Name / role ───────────────────────────────────────────────
    def test_name_missing_first(self):
        d = good_data()
        del d["name"]["first"]
        with self.assertRaises(SchemaError) as ctx:
            clb.validate_data(d)
        self.assertIn("name.first", str(ctx.exception))

    def test_role_not_string(self):
        d = good_data()
        d["role"] = 123
        with self.assertRaises(SchemaError) as ctx:
            clb.validate_data(d)
        self.assertIn("role", str(ctx.exception))

    def test_role_omitted_passes(self):
        d = good_data()
        d.pop("role", None)
        clb.validate_data(d)

    # ── Contact (same shape as the resume) ────────────────────────
    def test_contact_null_passes(self):
        d = good_data()
        d["contact"] = None
        clb.validate_data(d)

    def test_contact_not_mapping(self):
        d = good_data()
        d["contact"] = "123 Main St"
        with self.assertRaises(SchemaError) as ctx:
            clb.validate_data(d)
        self.assertIn("contact", str(ctx.exception))

    def test_contact_row_value_missing(self):
        d = good_data()
        d["contact"] = {"rows": [{"href": "tel:+10000000000"}]}
        with self.assertRaises(SchemaError) as ctx:
            clb.validate_data(d)
        self.assertIn("value", str(ctx.exception))

    def test_contact_rows_empty_passes(self):
        d = good_data()
        d["contact"] = {"address": "Somewhere", "rows": []}
        clb.validate_data(d)

    # ── Meta (optional) ───────────────────────────────────────────
    def test_meta_omitted_passes(self):
        d = good_data()
        d.pop("meta", None)
        clb.validate_data(d)

    def test_meta_description_not_string(self):
        d = good_data()
        d["meta"]["description"] = ["nope"]
        with self.assertRaises(SchemaError) as ctx:
            clb.validate_data(d)
        self.assertIn("description", str(ctx.exception))

    def test_meta_lang_not_string(self):
        d = good_data()
        d["meta"]["lang"] = ["en-US"]
        with self.assertRaises(SchemaError) as ctx:
            clb.validate_data(d)
        self.assertIn("lang", str(ctx.exception))

    # ── Letter ────────────────────────────────────────────────────
    def test_letter_not_mapping(self):
        d = good_data()
        d["letter"] = ["just a string"]
        with self.assertRaises(SchemaError) as ctx:
            clb.validate_data(d)
        self.assertIn("letter", str(ctx.exception))

    def test_body_missing(self):
        d = good_data()
        del d["letter"]["body"]
        with self.assertRaises(SchemaError) as ctx:
            clb.validate_data(d)
        self.assertIn("body", str(ctx.exception))

    def test_body_empty(self):
        d = good_data()
        d["letter"]["body"] = []
        with self.assertRaises(SchemaError) as ctx:
            clb.validate_data(d)
        self.assertIn("body", str(ctx.exception))

    def test_body_paragraph_not_string(self):
        d = good_data()
        d["letter"]["body"] = ["ok", 42]
        with self.assertRaises(SchemaError) as ctx:
            clb.validate_data(d)
        self.assertIn("body[1]", str(ctx.exception))

    def test_body_paragraph_blank(self):
        d = good_data()
        d["letter"]["body"] = ["   "]
        with self.assertRaises(SchemaError) as ctx:
            clb.validate_data(d)
        self.assertIn("body[0]", str(ctx.exception))

    def test_a_leftover_date_is_rejected_not_ignored(self):
        # The field was removed, not deprecated. Ignoring it would
        # leave someone editing a line with no effect and finding out
        # from a letter dated differently to the file that produced it.
        d = good_data()
        d["letter"]["date"] = "11 June 2026"
        with self.assertRaises(SchemaError) as ctx:
            clb.validate_data(d)
        message = str(ctx.exception)
        self.assertIn("date", message)
        self.assertIn("Delete the line", message)

    def test_recipient_accepts_a_pasted_block(self):
        d = good_data()
        d["letter"]["recipient"] = "Hiring Team\nAcme Inc."
        clb.validate_data(d)          # should not raise

    def test_recipient_of_the_wrong_type(self):
        d = good_data()
        d["letter"]["recipient"] = 42
        with self.assertRaises(SchemaError) as ctx:
            clb.validate_data(d)
        self.assertIn("recipient", str(ctx.exception))

    def test_recipient_block_that_is_all_whitespace(self):
        d = good_data()
        d["letter"]["recipient"] = "   \n\n  "
        with self.assertRaises(SchemaError) as ctx:
            clb.validate_data(d)
        self.assertIn("recipient", str(ctx.exception))

    def test_recipient_line_blank(self):
        d = good_data()
        d["letter"]["recipient"] = ["Acme", ""]
        with self.assertRaises(SchemaError) as ctx:
            clb.validate_data(d)
        self.assertIn("recipient[1]", str(ctx.exception))

    def test_the_removed_signoff_fields_are_rejected_by_name(self):
        # These are fields people have in their files today. A bare
        # "unexpected key" would say something is wrong without saying
        # what to do, and ignoring them would drop the greeting off the
        # letter silently — so each error names its replacement.
        for field, expect in (("salutation", "first paragraph"),
                              ("closing", "last paragraph"),
                              ("signature", "'name'")):
            with self.subTest(field=field):
                d = good_data()
                d["letter"][field] = "whatever"
                with self.assertRaises(SchemaError) as ctx:
                    clb.validate_data(d)
                message = str(ctx.exception)
                self.assertIn(field, message)
                self.assertIn(expect, message)
                self.assertIn("Delete the line", message)

    # ── Body as a block string (the easy paste-in form) ───────────
    def test_body_as_block_string_passes(self):
        d = good_data()
        d["letter"]["body"] = "Para one.\n\nPara two."
        clb.validate_data(d)  # should not raise

    def test_body_blank_string_fails(self):
        d = good_data()
        d["letter"]["body"] = "   \n  \n"
        with self.assertRaises(SchemaError) as ctx:
            clb.validate_data(d)
        self.assertIn("body", str(ctx.exception))

    def test_body_wrong_type_fails(self):
        d = good_data()
        d["letter"]["body"] = 42
        with self.assertRaises(SchemaError) as ctx:
            clb.validate_data(d)
        self.assertIn("body", str(ctx.exception))


class TestResolveLetter(unittest.TestCase):
    def test_the_signature_comes_from_the_name(self):
        # Which the shared profile supplies, so it is right on every
        # letter without being written on any of them.
        d = {"name": {"first": "Gaius", "last": "Caesar"},
             "letter": {"body": ["Hi."]}}
        self.assertEqual(clb.resolve_letter(d)["signature"], "Gaius Caesar")

    def test_the_signature_follows_a_changed_name(self):
        d = {"name": {"first": "Gaius", "last": "Octavius"},
             "letter": {"body": ["Hi."]}}
        self.assertEqual(clb.resolve_letter(d)["signature"], "Gaius Octavius")

    def test_the_greeting_and_signoff_stay_ordinary_paragraphs(self):
        # Nothing promotes them out of the body — they render as <p>
        # like everything else, which is the whole point of the change.
        letter = clb.resolve_letter(good_data())
        self.assertEqual(letter["body"][0], "Dear Hiring Team,")
        self.assertEqual(letter["body"][-1], "Sincerely,")
        self.assertNotIn("salutation", letter)
        self.assertNotIn("closing", letter)

    def test_input_not_mutated(self):
        d = {"name": {"first": "Gaius", "last": "Caesar"},
             "letter": {"body": ["Hi."]}}
        clb.resolve_letter(d)
        self.assertNotIn("signature", d["letter"])
        self.assertNotIn("date", d["letter"])

    def test_list_body_passed_through(self):
        d = {"name": {"first": "Gaius", "last": "Caesar"},
             "letter": {"body": ["One.", "Two."]}}
        self.assertEqual(clb.resolve_letter(d)["body"], ["One.", "Two."])

    def test_block_string_body_split_on_blank_lines(self):
        d = {"name": {"first": "Gaius", "last": "Caesar"},
             "letter": {"body": "Para one.\n\nPara two.\n\n\nPara three."}}
        self.assertEqual(
            clb.resolve_letter(d)["body"],
            ["Para one.", "Para two.", "Para three."],
        )

    def test_block_string_keeps_soft_wraps_within_paragraph(self):
        # A single newline inside a paragraph is NOT a split point; the
        # paragraph stays one string (HTML collapses the newline to a
        # space at render time).
        d = {"name": {"first": "Gaius", "last": "Caesar"},
             "letter": {"body": "Line one\nstill para one.\n\nPara two."}}
        body = clb.resolve_letter(d)["body"]
        self.assertEqual(len(body), 2)
        self.assertIn("\n", body[0])

    def test_block_string_strips_surrounding_blank_lines(self):
        d = {"name": {"first": "Gaius", "last": "Caesar"},
             "letter": {"body": "\n\n  Only para.  \n\n"}}
        self.assertEqual(clb.resolve_letter(d)["body"], ["Only para."])


class TestDerivePDFMetadata(unittest.TestCase):
    def test_title_author_subject(self):
        m = clb.derive_pdf_metadata(good_data(), "en-US", "default")
        self.assertEqual(m["title"], "Gaius Caesar — Cover Letter")
        self.assertEqual(m["author"], "Gaius Caesar")
        self.assertEqual(m["subject"], "Cover letter of Gaius Caesar.")

    def test_subject_strips_and_empty_when_absent(self):
        d = good_data()
        d["meta"]["description"] = "  spaced  "
        self.assertEqual(
            clb.derive_pdf_metadata(d, "en-US", "default")["subject"], "spaced")
        d2 = good_data()
        d2.pop("meta", None)
        self.assertEqual(
            clb.derive_pdf_metadata(d2, "en-US", "default")["subject"], "")

    def test_keywords_always_empty(self):
        m = clb.derive_pdf_metadata(good_data(), "en-US", "default")
        self.assertEqual(m["keywords"], "")

    def test_lang_and_source_passed_through(self):
        m = clb.derive_pdf_metadata(good_data(), "fr-FR", "mine")
        self.assertEqual(m["lang"], "fr-FR")
        self.assertEqual(m["data_source"], "mine")


class TestLoadData(unittest.TestCase):
    def setUp(self):
        self.tmpdir = Path(tempfile.mkdtemp())
        self.default_path = self.tmpdir / "letter_default.yml"
        self.mine_path = self.tmpdir / "letter.yml"
        assert self.default_path != self.mine_path
        # Patch the module-level path constants + ROOT (used for the
        # relative_to() display path) for the duration of each test.
        self._patches = [
            mock.patch.object(clb, "DATA_FILE_DEFAULT", self.default_path),
            mock.patch.object(clb, "DATA_FILE_MINE", self.mine_path),
            mock.patch.object(clb, "ROOT", self.tmpdir),
        ]
        for p in self._patches:
            p.start()
        self._saved_env = os.environ.pop(ENV_RESUME_DATA_SOURCE, None)

    def tearDown(self):
        for p in self._patches:
            p.stop()
        for f in self.tmpdir.iterdir():
            f.unlink()
        self.tmpdir.rmdir()
        if self._saved_env is not None:
            os.environ[ENV_RESUME_DATA_SOURCE] = self._saved_env
        else:
            os.environ.pop(ENV_RESUME_DATA_SOURCE, None)

    def test_unset_only_default_exists_loads_default(self):
        self.default_path.write_text(MINIMAL_YAML, encoding="utf-8")
        with silenced():
            data, source = clb.load_data()
        self.assertEqual(source, "default")
        self.assertEqual(data["name"]["first"], "Gaius")

    def test_unset_both_exist_prefers_mine(self):
        self.default_path.write_text(
            MINIMAL_YAML.replace("Gaius", "FromDefault"), encoding="utf-8")
        self.mine_path.write_text(
            MINIMAL_YAML.replace("Gaius", "FromLocal"), encoding="utf-8")
        with silenced():
            data, source = clb.load_data()
        self.assertEqual(source, "mine")
        self.assertEqual(data["name"]["first"], "FromLocal")

    def test_unset_neither_exists_fails(self):
        with silenced(), self.assertRaises(SystemExit) as ctx:
            clb.load_data()
        self.assertEqual(ctx.exception.code, 1)

    def test_default_explicit_loads_default_even_if_mine_exists(self):
        self.default_path.write_text(
            MINIMAL_YAML.replace("Gaius", "FromDefault"), encoding="utf-8")
        self.mine_path.write_text(
            MINIMAL_YAML.replace("Gaius", "FromLocal"), encoding="utf-8")
        os.environ[ENV_RESUME_DATA_SOURCE] = "default"
        with silenced():
            data, source = clb.load_data()
        self.assertEqual(source, "default")
        self.assertEqual(data["name"]["first"], "FromDefault")

    def test_mine_explicit_mine_missing_fails(self):
        self.default_path.write_text(MINIMAL_YAML, encoding="utf-8")
        os.environ[ENV_RESUME_DATA_SOURCE] = "mine"
        with silenced(), self.assertRaises(SystemExit):
            clb.load_data()

    def test_invalid_env_value_fails(self):
        self.default_path.write_text(MINIMAL_YAML, encoding="utf-8")
        os.environ[ENV_RESUME_DATA_SOURCE] = "neither"
        with silenced(), self.assertRaises(SystemExit):
            clb.load_data()

    def test_env_value_normalized(self):
        self.mine_path.write_text(MINIMAL_YAML, encoding="utf-8")
        os.environ[ENV_RESUME_DATA_SOURCE] = "  MINE  "
        with silenced():
            data, source = clb.load_data()
        self.assertEqual(source, "mine")

    def test_yaml_top_level_not_mapping_fails(self):
        self.default_path.write_text("- one\n- two\n", encoding="utf-8")
        with silenced(), self.assertRaises(SystemExit):
            clb.load_data()




class TestFormatDate(unittest.TestCase):
    """How the stamped date is written."""

    DAY = datetime.date(2026, 9, 20)

    def test_us_english_puts_the_month_first(self):
        self.assertEqual(clb.format_date(self.DAY, "en-US"),
                         "September 20, 2026")

    def test_day_first_english_regions(self):
        for lang in ("en-GB", "en-AU", "en-IE", "en-IN", "en-NZ", "en-ZA"):
            with self.subTest(lang=lang):
                self.assertEqual(clb.format_date(self.DAY, lang),
                                 "20 September 2026")

    def test_bare_english_defaults_to_month_first(self):
        self.assertEqual(clb.format_date(self.DAY, "en"), "September 20, 2026")

    def test_non_english_falls_back_to_iso(self):
        # Printing an English month name to a reader of another
        # language would be worse than printing a format every locale
        # reads correctly. This project has no translated month names.
        for lang in ("de-DE", "ja-JP", "fr", "pt-BR"):
            with self.subTest(lang=lang):
                self.assertEqual(clb.format_date(self.DAY, lang), "2026-09-20")

    def test_a_malformed_or_missing_tag_does_not_raise(self):
        for lang in (None, "", "-", "x", "en-", "EN-us"):
            with self.subTest(lang=lang):
                clb.format_date(self.DAY, lang)   # must not raise

    def test_the_tag_is_read_case_insensitively(self):
        self.assertEqual(clb.format_date(self.DAY, "EN-gb"), "20 September 2026")

    def test_no_day_is_zero_padded(self):
        self.assertEqual(clb.format_date(datetime.date(2026, 3, 5), "en-US"),
                         "March 5, 2026")

    def test_the_month_names_do_not_depend_on_the_machine_locale(self):
        # strftime("%B") consults LC_TIME; these are written out for
        # exactly that reason, so the same data file builds the same
        # letter on every machine. Twelve of them, all used.
        self.assertEqual(len(clb._MONTHS), 12)
        for month in range(1, 13):
            day = datetime.date(2026, month, 1)
            self.assertEqual(clb.format_date(day, "en-US").split()[0],
                             clb._MONTHS[month - 1])


class TestStampedDate(unittest.TestCase):
    """resolve_letter puts the date in, because the data no longer can."""

    def test_the_date_is_stamped_from_the_clock(self):
        letter = clb.resolve_letter(good_data(), lang="en-US",
                                    today=datetime.date(2026, 9, 20))
        self.assertEqual(letter["date"], "September 20, 2026")
        self.assertEqual(letter["date_iso"], "2026-09-20")

    def test_it_follows_the_documents_language(self):
        d = good_data()
        d["meta"]["lang"] = "en-GB"
        letter = clb.resolve_letter(d, today=datetime.date(2026, 9, 20))
        self.assertEqual(letter["date"], "20 September 2026")

    def test_the_iso_form_is_the_same_day_as_the_printed_one(self):
        # The template prints one and puts the other in <time datetime>.
        # If they ever disagreed, the document would say two things.
        for lang in ("en-US", "en-GB", "de-DE"):
            with self.subTest(lang=lang):
                day = datetime.date(2026, 12, 31)
                letter = clb.resolve_letter(good_data(), lang=lang, today=day)
                self.assertEqual(letter["date_iso"], "2026-12-31")
                self.assertIn("31", letter["date"])
                self.assertIn("2026", letter["date"])

    def test_defaults_to_the_real_clock(self):
        letter = clb.resolve_letter(good_data())
        self.assertEqual(letter["date_iso"], datetime.date.today().isoformat())

    def test_the_input_is_still_not_mutated(self):
        d = good_data()
        clb.resolve_letter(d, today=datetime.date(2026, 9, 20))
        self.assertNotIn("date", d["letter"])
        self.assertNotIn("date_iso", d["letter"])




class TestRecipientSplitting(unittest.TestCase):
    """An address is pasted, and pasted addresses are line-per-entry."""

    def letter_with(self, recipient):
        d = {"name": {"first": "Gaius", "last": "Caesar"},
             "letter": {"body": ["Hi."], "recipient": recipient}}
        return clb.resolve_letter(d)["recipient"]

    def test_a_block_splits_on_every_line(self):
        # NOT on blank lines, which is how `body` splits. An address
        # line is not a paragraph; "Acme Inc." and "100 Main St" must
        # not end up glued into one line of the address block.
        self.assertEqual(
            self.letter_with("Hiring Team\nAcme Inc.\n100 Main St\nSpringfield, IL 62701"),
            ["Hiring Team", "Acme Inc.", "100 Main St", "Springfield, IL 62701"])

    def test_blank_lines_in_a_pasted_block_are_dropped(self):
        # They are artifacts of copying out of a job posting, not
        # structure — and an empty <span> would open a gap in the
        # address block for no reason.
        self.assertEqual(
            self.letter_with("Hiring Team\n\n\nAcme Inc.\n   \n"),
            ["Hiring Team", "Acme Inc."])

    def test_surrounding_whitespace_is_trimmed(self):
        # A YAML block scalar keeps the indentation you pasted with.
        self.assertEqual(self.letter_with("  Hiring Team  \n\tAcme Inc.\t"),
                         ["Hiring Team", "Acme Inc."])

    def test_the_list_form_still_works_untouched(self):
        self.assertEqual(self.letter_with(["Hiring Team", "Acme Inc."]),
                         ["Hiring Team", "Acme Inc."])

    def test_an_absent_recipient_stays_absent(self):
        # The template's {% if %} closes the layout gap; an empty list
        # would render an empty <address> element instead.
        d = {"name": {"first": "Gaius", "last": "Caesar"},
             "letter": {"body": ["Hi."]}}
        self.assertIsNone(clb.resolve_letter(d).get("recipient"))

    def test_the_two_forms_agree(self):
        block = "Hiring Team\nAcme Inc.\n100 Main St"
        as_list = ["Hiring Team", "Acme Inc.", "100 Main St"]
        self.assertEqual(self.letter_with(block), self.letter_with(as_list))



class TestDateLocaleEdgeCases(unittest.TestCase):
    """Tags that are not the tidy `en-GB` the table was written for."""

    DAY = datetime.date(2026, 9, 20)
    US, DAY_FIRST = "September 20, 2026", "20 September 2026"

    def test_posix_underscore_is_read_as_a_hyphen(self):
        """`en_GB` is a locale name, but it is what people type."""
        for lang in ("en_GB", "en_AU", "EN_ie"):
            with self.subTest(lang=lang):
                self.assertEqual(clb.format_date(self.DAY, lang), self.DAY_FIRST)
        self.assertEqual(clb.format_date(self.DAY, "en_US"), self.US)

    def test_un_m49_europe_is_day_first(self):
        self.assertEqual(clb.format_date(self.DAY, "en-150"), self.DAY_FIRST)

    def test_un_m49_world_is_day_first(self):
        """en-001, "international English": month-first is the US habit."""
        self.assertEqual(clb.format_date(self.DAY, "en-001"), self.DAY_FIRST)

    def test_other_numeric_areas_fall_back_to_month_first(self):
        self.assertEqual(clb.format_date(self.DAY, "en-021"), self.US)

    def test_private_use_subtags_are_ignored(self):
        """The `gb` in en-x-gb is a private label, not the UK."""
        for lang in ("en-x-gb", "en-US-x-gb", "en-u-rg-gbzzzz"):
            with self.subTest(lang=lang):
                self.assertEqual(clb.format_date(self.DAY, lang), self.US)

    def test_a_script_subtag_is_skipped_over(self):
        self.assertEqual(clb.format_date(self.DAY, "en-Latn-GB"), self.DAY_FIRST)

    def test_region_before_a_singleton_still_counts(self):
        self.assertEqual(clb.format_date(self.DAY, "en-GB-x-foo"), self.DAY_FIRST)

    def test_us_and_bare_english_unchanged(self):
        for lang in ("en-US", "en", "EN", "en-us"):
            with self.subTest(lang=lang):
                self.assertEqual(clb.format_date(self.DAY, lang), self.US)

    def test_non_english_with_underscore_is_still_iso(self):
        self.assertEqual(clb.format_date(self.DAY, "de_DE"), "2026-09-20")


class TestLetterMetaNull(unittest.TestCase):
    """`meta:` left empty is the same as leaving it out."""

    def test_meta_null_validates_and_resolves_en_us(self):
        d = good_data()
        d["meta"] = None
        clb.validate_data(d)
        self.assertEqual(clb.resolve_letter(d, today=datetime.date(2026, 9, 20))
                         ["date"], "September 20, 2026")

    def test_meta_lang_null_validates(self):
        d = good_data()
        d["meta"]["lang"] = None
        clb.validate_data(d)

    def test_meta_lang_blank_is_en_us(self):
        d = good_data()
        d["meta"]["lang"] = "  "
        self.assertEqual(clb.build.resolve_lang(d), "en-US")


class TestExplicitLetterFileLabel(unittest.TestCase):
    """LETTER_DATA_FILE is labelled by what the file IS, not 'mine'."""

    def setUp(self):
        self.tmpdir = Path(tempfile.mkdtemp())
        self.default_path = self.tmpdir / "letter_default.yml"
        self.mine_path = self.tmpdir / "letter.yml"
        self._patches = [
            mock.patch.object(clb, "DATA_FILE_DEFAULT", self.default_path),
            mock.patch.object(clb, "DATA_FILE_MINE", self.mine_path),
            mock.patch.object(clb, "ROOT", self.tmpdir),
        ]
        for p in self._patches:
            p.start()
        self._saved = {n: os.environ.pop(n, None)
                       for n in (ENV_RESUME_DATA_SOURCE, ENV_LETTER_DATA_FILE)}

    def tearDown(self):
        for p in self._patches:
            p.stop()
        for f in self.tmpdir.iterdir():
            f.unlink()
        self.tmpdir.rmdir()
        for name, value in self._saved.items():
            if value is None:
                os.environ.pop(name, None)
            else:
                os.environ[name] = value

    def load_explicit(self, path):
        path.write_text(MINIMAL_YAML, encoding="utf-8")
        os.environ[ENV_LETTER_DATA_FILE] = str(path)
        with silenced():
            return clb.load_data()[1]

    def test_the_template_is_default(self):
        self.assertEqual(self.load_explicit(self.default_path), "default")

    def test_your_letter_is_mine(self):
        self.assertEqual(self.load_explicit(self.mine_path), "mine")

    def test_any_other_file_is_explicit(self):
        self.assertEqual(self.load_explicit(self.tmpdir / "acme.yml"), "explicit")


if __name__ == "__main__":
    unittest.main()
