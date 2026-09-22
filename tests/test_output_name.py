"""
Tests for _output_name — what the built PDFs are called.

The filename is the first thing a recruiter sees, before the document
opens, and it is also the most fragile part of the deliverable: it
passes through mail clients, HR portals and applicant-tracking systems
that are far less careful with bytes than the PDF's own metadata. So
the stem is reduced to ASCII, and these tests pin down exactly what
that reduction does to a name.

TWO FAILURES ARE WORSE THAN THE OTHERS, AND BOTH ARE SILENT
───────────────────────────────────────────────────────────
Losing a letter. Stripping "José" to "Jos" produces a filename that
looks fine, builds fine, and is somebody else's name. Every accented
character must fold to its base letter, and the atomic ones NFKD
cannot decompose (ø, ß, æ, ł …) must be transliterated by hand or they
vanish the same way — see TestFolding.

Producing an unwritable name. `name.first` is free text in a YAML file;
nothing stops a colon, a slash or a newline from ending up there, and
on Windows any of them makes the build fail at the last step, after
everything else succeeded. See TestFilesystemSafety.

The third class, TestDegenerateInput, covers what happens when there is
no usable name at all — a missing field, an empty string, a name with
no ASCII reading. The build must still have somewhere to write.
"""

import json
import sys
import unittest
from pathlib import Path

ROOT = Path(__file__).parent.parent
sys.path.insert(0, str(ROOT / "build"))

import _output_name as on  # noqa: E402


def stem(first, last, variant="resume"):
    """Shorthand: the stem for a {first, last} name."""
    return on.stem({"first": first, "last": last}, variant)


class TestTheConvention(unittest.TestCase):
    """The shape the user asked for: First_Last_Resume."""

    def test_resume_stem(self):
        self.assertEqual(stem("Gaius", "Caesar"), "Gaius_Caesar_Resume")

    def test_cover_letter_stem(self):
        self.assertEqual(stem("Gaius", "Caesar", "letter"),
                         "Gaius_Caesar_Cover_Letter")

    def test_color_takes_the_bare_stem(self):
        # The color PDF is the one that gets attached to applications,
        # so it gets the clean name; only grayscale is suffixed.
        self.assertEqual(on.color_pdf("dist", "Gaius_Caesar_Resume").name,
                         "Gaius_Caesar_Resume.pdf")
        self.assertEqual(on.grayscale_pdf("dist", "Gaius_Caesar_Resume").name,
                         "Gaius_Caesar_Resume_Grayscale.pdf")

    def test_both_variants_share_one_stem(self):
        # Not decoration: the app pairs them by stem, and pruneStale
        # in _output_name.js keeps whichever two the build produced.
        s = stem("Gaius", "Caesar")
        color = on.color_pdf("dist", s).name
        gray = on.grayscale_pdf("dist", s).name
        self.assertTrue(gray.startswith(color[:-len(".pdf")]))

    def test_unknown_variant_is_refused(self):
        with self.assertRaises(ValueError):
            on.stem({"first": "A", "last": "B"}, "invoice")


class TestFolding(unittest.TestCase):
    """Every letter survives, in some ASCII form."""

    def test_accents_fold_rather_than_strip(self):
        self.assertEqual(stem("José", "Álvarez"), "Jose_Alvarez_Resume")
        self.assertEqual(stem("Zoë", "Brontë"), "Zoe_Bronte_Resume")
        self.assertEqual(stem("François", "Noël"), "Francois_Noel_Resume")

    def test_letters_nfkd_cannot_decompose_are_transliterated(self):
        # These are atomic code points, not base + combining mark.
        # Without the explicit table they would be dropped entirely,
        # which is the silent letter-loss this module exists to avoid.
        self.assertEqual(stem("Søren", "Kierkegaard"), "Soren_Kierkegaard_Resume")
        self.assertEqual(stem("Lech", "Wałęsa"), "Lech_Walesa_Resume")
        self.assertEqual(stem("Strauß", "Hoffmann"), "Strauss_Hoffmann_Resume")
        self.assertEqual(stem("Æthelred", "Þorsteinn"), "Aethelred_Thorsteinn_Resume")

    def test_no_accented_character_disappears(self):
        # A property test over the Latin-1 supplement, which is where
        # European names live. Every letter must contribute at least
        # one ASCII character to the result.
        for code in range(0x00C0, 0x0180):
            ch = chr(code)
            if not ch.isalpha():
                continue
            folded = on.slug_part(f"A{ch}B")
            self.assertTrue(
                len(folded) >= 3,
                f"U+{code:04X} ({ch!r}) vanished: {folded!r}")

    def test_case_is_preserved(self):
        # The name is the name; this is not a slug for a URL.
        self.assertEqual(stem("Gaius", "IULIUS"), "Gaius_IULIUS_Resume")


