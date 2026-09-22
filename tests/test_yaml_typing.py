"""
Tests for build/_yaml_loader.py — the project's single YAML entry point.

Two things are pinned here, and they fail for different reasons.

TYPING. PyYAML implements YAML 1.1, whose implicit resolvers turn a
surprising amount of ordinary résumé text into something other than
text: `no` into False, `2024-01-05` into a date, `22:30` into 1350,
`3.90` into 3.9. _yaml_loader resolves scalars the way YAML 1.2's core
schema does instead, minus floats and timestamps. Every one of those
behaviors is asserted below, because they are invisible until the day
a language disappears from a résumé or a GPA rounds itself down, and a
future "let's just use safe_load, it's simpler" would restore all of
them silently.

AGREEMENT. schemas/*.schema.json exists to give an editor completion
and inline errors. It is advisory — build.validate_data() is the
authority — but an advisory schema that disagrees with the authority
is worse than none, so the last class here loads every real file in
data/ and asserts both accept it.
"""

import contextlib
import io
import json
import sys
import unittest
from pathlib import Path

import yaml

ROOT = Path(__file__).parent.parent
sys.path.insert(0, str(ROOT / "build"))

import _yaml_loader  # noqa: E402
import build  # noqa: E402
import build_letter  # noqa: E402

SCHEMA_DIR = ROOT / "schemas"
DATA_DIR = ROOT / "data"


class TestImplicitTyping(unittest.TestCase):
    """The YAML 1.1 footguns, each one pinned to its fixed behavior."""

    def load(self, text):
        return _yaml_loader.load(text)

    def test_norway_problem(self):
        """`no` is the ISO code for Norwegian, not False.

        The canonical YAML bug: a languages list containing `no` loses
        the entry entirely under YAML 1.1, because it resolves to a
        boolean and renders as 'False'.
        """
        got = self.load("langs: [no, yes, on, off, y, n, No, YES, Off]")
        self.assertEqual(
            got["langs"],
            ["no", "yes", "on", "off", "y", "n", "No", "YES", "Off"],
        )
        for value in got["langs"]:
            self.assertIsInstance(value, str)

    def test_real_booleans_still_parse(self):
        """The 1.2 core booleans are kept, so `gap: true` still works.

        This is why the bool resolver was replaced rather than dropped:
        dropping it would have made every `gap: true` a truthy string
        and every `gap: false` a truthy string too.
        """
        got = self.load("a: true\nb: false\nc: True\nd: FALSE\n")
        self.assertIs(got["a"], True)
        self.assertIs(got["b"], False)
        self.assertIs(got["c"], True)
        self.assertIs(got["d"], False)

    def test_dates_stay_strings(self):
        """A date in a résumé is display text, not a datetime.

        Before this, `date: 2026-09-16` produced a datetime.date, which
        validate_data then rejected with "must be a string if provided"
        — a true statement and a baffling one.
        """
        got = self.load("date: 2024-01-05\nstamp: 2024-01-05T09:00:00Z\n")
        self.assertEqual(got["date"], "2024-01-05")
        self.assertEqual(got["stamp"], "2024-01-05T09:00:00Z")
        self.assertIsInstance(got["date"], str)
        self.assertIsInstance(got["stamp"], str)

    def test_sexagesimal_stays_a_string(self):
        """YAML 1.1 reads `22:30` as base-60. It is a time, or a score."""
        got = self.load("shift: 22:30\nscore: 1:2:3\n")
        self.assertEqual(got["shift"], "22:30")
        self.assertEqual(got["score"], "1:2:3")

    def test_trailing_zeros_survive(self):
        """`3.90` must print as 3.90. Nobody rounds their own GPA down."""
        got = self.load("gpa: 3.90\nversion: 1.10\nsci: 1e5\n")
        self.assertEqual(got["gpa"], "3.90")
        self.assertEqual(got["version"], "1.10")
        self.assertEqual(got["sci"], "1e5")

    def test_integers_still_parse(self):
        """meta.maxPages is validated with isinstance(int) — keep ints."""
        got = self.load("pages: 10\nneg: -7\nhexv: 0x1f\n")
        self.assertEqual(got["pages"], 10)
        self.assertIsInstance(got["pages"], int)
        self.assertNotIsInstance(got["pages"], bool)
        self.assertEqual(got["neg"], -7)
        self.assertEqual(got["hexv"], 31)

    def test_leading_zeros_stay_strings(self):
        """A leading-zero number is an identifier (a ZIP, a room number).

        YAML 1.1 read `010` as octal 8 and `0451` as 297 — silently —
        and `09` / `02139` as a ValueError traceback, because PyYAML's
        int constructor hands anything starting with 0 to int(x, 8).
        """
        got = self.load("a: 0451\nb: 09\nc: 010\nzip: 02139\nneg: -07\n")
        self.assertEqual(got, {"a": "0451", "b": "09", "c": "010",
                               "zip": "02139", "neg": "-07"})
        for value in got.values():
            self.assertIsInstance(value, str)

    def test_plain_decimals_are_base_ten(self):
        got = self.load("zero: 0\nn: 42\nneg: -5\nplus: +3\nmz: -0\n")
        self.assertEqual(got, {"zero": 0, "n": 42, "neg": -5, "plus": 3, "mz": 0})
        for value in got.values():
            self.assertIs(type(value), int)

    def test_hex_and_octal_prefixes(self):
        """The YAML 1.2 core forms, unsigned, as in the spec."""
        got = self.load("h: 0x1f\no: 0o17\nsigned: -0x1f\n")
        self.assertEqual(got["h"], 31)
        self.assertEqual(got["o"], 15)
        self.assertEqual(got["signed"], "-0x1f")

    def test_explicit_int_tag_is_decimal(self):
        """`!!int 010` asks for an int, and gets the YAML 1.2 one: ten."""
        self.assertEqual(self.load("a: !!int 010\n")["a"], 10)

    def test_explicit_int_tag_on_text_is_an_error(self):
        with self.assertRaises(yaml.YAMLError):
            self.load("a: !!int abc\n")

    def test_nulls_still_parse(self):
        """Optional fields are commonly left empty; that must stay None."""
        got = self.load("a: null\nb: ~\nc:\nd: NULL\n")
        for key in "abcd":
            self.assertIsNone(got[key], f"{key!r} should be None")

    def test_explicit_tags_still_work(self):
        """Nothing is taken from someone who asks for it by name."""
        got = self.load(
            "f: !!float 3.90\n"
            "d: !!timestamp 2024-01-05\n"
            "b: !!bool yes\n"
        )
        self.assertIsInstance(got["f"], float)
        self.assertEqual(got["f"], 3.9)
        self.assertEqual(str(got["d"]), "2024-01-05")
        self.assertIs(got["b"], True)

    def test_global_safe_load_is_untouched(self):
        """The resolver surgery must not leak into the rest of Python.

        `yaml_implicit_resolvers` is a plain class attribute. Mutating
        the inherited dict in place instead of copying it would re-type
        every other yaml.safe_load() in the process, including ones
        inside libraries that never asked for any of this.
        """
        stray = yaml.safe_load("langs: [no]\ngpa: 3.90\n")
        self.assertEqual(stray["langs"], [False])
        self.assertEqual(stray["gpa"], 3.9)


