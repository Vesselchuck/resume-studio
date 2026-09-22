"""
Tests for the shared profile — build.deep_merge and build.apply_profile.

data/_profile.yml holds what is true of you regardless of which job
you are applying for: name, contact details, document language. Every
build merges it underneath the document it is building, so a phone
number lives in one file instead of four.

The merge is implicit — no document names the profile — which puts the
burden of proof here. Three properties matter, and each has its own
class below:

  SAFETY      A profile that duplicates what a document already says
              must change nothing. This is what makes the file safe to
              add before migrating anything, and what makes a
              half-migrated data/ directory produce the same PDF as a
              fully-migrated one.

  PRECEDENCE  The document always wins. If the two disagree, the
              document's value is the one that renders — otherwise a
              file could not override its own contact block.

  ISOLATION   A profile applies to the files beside it. A document
              loaded from elsewhere via RESUME_DATA_FILE must not
              silently inherit this project's profile.
"""

import contextlib
import io
import sys
import tempfile
import unittest
from pathlib import Path

ROOT = Path(__file__).parent.parent
sys.path.insert(0, str(ROOT / "build"))

import build  # noqa: E402


@contextlib.contextmanager
def silenced():
    """apply_profile announces itself through _console; hush it."""
    with contextlib.redirect_stdout(io.StringIO()) as out, \
            contextlib.redirect_stderr(io.StringIO()):
        yield out


class TestDeepMerge(unittest.TestCase):
    def test_override_wins_on_scalars(self):
        self.assertEqual(
            build.deep_merge({"a": 1, "b": 2}, {"b": 3}),
            {"a": 1, "b": 3},
        )

    def test_nested_mappings_merge_key_by_key(self):
        """meta.lang from the profile, meta.description from the document."""
        merged = build.deep_merge(
            {"meta": {"lang": "en-US"}},
            {"meta": {"description": "A resume"}},
        )
        self.assertEqual(merged["meta"], {"lang": "en-US", "description": "A resume"})

    def test_lists_replace_rather_than_concatenate(self):
        """The contact.rows case.

        Concatenating would give a document four rows when it asked for
        one, in an order nobody chose, and would make overriding a
        single row impossible to express.
        """
        merged = build.deep_merge(
            {"contact": {"rows": [{"value": "a"}, {"value": "b"}]}},
            {"contact": {"rows": [{"value": "z"}]}},
        )
        self.assertEqual(merged["contact"]["rows"], [{"value": "z"}])

    def test_a_mapping_can_be_replaced_by_a_scalar(self):
        """Type changes take the override, rather than half-merging."""
        self.assertEqual(build.deep_merge({"a": {"b": 1}}, {"a": "x"}), {"a": "x"})

    def test_null_does_not_wipe_a_profile_mapping(self):
        """`meta:` with nothing under it is 'not set', not 'delete'.

        It used to replace the profile's whole meta block, so the build
        then failed on a missing maxPages the document never mentioned,
        and lost the profile's meta.lang.
        """
        merged = build.deep_merge(
            {"meta": {"lang": "en-GB", "maxPages": 2}},
            {"meta": None, "role": "X"},
        )
        self.assertEqual(merged["meta"], {"lang": "en-GB", "maxPages": 2})

    def test_null_does_not_wipe_a_nested_profile_value(self):
        merged = build.deep_merge(
            {"meta": {"lang": "en-GB"}},
            {"meta": {"lang": None, "description": "D"}},
        )
        self.assertEqual(merged["meta"], {"lang": "en-GB", "description": "D"})

    def test_null_with_nothing_underneath_is_kept(self):
        """No profile value to fall back on: the document's null stays."""
        self.assertEqual(build.deep_merge({"a": 1}, {"b": None}),
                         {"a": 1, "b": None})

    def test_inputs_are_not_mutated(self):
        """The profile dict is reused across two builds in the warm engine."""
        base = {"name": {"first": "Gaius"}}
        override = {"name": {"last": "Caesar"}}
        build.deep_merge(base, override)
        self.assertEqual(base, {"name": {"first": "Gaius"}})
        self.assertEqual(override, {"name": {"last": "Caesar"}})


class TestResolveLang(unittest.TestCase):
    """build.resolve_lang: one answer for both the HTML and the PDF."""

    def test_meta_null_does_not_crash(self):
        self.assertEqual(build.resolve_lang({"meta": None}), "en-US")

    def test_meta_absent(self):
        self.assertEqual(build.resolve_lang({}), "en-US")

    def test_whitespace_only_lang_falls_back(self):
        """lang="" in the HTML while crop_pdf stamped en-US was the bug."""
        self.assertEqual(build.resolve_lang({"meta": {"lang": "   "}}), "en-US")

    def test_lang_is_stripped(self):
        self.assertEqual(build.resolve_lang({"meta": {"lang": " en-GB "}}), "en-GB")

    def test_null_lang(self):
        self.assertEqual(build.resolve_lang({"meta": {"lang": None}}), "en-US")

    def test_document_meta_null_keeps_profile_lang(self):
        merged = build.deep_merge({"meta": {"lang": "fr-FR"}}, {"meta": None})
        self.assertEqual(build.resolve_lang(merged), "fr-FR")


