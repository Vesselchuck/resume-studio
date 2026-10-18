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

    def test_one_and_true_are_one_key_and_refused_naming_both(self):
        """`1:` and `true:` are two spellings of one key once read
        (True == 1), and the mapping can hold only one of them: accepted,
        the first value would be dropped exactly as for any duplicate.
        So the pair is refused, and the message names both spellings
        rather than calling `true` a duplicate of itself."""
        self.assertEqual(yaml.safe_load("1: a\ntrue: b\n"), {1: "b"})
        for text, first, second in (("1: a\ntrue: b\n", "1", "True"),
                                    ("false: a\n0: b\n", "False", "0")):
            with self.subTest(text=text):
                with self.assertRaises(yaml.YAMLError) as ctx:
                    _yaml_loader.load(text)
                msg = str(ctx.exception)
                self.assertIn(f"found key {second}, which is read as the same "
                              f"key as {first} (first set on line 1)", msg)


class TestErrorPositions(unittest.TestCase):
    """A YAML error names the file, and the line an editor shows."""

    def error(self, text, name="data/resume.yml"):
        with self.assertRaises(yaml.YAMLError) as ctx:
            _yaml_loader.load(text, name=name)
        return str(ctx.exception)

    def test_a_syntax_error_names_the_file(self):
        msg = self.error("a: [\n")
        self.assertIn('in "data/resume.yml", line 2, column 1', msg)
        self.assertNotIn("<unicode string>", msg)

    def test_line_separators_do_not_shift_the_line(self):
        # YAML counts U+2028, U+2029 and U+0085 as line breaks; an editor
        # does not, so the error was reported a line too far down for each.
        for sep in ("\u2028", "\u2029", "\u0085"):
            with self.subTest(sep=hex(ord(sep))):
                msg = self.error(f'a: "x{sep}y"\nb: [\n')
                self.assertIn('line 3, column 1', msg)

    def test_a_duplicate_key_reports_editor_lines_for_both(self):
        msg = self.error('a: "1\u2028\u2028x"\nb: 2\na: 3\n')
        self.assertIn("first set on line 1", msg)
        self.assertIn('in "data/resume.yml", line 3, column 1', msg)

    def test_a_bom_does_not_shift_the_column(self):
        msg = self.error("\ufeffa: !!int abc\n")
        self.assertIn('line 1, column 4', msg)

    def test_a_reader_error_names_the_file(self):
        msg = self.error("a: \x01\n")
        self.assertIn('"data/resume.yml"', msg)

    def test_a_reader_error_gives_the_editor_line_and_column(self):
        # It gave only "position N", and libyaml counts that in UTF-8
        # bytes: the é and the emoji put it 5 past the character.
        msg = self.error('a: 1\nb: "' + chr(0xe9) + chr(0x1F600) + chr(1) + '"\n')
        self.assertIn('in "data/resume.yml", line 2, column 8', msg)
        self.assertNotIn("position", msg)
        with self.assertRaises(yaml.reader.ReaderError) as ctx:
            _yaml_loader.load(chr(0xfeff) + "a: x" + chr(7) + "\n")
        self.assertIn("line 1, column 5", str(ctx.exception))

    def test_without_a_name_the_position_is_still_corrected(self):
        with self.assertRaises(yaml.YAMLError) as ctx:
            _yaml_loader.load('a: "x\u2028y"\nb: [\n')
        self.assertIn("line 3, column 1", str(ctx.exception))

    def test_read_yaml_names_the_file_it_read(self):
        import tempfile
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "resume.yml"
            path.write_bytes("name: [\n".encode("utf-8"))
            err = io.StringIO()
            with contextlib.redirect_stdout(io.StringIO()), \
                    contextlib.redirect_stderr(err), \
                    self.assertRaises(SystemExit):
                build.read_yaml(path)
        self.assertIn(str(build._shown(path)), err.getvalue())
        self.assertIn("line 2, column 1", err.getvalue())
        self.assertNotIn("<unicode string>", err.getvalue())


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
        what tells you, as you type, that a key in it is misspelled."""
        path = DATA_DIR / build.PROFILE_NAME
        if not path.exists():
            # A clean checkout (CI) has only the shipped template; check
            # that one rather than skipping, so the schema is checked
            # against something on every run.
            path = DATA_DIR / "_profile_default.yml"
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
            "href without a scheme":
                lambda d: block(d, 1)["rows"][0].update(href="x.example"),
            "href host:port":
                lambda d: block(d, 1)["rows"][0].update(href="localhost:3000"),
            "href with nothing after the scheme":
                lambda d: block(d, 1)["rows"][0].update(href="https:"),
            "href of another scheme":
                lambda d: block(d, 1)["rows"][0].update(href="ms-msdt:/id"),
            "contact href of another scheme":
                lambda d: d.update(contact={"rows": [{"value": "v",
                                                      "href": "smb://h/s"}]}),
            "empty contact value":
                lambda d: d.update(contact={"rows": [{"value": ""}]}),
            "empty sidebar heading": lambda d: block(d, 0).update(heading=""),
            "block type as a list": lambda d: block(d, 0).update(type=["list"]),
            # Blank as the build counts it (str.strip), which is not what
            # an editor's \S counts (see test_schema_patterns_js.py).
            "bullet of U+001C": lambda d: job(d).update(bullets=[chr(0x1c)]),
            "bullet of U+0085": lambda d: job(d).update(bullets=[chr(0x85)]),
            "regular job with bullets left empty":
                lambda d: job(d).update(bullets=None),
            "no summary section":
                lambda d: d["mainColumn"].__setitem__(
                    0, {"type": "education", "heading": "E", "items": []}),
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
            "empty href (no link)":
                lambda d: d["sidebar"]["blocks"][1]["rows"][0].update(href=""),
            "contact tel: href":
                lambda d: d.update(contact={"rows": [{"value": "v",
                                                      "href": "tel:+15550100"}]}),
            # Empty means not set (deep_merge), wherever a field is optional.
            "null role": lambda d: d.update(role=None),
            "empty role": lambda d: d.update(role=""),
            "null contact": lambda d: d.update(contact=None),
            "null contact address":
                lambda d: d.update(contact={"address": None, "rows": []}),
            "null contact href":
                lambda d: d.update(contact={"rows": [{"value": "v", "href": None}]}),
            "null details href":
                lambda d: d["sidebar"]["blocks"][1]["rows"][0].update(href=None),
            "gap entry with bullets left empty":
                lambda d: job(d, 2).update(bullets=None),
            "gap entry with a location": lambda d: job(d, 2).update(location="Roma"),
            "one name part empty": lambda d: d["name"].update(first=""),
            "blank meta.lang": lambda d: d["meta"].update(lang="  "),
            "POSIX-style lang": lambda d: d["meta"].update(lang="en_GB"),
            "private-use lang": lambda d: d["meta"].update(lang="x-private"),
            "href after what a browser strips":
                lambda d: d["sidebar"]["blocks"][1]["rows"][0].update(
                    href=chr(1) + " https://x.example"),
            # Warned about, not refused: each built in 1.0.0.
            "href after a no-break space":
                lambda d: d["sidebar"]["blocks"][1]["rows"][0].update(
                    href=chr(0xa0) + "https://x.example"),
            "mailto: with only a zero-width space":
                lambda d: d.update(contact={"rows": [{"value": "v",
                                                      "href": "mailto:" + chr(0x200b)}]}),
            "both name parts blank":
                lambda d: d.update(name={"first": " ", "last": ""}),
            "blank role": lambda d: d.update(role="  "),
            "blank contact address":
                lambda d: d.update(contact={"address": " ", "rows": []}),
            "blank contact value":
                lambda d: d.update(contact={"rows": [{"value": "   "}]}),
            "blank sidebar heading":
                lambda d: d["sidebar"]["blocks"][0].update(heading=" "),
            "lang with a control character":
                lambda d: d["meta"].update(lang="en" + chr(1) + "US"),
            "lang with a space": lambda d: d["meta"].update(lang="en US"),
            "locale name as lang": lambda d: d["meta"].update(lang="en_US.UTF-8"),
            "bullet of U+FEFF": lambda d: job(d).update(bullets=[chr(0xfeff)]),
        }
        for label, mutate in cases.items():
            with self.subTest(case=label):
                doc = self._doc()
                mutate(doc)
                self.assertEqual(self._verdicts(doc), (True, True),
                                 "(schema accepts?, validator accepts?)")

    def test_every_href_pattern_agrees_with_the_validator(self):
        """Each schema's `href` pattern is the build's allowlist, found
        wherever it appears, so a link an editor accepts builds."""
        hrefs = ["", "https://x.example", "http://x.example/a", "HTTPS://X.EXAMPLE",
                 " https://x.example ", "mailto:a@b.c", "tel:+15550100",
                 "https:", "https://", "mailto:", "tel: ", "  ", "x.example",
                 "//x.example", "a@b.c", "localhost:3000", "x.example:8080/a",
                 "javascript:alert(1)", "file:///etc/passwd", "about:blank",
                 "smb://h/s", "search-ms:query=x", "ms-msdt:/id", "ftp://x.example",
                 "C:\\Users\\me"]
        # A browser strips U+0000 to U+0020 from a URL and nothing else, so
        # a leading no-break space, U+3000 or U+2028 makes it relative.
        hrefs += [chr(cp) + "https://x.example" for cp in (0x00, 0x01, 0x1f, 0xa0,
                                                            0x2028, 0x3000, 0xfeff)]
        hrefs += [chr(0xa0) + "mailto:a@b.c", "https://x.example" + chr(0xa0)]
        # Stripped with a warning, a dropped link, or still refused.
        hrefs += [chr(0x200b) + chr(0x2060) + "https://x.example", chr(0x200b),
                  chr(0x200b) + "x.example", "ht" + chr(0) + "tps://x.example",
                  "https://x.example/a" + chr(0) + "b", "mailto:" + chr(0x200b),
                  "tel:" + chr(0xa0) + chr(0x2060), "mailto:" + chr(0),
                  chr(0x180e) + chr(0x1c) + "tel:+15550100"]

        def href_schemas(node):
            if isinstance(node, dict):
                for key, value in node.items():
                    if key == "href" and isinstance(value, dict):
                        yield value
                    else:
                        yield from href_schemas(value)
            elif isinstance(node, list):
                for value in node:
                    yield from href_schemas(value)

        for name in ("resume.schema.json", "letter.schema.json",
                     "profile.schema.json"):
            found = list(href_schemas(self.schema(name)["properties"]))
            found += list(href_schemas(self.schema(name).get("definitions", {})))
            self.assertTrue(found, f"{name} has an href")
            for sub in found:
                for href in hrefs:
                    with self.subTest(schema=name, href=href):
                        schema_ok = not list(self.Draft7Validator(sub).iter_errors(href))
                        try:
                            build._check_href(href, "row")
                            validator_ok = True
                        except build.SchemaError:
                            validator_ok = False
                        self.assertEqual(schema_ok, validator_ok,
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

    def test_document_meta_without_description_is_schema_valid(self):
        """Likewise meta.description: the profile may supply it, so the
        document's schema does not require it (empty or absent). The
        build still requires one after the merge."""
        schema = self.schema("resume.schema.json")
        for meta in ({"maxPages": 2}, {"description": None, "maxPages": 2}, None):
            with self.subTest(meta=meta):
                doc = self._doc()
                doc["meta"] = meta
                self.assertEqual(list(self.Draft7Validator(schema).iter_errors(doc)), [])
                with self.assertRaises(build.SchemaError):
                    build.validate_data(doc)

    def test_synthetic_profile_leniencies_agree(self):
        """What the build accepts from a profile, its schema accepts.

        The profile is merged under a document and validated as part of
        it, so an unknown top-level or meta key is a warning there, and a
        key left empty is not set."""
        def profile():
            return {"name": {"first": "Gaius", "last": "Caesar"},
                    "contact": {"address": "Roma", "rows": [
                        {"value": "v", "href": "tel:+15550100"}]},
                    "meta": {"lang": "en-US", "maxPages": 2}}

        doc = self._doc()
        for key in ("name", "contact"):
            doc.pop(key, None)
        doc["meta"].pop("maxPages")
        cases = {
            "unknown top-level key": lambda p: p.update(extra=1),
            "unknown meta key": lambda p: p["meta"].update(extra=1),
            "one name part empty": lambda p: p["name"].update(first=""),
            "null meta.lang": lambda p: p["meta"].update(lang=None),
            "null contact": lambda p: p.update(contact=None),
            "null address": lambda p: p["contact"].update(address=None),
            "null href": lambda p: p["contact"]["rows"][0].update(href=None),
            "null role": lambda p: p.update(role=None),
            "description fallback": lambda p: p["meta"].update(description="D"),
            # Warned about, not refused: each built in 1.0.0.
            "both name parts blank":
                lambda p: p.update(name={"first": "", "last": " "}),
            "lang with a control character":
                lambda p: p["meta"].update(lang="en" + chr(1) + "US"),
            "blank role": lambda p: p.update(role=" "),
            "blank address": lambda p: p["contact"].update(address=" "),
            "blank contact value":
                lambda p: p["contact"]["rows"][0].update(value=" "),
        }
        schema = self.schema("profile.schema.json")
        for label, mutate in cases.items():
            with self.subTest(case=label):
                p = profile()
                mutate(p)
                self.assertEqual(list(self.Draft7Validator(schema).iter_errors(p)), [])
                build.validate_data(build.deep_merge(p, doc))  # should not raise

    def test_synthetic_profile_rejections_agree(self):
        def profile():
            return {"name": {"first": "Gaius", "last": "Caesar"},
                    "meta": {"lang": "en-US", "maxPages": 2}}

        doc = self._doc()
        doc.pop("name")
        doc["meta"].pop("lang")         # so the profile's is the one used
        cases = {
            "unknown name key": lambda p: p["name"].update(middle="J"),
            "empty contact value":
                lambda p: p.update(contact={"rows": [{"value": ""}]}),
            "href without a scheme":
                lambda p: p.update(contact={"rows": [{"value": "v",
                                                      "href": "x.example"}]}),
        }
        schema = self.schema("profile.schema.json")
        for label, mutate in cases.items():
            with self.subTest(case=label):
                p = profile()
                mutate(p)
                self.assertNotEqual(list(self.Draft7Validator(schema).iter_errors(p)), [])
                with self.assertRaises(build.SchemaError):
                    build.validate_data(build.deep_merge(p, doc))

    def test_schema_does_not_promise_italics(self):
        """markdown_filter only knows **bold**."""
        text = json.dumps(self.schema("resume.schema.json"))
        self.assertNotIn("*italic*", text)

    # ── The letter, the same way ────────────────────────────────

    @staticmethod
    def _letter():
        """A letter that both the schema and the validator accept."""
        return {
            "name": {"first": "Gaius", "last": "Caesar"},
            "contact": {"address": "Roma", "rows": [
                {"value": "x.example", "href": "https://x.example"},
                {"value": "No link"}]},
            "meta": {"description": "D", "lang": "en-GB"},
            "letter": {"recipient": ["Senate", "Curia Julia"],
                       "body": ["Dear Senate,", "Veni.", "Sincerely,"]},
        }

    def _letter_verdicts(self, doc):
        schema = self.schema("letter.schema.json")
        schema_ok = not list(self.Draft7Validator(schema).iter_errors(doc))
        try:
            build_letter.validate_data(doc)
            validator_ok = True
        except build.SchemaError:
            validator_ok = False
        return schema_ok, validator_ok

    def test_synthetic_letter_is_accepted_by_both(self):
        self.assertEqual(self._letter_verdicts(self._letter()), (True, True))

    def test_synthetic_letter_rejections_agree(self):
        """Blank text was accepted by the schema (minLength counts
        spaces) and refused by the build; an unknown key in name,
        contact or a row was refused by the schema and only warned
        about by the build, while the résumé refused the same block."""
        def letter(d):
            return d["letter"]

        def row(d):
            return d["contact"]["rows"][0]

        cases = {
            "blank paragraph": lambda d: letter(d).update(body=["Dear", "  "]),
            "blank body block": lambda d: letter(d).update(body=" \n\n "),
            "blank recipient line":
                lambda d: letter(d).update(recipient=["Senate", " "]),
            "blank recipient block": lambda d: letter(d).update(recipient="  "),
            "letter.date": lambda d: letter(d).update(date="Ides of March"),
            "letter.salutation": lambda d: letter(d).update(salutation="Hi"),
            "unknown name key": lambda d: d["name"].update(middle="J"),
            "unknown contact key": lambda d: d["contact"].update(adress="x"),
            "unknown row key": lambda d: row(d).update(hre="https://x.example"),
            "empty row value": lambda d: row(d).update(value=""),
        }
        for label, mutate in cases.items():
            with self.subTest(case=label):
                doc = self._letter()
                mutate(doc)
                self.assertEqual(self._letter_verdicts(doc), (False, False),
                                 "(schema accepts?, validator accepts?)")

    def test_synthetic_letter_leniencies_agree(self):
        """Keys the letter only warns about stay out of the schema's
        way, as the résumé's top level, meta and sections do."""
        cases = {
            "unknown letter key": lambda d: d["letter"].update(recipent=["x"]),
            "unknown top-level key": lambda d: d.update(extra=1),
            "unknown meta key": lambda d: d["meta"].update(maxPages=1),
            "block body": lambda d: d["letter"].update(body="Dear,\n\nVeni."),
            "no recipient": lambda d: d["letter"].pop("recipient"),
            # Empty means not set (deep_merge).
            "null recipient": lambda d: d["letter"].update(recipient=None),
            "null role": lambda d: d.update(role=None),
            "null contact": lambda d: d.update(contact=None),
            "null address": lambda d: d["contact"].update(address=None),
            "null href": lambda d: d["contact"]["rows"][0].update(href=None),
            "null meta": lambda d: d.update(meta=None),
            "null meta.description":
                lambda d: d["meta"].update(description=None),
            "one name part empty": lambda d: d["name"].update(last=""),
            # Warned about, not refused: each built in 1.0.0.
            "blank row value": lambda d: d["contact"]["rows"][0].update(value="  "),
            "both name parts blank":
                lambda d: d.update(name={"first": "", "last": ""}),
            "blank role": lambda d: d.update(role=" "),
            "blank address": lambda d: d["contact"].update(address=" "),
            "lang with a control character":
                lambda d: d["meta"].update(lang="en" + chr(1) + "GB"),
        }
        for label, mutate in cases.items():
            with self.subTest(case=label):
                doc = self._letter()
                mutate(doc)
                self.assertEqual(self._letter_verdicts(doc), (True, True),
                                 "(schema accepts?, validator accepts?)")

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