class TestDuplicateKeys(unittest.TestCase):
    """PyYAML keeps the last of two identical keys; this loader refuses."""

    def test_duplicate_key_is_an_error_naming_key_and_lines(self):
        text = ("jobs:\n"
                "  - id: a\n"
                "    bullets: [one]\n"
                "    title: T\n"
                "    bullets: [two]\n")
        with self.assertRaises(yaml.YAMLError) as ctx:
            _yaml_loader.load(text)
        msg = str(ctx.exception)
        self.assertIn("duplicate key 'bullets'", msg)
        self.assertIn("first set on line 3", msg)
        self.assertIn("line 5", msg)

    def test_duplicate_top_level_key(self):
        with self.assertRaises(yaml.YAMLError) as ctx:
            _yaml_loader.load("meta: {lang: en}\nname: x\nmeta: {lang: fr}\n")
        self.assertIn("'meta'", str(ctx.exception))

    def test_duplicate_in_flow_mapping(self):
        with self.assertRaises(yaml.YAMLError):
            _yaml_loader.load("a: {x: 1, x: 2}\n")

    def test_same_key_in_different_mappings_is_fine(self):
        got = _yaml_loader.load("- {id: a}\n- {id: b}\n")
        self.assertEqual(got, [{"id": "a"}, {"id": "b"}])

    def test_overriding_a_merged_key_is_not_a_duplicate(self):
        got = _yaml_loader.load("base: &b {x: 1, y: 2}\nd:\n  <<: *b\n  x: 3\n")
        self.assertEqual(got["d"], {"x": 3, "y": 2})

    def test_int_and_string_keys_are_distinct(self):
        got = _yaml_loader.load('1: a\n"1": b\n')
        self.assertEqual(got, {1: "a", "1": "b"})

    def test_global_safe_load_still_merges_silently(self):
        """The override lives on ResumeLoader only."""
        self.assertEqual(yaml.safe_load("a: 1\na: 2\n"), {"a": 2})


