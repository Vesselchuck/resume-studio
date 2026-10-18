"""
_project.py — a throwaway copy of the project, for the Python suites
that need built output. The twin of tests/_project.js.

WHY
───
test_worker_equivalence.py and test_preview_raster.py used to read and
write this checkout's dist/. They could only test what the last build
had left there: they skipped when nothing was built, skipped their
final-mode checks when dist/placement.json was solved for other data
(the Studio defaults to your own), and wrote into the same files your
last build produced, restoring them afterwards. A copy removes the
shared state instead of managing it: its data/ holds only the shipped
*_default.yml templates, it is built from them by the real CLI, and it
is deleted afterwards. real_dist_fingerprint() is how a suite proves it
left this checkout's dist/ alone.

node_modules is linked into the copy, not copied — a directory symlink,
or a junction on Windows. remove() takes the link away first, on its
own: shutil.rmtree before Python 3.12 does not recognise a junction as a
link, and would walk into it and delete the real node_modules.

A build that fails is a failure of the suite, not a skip: the build is
the code under test. Suites raise build_error(problem), which is a skip
only when there is no node to build with, or Playwright's Chromium
cannot start (checked directly).
"""

import os
import shutil
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path

ROOT = Path(__file__).parent.parent

# What Project.build() returns when there is no node on PATH.
NO_NODE = "node not found"

_DIRS = ("build", "styles", "templates", "fonts", "ui", "schemas")
_FILES = ("resume.js", "letter.js", "package.json")

# Variables that choose which data a build reads. A copy builds its own
# templates, whatever the environment running the tests says.
_DATA_ENV = ("RESUME_DATA_FILE", "LETTER_DATA_FILE", "RESUME_DATA_SOURCE")

BUILD_TIMEOUT = 300


def _link_dir(target: Path, link: Path) -> None:
    if sys.platform == "win32":
        import _winapi
        _winapi.CreateJunction(str(target), str(link))
    else:
        os.symlink(target, link, target_is_directory=True)


def _unlink_dir(link: Path) -> None:
    """Remove a directory link without touching what it points at."""
    if not (link.is_symlink() or link.exists()):
        return
    if sys.platform == "win32":
        os.rmdir(link)       # removes a junction itself, never its target
    else:
        os.unlink(link)


class Project:
    """A copy of the project in the system temp directory.

    root   the copy
    dist   root / "dist"
    """

    def __init__(self, prefix="project-test"):
        self._tmp = Path(tempfile.mkdtemp(prefix=f"{prefix}-"))
        self.root = self._tmp / "project"
        self.dist = self.root / "dist"
        ignore = shutil.ignore_patterns("__pycache__")
        for name in _DIRS:
            if (ROOT / name).is_dir():
                shutil.copytree(ROOT / name, self.root / name, ignore=ignore)
        for name in _FILES:
            shutil.copyfile(ROOT / name, self.root / name)
        (self.root / "data").mkdir()
        for f in (ROOT / "data").glob("*_default.yml"):
            shutil.copyfile(f, self.root / "data" / f.name)
        if (ROOT / "node_modules").is_dir():
            _link_dir(ROOT / "node_modules", self.root / "node_modules")

    def build(self):
        """Build both documents with the real CLI. Returns None, or why not.

        The same `node resume.js` / `node letter.js` a Build runs, tests
        and snapshot off — so dist/ holds a compiled stylesheet, a solved
        placement and both PDFs, all from the templates.
        """
        node = shutil.which("node")
        if not node:
            return NO_NODE
        env = {k: v for k, v in os.environ.items() if k not in _DATA_ENV}
        env.update({"RESUME_TESTS": "off", "RESUME_SNAPSHOT": "off"})
        for script in ("resume.js", "letter.js"):
            try:
                r = subprocess.run([node, script], cwd=str(self.root), env=env,
                                   capture_output=True, text=True, encoding="utf-8",
                                   errors="replace", timeout=BUILD_TIMEOUT)
            except subprocess.TimeoutExpired:
                return f"{script} timed out"
            if r.returncode != 0:
                tail = (r.stderr or r.stdout).strip().splitlines()[-3:]
                return f"{script} failed: " + " | ".join(tail)
        return None

    def remove(self):
        link = self.root / "node_modules"
        try:
            _unlink_dir(link)
        except OSError:
            # Never fall through to rmtree with the link still in place.
            return
        if link.is_symlink() or link.exists():
            return   # still there somehow: leave the copy rather than risk the target
        shutil.rmtree(self._tmp, ignore_errors=True)


# Why Playwright's Chromium cannot start here: None until probed, then
# "" when it starts. Probed once per process, and only after a build
# has failed.
_CHROMIUM_PROBLEM = None

_CHROMIUM_PROBE = (
    "require('playwright').chromium.launch()"
    ".then((b) => b.close())"
    ".catch((e) => { process.stdout.write(String(e.message).split('\\n')[0]);"
    " process.exitCode = 1; })"
)


def chromium_problem():
    """Why Playwright's Chromium cannot start, or "" when it can.

    The same check the JS suites make before they skip: launch it and
    close it. Asked directly, not read from the build's output, so a
    build that fails for any other reason is never taken for this one.
    """
    global _CHROMIUM_PROBLEM
    if _CHROMIUM_PROBLEM is None:
        try:
            r = subprocess.run([shutil.which("node") or "node", "-e", _CHROMIUM_PROBE],
                               cwd=str(ROOT), capture_output=True, text=True,
                               encoding="utf-8", errors="replace", timeout=120)
            _CHROMIUM_PROBLEM = "" if r.returncode == 0 else (
                r.stdout.strip() or (r.stderr.strip().splitlines() or ["no reason given"])[-1])
        except (OSError, subprocess.TimeoutExpired) as err:
            _CHROMIUM_PROBLEM = f"the launch check did not finish ({type(err).__name__})"
    return _CHROMIUM_PROBLEM


def build_error(problem):
    """The exception a suite raises when Project.build() returned `problem`.

    Two things are a skip, both of them the machine and not the code:
    no node to build with, and a Chromium that cannot start (as in the
    JS suites; the build prints with it). Every other problem (a build
    that exits non-zero or times out with Chromium working) is the code
    under test failing, and is an error. These suites used to skip on
    any of them, so a broken build left five suites skipped and the run
    green.
    """
    if problem == NO_NODE:
        return unittest.SkipTest("node is not on PATH, so no copy of the project can be built")
    why = chromium_problem()
    if why:
        return unittest.SkipTest(f"chromium unavailable ({why})")
    return RuntimeError(f"could not build a copy of the project to test against ({problem})")


def real_dist_fingerprint():
    """Every file in this checkout's dist/, with its size and mtime.

    Taken before and after a suite: if anything wrote there, even the
    same bytes back, the two differ.
    """
    dist = ROOT / "dist"
    if not dist.is_dir():
        return []
    out = []
    for p in sorted(dist.rglob("*")):
        if p.is_file():
            st = p.stat()
            out.append(f"{p.relative_to(dist).as_posix()} {st.st_size} {st.st_mtime_ns}")
    return out
