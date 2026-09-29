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


class TestErrorLocation(unittest.TestCase):
    """A validation error names the file and line the bad value is on.

    The validators see the merged data; build.explain_schema_error walks
    the message's path back through the document and the profile the way
    deep_merge combined them, so a value the profile supplied is blamed
    on the profile, not on a document that never mentions it.
    """

    DOC = (
        "meta:\n"
        "  description: A resume\n"
        "sidebar:\n"
        "  blocks:\n"
        "    - id: skills\n"
        "      type: list\n"
        "      heading: Skills\n"
        "      items: [Filing]\n"
        "mainColumn:\n"
        "  - type: summary\n"
        "    heading: Summary\n"
        "    text: Hello.\n"
        "  - type: experience\n"
        "    heading: Work\n"
        "    jobs:\n"
        "      - id: a-job\n"
        "        title: Clerk\n"
        "        date: \"2020\"\n"
        "        bullets: [Did the thing.]\n"
        "  - type: education\n"
        "    heading: Education\n"
        "    items: [{title: BA}]\n"
    )
    PROFILE = (
        "name:\n"
        "  first: Gaius\n"
        "  last: Caesar\n"
        "meta:\n"
        "  lang: en-US\n"
        "  maxPages: 2\n"
    )

    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp())
        self.doc = self.tmp / "resume.yml"
        self.prof = self.tmp / build.PROFILE_NAME

    def explain(self, doc, profile=PROFILE):
        self.doc.write_text(doc, encoding="utf-8")
        self.prof.write_text(profile, encoding="utf-8")
        with silenced():
            data = build.apply_profile(build.read_yaml(self.doc), self.doc)
        with self.assertRaises(build.SchemaError) as ctx:
            build.validate_data(data)
        return build.explain_schema_error(ctx.exception)

    @staticmethod
    def line_of(text, needle):
        return text.splitlines().index(needle) + 1

    def test_a_value_in_the_document_is_located_there(self):
        doc = self.DOC.replace("title: Clerk", "title: \"\"")
        said = self.explain(doc)
        line = self.line_of(doc, "        title: \"\"")
        self.assertTrue(said.startswith(f"{self.doc}:{line}:9: "), said)
        self.assertIn("jobs[0]: 'title' is blank", said)
        self.assertNotIn(build.PROFILE_NAME, said)

    def test_a_value_from_the_profile_is_blamed_on_the_profile(self):
        said = self.explain(self.DOC, self.PROFILE.replace("maxPages: 2", "maxPages: 0"))
        line = self.line_of(self.PROFILE, "  maxPages: 2")
        self.assertTrue(said.startswith(f"{self.prof}:{line}:3: "), said)
        self.assertIn(f"from {self.prof}", said)

    def test_the_document_overriding_the_profile_is_blamed(self):
        doc = self.DOC.replace("  description: A resume\n",
                               "  description: A resume\n  maxPages: 0\n")
        said = self.explain(doc)
        self.assertTrue(said.startswith(f"{self.doc}:3:3: "), said)
        self.assertNotIn(build.PROFILE_NAME, said)

    def test_a_section_is_found_by_its_type(self):
        doc = self.DOC.replace("    heading: Education\n", "    heading: \"\"\n")
        said = self.explain(doc)
        line = self.line_of(doc, "    heading: \"\"")
        self.assertTrue(said.startswith(f"{self.doc}:{line}:5: "), said)

    def test_a_missing_top_level_key_names_the_file(self):
        doc = self.DOC.replace("sidebar:", "sidebars:")
        said = self.explain(doc)
        self.assertTrue(said.startswith(f"{self.doc}: missing top-level key"), said)

    def test_the_text_that_was_parsed_is_located_not_the_file_now(self):
        doc = self.DOC.replace("title: Clerk", "title: \"\"")
        self.doc.write_text(doc, encoding="utf-8")
        self.prof.write_text(self.PROFILE, encoding="utf-8")
        with silenced():
            data = build.apply_profile(build.read_yaml(self.doc), self.doc)
        self.doc.write_text("# saved again meanwhile\n" * 5 + doc, encoding="utf-8")
        with self.assertRaises(build.SchemaError) as ctx:
            build.validate_data(data)
        line = self.line_of(doc, "        title: \"\"")
        self.assertTrue(build.explain_schema_error(ctx.exception)
                        .startswith(f"{self.doc}:{line}:9: "))

    def test_a_value_merged_in_with_an_anchor_is_the_documents(self):
        # `<<: *m` is still this file: the profile, which also has a
        # maxPages, must not be blamed for the document's own 0.
        doc = self.DOC.replace("meta:\n", "base: &m\n  maxPages: 0\nmeta:\n  <<: *m\n", 1)
        said = self.explain(doc)
        self.assertTrue(said.startswith(f"{self.doc}:2:3: "), said)
        self.assertNotIn(build.PROFILE_NAME, said)

    def test_lines_are_counted_as_an_editor_counts_them(self):
        # U+2028/U+2029/U+0085 are line breaks to YAML but not to an
        # editor; a BOM and CRLF are nothing; columns are UTF-16 units.
        doc = self.DOC.replace("title: Clerk", "title: \"\"").replace(
            "text: Hello.", "text: \"Hel\u2028lo\u2029 \u0085there\"")
        line = self.line_of(doc.replace("\u2028", "").replace("\u2029", "")
                            .replace("\u0085", ""), "        title: \"\"")
        self.doc.write_bytes(("\ufeff" + doc).replace("\n", "\r\n").encode("utf-8"))
        self.prof.write_text(self.PROFILE, encoding="utf-8")
        with silenced():
            data = build.apply_profile(build.read_yaml(self.doc), self.doc)
        with self.assertRaises(build.SchemaError) as ctx:
            build.validate_data(data)
        said = build.explain_schema_error(ctx.exception)
        self.assertTrue(said.startswith(f"{self.doc}:{line}:9: "), said)
        text = "a: 1\nb: [\"\U0001F600\", x]\n"
        node = build._yaml_loader.compose(text).value[1][1].value[1]
        self.assertEqual(build._editor_position(text, node.start_mark), (2, 11))  # 10 in code points

    def test_without_a_load_the_message_is_unchanged(self):
        self.assertEqual(build.explain_schema_error(build.SchemaError("x"), merge=[]),
                         "x")


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
