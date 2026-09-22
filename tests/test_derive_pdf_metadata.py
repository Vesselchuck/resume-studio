"""
Tests for build.derive_pdf_metadata — pulls authoritative PDF metadata
fields from the resume data dict.

Coverage targets:
  • Title is "{first} {last} — Resume"
  • Author is "{first} {last}"
  • Subject is meta.description (stripped of surrounding whitespace)
  • Keywords:
      - Pulled from a sidebar block matching id='key-skills' OR
        heading == 'Key Skills' (case-insensitive)
      - Capped at 10 items
      - Non-string items (e.g. {group: ...} group-heading dicts)
        are skipped
      - Empty string when no matching block exists
  • lang and data_source are taken verbatim from the caller's
    arguments — derive_pdf_metadata does no resolution of its own
    (caller is build(), which resolves once and threads through)
  • max_pages: comes from meta.maxPages; KeyError if absent (no default)
"""

import sys
import unittest
from pathlib import Path

ROOT = Path(__file__).parent.parent
sys.path.insert(0, str(ROOT / "build"))

import build  # noqa: E402
from build import derive_pdf_metadata  # noqa: E402


def good_data():
    """Minimal-but-complete fixture covering every metadata branch."""
    return {
        "name": {"first": "Gaius", "last": "Caesar"},
        "meta": {
            "description": "Test resume",
            "maxPages": 7,
            "lang": "en-US",
        },
        "sidebar": {
            "blocks": [
                {
                    "id": "key-skills",
                    "type": "list",
                    "heading": "Key Skills",
                    "items": ["Python", "SQL", "Design"],
                },
            ],
        },
    }


def call(data, lang="en-US", data_source="default"):
    """Test helper: derive_pdf_metadata with sensible defaults for the
    explicit lang/data_source parameters introduced when those were
    lifted out of the function's responsibility (Phase 2, M3/M9)."""
    return derive_pdf_metadata(data, lang, data_source)


class TestDerivePDFMetadata(unittest.TestCase):
    # ── Title / Author ─────────────────────────────────────────────

    def test_title_is_first_last_resume(self):
        m = call(good_data())
        self.assertEqual(m["title"], "Gaius Caesar — Resume")

    def test_author_is_first_last(self):
        m = call(good_data())
        self.assertEqual(m["author"], "Gaius Caesar")

    # ── Subject ────────────────────────────────────────────────────

    def test_subject_comes_from_description(self):
        d = good_data()
        d["meta"]["description"] = "A test resume."
        m = call(d)
        self.assertEqual(m["subject"], "A test resume.")

    def test_subject_strips_surrounding_whitespace(self):
        d = good_data()
        d["meta"]["description"] = "   Test resume\n"
        m = call(d)
        self.assertEqual(m["subject"], "Test resume")

    def test_subject_empty_when_description_missing(self):
        d = good_data()
        del d["meta"]["description"]
        m = call(d)
        self.assertEqual(m["subject"], "")

    def test_subject_empty_when_description_none(self):
        d = good_data()
        d["meta"]["description"] = None
        m = call(d)
        self.assertEqual(m["subject"], "")

    # ── Keywords ───────────────────────────────────────────────────

    def test_keywords_from_id_key_skills(self):
        d = good_data()
        # Has id='key-skills', items are strings.
        m = call(d)
        self.assertEqual(m["keywords"], "Python, SQL, Design")

    def test_keywords_from_heading_case_insensitive(self):
        d = good_data()
        # Match by heading instead of id (heading match is case-
        # insensitive after .lower()).
        d["sidebar"]["blocks"][0]["id"] = "something-else"
        d["sidebar"]["blocks"][0]["heading"] = "KEY SKILLS"
        m = call(d)
        self.assertEqual(m["keywords"], "Python, SQL, Design")

    def test_keywords_skip_group_heading_dicts(self):
        # The 'list' block type allows interleaved group-heading dicts
        # like {group: 'Education'}; those aren't keywords and must be
        # filtered out.
        d = good_data()
        d["sidebar"]["blocks"][0]["items"] = [
            {"group": "Languages"},
            "Python",
            "SQL",
            {"group": "Domains"},
            "Education",
        ]
        m = call(d)
        self.assertEqual(m["keywords"], "Python, SQL, Education")

    def test_keywords_capped_at_ten(self):
        d = good_data()
        d["sidebar"]["blocks"][0]["items"] = [f"Skill{i}" for i in range(15)]
        m = call(d)
        self.assertEqual(
            m["keywords"],
            ", ".join(f"Skill{i}" for i in range(10)),
        )

    def test_keywords_empty_when_no_matching_block(self):
        d = good_data()
        d["sidebar"]["blocks"][0]["id"] = "other"
        d["sidebar"]["blocks"][0]["heading"] = "Other"
        m = call(d)
        self.assertEqual(m["keywords"], "")

    def test_keywords_empty_when_no_sidebar(self):
        # Defensive: bad/missing sidebar shape shouldn't crash; the
        # function should swallow KeyError/TypeError and return ''.
        d = good_data()
        d["sidebar"] = None
        m = call(d)
        self.assertEqual(m["keywords"], "")

    # ── Lang (passed in by caller) ─────────────────────────────────

    def test_lang_passed_through_verbatim(self):
        m = call(good_data(), lang="fr-FR")
        self.assertEqual(m["lang"], "fr-FR")

    def test_lang_is_not_resolved_from_data(self):
        # Even if data['meta']['lang'] disagrees with the explicit
        # argument, the function uses the argument. Resolution is the
        # caller's job (build() calls resolve_lang once and threads
        # through).
        d = good_data()
        d["meta"]["lang"] = "ja-JP"
        m = call(d, lang="en-US")
        self.assertEqual(m["lang"], "en-US")

    # ── data_source (passed in by caller) ──────────────────────────

    def test_data_source_passed_through_verbatim(self):
        m = call(good_data(), data_source="mine")
        self.assertEqual(m["data_source"], "mine")

    def test_explicit_data_source_passed_through(self):
        """snapshot_pdf.py keys its refusal off this exact value."""
        m = call(good_data(), data_source="explicit")
        self.assertEqual(m["data_source"], "explicit")
        self.assertIn("explicit", build.DATA_SOURCES)

    def test_data_source_is_not_read_from_data(self):
        # The function takes data_source as a parameter; any
        # _data_source field on the dict (legacy or otherwise) is
        # ignored. This pins the new contract — load_data() returns
        # (data, source) and the source is threaded explicitly.
        d = good_data()
        d["_data_source"] = "legacy-stamped"  # should be ignored
        m = call(d, data_source="default")
        self.assertEqual(m["data_source"], "default")

    # ── max_pages ──────────────────────────────────────────────────

    def test_max_pages_from_meta_max_pages(self):
        m = call(good_data())
        self.assertEqual(m["max_pages"], 7)

    def test_max_pages_missing_raises_keyerror(self):
        # max_pages has no default. The validator catches this much
        # earlier in the build flow (validate_data requires it), so
        # by the time derive_pdf_metadata runs, it's guaranteed
        # present. This test pins that hard-fail behavior so a
        # regression that adds a silent default would be caught.
        d = good_data()
        del d["meta"]["maxPages"]
        with self.assertRaises(KeyError):
            call(d)


if __name__ == "__main__":
    unittest.main()
