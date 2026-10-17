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

Unlike test_anonymized.py it needs none of your data files, so it runs
on GitHub too.

To fix a failure, drop the other chunks and keep the bytes of these
five as they are; the pixels do not change.
"""

import struct
import subprocess
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


if __name__ == "__main__":
    unittest.main()