class TestApplyProfile(unittest.TestCase):
    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp())
        self.doc = self.tmp / "resume.yml"
        self.doc.write_text("unused: true\n", encoding="utf-8")

    def profile(self, text):
        (self.tmp / build.PROFILE_NAME).write_text(text, encoding="utf-8")

    def test_no_profile_is_a_no_op(self):
        data = {"name": {"first": "Gaius", "last": "Caesar"}}
        with silenced():
            self.assertEqual(build.apply_profile(data, self.doc), data)

    def test_profile_fills_what_the_document_omits(self):
        self.profile("name:\n  first: Gaius\n  last: Caesar\nmeta:\n  lang: en-US\n")
        with silenced():
            merged = build.apply_profile({"meta": {"maxPages": 2}}, self.doc)
        self.assertEqual(merged["name"], {"first": "Gaius", "last": "Caesar"})
        self.assertEqual(merged["meta"], {"lang": "en-US", "maxPages": 2})

    def test_document_wins(self):
        """The property that makes migration safe one file at a time."""
        self.profile("name:\n  first: Gaius\n  last: Caesar\n")
        with silenced():
            merged = build.apply_profile(
                {"name": {"first": "Gaius", "last": "Caesar"}}, self.doc)
        self.assertEqual(merged["name"], {"first": "Gaius", "last": "Caesar"})

    def test_duplicate_profile_changes_nothing(self):
        """A profile identical to what every document already says is inert.

        This is the state right after the file is created and before
        anything is migrated, so it had better be a no-op.
        """
        self.profile("name:\n  first: Gaius\n  last: Caesar\n")
        data = {"name": {"first": "Gaius", "last": "Caesar"}, "role": "Cook"}
        with silenced():
            self.assertEqual(build.apply_profile(data, self.doc), data)

    def test_empty_profile_is_tolerated(self):
        """A file you have created but not filled in yet is not an error."""
        self.profile("# nothing here yet\n")
        data = {"role": "Cook"}
        with silenced():
            self.assertEqual(build.apply_profile(data, self.doc), data)

    def test_non_mapping_profile_fails_loudly(self):
        self.profile("- one\n- two\n")
        with silenced(), self.assertRaises(SystemExit):
            build.apply_profile({}, self.doc)

    def test_profile_beside_the_document_not_beside_the_project(self):
        """ISOLATION.

        A file rendered from somewhere else via RESUME_DATA_FILE gets
        the profile next to *it*, or none — never this project's,
        which would put your name on someone else's document.
        """
        elsewhere = Path(tempfile.mkdtemp()) / "other.yml"
        elsewhere.write_text("unused: true\n", encoding="utf-8")
        self.profile("name:\n  first: Gaius\n  last: Caesar\n")
        with silenced():
            merged = build.apply_profile({"role": "Cook"}, elsewhere)
        self.assertNotIn("name", merged)

    def test_the_merge_is_announced(self):
        """The log line is the whole justification for an implicit merge.

        It names the dotted path of every value the profile supplied,
        recursing into mappings so 'meta.lang' shows up even when the
        document has its own meta block.
        """
        self.profile("name:\n  first: Gaius\n  last: Caesar\nmeta:\n  lang: en-US\n")
        with silenced() as out:
            build.apply_profile({"meta": {"maxPages": 2}}, self.doc)
        logged = out.getvalue()
        self.assertIn(build.PROFILE_NAME, logged)
        self.assertIn("name", logged)
        self.assertIn("meta.lang", logged)

    def test_contributions_recurse_into_mappings(self):
        contributions = build._profile_contributions(
            {"name": {"first": "Gaius"}, "meta": {"lang": "en-US"}},
            {"meta": {"maxPages": 2}},
        )
        self.assertEqual(contributions, ["meta.lang", "name"])

    def test_contributions_count_a_null_as_missing(self):
        """The log must name what the profile filled in for a `meta:` left
        empty, since the merge now fills it."""
        contributions = build._profile_contributions(
            {"meta": {"lang": "en-US", "maxPages": 2}},
            {"meta": {"lang": None}},
        )
        self.assertEqual(contributions, ["meta.lang", "meta.maxPages"])

    def test_document_meta_null_keeps_the_profile_meta(self):
        self.profile("meta:\n  lang: en-GB\n  maxPages: 2\n")
        with silenced() as out:
            merged = build.apply_profile({"meta": None}, self.doc)
        self.assertEqual(merged["meta"], {"lang": "en-GB", "maxPages": 2})
        self.assertIn("meta", out.getvalue())

    def test_document_contact_rows_keep_the_profile_address(self):
        """What resume.schema.json's contact description now says."""
        self.profile("contact:\n  address: Roma\n  rows:\n    - value: a\n")
        with silenced():
            merged = build.apply_profile(
                {"contact": {"rows": [{"value": "z"}]}}, self.doc)
        self.assertEqual(merged["contact"],
                         {"address": "Roma", "rows": [{"value": "z"}]})


