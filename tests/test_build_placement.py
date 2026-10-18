"""
Tests for what build.py does with a placement and with a gap entry,
through the real script and the real worker, in a copy of the project.

Two failures these pin, both of which only showed up in a render:

  • A gap entry's `bullets:` left empty is null. validate_data accepts
    it (empty means not set, and a gap entry has no bullets), and the
    job macro then sliced None and the build died with a TypeError.
    build() now hands the template the empty list it means.

  • A placement for other data — a sidebar block or job the data no
    longer has, or a placement.json that is not JSON — failed three
    different ways: a template error, a KeyError the worker reported as
    "internal", a JSON error it reported as a bad request. It is one
    cause, and build.read_placement now fails with it, as StalePlacement:
    exit 1 from the CLI, kind 'stale_placement' from the worker.

ISOLATION
  Everything runs in a throwaway copy (tests/_project.py). Nothing is
  printed by Chromium: dist/styles.css is written by hand, newer than
  the SCSS, which is all build.py checks, and dist/placement.json is
  written by hand too. So this suite needs neither Node nor Chromium,
  and this checkout's dist/ is fingerprinted and must be untouched.
"""

import json
import os
import subprocess
import sys
import unittest
from pathlib import Path

ROOT = Path(__file__).parent.parent
sys.path.insert(0, str(ROOT / "build"))
sys.path.insert(0, str(ROOT / "tests"))

import yaml  # noqa: E402
import _yaml_loader  # noqa: E402
from _project import Project, real_dist_fingerprint  # noqa: E402

FRAME_PREFIX = "\x1e"
TIMEOUT = 120

PROJECT = None
_REAL_DIST = None


def setUpModule():
    global PROJECT, _REAL_DIST
    _REAL_DIST = real_dist_fingerprint()
    PROJECT = Project("build-placement-test")
    dist = PROJECT.root / "dist"
    dist.mkdir()
    # Newer than every .scss in the copy: check_stylesheet_freshness
    # compares modification times, and nothing here looks at the CSS.
    (dist / "styles.css").write_text("/* test */\n", encoding="utf-8")


def tearDownModule():
    if PROJECT is not None:
        PROJECT.remove()
    assert real_dist_fingerprint() == _REAL_DIST, \
        "this checkout's dist/ was written during the suite"


def template_data():
    """The shipped résumé with its profile merged in: a self-contained file."""
    data_dir = PROJECT.root / "data"
    doc = _yaml_loader.load((data_dir / "resume_default.yml").read_text(encoding="utf-8"))
    profile = _yaml_loader.load((data_dir / "_profile_default.yml").read_text(encoding="utf-8"))
    for key, value in profile.items():
        if isinstance(value, dict) and isinstance(doc.get(key), dict):
            doc[key] = {**value, **doc[key]}
        else:
            doc.setdefault(key, value)
    return doc


def jobs_of(data):
    return next(s for s in data["mainColumn"] if s["type"] == "experience")["jobs"]


def placement_for(data):
    """One page holding everything: what the solver writes, by hand."""
    sections = []
    for s in data["mainColumn"]:
        entry = {"type": s["type"], "continuation_of": None}
        if s["type"] == "experience":
            entry["jobs"] = [{"job_id": j["id"], "continuation": False,
                              "bullets_offset": 0, "bullets_limit": None}
                             for j in s["jobs"]]
        sections.append(entry)
    return {"pages": [{
        "page_number": 1,
        "sidebar_blocks": [{"block_id": b["id"], "continuation": False,
                            "items_offset": 0, "items_limit": None}
                           for b in data["sidebar"]["blocks"]],
        "main_sections": sections,
    }]}


def write_data(data, name="placement-test.yml"):
    path = PROJECT.root / "data" / name
    path.write_text(yaml.safe_dump(data, allow_unicode=True, sort_keys=False),
                    encoding="utf-8")
    return path


def write_placement(placement):
    path = PROJECT.root / "dist" / "placement.json"
    text = placement if isinstance(placement, str) else json.dumps(placement)
    path.write_text(text, encoding="utf-8")


def run_build(mode, data_file):
    env = {k: v for k, v in os.environ.items()
           if k not in ("RESUME_DATA_FILE", "LETTER_DATA_FILE", "RESUME_DATA_SOURCE")}
    env["RESUME_DATA_FILE"] = str(data_file)
    env["PYTHONIOENCODING"] = "utf-8"
    return subprocess.run(
        [sys.executable, "-B", str(PROJECT.root / "build" / "build.py"), f"--mode={mode}"],
        cwd=str(PROJECT.root), env=env, capture_output=True, text=True,
        encoding="utf-8", errors="replace", timeout=TIMEOUT)