class TestPunctuationInNames(unittest.TestCase):
    """Real surnames are not [A-Za-z]+."""

    def test_apostrophes_are_dropped_not_separated(self):
        # O_Brien reads like two names. OBrien reads like one.
        self.assertEqual(stem("Seamus", "O'Brien"), "Seamus_OBrien_Resume")
        self.assertEqual(stem("Seamus", "O’Brien"), "Seamus_OBrien_Resume")

    def test_hyphens_and_spaces_become_the_separator(self):
        self.assertEqual(stem("Anne-Marie", "Smith Jones"),
                         "Anne_Marie_Smith_Jones_Resume")

    def test_particles_survive(self):
        self.assertEqual(stem("Ludwig", "van der Berg"),
                         "Ludwig_van_der_Berg_Resume")

    def test_runs_collapse_and_edges_are_trimmed(self):
        self.assertEqual(on.slug_part("  --Gaius--  "), "Gaius")
        self.assertEqual(on.slug_part("A....B"), "A_B")

    def test_a_name_cannot_impersonate_the_variant_suffix(self):
        # GRAYSCALE_SUFFIX is '_Grayscale' — the same separator the name
        # parts use. That is only safe because DOC_SUFFIX always ends
        # the stem, so the marker can never be mistaken for part of a
        # name, however the name is spelled.
        for surname in ("Grayscale", "Smith-Jones", "Grayscale Jones"):
            with self.subTest(surname=surname):
                s = stem("Anne", surname)
                self.assertTrue(s.endswith("_Resume"),
                                f"{s!r} does not end with the document suffix")
                self.assertFalse(s.endswith(on.GRAYSCALE_SUFFIX),
                                 f"{s!r} looks like a grayscale filename")


class TestFilesystemSafety(unittest.TestCase):
    """Nothing the YAML can hold may produce an unwritable name."""

    def test_windows_forbidden_characters_never_survive(self):
        for ch in '\\/:*?"<>|':
            with self.subTest(ch=ch):
                self.assertNotIn(ch, stem("A" + ch + "B", "C"))

    def test_control_characters_and_newlines_never_survive(self):
        result = stem("Gaius\nIulius\tCaesar\x00", "Rex")
        for ch in result:
            self.assertTrue(ch.isprintable(), f"{ch!r} in {result!r}")
        self.assertNotIn(" ", result)

    def test_path_traversal_cannot_be_expressed(self):
        # `name.first` is free text. If a stem could contain a
        # separator, the build would write outside dist/.
        self.assertNotIn("/", stem("../../etc", "passwd"))
        self.assertNotIn("\\", stem("..\\..\\Windows", "System32"))
        self.assertNotIn("..", stem("..", ".."))

    def test_long_names_are_capped_per_part(self):
        # Windows' 260-character path limit is the real constraint.
        long_stem = stem("A" * 200, "B" * 200)
        self.assertLessEqual(len(long_stem), 2 * on.MAX_PART + 32)

    def test_the_cap_does_not_leave_a_trailing_separator(self):
        # Cutting mid-run would otherwise yield "Foo_..._" + "_Resume".
        raw = ("x" * (on.MAX_PART - 1)) + "  yyy"
        self.assertFalse(on.slug_part(raw).endswith("_"))
        self.assertNotIn("__", stem(raw, "Caesar"))


class TestDegenerateInput(unittest.TestCase):
    """There is always somewhere to write."""

    def test_missing_last_name_drops_out_cleanly(self):
        self.assertEqual(on.stem({"first": "Prince"}, "resume"),
                         "Prince_Resume")
        self.assertNotIn("__", on.stem({"first": "Prince"}, "resume"))

    def test_no_name_at_all_falls_back_to_the_document(self):
        self.assertEqual(on.stem({}, "resume"), "Resume")
        self.assertEqual(on.stem({}, "letter"), "Cover_Letter")
        self.assertEqual(on.stem(None, "resume"), "Resume")

    def test_empty_and_whitespace_names_are_treated_as_absent(self):
        self.assertEqual(stem("", ""), "Resume")
        self.assertEqual(stem("   ", "\t"), "Resume")

    def test_non_string_name_fields_do_not_crash(self):
        # YAML 1.2 core typing means a bare `last: 1984` is an int.
        self.assertEqual(on.stem({"first": "Gaius", "last": 42}, "resume"),
                         "Gaius_Resume")

    def test_a_name_with_no_ascii_reading_keeps_its_characters(self):
        # Folding a CJK name to nothing and shipping "Resume.pdf" is
        # not an acceptable answer for the person whose name it is.
        result = stem("李", "世民")
        self.assertIn("李", result)
        self.assertTrue(result.endswith("_Resume"))


