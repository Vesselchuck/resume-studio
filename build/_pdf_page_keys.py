"""
_pdf_page_keys.py — A key per page of a PDF, from its bytes, without rendering.

WHAT IT IS FOR
--------------
The live preview rasterizes every page of a fresh print on every edit.
Most edits touch one page, and rasterizing the others only to find
their pixels unchanged is most of the rasterize step's work. If two
prints give a page the same key here, that page renders to the same
pixels, so the worker reuses the image it already has instead of
rendering it again.

WHAT A KEY COVERS
-----------------
A page's key hashes:

  • the page's own object (its dictionary: boxes, resource names,
    annotations list),
  • its content stream objects, raw (still compressed) bytes,
  • and every other object in the file: fonts, their glyph programs,
    graphics states, annotations, the page tree, the catalog — in one
    digest shared by all pages.

Only the other pages' own objects and content streams, and the /Info
dictionary (which carries the print's timestamp and is never drawn),
are left out. So an edit that changes one page's content stream changes
that page's key alone, and anything that touches a shared object — a
new glyph added to a font, say — changes every key. That is coarse on
purpose: being wrong here means showing a stale page, so the rule is
"when in doubt, it changed".

The key says nothing about how a page is rendered. The caller adds the
scale, the crop and the renderer's version before comparing.

WHAT IT READS, AND WHEN IT GIVES UP
-----------------------------------
Chromium (Skia) writes one simple shape of PDF: numbered objects in a
row, then a classic `xref` table, then a trailer and `startxref`. This
module reads exactly that shape and nothing else. It returns None —
meaning "render every page" — for anything it does not fully
understand, including:

  • a cross-reference stream or object streams (PDF 1.5 compression),
  • an incremental update (more than one `startxref`, a /Prev),
  • encryption,
  • an object that is not where the xref table says it is,
  • a page tree it cannot walk, or a /Contents it cannot read,
  • a content stream shared by two pages, or referenced from anywhere
    other than its own page's /Contents (it would then be part of what
    another page draws, and leaving it out of that page's key would be
    wrong).

Standard library only.
"""

import hashlib
import re

_STARTXREF = re.compile(rb"startxref\s+(\d+)\s+%%EOF\s*\Z")
_SUBSECTION = re.compile(rb"(\d+)[ ]+(\d+)[ \t]*(?:\r\n|\r|\n)")
_ENTRY = re.compile(rb"(\d{10}) (\d{5}) ([nf])(?: \r| \n|\r\n)")
_OBJ_HEADER = re.compile(rb"\s*(\d+)\s+(\d+)\s+obj\b")
_REF = re.compile(rb"(\d+)\s+(\d+)\s+R(?![A-Za-z0-9])")
_TYPE_PAGE = re.compile(rb"/Type\s*/Page(?![A-Za-z0-9#])")
_TYPE_PAGES = re.compile(rb"/Type\s*/Pages(?![A-Za-z0-9#])")
_KIDS = re.compile(rb"/Kids\s*\[([^\]]*)\]")
_PAGES_REF = re.compile(rb"/Pages\s*(\d+)\s+(\d+)\s+R")
_ROOT_REF = re.compile(rb"/Root\s*(\d+)\s+(\d+)\s+R")
_INFO_REF = re.compile(rb"/Info\s*(\d+)\s+(\d+)\s+R")
_CONTENTS = re.compile(rb"/Contents(?![A-Za-z0-9#])\s*")
_STREAM_START = re.compile(rb">>\s*stream(?:\r\n|\n)")
_UNSUPPORTED_TYPES = re.compile(rb"/Type\s*/(?:ObjStm|XRef)(?![A-Za-z0-9#])")

#: Bump when what a key covers changes, so keys from before never match.
KEY_VERSION = b"page-keys/1"

_MAX_TREE_DEPTH = 32


class _Unsupported(Exception):
    """The file is not the simple shape this module reads."""


def page_keys(data: bytes):
    """One hex key per page, in page order — or None to render them all.

    Never raises on bad input: anything unexpected is None.
    """
    try:
        return _page_keys(data)
    except _Unsupported:
        return None
    except (ValueError, IndexError, KeyError, RecursionError):
        return None


def _dict_part(body: bytes) -> bytes:
    """An object's dictionary, without the data of a stream object."""
    m = _STREAM_START.search(body)
    return body[:m.start() + 2] if m else body


def _is_stream(body: bytes) -> bool:
    return _STREAM_START.search(body) is not None


def _read_xref(data: bytes):
    """(offsets {num: (offset, gen)}, xref offset, trailer bytes)."""
    if data.count(b"startxref") != 1:
        raise _Unsupported("incremental update or stray startxref")
    tail = data.rfind(b"startxref")
    m = _STARTXREF.match(data, tail)
    if not m:
        raise _Unsupported("no startxref at the end")
    xref_at = int(m.group(1))
    if xref_at >= tail or not data.startswith(b"xref", xref_at):
        raise _Unsupported("xref stream, not a classic xref table")

    pos = xref_at + 4
    while data[pos:pos + 1] in (b"\r", b"\n", b" "):
        pos += 1
    offsets = {}
    while not data.startswith(b"trailer", pos):
        sub = _SUBSECTION.match(data, pos)
        if not sub:
            raise _Unsupported("malformed xref subsection")
        first, count = int(sub.group(1)), int(sub.group(2))
        pos = sub.end()
        for i in range(count):
            entry = _ENTRY.match(data, pos)
            if not entry:
                raise _Unsupported("malformed xref entry")
            pos = entry.end()
            if entry.group(3) == b"n":
                num = first + i
                if num in offsets:
                    raise _Unsupported("object listed twice")
                offsets[num] = (int(entry.group(1)), int(entry.group(2)))
        while data[pos:pos + 1] in (b"\r", b"\n", b" "):
            pos += 1

    trailer = data[pos:tail]
    for bad in (b"/Prev", b"/XRefStm", b"/Encrypt"):
        if bad in trailer:
            raise _Unsupported(f"trailer has {bad.decode()}")
    return offsets, xref_at, trailer


