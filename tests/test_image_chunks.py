"""
Tests that committed PNGs carry their pixels and nothing else.

WHY
───
A PNG is a list of chunks, and only a few of them draw the picture.
The rest are notes the tool that saved it chose to leave: text chunks
with a file path, a user name or the software's name, EXIF, and
provenance manifests (C2PA, chunk `caBX`) that say what made the
image and how. Pillow skips chunk types it does not know, so
tests/test_anonymized.py, which reads metadata through Pillow, never
sees a provenance manifest at all: two screenshots and every app icon
were committed with one, about 5.7 KB each, and every check passed.

WHAT THIS CHECKS
────────────────
Every PNG git would commit, and every PNG stored inside a committed
.ico, holds only the chunks that carry pixels: IHDR, PLTE, tRNS, IDAT
and IEND. Anything else fails, named with its file.

The history too: every PNG and .ico blob reachable from any ref, so a
manifest stripped from the working tree but still in an older commit
is found before that commit is pushed. A history rewrite leaves the
old objects reachable through any ref that still names them (a
refs/original/ backup, an old branch or tag) until that ref is gone.

Unlike test_anonymized.py it needs none of your data files, so it runs
on GitHub too.

To fix a failure, drop the other chunks and keep the bytes of these
five as they are; the pixels do not change.
"""

import os
import struct
import subprocess
import tempfile
import unittest
from pathlib import Path

ROOT = Path(__file__).parent.parent

SIGNATURE = b"\x89PNG\r\n\x1a\n"
# The chunks that make up the picture: the header, the palette and its
# transparency for indexed images, the compressed pixels, the end.
PIXEL_CHUNKS = {"IHDR", "PLTE", "tRNS", "IDAT", "IEND"}


def chunk_types(data):
    """The chunk types of one PNG, in order."""
    if not data.startswith(SIGNATURE):
        raise ValueError("not a PNG")
    types, i = [], len(SIGNATURE)
    while i + 8 <= len(data):
        length, kind = struct.unpack(">I4s", data[i:i + 8])
        types.append(kind.decode("latin-1"))
        i += 12 + length              # length, type, data, CRC
    return types


def pngs_in_ico(data):
    """(entry number, bytes) of every image in an .ico stored as PNG.
    The others are BMP, which has no room for metadata."""
    count = struct.unpack("<H", data[4:6])[0]
    for n in range(count):
        size, offset = struct.unpack("<II", data[6 + 16 * n + 8:6 + 16 * n + 16])
        blob = data[offset:offset + size]
        if blob.startswith(SIGNATURE):
            yield n, blob


def committable(*suffixes):
    """The files git would commit with one of these suffixes."""
    out = subprocess.run(
        ["git", "-C", str(ROOT), "ls-files", "-z",
         "--cached", "--others", "--exclude-standard"],
        capture_output=True, check=True).stdout
    for rel in out.decode("utf-8").split("\0"):
        path = ROOT / rel
        if rel and path.suffix.lower() in suffixes and path.is_file():
            yield rel, path


def extra_chunks(data):
    return sorted(set(chunk_types(data)) - PIXEL_CHUNKS)


def is_git_checkout(root=ROOT):
    try:
        subprocess.run(["git", "-C", str(root), "rev-parse", "--git-dir"],
                       capture_output=True, check=True)
    except (OSError, subprocess.CalledProcessError):
        return False
    return True


def history_images(root=ROOT, suffixes=(".png", ".ico")):
    """(path, blob id, bytes) of every blob with one of these suffixes
    reachable from any ref, each blob once (under the first path git
    lists it at)."""
    listing = subprocess.run(
        ["git", "-C", str(root), "rev-list", "--all", "--objects"],
        capture_output=True, check=True).stdout.decode("utf-8", "replace")
    wanted = {}
    for line in listing.splitlines():
        sha, _, rel = line.partition(" ")
        if rel and Path(rel).suffix.lower() in suffixes:
            wanted.setdefault(sha, rel)
    if not wanted:
        return []
    # One cat-file for all of them: "<id> <type> <size>\n<bytes>\n" each.
    out = subprocess.run(
        ["git", "-C", str(root), "cat-file", "--batch"],
        input="".join(f"{sha}\n" for sha in wanted).encode("ascii"),
        capture_output=True, check=True).stdout
    found, i = [], 0
    for sha, rel in wanted.items():
        end = out.index(b"\n", i)
        size = int(out[i:end].split()[2])
        found.append((rel, sha, out[end + 1:end + 1 + size]))
        i = end + 1 + size + 1
    return found


