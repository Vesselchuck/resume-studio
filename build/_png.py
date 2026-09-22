"""
_png.py — A minimal, fast PNG writer for the Studio's preview images.

WHY NOT Image.save(format="PNG")
--------------------------------
Pillow's encoder picks a row filter per row (adaptive filtering) before
it compresses. That can buy a smaller file, which matters for a PNG that
is stored or downloaded, and is wasted work for one that crosses a
loopback socket to be decoded once and thrown away. Encoding was most
of the rasterize step of a live preview.

This writer emits every row with filter 0 ("None") and compresses the
result with zlib at level 1. The file is a standard 8-bit, non-
interlaced PNG any browser decodes, and it decodes to exactly the pixels it
was given (PNG is lossless whatever the filter). Measured on a resume
page at the preview's scale it took about half as long as Pillow at
compress_level=1, and the file came out no larger — a page of mostly
white paper compresses well without per-row filtering.

Standard library only: zlib, struct. No numpy; the rows are prefixed
with their filter byte by slicing Pillow's raw buffer.

Not used for anything written to disk. The deliverables are PDFs, and
the snapshot test's diff images still go through Pillow.
"""

import struct
import zlib

_SIGNATURE = b"\x89PNG\r\n\x1a\n"

#: Pillow mode -> (PNG color type, bytes per pixel).
_COLOR_TYPES = {
    "L": (0, 1),
    "RGB": (2, 3),
    "RGBA": (6, 4),
}

#: zlib level. 1 is the fastest that still compresses a page of mostly
#: white paper to a small fraction of its raw size.
COMPRESS_LEVEL = 1


def _chunk(tag: bytes, data: bytes) -> bytes:
    return (struct.pack(">I", len(data)) + tag + data
            + struct.pack(">I", zlib.crc32(data, zlib.crc32(tag)) & 0xFFFFFFFF))


def encode(img) -> bytes:
    """Encode a Pillow image (mode L, RGB or RGBA) as PNG bytes.

    Any other mode is converted to RGB first — the rasterizer only ever
    hands this RGB.
    """
    if img.mode not in _COLOR_TYPES:
        img = img.convert("RGB")
    color_type, bpp = _COLOR_TYPES[img.mode]
    width, height = img.size
    stride = width * bpp

    raw = img.tobytes()
    if len(raw) != stride * height:
        raise ValueError(f"unexpected raw size {len(raw)} for {img.mode} {width}x{height}")

    # One filter-type byte (0 = None) in front of every row.
    rows = bytearray((stride + 1) * height)
    view = memoryview(raw)
    for y in range(height):
        start = y * (stride + 1) + 1
        rows[start:start + stride] = view[y * stride:(y + 1) * stride]

    ihdr = struct.pack(">IIBBBBB", width, height, 8, color_type, 0, 0, 0)
    return b"".join((
        _SIGNATURE,
        _chunk(b"IHDR", ihdr),
        _chunk(b"IDAT", zlib.compress(rows, COMPRESS_LEVEL)),
        _chunk(b"IEND", b""),
    ))