def _slice_objects(data, offsets, xref_at):
    """{num: bytes of 'num gen obj ... endobj' and the whitespace after it}."""
    order = sorted(offsets.items(), key=lambda kv: kv[1][0])
    objs = {}
    for i, (num, (offset, gen)) in enumerate(order):
        end = order[i + 1][1][0] if i + 1 < len(order) else xref_at
        if not 0 < offset < end <= xref_at:
            raise _Unsupported("object offsets out of order or out of range")
        body = data[offset:end]
        head = _OBJ_HEADER.match(body)
        if not head or int(head.group(1)) != num or int(head.group(2)) != gen:
            raise _Unsupported(f"object {num} is not where the xref says")
        if not body.rstrip().endswith(b"endobj"):
            raise _Unsupported(f"object {num} does not end with endobj")
        if _UNSUPPORTED_TYPES.search(_dict_part(body)):
            raise _Unsupported("object or xref stream")
        objs[num] = body
    first = order[0][1][0] if order else xref_at
    return objs, data[:first]


def _ref(pattern, text):
    m = pattern.search(text)
    return int(m.group(1)) if m else None


def _walk_pages(objs, node, depth, seen, out):
    if depth > _MAX_TREE_DEPTH or node in seen or node not in objs:
        raise _Unsupported("page tree")
    seen.add(node)
    body = objs[node]
    if _is_stream(body):
        raise _Unsupported("page tree node is a stream")
    text = _dict_part(body)
    if _TYPE_PAGES.search(text):
        kids = _KIDS.search(text)
        if not kids:
            raise _Unsupported("page tree node without /Kids")
        for m in _REF.finditer(kids.group(1)):
            _walk_pages(objs, int(m.group(1)), depth + 1, seen, out)
    elif _TYPE_PAGE.search(text):
        out.append(node)
    else:
        raise _Unsupported("page tree node of unknown type")


def _contents_of(page_text):
    """The object numbers of a page's content streams."""
    found = list(_CONTENTS.finditer(page_text))
    if not found:
        return []
    if len(found) > 1:
        raise _Unsupported("more than one /Contents")
    rest = page_text[found[0].end():]
    single = re.match(rb"(\d+)\s+(\d+)\s+R(?![A-Za-z0-9])", rest)
    if single:
        return [int(single.group(1))]
    array = re.match(rb"\[([^\]]*)\]", rest)
    if array:
        refs = [int(m.group(1)) for m in _REF.finditer(array.group(1))]
        # Nothing but references inside the brackets.
        if _REF.sub(b"", array.group(1)).strip():
            raise _Unsupported("/Contents array holds more than references")
        return refs
    raise _Unsupported("/Contents is neither a reference nor an array of them")


def _page_keys(data: bytes):
    offsets, xref_at, trailer = _read_xref(data)
    objs, header = _slice_objects(data, offsets, xref_at)

    root = _ref(_ROOT_REF, trailer)
    info = _ref(_INFO_REF, trailer)
    if root is None or root not in objs:
        raise _Unsupported("no /Root")
    pages_root = _ref(_PAGES_REF, _dict_part(objs[root]))
    if pages_root is None:
        raise _Unsupported("catalog without /Pages")

    pages = []
    _walk_pages(objs, pages_root, 0, set(), pages)
    if not pages:
        raise _Unsupported("no pages")

    contents = {}
    owner = {}
    for page in pages:
        refs = _contents_of(_dict_part(objs[page]))
        for num in refs:
            if num not in objs or num in owner or num in pages or num == info:
                raise _Unsupported("content stream missing, shared or misplaced")
            if not _is_stream(objs[num]):
                raise _Unsupported("content is not a stream")
            owner[num] = page
        contents[page] = refs

    # A content stream may be referenced from its own page's /Contents
    # and nowhere else, and nothing may reference /Info: either would
    # make an object left out of a page's key part of what it draws.
    watched = set(owner)
    if info is not None:
        watched.add(info)
    seen_refs = {num: 0 for num in watched}
    for num, body in objs.items():
        for m in _REF.finditer(_dict_part(body)):
            target = int(m.group(1))
            if target in seen_refs:
                seen_refs[target] += 1
    for num in owner:
        if seen_refs[num] != 1:
            raise _Unsupported("content stream referenced from elsewhere")
    if info is not None and seen_refs[info]:
        raise _Unsupported("/Info referenced from an object")

    skip = set(pages) | set(owner)
    if info is not None:
        skip.add(info)
    shared = hashlib.sha256(KEY_VERSION)
    shared.update(header)
    for num in sorted(objs):
        if num in skip:
            continue
        shared.update(b"\0%d\0" % num)
        shared.update(objs[num])
    shared_digest = shared.digest()

    keys = []
    for page in pages:
        h = hashlib.sha256(KEY_VERSION)
        h.update(objs[page])
        for num in contents[page]:
            h.update(b"\0")
            h.update(objs[num])
        h.update(b"\0")
        h.update(shared_digest)
        keys.append(h.hexdigest())
    return keys