def dirty_history_images(root=ROOT):
    """One line per PNG or .ico blob in the history that carries chunks
    besides its pixels."""
    found = []
    for rel, sha, data in history_images(root):
        if rel.lower().endswith(".ico"):
            for n, blob in pngs_in_ico(data):
                extra = extra_chunks(blob)
                if extra:
                    found.append(f"{rel} (blob {sha[:10]}, image {n}): {', '.join(extra)}")
        elif data.startswith(SIGNATURE):
            extra = extra_chunks(data)
            if extra:
                found.append(f"{rel} (blob {sha[:10]}): {', '.join(extra)}")
    return found


class TestChunkReader(unittest.TestCase):
    """The reader finds what the real test looks for."""

    @staticmethod
    def _png(*kinds):
        body = b"".join(struct.pack(">I4s", 0, k.encode()) + b"\0\0\0\0" for k in kinds)
        return SIGNATURE + body

    def test_pixel_chunks_alone_are_clean(self):
        self.assertEqual(extra_chunks(self._png("IHDR", "IDAT", "IEND")), [])

    def test_text_and_provenance_chunks_are_found(self):
        self.assertEqual(extra_chunks(self._png("IHDR", "tEXt", "caBX", "IDAT", "IEND")),
                         ["caBX", "tEXt"])

    def test_not_a_png_is_an_error(self):
        with self.assertRaises(ValueError):
            chunk_types(b"GIF89a")


class TestHistoryReader(unittest.TestCase):
    """The history check finds a dirty PNG in an older commit, and
    passes a history that has only clean ones."""

    @staticmethod
    def _png(*kinds):
        return TestChunkReader._png(*kinds)

    def test_a_clean_history_passes_and_a_dirty_commit_fails(self):
        with tempfile.TemporaryDirectory() as work:
            repo = Path(work)
            # Without this machine's git configuration: its hooks and
            # signing settings are not what is tested here.
            env = {**os.environ, "GIT_CONFIG_GLOBAL": os.devnull, "GIT_CONFIG_NOSYSTEM": "1",
                   "GIT_AUTHOR_NAME": "t", "GIT_AUTHOR_EMAIL": "t@example.invalid",
                   "GIT_COMMITTER_NAME": "t", "GIT_COMMITTER_EMAIL": "t@example.invalid"}

            def git(*args):
                subprocess.run(["git", "-C", str(repo), *args], env=env,
                               capture_output=True, check=True)
            try:
                git("init", "-q")
            except (OSError, subprocess.CalledProcessError):
                self.skipTest("git is not available to make a repository")
            (repo / "clean.png").write_bytes(self._png("IHDR", "IDAT", "IEND"))
            git("add", "clean.png")
            git("commit", "-q", "-m", "clean")
            self.assertEqual(dirty_history_images(repo), [])

            (repo / "dirty.png").write_bytes(self._png("IHDR", "caBX", "IDAT", "IEND"))
            git("add", "dirty.png")
            git("commit", "-q", "-m", "dirty")
            (repo / "dirty.png").write_bytes(self._png("IHDR", "IDAT", "IEND"))
            git("commit", "-q", "-am", "stripped")
            found = dirty_history_images(repo)
            self.assertEqual(len(found), 1, found)
            self.assertTrue(found[0].startswith("dirty.png (blob ") and found[0].endswith(": caBX"),
                            found)


class TestCommittedImages(unittest.TestCase):

    def test_pngs_carry_only_pixels(self):
        found = []
        pngs = list(committable(".png"))
        self.assertGreater(len(pngs), 0, "no committable PNG found to check")
        for rel, path in pngs:
            extra = extra_chunks(path.read_bytes())
            if extra:
                found.append(f"{rel}: {', '.join(extra)}")
        self.assertEqual(found, [],
                         "PNGs that would be committed carry chunks besides their "
                         "pixels. Strip them (see this file's docstring):\n  "
                         + "\n  ".join(found))

    def test_pngs_inside_icons_carry_only_pixels(self):
        found = []
        for rel, path in committable(".ico"):
            for n, blob in pngs_in_ico(path.read_bytes()):
                extra = extra_chunks(blob)
                if extra:
                    found.append(f"{rel} (image {n}): {', '.join(extra)}")
        self.assertEqual(found, [],
                         "Icons that would be committed hold PNGs with chunks "
                         "besides their pixels:\n  " + "\n  ".join(found))

    def test_history_holds_no_image_with_other_chunks(self):
        if not is_git_checkout():
            self.skipTest("not a git checkout — there is no history to read")
        found = dirty_history_images()
        self.assertEqual(found, [],
                         "Commits reachable from a ref hold PNGs (or icons) with "
                         "chunks besides their pixels. Pushed, they are public "
                         "even after the working tree is clean: rewrite those "
                         "commits, then remove any ref that still names the old "
                         "ones (refs/original/ included):\n  " + "\n  ".join(found))


if __name__ == "__main__":
    unittest.main()