class TestMixedScriptNames(unittest.TestCase):
    """A part that would lose letters to folding is kept whole.

    The ASCII pass used to keep only the Latin half: "Zoë 山田" became
    "Zoe", "Petrov-Смирнов" became "Petrov" — a file named after half
    a person.
    """

    def test_latin_and_cjk_in_one_part(self):
        self.assertEqual(on.slug_part("Zoë 山田"), "Zoë_山田")

    def test_latin_and_cyrillic_in_one_part(self):
        self.assertEqual(on.slug_part("Petrov-Смирнов"), "Petrov-Смирнов")

    def test_the_full_stem(self):
        self.assertEqual(
            on.stem({"first": "Zoë 山田", "last": "Petrov-Смирнов"}, "resume"),
            "Zoë_山田_Petrov-Смирнов_Resume")

    def test_pure_latin_parts_are_still_folded(self):
        self.assertEqual(on.slug_part("Zoë"), "Zoe")
        self.assertEqual(on.stem({"first": "Zoë", "last": "山田"}, "resume"),
                         "Zoe_山田_Resume")

    def test_mixed_script_stays_filesystem_safe(self):
        got = on.slug_part('Zoë/山田: "x"*?<>|\x07')
        for ch in '\\/:*?"<>|\x07':
            self.assertNotIn(ch, got)
        self.assertIn("山田", got)

    def test_mixed_script_is_capped(self):
        got = on.slug_part("Zoë " + "山" * 200)
        self.assertLessEqual(len(got), on.MAX_PART)

    def test_symbols_alone_do_not_trigger_the_fallback(self):
        """Only letters count: an emoji or a dash is not a lost letter."""
        self.assertEqual(on.slug_part("Ann – Lee ★"), "Ann_Lee")


class TestMetadataRoundTrip(unittest.TestCase):
    """The stem travels to Node through the build metadata."""

    def test_stem_is_read_back_from_a_metadata_file(self):
        import tempfile
        with tempfile.TemporaryDirectory() as tmp:
            meta = Path(tmp) / "pdf_meta.json"
            meta.write_text(json.dumps({"output_stem": "Gaius_Caesar_Resume"}),
                            encoding="utf-8")
            self.assertEqual(on.stem_from_meta(meta, "resume"),
                             "Gaius_Caesar_Resume")

    def test_a_missing_metadata_file_yields_the_document_suffix(self):
        # The bootstrap case: nothing has been built, so the fallback
        # only ever names a file that is correctly reported as absent.
        self.assertEqual(on.stem_from_meta(ROOT / "nope.json", "resume"),
                         "Resume")
        self.assertEqual(on.stem_from_meta(ROOT / "nope.json", "letter"),
                         "Cover_Letter")

    def test_a_corrupt_metadata_file_does_not_raise(self):
        import tempfile
        with tempfile.TemporaryDirectory() as tmp:
            meta = Path(tmp) / "pdf_meta.json"
            meta.write_text("{ not json", encoding="utf-8")
            self.assertEqual(on.stem_from_meta(meta, "resume"), "Resume")

    def test_a_metadata_file_without_the_field_does_not_raise(self):
        # An older build wrote this file before output_stem existed.
        import tempfile
        with tempfile.TemporaryDirectory() as tmp:
            meta = Path(tmp) / "pdf_meta.json"
            meta.write_text(json.dumps({"title": "x"}), encoding="utf-8")
            self.assertEqual(on.stem_from_meta(meta, "resume"), "Resume")


class TestAgainstTheRealData(unittest.TestCase):
    """The names in data/ produce usable filenames."""

    def test_every_data_file_yields_a_writable_stem(self):
        import build  # noqa: PLC0415 — deliberately late; needs sys.path
        data_dir = ROOT / "data"
        if not data_dir.is_dir():
            self.skipTest("no data/ directory in this checkout")

        seen = 0
        for path in sorted(data_dir.glob("*.yml")):
            doc = build._yaml_loader.load(path.read_text(encoding="utf-8"))
            if not isinstance(doc, dict) or "name" not in doc:
                continue
            seen += 1
            variant = "letter" if "letter" in doc else "resume"
            result = on.stem(doc["name"], variant)
            with self.subTest(file=path.name):
                self.assertTrue(result, "empty stem")
                for ch in '\\/:*?"<>|':
                    self.assertNotIn(ch, result)
                self.assertTrue(
                    result.endswith(on.DOC_SUFFIX[variant]),
                    f"{result!r} does not end with the document suffix")
        # Guard against this passing by iterating over nothing — the
        # exact failure mode that let a repaired test skip forever.
        self.assertGreater(seen, 0, "no named data files were checked")


if __name__ == "__main__":
    unittest.main()