class TestMigratedDocumentValidates(unittest.TestCase):
    """A document stripped down to its own content still passes validation
    once the profile is merged in — which is the end state of migrating."""

    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp())
        self.doc = self.tmp / "resume.yml"
        self.doc.write_text("unused: true\n", encoding="utf-8")
        (self.tmp / build.PROFILE_NAME).write_text(
            "name:\n"
            "  first: Gaius\n"
            "  last: Caesar\n"
            "contact:\n"
            "  address: Springfield, Illinois\n"
            "  rows:\n"
            "    - value: gaius@example.com\n"
            "      href: \"mailto:gaius@example.com\"\n"
            "meta:\n"
            "  lang: en-US\n",
            encoding="utf-8",
        )

    def test_document_without_identity_fields_validates_after_merge(self):
        stripped = {
            "role": "Administrative Assistant",
            "meta": {"description": "A resume", "maxPages": 2},
            "sidebar": {"blocks": [
                {"id": "skills", "type": "list", "heading": "Skills",
                 "items": ["Filing"]},
            ]},
            "mainColumn": [
                {"type": "summary", "heading": "Summary", "text": "Hello."},
                {"type": "experience", "heading": "Work", "jobs": [
                    {"id": "a-job", "title": "Clerk", "date": "2020",
                     "bullets": ["Did the thing."]},
                ]},
                {"type": "education", "heading": "Education",
                 "items": [{"title": "BA"}]},
            ],
        }
        # Without the profile it is incomplete, and the build says so.
        with self.assertRaises(build.SchemaError):
            build.validate_data(stripped)

        with silenced():
            merged = build.apply_profile(stripped, self.doc)
        build.validate_data(merged)          # raises if the merge fell short
        self.assertEqual(merged["name"]["last"], "Caesar")
        self.assertEqual(merged["meta"]["lang"], "en-US")
        self.assertEqual(merged["meta"]["maxPages"], 2)


class TestTwoWorlds(unittest.TestCase):
    """The template never merges your profile.

    The template (resume_default.yml, letter_default.yml) is what the
    committed snapshot fixtures are rendered from. If it merged your
    real _profile.yml, any key it omitted would be filled with your real
    data and written into a file git tracks. These tests pin the rule
    that makes that impossible: each document merges the profile from
    its own world.
    """

    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp())
        (self.tmp / build.PROFILE_NAME).write_text(
            "name:\n  first: Real\n  last: Person\n"
            "contact:\n  address: Real Street 1\n  rows: []\n",
            encoding="utf-8")
        (self.tmp / build.DEFAULT_PROFILE_NAME).write_text(
            "name:\n  first: Gaius\n  last: Caesar\n"
            "contact:\n  address: Roma\n  rows: []\n",
            encoding="utf-8")

    def merged(self, doc_name, data):
        doc = self.tmp / doc_name
        doc.write_text("unused: true\n", encoding="utf-8")
        with silenced():
            return build.apply_profile(data, doc)

    def test_your_documents_merge_your_profile(self):
        for name in ("resume.yml", "letter.yml"):
            with self.subTest(doc=name):
                self.assertEqual(self.merged(name, {})["name"]["first"], "Real")

    def test_template_documents_merge_the_template_profile(self):
        for name in ("resume_default.yml", "letter_default.yml"):
            with self.subTest(doc=name):
                self.assertEqual(self.merged(name, {})["name"]["first"], "Gaius")

    def test_a_template_with_gaps_is_never_filled_from_your_profile(self):
        # The failure this exists to prevent: a template that omits
        # contact details must not borrow yours.
        merged = self.merged("resume_default.yml", {"meta": {"maxPages": 2}})
        flat = repr(merged)
        self.assertNotIn("Real", flat)
        self.assertNotIn("Real Street", flat)

    def test_a_template_without_a_template_profile_merges_nothing(self):
        (self.tmp / build.DEFAULT_PROFILE_NAME).unlink()
        data = {"meta": {"maxPages": 2}}
        self.assertEqual(self.merged("resume_default.yml", data), data)

    def test_the_rule_is_the_file_name_not_the_folder(self):
        self.assertEqual(build.profile_for(self.tmp / "resume_default.yml").name,
                         build.DEFAULT_PROFILE_NAME)
        self.assertEqual(build.profile_for(self.tmp / "resume.yml").name,
                         build.PROFILE_NAME)
        # "default" elsewhere in the name does not count.
        self.assertEqual(build.profile_for(self.tmp / "default_resume.yml").name,
                         build.PROFILE_NAME)


if __name__ == "__main__":
    unittest.main()
