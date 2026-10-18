"""
What validate_data warns about reaches the page as it says.

The header's blank text and an href's invisible characters are warned
about rather than refused (a stricter build must not reject a file that
built), and validate_data tidies the data the build then renders: the
href without its invisible characters, so the PDF's link works; a blank
role, address or contact line left out, rather than printed as an empty
element; a meta.lang that is not a tag marked en-US. These render the
measurement page, which carries the same header as the final one, from
data that went through validate_data, as build() does.
"""

import copy
import sys
import unittest
from pathlib import Path

ROOT = Path(__file__).parent.parent
sys.path.insert(0, str(ROOT / "build"))

import build  # noqa: E402
import build_letter  # noqa: E402

BASE = {
    "name": {"first": "Gaius", "last": "Caesar"},
    "contact": {"address": "Roma", "rows": [
        {"value": "gaius@example.com", "href": "mailto:gaius@example.com"},
        {"value": "example.com/gaius", "href": "https://example.com/gaius"}]},
    "meta": {"description": "D", "maxPages": 2, "lang": "en-US"},
    "sidebar": {"blocks": [
        {"id": "online", "type": "details", "heading": "Online",
         "rows": [{"label": "Web", "value": "x.example",
                   "href": "https://x.example"}]},
        {"id": "skills", "type": "list", "heading": "Skills", "items": ["One"]},
    ]},
    "mainColumn": [
        {"type": "summary", "heading": "Summary", "text": "Hi."},
        {"type": "experience", "heading": "Work", "jobs": [
            {"id": "a-job", "title": "T", "date": "2020", "bullets": ["x"]}]},
        {"type": "education", "heading": "Education", "items": [{"title": "BA"}]},
    ],
}


def render(mutate):
    """(html, warnings) for BASE changed by `mutate`, rendered as build() does."""
    data = copy.deepcopy(BASE)
    mutate(data)
    warnings = build.validate_data(data)
    sections = {s["type"]: s for s in data["mainColumn"]}
    html = build.jinja_env().get_template("measurement.j2").render(
        name=data["name"], role=data.get("role"), contact=data.get("contact"),
        meta=data["meta"], summary=sections["summary"],
        experience=sections["experience"], education=sections["education"],
        sidebar_blocks=data["sidebar"]["blocks"], accent="#000000",
        lang=build.resolve_lang(data))
    return html, warnings


def row(i=1):
    return lambda d: d["contact"]["rows"][i]


class TestHrefsRenderWithoutInvisibleCharacters(unittest.TestCase):

    def test_the_link_is_written_without_them(self):
        for lead in ("\u00a0", "\0", "\u200b\ufeff", "\u3000"):
            with self.subTest(lead=ascii(lead)):
                html, warnings = render(lambda d: row()(d).update(
                    href=lead + "https://example.com/gaius"))
                self.assertIn('<a href="https://example.com/gaius">', html)
                self.assertNotIn(lead, html.split("<body", 1)[1])
                self.assertEqual(len(warnings), 1, warnings)

    def test_a_details_row_link_too(self):
        html, _ = render(lambda d: d["sidebar"]["blocks"][0]["rows"][0].update(
            href="\u00a0https://x.example"))
        self.assertIn('<a href="https://x.example">', html)

    def test_mailto_with_nothing_visible_prints_as_text(self):
        html, warnings = render(lambda d: row(0)(d).update(href="mailto:\u200b"))
        self.assertNotIn('href="mailto:', html)
        self.assertIn("gaius@example.com", html)
        self.assertEqual(len(warnings), 1, warnings)


class TestBlankHeaderTextIsLeftOut(unittest.TestCase):

    def test_blank_role_prints_no_subtitle(self):
        html, _ = render(lambda d: d.update(role="  "))
        self.assertNotIn('class="name-role"', html)
        html, _ = render(lambda d: d.update(role="Consul"))
        self.assertIn('class="name-role"', html)

    def test_blank_address_prints_no_line(self):
        html, _ = render(lambda d: d["contact"].update(address=" "))
        self.assertNotIn("header-contact-address", html)
        self.assertIn("header-contact-item", html)

    def test_blank_contact_value_prints_no_row(self):
        html, _ = render(lambda d: row(0)(d).update(value="  "))
        self.assertEqual(html.count('class="header-contact-item"'), 1)
        self.assertNotIn('href="mailto:', html)


class TestLanguage(unittest.TestCase):

    def test_posix_spelling_is_written_as_a_tag(self):
        html, warnings = render(lambda d: d["meta"].update(lang="en_GB"))
        self.assertIn('<html lang="en-GB">', html)
        self.assertEqual(warnings, [])

    def test_a_locale_name_is_marked_en_us(self):
        for lang in ("en_US.UTF-8", "C", "en US", "en\u2011US"):
            with self.subTest(lang=ascii(lang)):
                html, warnings = render(lambda d: d["meta"].update(lang=lang))
                self.assertIn('<html lang="en-US">', html)
                self.assertEqual(len(warnings), 1, warnings)


class TestOneNamePart(unittest.TestCase):
    """A blank part adds no space to what is made of the name."""

    def test_metadata_and_signature(self):
        for first, last, shown in (("", "Caesar", "Caesar"), ("Gaius ", " ", "Gaius"),
                                   (" Gaius", "Caesar", "Gaius Caesar")):
            with self.subTest(first=first, last=last):
                data = copy.deepcopy(BASE)
                data["name"] = {"first": first, "last": last}
                meta = build.derive_pdf_metadata(data, "en-US", "mine")
                self.assertEqual(meta["title"], f"{shown} — Resume")
                self.assertEqual(meta["author"], shown)
                letter = {"name": data["name"], "letter": {"body": ["Veni."]}}
                self.assertEqual(build_letter.resolve_letter(letter)["signature"], shown)
                meta = build_letter.derive_pdf_metadata(letter, "en-US", "mine")
                self.assertEqual(meta["title"], f"{shown} — Cover Letter")
                self.assertEqual(meta["author"], shown)

    def test_no_name_at_all(self):
        data = copy.deepcopy(BASE)
        data["name"] = {"first": " ", "last": ""}
        meta = build.derive_pdf_metadata(data, "en-US", "mine")
        self.assertEqual((meta["title"], meta["author"]), ("Resume", ""))


if __name__ == "__main__":
    unittest.main()
