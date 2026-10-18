"""
Tests for build/worker.py's own plumbing: the frame writer, the
per-request environment, the stale-placement report and op_compare's
arithmetic. Each was a way for the worker to go down, leak state into
later builds, or call two different PDFs equal.

Everything here runs in-process against the worker module, except the
one test that needs the real stdout pipe, which starts a worker and
sends it requests that build nothing.
"""

import io
import json
import os
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path
from unittest import mock

ROOT = Path(__file__).parent.parent
sys.path.insert(0, str(ROOT / "build"))

import build  # noqa: E402
import worker  # noqa: E402

FRAME_PREFIX = "\x1e"
LONE_SURROGATE = chr(0xD800)


class TestSend(unittest.TestCase):
    """A frame always reaches the pipe as one line of ASCII JSON."""

    def sent(self, payload):
        stream = io.TextIOWrapper(io.BytesIO(), encoding="utf-8", newline="\n")
        with mock.patch.object(worker, "_FRAME_OUT", stream):
            worker._send(payload)
            stream.flush()
            raw = stream.buffer.getvalue()
        self.assertTrue(raw.startswith(FRAME_PREFIX.encode("ascii")))
        self.assertEqual(raw.count(b"\n"), 1)
        raw.decode("ascii")   # raises if anything is not ASCII
        return json.loads(raw[1:].decode("ascii"))

    def test_a_lone_surrogate_is_escaped_not_fatal(self):
        # It used to raise UnicodeEncodeError on the UTF-8 pipe, outside
        # handle(), and the worker exited.
        frame = self.sent({"id": 1, "op": LONE_SURROGATE, "ok": False})
        self.assertEqual(frame["op"], LONE_SURROGATE)

    def test_text_round_trips(self):
        text = "Caesar — ✅ 漢字 \U0001F600"
        self.assertEqual(self.sent({"id": 1, "log": [text]})["log"], [text])

    def test_a_payload_that_is_not_json_becomes_an_error_frame(self):
        frame = self.sent({"id": 7, "op": "build", "ok": True,
                           "result": {"path": Path("x")}})
        self.assertEqual(frame["id"], 7)
        self.assertFalse(frame["ok"])
        self.assertEqual(frame["error"]["kind"], "internal")

    def test_nan_and_infinity_become_an_error_frame(self):
        # json.dumps wrote them as NaN and Infinity, which JSON.parse on
        # the Node side refuses, so the request never got its answer.
        for value in (float("nan"), float("inf"), float("-inf")):
            with self.subTest(value=value):
                stream = io.TextIOWrapper(io.BytesIO(), encoding="utf-8",
                                          newline="\n")
                with mock.patch.object(worker, "_FRAME_OUT", stream):
                    worker._send({"id": 3, "op": "compare", "ok": True,
                                  "result": {"diff": value}})
                    stream.flush()
                raw = stream.buffer.getvalue()[1:].decode("ascii")

                def refuse(name):
                    raise ValueError(f"{name} is not JSON")

                frame = json.loads(raw, parse_constant=refuse)
                self.assertEqual(frame["id"], 3)
                self.assertFalse(frame["ok"])
                self.assertEqual(frame["error"]["kind"], "internal")


class TestWorkerSurvivesALoneSurrogate(unittest.TestCase):
    """The real pipe: five such requests used to keep the worker down."""

    def test_it_answers_and_keeps_answering(self):
        env = {k: v for k, v in os.environ.items()
               if k not in ("RESUME_DATA_FILE", "LETTER_DATA_FILE", "RESUME_DATA_SOURCE")}
        with tempfile.TemporaryDirectory() as cwd:
            proc = subprocess.Popen(
                [sys.executable, "-B", str(ROOT / "build" / "worker.py")],
                stdin=subprocess.PIPE, stdout=subprocess.PIPE,
                stderr=subprocess.DEVNULL, cwd=cwd, env=env)
            try:
                def read():
                    for line in proc.stdout:
                        if line.startswith(FRAME_PREFIX.encode("ascii")):
                            return json.loads(line[1:].decode("utf-8"))
                    raise AssertionError("the worker exited")

                read()                                  # hello
                # json.dumps escapes the surrogate, as Node's JSON.stringify does.
                proc.stdin.write((json.dumps({"id": 1, "op": LONE_SURROGATE})
                                  + "\n").encode("ascii"))
                proc.stdin.flush()
                frame = read()
                self.assertEqual(frame["error"]["kind"], "unknown_op")
                proc.stdin.write(b'{"id": 2, "op": "ping"}\n')
                proc.stdin.flush()
                self.assertTrue(read()["ok"])
            finally:
                proc.stdin.close()
                proc.wait(timeout=60)
                proc.stdout.close()


class TestEnvOverrides(unittest.TestCase):
    def test_a_refused_value_leaves_nothing_set(self):
        # os.environ refuses a NUL. The variable set before it used to
        # stay set for every later build.
        names = ("RESUME_TEST_OVERRIDE_A", "RESUME_TEST_OVERRIDE_B")
        for name in names:
            os.environ.pop(name, None)
        with self.assertRaises(ValueError):
            with worker._env_overrides({names[0]: "set", names[1]: "a\x00b"}):
                pass
        for name in names:
            self.assertNotIn(name, os.environ)

    def test_a_refused_value_restores_what_was_there(self):
        name = "RESUME_TEST_OVERRIDE_C"
        os.environ[name] = "before"
        self.addCleanup(os.environ.pop, name, None)
        with self.assertRaises(ValueError):
            with worker._env_overrides({name: "during", "RESUME_TEST_OVERRIDE_D": "\x00"}):
                pass
        self.assertEqual(os.environ[name], "before")
        self.assertNotIn("RESUME_TEST_OVERRIDE_D", os.environ)