class TestLoaderWiring(unittest.TestCase):
    """The loader is actually the one being used, and it is the fast one."""

    def test_libyaml_is_available(self):
        """Informational, not a correctness requirement.

        The pure-Python fallback is ~15x slower but behaves identically,
        so this is a skip rather than a failure — it exists so a machine
        that quietly lost the C extension says so out loud instead of
        just getting slower.
        """
        if not _yaml_loader.USING_LIBYAML:
            self.skipTest(
                "libyaml (yaml.CSafeLoader) not available on this Python; "
                "falling back to the pure-Python parser, which is correct "
                "but roughly 15x slower"
            )

    def test_builders_use_the_shared_loader(self):
        """Guard against a future edit reaching for yaml.safe_load again."""
        for module in (build, build_letter):
            source = Path(module.__file__).read_text(encoding="utf-8")
            self.assertNotIn(
                "yaml.safe_load", source,
                f"{Path(module.__file__).name} calls yaml.safe_load directly; "
                f"use _yaml_loader.load so typing stays consistent",
            )


class TestGapIsABoolean(unittest.TestCase):
    """`gap` decides whether bullets are required; a string would lie."""

    def _resume(self, gap_value):
        return {
            "name": {"first": "Gaius", "last": "Caesar"},
            "meta": {"description": "Test", "maxPages": 2},
            "sidebar": {"blocks": [
                {"id": "skills", "type": "list", "heading": "Skills",
                 "items": ["One"]},
            ]},
            "mainColumn": [
                {"type": "summary", "heading": "Summary", "text": "Hi."},
                {"type": "experience", "heading": "Work", "jobs": [
                    {"id": "a-job", "title": "T", "date": "2020",
                     "gap": gap_value},
                ]},
                {"type": "education", "heading": "Education", "items": [
                    {"title": "BA"},
                ]},
            ],
        }

    def test_unquoted_true_is_accepted(self):
        build.validate_data(self._resume(True))

    def test_quoted_true_is_rejected(self):
        """'true' is a non-empty string, and every non-empty string is
        truthy — so the quoted form would mark a gap entry silently."""
        with self.assertRaises(build.SchemaError) as ctx:
            build.validate_data(self._resume("true"))
        self.assertIn("gap", str(ctx.exception))

    def test_quoted_false_is_rejected(self):
        """The dangerous one: it reads as 'not a gap' and behaves as one."""
        with self.assertRaises(build.SchemaError) as ctx:
            build.validate_data(self._resume("false"))
        self.assertIn("gap", str(ctx.exception))

    def test_yaml_no_is_rejected_rather_than_silently_false(self):
        """`gap: no` used to be False. Now it is the string 'no', and
        the type check turns that into a clear error instead of a
        gap entry that mysteriously demands bullets."""
        parsed = _yaml_loader.load("gap: no\n")
        self.assertEqual(parsed["gap"], "no")
        with self.assertRaises(build.SchemaError):
            build.validate_data(self._resume(parsed["gap"]))


