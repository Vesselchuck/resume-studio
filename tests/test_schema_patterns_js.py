"""
The schemas' patterns mean the same in an editor as in these tests.

tests/test_yaml_typing.py checks that each schema agrees with the build,
through the jsonschema package, which runs a `pattern` with Python's
`re`. An editor runs it as a JavaScript RegExp, and the two engines
disagree on what `\\s` is: JavaScript counts U+FEFF as a space and
U+001C to U+001F and U+0085 as visible, Python the other way round. So
a pattern written with `\\s` or `\\S` could agree with the build here
and still underline, in VS Code, a bullet the build accepts (or pass
one it refuses). The patterns spell their classes out instead, and this
runs every one of them through Node's RegExp, with and without the `u`
flag, and through `re`, over the characters where the engines differ
and the ones the build strips or counts as blank, and asserts the same
verdict from all three.
"""

import json
import re
import shutil
import subprocess
import sys
import unittest
from pathlib import Path

ROOT = Path(__file__).parent.parent
sys.path.insert(0, str(ROOT / "build"))

import build  # noqa: E402

SCHEMA_DIR = ROOT / "schemas"

#: Evaluates each pattern against each probe, as a JSON matrix:
#: result[flag][pattern][probe], flag "" or "u". A pattern the engine
#: refuses to compile is reported as its error message.
JUDGE = r"""
const { patterns, probes } = JSON.parse(require('fs').readFileSync(0, 'utf8'));
const out = {};
for (const flag of ['', 'u']) {
  out[flag] = patterns.map((p) => {
    let re;
    try { re = new RegExp(p, flag); } catch (e) { return String(e.message); }
    return probes.map((s) => re.test(s));
  });
}
process.stdout.write(JSON.stringify(out));
"""


def _patterns(node):
    if isinstance(node, dict):
        for key, value in node.items():
            if key == "pattern" and isinstance(value, str):
                yield value
            else:
                yield from _patterns(value)
    elif isinstance(node, list):
        for value in node:
            yield from _patterns(value)


def _probes():
    """Strings on which the two engines could part, and the build's own
    edge cases: every character it strips from an href or counts as
    blank, alone, before a link and after mailto:."""
    chars = set(map(chr, range(0x21)))
    chars |= {chr(cp) for cp in range(0x10000)
              if not 0xD800 <= cp <= 0xDFFF
              and (chr(cp).isspace() or build._HREF_LEAD.match(chr(cp)))}
    chars |= {"\x85", "\ufeff", "\u180e", "\u200b", "a", "-", "\U0001F600"}
    probes = ["", "Latin", "https://x.example", "mailto:a@b.c", "tel:+15550100",
              "x.example", "en-US"]
    for ch in sorted(chars):
        probes += [ch, ch * 2, f"a{ch}", ch + "https://x.example",
                   "mailto:" + ch, "tel:" + ch + "1", "https://" + ch]
    # Not text that ends in a line feed: Python's `$` also matches just
    # before one, and JavaScript's does not. That is the anchors, not the
    # classes this is about, and a value ending in a line feed is a YAML
    # block scalar, which no field with a pattern is written as.
    return [s for s in probes if not s.endswith("\n")]


class TestPatternsReadTheSameInJavaScript(unittest.TestCase):

    def test_every_pattern_agrees_with_python(self):
        node = shutil.which("node")
        if not node:
            self.skipTest("node not found, so the patterns cannot be run as JavaScript")
        patterns = []
        for name in ("resume.schema.json", "letter.schema.json",
                     "profile.schema.json"):
            schema = json.loads((SCHEMA_DIR / name).read_text(encoding="utf-8"))
            patterns += [p for p in _patterns(schema) if p not in patterns]
        self.assertTrue(patterns)
        probes = _probes()
        r = subprocess.run([node, "-e", JUDGE], input=json.dumps(
            {"patterns": patterns, "probes": probes}), capture_output=True,
            text=True, encoding="utf-8", timeout=60)
        self.assertEqual(r.returncode, 0, r.stderr)
        js = json.loads(r.stdout)
        for i, pattern in enumerate(patterns):
            python = [re.search(pattern, s) is not None for s in probes]
            for flag in ("", "u"):
                with self.subTest(pattern=pattern, flag=flag):
                    got = js[flag][i]
                    self.assertIsInstance(got, list, f"RegExp refused it: {got}")
                    differ = [ascii(s) for s, a, b in zip(probes, python, got)
                              if a != b]
                    self.assertEqual(differ, [],
                                     "probes on which re and RegExp disagree")


if __name__ == "__main__":
    unittest.main()