class TestReadPlacement(unittest.TestCase):
    """build.read_placement, and how the worker reports what it refuses."""

    def setUp(self):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        self.path = Path(tmp.name) / "placement.json"
        self.blocks = {"skills": {"heading": "Skills"}}
        self.jobs = {"a-job": {}}

    def placement(self, block="skills", job="a-job"):
        return {"pages": [{"page_number": 1,
                           "sidebar_blocks": [{"block_id": block}],
                           "main_sections": [{"type": "summary"},
                                             {"type": "experience",
                                              "jobs": [{"job_id": job}]}]}]}

    def read(self, content):
        text = content if isinstance(content, str) else json.dumps(content)
        self.path.write_text(text, encoding="utf-8")
        err = io.StringIO()
        with mock.patch("sys.stderr", err), mock.patch("sys.stdout", io.StringIO()):
            try:
                return build.read_placement(self.path, self.blocks, self.jobs), ""
            except build.StalePlacement as e:
                self.assertEqual(e.code, 1)
                return None, err.getvalue()

    def test_a_matching_placement_is_returned(self):
        placement, _ = self.read(self.placement())
        self.assertEqual(placement, self.placement())

    def test_ids_the_data_lacks_are_stale(self):
        for kwargs, needle in (({"block": "gone"}, "sidebar block 'gone'"),
                               ({"job": "gone"}, "job 'gone'")):
            with self.subTest(**kwargs):
                placement, err = self.read(self.placement(**kwargs))
                self.assertIsNone(placement)
                self.assertIn("does not match the data just loaded", err)
                self.assertIn(needle, err)

    def test_an_unreadable_placement_is_stale(self):
        for text in ("", "{not json", "[]", '{"pages": 3}',
                     '{"pages": [{"sidebar_blocks": [["x"]]}]}',
                     '{"pages": [{"sidebar_blocks": [{"block_id": ["x"]}]}]}'):
            with self.subTest(text=text):
                placement, err = self.read(text)
                self.assertIsNone(placement)
                self.assertIn("could not be read", err)

    def test_stale_placement_is_a_system_exit(self):
        # So the CLI exits 1 for it like for any other fail().
        self.assertTrue(issubclass(build.StalePlacement, SystemExit))

    def test_the_worker_reports_it_as_stale_placement(self):
        self.path.write_text("{not json", encoding="utf-8")

        def stale_build(mode):
            build.read_placement(self.path, self.blocks, self.jobs)

        with mock.patch.object(worker.build_mod, "build", stale_build):
            frame = worker.handle({"id": 1, "op": "build", "mode": "final"})
        self.assertFalse(frame["ok"])
        self.assertEqual(frame["error"]["kind"], "stale_placement")
        self.assertIn("could not be read", frame["error"]["message"])
        self.assertTrue(any("placement" in line.lower()
                            for line in frame["error"]["detail"]))


class TestCompareArithmetic(unittest.TestCase):
    """op_compare counts a pixel as differing by its largest channel
    difference, as snapshot_pdf.diff_images does — not by luma."""

    def compare(self, a_color, b_color, size=(10, 10)):
        from PIL import Image
        import snapshot_pdf
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        a, b = Path(tmp.name) / "a.pdf", Path(tmp.name) / "b.pdf"
        a.write_bytes(b"%PDF-a")
        b.write_bytes(b"%PDF-b")
        images = {str(a): [Image.new("RGB", size, a_color)],
                  str(b): [Image.new("RGB", size, b_color)]}
        with mock.patch.object(snapshot_pdf, "render_pdf_pages",
                               lambda _pdfium, path, *rest: images[str(path)]):
            return worker.op_compare({"a": str(a), "b": str(b)})

    def test_a_blue_only_difference_counts(self):
        # Blue 30 apart is luma 3, under the tolerance of 4, so the luma
        # count called these two pages identical.
        import snapshot_pdf
        self.assertGreater(30, snapshot_pdf.PIXEL_RGB_TOLERANCE)
        result = self.compare((10, 10, 10), (10, 10, 40))
        page = result["pages"][0]
        self.assertEqual(page["differingPixels"], 100)
        self.assertFalse(page["pass"])
        self.assertFalse(result["allPass"])

    def test_a_difference_within_tolerance_does_not_count(self):
        import snapshot_pdf
        tol = snapshot_pdf.PIXEL_RGB_TOLERANCE
        result = self.compare((10, 10, 10), (10 + tol, 10, 10 + tol))
        self.assertEqual(result["pages"][0]["differingPixels"], 0)
        self.assertTrue(result["allPass"])

    def test_identical_pages_pass(self):
        result = self.compare((200, 100, 50), (200, 100, 50))
        self.assertEqual(result["pages"][0]["fraction"], 0.0)
        self.assertTrue(result["allPass"])


if __name__ == "__main__":
    unittest.main()