class TestGapEntryWithBulletsLeftEmpty(unittest.TestCase):
    def setUp(self):
        self.data = template_data()
        gaps = [j for j in jobs_of(self.data) if j.get("gap")]
        self.assertTrue(gaps, "the template has no gap entry to test with")
        gaps[0]["bullets"] = None      # `bullets:` with nothing after it
        self.gap_id = gaps[0]["id"]
        self.path = write_data(self.data)

    def test_measurement_build_renders_it(self):
        r = run_build("measurement", self.path)
        self.assertEqual(r.returncode, 0, r.stderr)
        html = (PROJECT.root / "dist" / "index.html").read_text(encoding="utf-8")
        self.assertIn(f'id="{self.gap_id}"', html)

    def test_final_build_renders_it(self):
        write_placement(placement_for(self.data))
        r = run_build("final", self.path)
        self.assertEqual(r.returncode, 0, r.stderr)
        html = (PROJECT.root / "dist" / "index.html").read_text(encoding="utf-8")
        self.assertIn(f'id="{self.gap_id}"', html)


class TestStalePlacementFromTheCli(unittest.TestCase):
    def setUp(self):
        self.data = template_data()
        self.path = write_data(self.data)

    def test_a_placement_for_this_data_builds(self):
        write_placement(placement_for(self.data))
        r = run_build("final", self.path)
        self.assertEqual(r.returncode, 0, r.stderr)

    def test_a_removed_sidebar_block_is_a_stale_placement(self):
        placement = placement_for(self.data)
        placement["pages"][0]["sidebar_blocks"][0]["block_id"] = "no-such-block"
        write_placement(placement)
        r = run_build("final", self.path)
        self.assertEqual(r.returncode, 1)
        self.assertIn("does not match the data just loaded", r.stderr)
        self.assertIn("sidebar block 'no-such-block'", r.stderr)
        self.assertNotIn("Traceback", r.stderr)

    def test_a_removed_job_is_a_stale_placement(self):
        placement = placement_for(self.data)
        placement["pages"][0]["main_sections"][1]["jobs"][0]["job_id"] = "no-such-job"
        write_placement(placement)
        r = run_build("final", self.path)
        self.assertEqual(r.returncode, 1)
        self.assertIn("job 'no-such-job'", r.stderr)
        self.assertNotIn("Traceback", r.stderr)

    def test_a_corrupt_placement_is_a_stale_placement(self):
        for text in ("{not json", "[1, 2]", '{"pages": [{"sidebar_blocks": [1]}]}'):
            with self.subTest(text=text):
                write_placement(text)
                r = run_build("final", self.path)
                self.assertEqual(r.returncode, 1)
                self.assertIn("could not be read", r.stderr)
                self.assertNotIn("Traceback", r.stderr)


class Worker:
    """A worker started from the copy; just enough protocol to ask."""

    def __init__(self):
        env = {k: v for k, v in os.environ.items()
               if k not in ("RESUME_DATA_FILE", "LETTER_DATA_FILE", "RESUME_DATA_SOURCE")}
        self.proc = subprocess.Popen(
            [sys.executable, "-B", str(PROJECT.root / "build" / "worker.py")],
            stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=subprocess.DEVNULL,
            text=True, encoding="utf-8", bufsize=1, cwd=str(PROJECT.root), env=env)
        self.read()
        self.next_id = 0

    def read(self):
        for line in self.proc.stdout:
            if line.startswith(FRAME_PREFIX):
                return json.loads(line[1:])
        raise AssertionError("worker stdout ended before a frame arrived")

    def call(self, **req):
        self.next_id += 1
        req["id"] = self.next_id
        self.proc.stdin.write(json.dumps(req) + "\n")
        self.proc.stdin.flush()
        return self.read()

    def close(self):
        try:
            self.call(op="shutdown")
        except (AssertionError, OSError, ValueError):
            pass
        self.proc.stdin.close()
        self.proc.wait(timeout=TIMEOUT)
        self.proc.stdout.close()


class TestStalePlacementFromTheWorker(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.worker = Worker()

    @classmethod
    def tearDownClass(cls):
        cls.worker.close()

    def setUp(self):
        self.data = template_data()
        self.path = write_data(self.data)

    def build_final(self):
        return self.worker.call(op="build", mode="final",
                                env={"RESUME_DATA_FILE": str(self.path)})

    def test_a_removed_sidebar_block_is_named_as_such(self):
        # It used to be a KeyError, reported as kind 'internal'.
        placement = placement_for(self.data)
        placement["pages"][0]["sidebar_blocks"][0]["block_id"] = "no-such-block"
        write_placement(placement)
        frame = self.build_final()
        self.assertFalse(frame["ok"])
        self.assertEqual(frame["error"]["kind"], "stale_placement")
        self.assertIn("does not match", frame["error"]["message"])
        self.assertTrue(any("placement" in line.lower() for line in frame["error"]["detail"]))

    def test_a_corrupt_placement_is_named_as_such(self):
        # It used to be a JSONDecodeError, reported as kind 'bad_request'.
        write_placement("{not json")
        frame = self.build_final()
        self.assertFalse(frame["ok"])
        self.assertEqual(frame["error"]["kind"], "stale_placement")

    def test_the_worker_stays_up_and_builds_once_it_matches(self):
        write_placement("{not json")
        self.build_final()
        write_placement(placement_for(self.data))
        frame = self.build_final()
        self.assertTrue(frame["ok"], frame.get("error"))


if __name__ == "__main__":
    unittest.main()