class TestSchemasAgreeWithValidators(unittest.TestCase):
    """The editor schema and the build validator must accept the same files."""

    @classmethod
    def setUpClass(cls):
        try:
            from jsonschema import Draft7Validator
        except ImportError:
            raise unittest.SkipTest(
                "jsonschema not installed; the schemas are editor-only and "
                "the build does not need it. pip install jsonschema to run "
                "these checks."
            )
        cls.Draft7Validator = Draft7Validator

    def schema(self, name):
        path = SCHEMA_DIR / name
        self.assertTrue(path.exists(), f"missing schema: {path}")
        return json.loads(path.read_text(encoding="utf-8"))

    def test_schemas_are_valid_draft7(self):
        for name in ("resume.schema.json", "letter.schema.json",
                     "profile.schema.json"):
            with self.subTest(schema=name):
                self.Draft7Validator.check_schema(self.schema(name))

    def test_shared_profile_matches_its_schema(self):
        """The profile has no Python validator of its own — it is merged
        into a document and validated as part of it — so the schema is
        the only thing that will tell you a key is misspelled."""
        path = DATA_DIR / build.PROFILE_NAME
        if not path.exists():
            self.skipTest(f"no {build.PROFILE_NAME} in data/")
        data = _yaml_loader.load(path.read_text(encoding="utf-8"))
        errors = sorted(
            self.Draft7Validator(self.schema("profile.schema.json"))
                .iter_errors(data),
            key=lambda e: list(e.path),
        )
        if errors:
            first = errors[0]
            where = ".".join(str(p) for p in first.path) or "(root)"
            self.fail(f"{path.name} at {where}: {first.message}")

    def _data_files(self, kind):
        """Real data files of one kind, by the same globs .vscode uses."""
        if not DATA_DIR.exists():
            return []
        if kind == "resume":
            return sorted(DATA_DIR.glob("resume*.yml"))
        # letter*.yml covers letter.yml and letter_default.yml.
        return sorted(DATA_DIR.glob("letter*.yml"))

    def _assert_agrees(self, path, schema, validator):
        """
        Check each half against the thing it is actually responsible for.

        The two halves do not see the same document, and conflating
        them is how this test first went wrong:

          THE SCHEMA judges the file as written, because that is all
          an editor has. A migrated document legitimately has no
          `name` — it comes from data/_profile.yml — which is exactly
          why `name` is not in the schema's `required` list.

          THE VALIDATOR judges what the build validates, which is the
          document after the profile is merged into it. Running it on
          the raw file asserts a rule the build does not have, and
          fails the moment a document is migrated.
        """
        raw = _yaml_loader.load(path.read_text(encoding="utf-8"))

        errors = sorted(
            self.Draft7Validator(schema).iter_errors(raw),
            key=lambda e: list(e.path),
        )
        if errors:
            first = errors[0]
            where = ".".join(str(p) for p in first.path) or "(root)"
            self.fail(
                f"{path.name} is accepted by the build but rejected by the "
                f"schema at {where}: {first.message}\n"
                f"The schema is advisory — fix schemas/, not the data."
            )

        # apply_profile announces itself through _console; hush it so a
        # passing run stays quiet.
        with contextlib.redirect_stdout(io.StringIO()):
            merged = build.apply_profile(raw, path)
        validator(merged)

    def test_resume_files_agree(self):
        schema = self.schema("resume.schema.json")
        files = self._data_files("resume")
        if not files:
            self.skipTest("no resume data files present")
        for path in files:
            with self.subTest(file=path.name):
                self._assert_agrees(path, schema, build.validate_data)

    # ── Synthetic cases: one per validation rule ─────────────────
    #
    # The real files above only prove the schema accepts what the build
    # accepts TODAY. These pin each rule in both directions, so a rule
    # added to one side and not the other fails here rather than in an
    # editor that underlines a file the build is happy with (or worse,
    # stays quiet about one the build will reject).

    @staticmethod
    def _doc():
        """A document that both the schema and the validator accept."""
        return {
            "name": {"first": "Gaius", "last": "Caesar"},
            "meta": {"description": "D", "maxPages": 2, "lang": "en-US"},
            "sidebar": {"blocks": [
                {"id": "skills", "type": "list", "heading": "Skills",
                 "items": [{"group": "G"}, "One", 2024]},
                {"id": "online", "type": "details", "heading": "Online",
                 "rows": [{"label": "Web", "value": "x.example",
                           "href": "https://x.example"},
                          {"label": "", "value": "y.example"}]},
            ]},
            "mainColumn": [
                {"type": "summary", "heading": "Summary", "text": "Hi."},
                {"type": "experience", "heading": "Work", "jobs": [
                    {"id": "a-job", "title": "T", "date": "2020",
                     "bullets": ["Did **one** thing"]},
                    {"id": "b-job", "title": "T", "date": 2019,
                     "datetime": 2019, "location": "Roma",
                     "bullets": ["x"]},
                    {"id": "gap", "title": "Gap", "date": "2018",
                     "gap": True},
                ]},
                {"type": "education", "heading": "Education", "items": [
                    {"title": "BA"},
                    {"title": "MA", "subtitle": 2015, "institution": "U"},
                ]},
            ],
        }

    def _verdicts(self, doc):
        schema = self.schema("resume.schema.json")
        schema_ok = not list(self.Draft7Validator(schema).iter_errors(doc))
        try:
            build.validate_data(doc)
            validator_ok = True
        except build.SchemaError:
            validator_ok = False
        return schema_ok, validator_ok

    def test_synthetic_valid_document_is_accepted_by_both(self):
        self.assertEqual(self._verdicts(self._doc()), (True, True))
        self.assertEqual(build.validate_data(self._doc()), [])

    def test_synthetic_rejections_agree(self):
        def job(d, i=0):
            return d["mainColumn"][1]["jobs"][i]

        def block(d, i):
            return d["sidebar"]["blocks"][i]

        cases = {
            "job without title": lambda d: job(d).pop("title"),
            "job without date": lambda d: job(d).pop("date"),
            "job with unknown key": lambda d: job(d).update(locaton="x"),
            "gap entry with bullets":
                lambda d: job(d, 2).update(bullets=["x"]),
            "bullet read as a mapping":
                lambda d: job(d).update(bullets=[{"Led": "cut 30%"}]),
            "empty bullet": lambda d: job(d).update(bullets=[None]),
            "blank bullet": lambda d: job(d).update(bullets=["  "]),
            "boolean bullet": lambda d: job(d).update(bullets=[True]),
            "items as one string":
                lambda d: block(d, 0).update(items="Latin, Greek"),
            "empty items": lambda d: block(d, 0).update(items=[]),
            "list without items": lambda d: block(d, 0).pop("items"),
            "list item mapping without group":
                lambda d: block(d, 0).update(items=[{"Latin": "native"}]),
            "empty rows": lambda d: block(d, 1).update(rows=[]),
            "row value not text":
                lambda d: block(d, 1).update(rows=[{"label": "a",
                                                    "value": ["b"]}]),
            "row without label":
                lambda d: block(d, 1).update(rows=[{"value": "b"}]),
            "section without heading":
                lambda d: d["mainColumn"][0].pop("heading"),
            "summary without text": lambda d: d["mainColumn"][0].pop("text"),
            "education without items":
                lambda d: d["mainColumn"][2].pop("items"),
            "education item without title":
                lambda d: d["mainColumn"][2]["items"][0].pop("title"),
            "maxPages true": lambda d: d["meta"].update(maxPages=True),
            "maxPages zero": lambda d: d["meta"].update(maxPages=0),
            "unknown name key": lambda d: d["name"].update(middle="J"),
        }
        for label, mutate in cases.items():
            with self.subTest(case=label):
                doc = self._doc()
                mutate(doc)
                self.assertEqual(self._verdicts(doc), (False, False),
                                 "(schema accepts?, validator accepts?)")

    def test_synthetic_leniencies_agree(self):
        """Things that built before and must keep building."""
        def job(d, i=0):
            return d["mainColumn"][1]["jobs"][i]

        cases = {
            "job without datetime": lambda d: job(d).pop("datetime", None),
            "null datetime": lambda d: job(d).update(datetime=None),
            "null location": lambda d: job(d, 1).update(location=None),
            "education item without subtitle or institution":
                lambda d: d["mainColumn"][2]["items"][1].pop("subtitle"),
            "empty education list":
                lambda d: d["mainColumn"][2].update(items=[]),
            "null meta.lang": lambda d: d["meta"].update(lang=None),
            "gap entry with an empty bullets list":
                lambda d: job(d, 2).update(bullets=[]),
            "contact with empty rows":
                lambda d: d.update(contact={"address": "A", "rows": []}),
        }
        for label, mutate in cases.items():
            with self.subTest(case=label):
                doc = self._doc()
                mutate(doc)
                self.assertEqual(self._verdicts(doc), (True, True),
                                 "(schema accepts?, validator accepts?)")

    def test_document_contact_without_rows_is_schema_valid(self):
        """The schema judges the file as written: a document may set only
        `address` and inherit `rows` from the profile, so `rows` is not
        required there. The build still requires it after the merge."""
        doc = self._doc()
        doc["contact"] = {"address": "Roma"}
        schema = self.schema("resume.schema.json")
        self.assertEqual(list(self.Draft7Validator(schema).iter_errors(doc)), [])
        with self.assertRaises(build.SchemaError):
            build.validate_data(doc)

    def test_schema_does_not_promise_italics(self):
        """markdown_filter only knows **bold**."""
        text = json.dumps(self.schema("resume.schema.json"))
        self.assertNotIn("*italic*", text)

    def test_letter_files_agree(self):
        schema = self.schema("letter.schema.json")
        files = self._data_files("letter")
        if not files:
            self.skipTest("no letter data files present")
        for path in files:
            with self.subTest(file=path.name):
                self._assert_agrees(
                    path, schema, build_letter.validate_data)


if __name__ == "__main__":
    unittest.main()
