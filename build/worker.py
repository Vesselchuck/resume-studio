"""
worker.py — Long-lived build worker for the Studio GUI.

WHY THIS EXISTS
---------------
A cold `node resume.js` spends most of its wall-clock on *startup*, not
on work: one Chromium launch plus four separate `python -B` interpreter
starts (build.py x2, crop_pdf.py x2). The rendering, solving and
cropping in between are milliseconds. A GUI that re-renders on every
keystroke cannot pay that startup cost per edit.

This module is the Python half of the fix. It starts once, imports
build.py and crop_pdf.py once, and then serves render/crop/rasterize
requests over stdin/stdout until told to stop.

THE NON-NEGOTIABLE CONSTRAINT
-----------------------------
This worker must never become a second implementation of the pipeline.
Every operation below calls the SAME functions the CLI entry points
call:

    build        -> build.build(mode=...)         (build.py's own main() calls this)
    build_letter -> build_letter.build_letter()
    crop    -> crop_pdf.crop_pages / apply_metadata / apply_language
    raster  -> snapshot_pdf.render_pdf_pages      (the snapshot test's own rasterizer,
               with crop_pdf's crop applied in memory for a live preview)

Nothing here reimplements layout, metadata derivation, cropping or
rasterization. If a behavior needs to change, it changes in those
modules and both paths get it. tests/test_worker_equivalence.py asserts
that the warm path and the cold CLI path produce byte-identical files;
if this module ever drifts, that test fails.

`node resume.js` and `node letter.js` are unaffected by this file's
existence.

PROTOCOL
--------
Newline-delimited JSON over stdin/stdout. One response per request.

Every response frame written to stdout is prefixed with a single ASCII
RECORD SEPARATOR byte (0x1e). Anything on stdout without that prefix is
stray output from a library that wrote to the stream directly, and the
client is expected to treat it as log noise rather than losing framing.
See "STDOUT DISCIPLINE" below.

  Request   {"id": 1, "op": "build", "mode": "measurement"}
  Response  \x1e{"id": 1, "ok": true, "result": {...}, "log": [...],
                 "diagnostics": [...], "ms": 41.2}

Operations
  ping        -> {} .............. liveness + version handshake
  build       -> {mode}  ......... mode: "final" | "measurement"
                 {env}  ......... optional {"RESUME_DATA_SOURCE": "mine"};
                                  a null value unsets the variable
  build_letter-> {env} ........... the single-page cover letter; env as
                                   for build, e.g. {"LETTER_DATA_FILE": ...}
  crop        -> {input, output, meta}
  raster      -> {path, scale, pages, known, crop, slot} -> base64 PNGs;
                 see op_raster
  compare     -> {a, b, scale} -> per-page pixel-diff verdicts
  shutdown    -> {} .............. exits 0

Errors never kill the worker. A malformed YAML file, a missing data
file, a locked PDF — all come back as {"ok": false, "error": {...}} and
the worker stays up for the next keystroke.

USAGE
-----
    py -B build/worker.py            (Windows)
    python3 -B build/worker.py       (macOS / Linux)

Reads requests from stdin, writes frames to stdout. Not intended to be
run interactively; the GUI drives it.
"""

import base64
import hashlib
import io
import json
import os
import sys
import time
import traceback
from concurrent.futures import ThreadPoolExecutor
from contextlib import contextmanager, redirect_stdout, redirect_stderr
from pathlib import Path

# build/ on sys.path so sibling modules import the same way they do when
# run as scripts. Mirrors the `sys.path.insert` line at the top of
# build.py, crop_pdf.py and snapshot_pdf.py.
sys.path.insert(0, str(Path(__file__).parent))

import build as build_mod            # noqa: E402
import crop_pdf as crop_mod          # noqa: E402
import _console as c                 # noqa: E402
import _pdf_page_keys as _page_keys  # noqa: E402
import _png                          # noqa: E402

PROTOCOL_VERSION = 1

# Single byte that marks a line as a protocol frame. Chosen because
# ASCII RS cannot appear in _console output or in JSON text.
FRAME_PREFIX = "\x1e"

ROOT = Path(__file__).parent.parent


# ---------------------------------------------------------------------------
# STDOUT DISCIPLINE
#
# build.py logs through _console, which prints to sys.stdout (ok/ok_pair/
# banner/info_pair) and sys.stderr (err/warn). Those prints would shred
# the frame stream if they landed on the real stdout, so the real stream
# is captured here at import time and sys.stdout is left pointing at
# whatever the current request's capture buffer is.
#
# Frames go to _FRAME_OUT. Everything else is collected and returned
# inside the frame as "log" / "diagnostics", which is how the GUI gets
# its build log without a second channel.
#
# Known limit: this redirects at the Python level, not the file
# descriptor level. A C extension writing straight to fd 1 would still
# leak onto the frame stream. None of the pinned dependencies (PyYAML,
# Jinja2, pypdf, pypdfium2, Pillow) do, and the FRAME_PREFIX sentinel
# means such a leak degrades to log noise instead of breaking framing.
# ---------------------------------------------------------------------------
_FRAME_OUT = sys.stdout


def _send(payload):
    """Write one protocol frame. The only function that may touch _FRAME_OUT."""
    _FRAME_OUT.write(FRAME_PREFIX + json.dumps(payload, ensure_ascii=False) + "\n")
    _FRAME_OUT.flush()


def _split_lines(text):
    """Console capture -> list of lines, trailing blanks dropped."""
    return [ln for ln in text.splitlines() if ln.strip()]


# ---------------------------------------------------------------------------
# Operations
#
# Each op_* returns a plain dict (the "result" field). Raising is fine —
# the dispatcher turns any exception, including the SystemExit that
# build.py's fail() raises, into an error frame.
# ---------------------------------------------------------------------------

def op_ping(_req):
    return {
        "protocol": PROTOCOL_VERSION,
        "python": sys.version.split()[0],
        "root": str(ROOT),
        "pid": os.getpid(),
    }


@contextmanager
def _env_overrides(overrides):
    """Apply per-request env overrides for one call, then restore them.

    build.py's and build_letter.py's load_data() read os.environ at call
    time, so this is the same switch the CLI gets from the shell. A value
    of None removes the variable for the duration of the call — which is
    how the Studio says "this document has no such selection", so a
    variable inherited from the shell cannot leak into a render.
    """
    overrides = overrides or {}
    saved = {k: os.environ.get(k) for k in overrides}
    for k, v in overrides.items():
        if v is None:
            os.environ.pop(k, None)
        else:
            os.environ[k] = str(v)
    try:
        yield
    finally:
        for k, v in saved.items():
            if v is None:
                os.environ.pop(k, None)
            else:
                os.environ[k] = v


def op_build(req):
    """Render dist/index.html + dist/pdf_meta.json.

    Calls build.build() — the same function build.py's main() calls
    after parsing --mode. No layout logic lives here.

    Writes exactly where the CLI writes: dist/index.html and
    dist/pdf_meta.json. Both are gitignored intermediates that the CLI
    overwrites on every run, so a live-preview render touching them
    costs nothing. The PDFs are the deliverables, and this op does not
    write them — the caller renders live previews to a temp path and
    only an explicit Build writes dist/*.pdf.

    ORDERING INVARIANT — the caller's responsibility
    ------------------------------------------------
    mode='final' consumes dist/placement.json, which the solver wrote
    for one specific set of job and sidebar ids. A cold CLI build always
    runs measurement -> measure -> solve -> final in sequence, so the
    placement on disk always matches the data. A warm worker makes it
    possible to skip straight to 'final' against a placement solved for
    different data — most easily by changing RESUME_DATA_SOURCE, since
    resume.yml and resume_default.yml have different job ids.

    That fails loudly rather than producing a wrong document (the
    template raises on the missing id, and this op reports it as
    kind='stale_placement'), but the orchestrator must still re-run the
    full sequence whenever the data source or the data itself changes.
    Do not treat 'final' as a cheap repeat of the last render.
    """
    mode = req.get("mode", "final")
    if mode not in ("final", "measurement"):
        raise ValueError(f"unknown mode {mode!r}; expected 'final' or 'measurement'")

    # _console prefixes the first banner of a process with a newline.
    # Reset it per request so a warm build's captured log is formatted
    # identically to a cold run's.
    with _env_overrides(req.get("env")):
        c._first_banner = True
        build_mod.build(mode=mode)

    meta_path = ROOT / "dist" / "pdf_meta.json"
    meta = {}
    if meta_path.exists():
        meta = json.loads(meta_path.read_text(encoding="utf-8"))

    return {
        "mode": mode,
        "html": str((ROOT / "dist" / "index.html").relative_to(ROOT)),
        "meta": meta,
        "dataSource": meta.get("data_source"),
    }


def op_build_letter(req):
    """Render dist/letter.html + dist/letter_meta.json.

    Calls build_letter.build_letter() — the same function
    that module's main() calls. The letter needs no measurement pass,
    no solver and no invariant check: it is one flowing column, so
    there is no placement to get stale and none of the resume's
    ordering hazard applies here.

    Imported lazily so a worker that only ever builds resumes does not
    pay for the module.
    """
    import build_letter

    # Per-request env overrides (LETTER_DATA_FILE), applied and restored
    # exactly as op_build does. The letter's data selection is its own:
    # the caller sends LETTER_DATA_FILE when a file is picked and clears
    # RESUME_DATA_SOURCE, which belongs to the resume.
    with _env_overrides(req.get("env")):
        c._first_banner = True
        build_letter.build_letter()

    meta_path = ROOT / "dist" / "letter_meta.json"
    meta = {}
    if meta_path.exists():
        meta = json.loads(meta_path.read_text(encoding="utf-8"))

    return {
        "html": str((ROOT / "dist" / "letter.html").relative_to(ROOT)),
        "meta": meta,
        "dataSource": meta.get("data_source"),
    }


def op_crop(req):
    """Crop a printed PDF to true US Letter and stamp metadata.

    Mirrors crop_pdf.main()'s body exactly — crop_pages, then
    apply_metadata, then apply_language, then write — but takes its
    arguments from the request instead of argparse, and returns the
    final page box instead of printing it.
    """
    from pypdf import PdfReader, PdfWriter

    src = Path(req["input"])
    dst = Path(req["output"])
    meta = Path(req["meta"]) if req.get("meta") else None

    if meta is not None and not meta.exists():
        raise FileNotFoundError(f"--meta file not found: {meta}")

    reader = PdfReader(str(src))
    writer = PdfWriter()

    crop_mod.crop_pages(reader, writer)
    crop_mod.apply_metadata(writer, reader, meta)
    crop_mod.apply_language(writer, meta)

    dst.parent.mkdir(parents=True, exist_ok=True)
    with open(dst, "wb") as f:
        writer.write(f)

    final = PdfReader(str(dst))
    box = final.pages[0].mediabox
    return {
        "output": str(dst),
        "pages": len(final.pages),
        "widthPt": float(box.width),
        "heightPt": float(box.height),
        "bytes": dst.stat().st_size,
    }


def _pixel_hash(img):
    """A fingerprint of a rendered page: its size, mode and every pixel.

    SHA-256 from the standard library: of the hashes hashlib always has,
    the fastest over a page's ~6 MB of pixels on current CPUs (it has
    hardware support where blake2b has none). Cut to 128 bits, the
    length the blake2b digest it replaces had; the UI treats the value
    as an opaque string.
    """
    h = hashlib.sha256()
    h.update(f"{img.mode}:{img.width}x{img.height}:".encode("ascii"))
    h.update(img.tobytes())
    return h.hexdigest()[:32]


def _encode_png(img):
    """Base64 PNG for the preview pane. See build/_png.py for the writer."""
    return base64.b64encode(_png.encode(img)).decode("ascii")


# ---------------------------------------------------------------------------
# Skipping pages that did not change
#
# A preview renders a fresh print of the whole document on every edit,
# and most edits touch one page. _pdf_page_keys reads, from the PDF's
# bytes, a key per page that changes whenever anything that page draws
# changes. A caller that names a `slot` (the engine uses the document:
# "resume", "letter") gets the pages whose key, scale, crop and pdfium
# version all match that slot's previous render back without rendering
# them again: same key, same pixels, so the same hash and the same PNG.
#
# The keys are coarse on purpose (a new glyph in a shared font changes
# every page's key), and when the file is not the simple shape the key
# reader understands it returns None and every page is rendered — a
# page is only ever skipped on positive proof that its inputs are
# byte-for-byte those of a page already rendered.
#
# Only the last render of each slot is remembered, so this holds at
# most one document's page images per slot.
# ---------------------------------------------------------------------------
_RENDERED = {}

#: What the render of a page depends on besides its key. A change to
#: any of these must never let an old image through.
def _render_signature(scale, crop):
    import pypdfium2 as pdfium
    return (
        f"scale={scale!r}|crop={crop or 'none'}|"
        f"pdfium={pdfium.PDFIUM_INFO}|pypdfium2={pdfium.PYPDFIUM_INFO}|"
        f"raster=rgb/1"
    ).encode("utf-8")


def _slot_keys(data, scale, crop):
    """Per-page cache keys for one render, or None to render every page."""
    keys = _page_keys.page_keys(data)
    if keys is None:
        return None
    signature = _render_signature(scale, crop)
    return [hashlib.sha256(signature + b"|" + k.encode("ascii")).hexdigest() for k in keys]


def _rasterize(path, scale, crop, only):
    """snapshot_pdf.render_pdf_pages at `scale`, with the crop applied.

    crop='letter' is the live preview: Chromium's raw print, cropped to
    US Letter in memory by crop_pdf's own geometry. Everything else
    renders the file as it is. If a page cannot be cropped in memory
    (its MediaBox is inherited, which Chromium never does), the file is
    cropped the CLI's way into a temp copy and that is rendered instead.
    """
    import pypdfium2 as pdfium
    import snapshot_pdf

    prepare = crop_mod.crop_pdfium_page_to_letter if crop == "letter" else None

    # render_pdf_pages applies snapshot_pdf.SCALE. Swap it for the
    # requested preview scale and put it back, so the snapshot test's
    # own constant is never left modified.
    saved_scale = snapshot_pdf.SCALE
    snapshot_pdf.SCALE = scale
    try:
        try:
            return snapshot_pdf.render_pdf_pages(pdfium, path, prepare, only)
        except crop_mod.NoOwnMediaBox:
            return _rasterize_file_cropped(pdfium, snapshot_pdf, path, only)
    finally:
        snapshot_pdf.SCALE = saved_scale


def _rasterize_file_cropped(pdfium, snapshot_pdf, path, only):
    import tempfile
    from pypdf import PdfReader, PdfWriter

    writer = PdfWriter()
    crop_mod.crop_pages(PdfReader(str(path)), writer)
    fd, tmp = tempfile.mkstemp(suffix=".pdf", prefix="studio-crop-")
    try:
        with os.fdopen(fd, "wb") as f:
            writer.write(f)
        return snapshot_pdf.render_pdf_pages(pdfium, Path(tmp), None, only)
    finally:
        try:
            os.unlink(tmp)
        except OSError:
            pass


def _item_for(idx, img, entry, keys, previous):
    """The response item and the cache entry for one page.

    `entry` is a reusable entry from this slot's previous render, in
    which case `img` is None and nothing is rendered or hashed again.
    Otherwise `img` is the freshly rendered page.

    Shared by the one-reply and the streaming paths below so the two
    cannot describe the same page differently.
    """
    if entry is not None:
        item = {"page": idx + 1, "width": entry["width"],
                "height": entry["height"], "hash": entry["hash"]}
        return item, dict(entry)

    item = {"page": idx + 1, "width": img.width, "height": img.height,
            "hash": _pixel_hash(img)}
    cached = {"width": img.width, "height": img.height, "hash": item["hash"]}
    prev = previous.get(keys[idx]) if keys is not None else None
    if prev and prev["hash"] == item["hash"] and prev.get("png"):
        cached["png"] = prev["png"]
    return item, cached


def _fill_png(item, cached, known, idx):
    """Attach what the caller needs, or say a PNG must still be encoded.

    A page whose pixels the caller already holds is marked unchanged and
    carries nothing; a page this slot has already encoded carries that
    PNG. Otherwise the caller encodes it and puts it in both.
    """
    if known.get(str(idx + 1)) == item["hash"]:
        item["unchanged"] = True
        return False
    if cached.get("png"):
        item["png"] = cached["png"]
        return False
    return True


def _priority_order(page_count, wanted, order):
    """Page indices to produce, the asked-for ones first.

    `order` is 1-based page numbers, most wanted first — the UI sends
    the pages that are on screen, in the order they appear there. Pages
    it does not name follow in page order, so every wanted page is
    produced exactly once however partial or strange the request is.
    """
    pages = [i for i in range(page_count) if not wanted or (i + 1) in wanted]
    if not order:
        return pages
    allowed = set(pages)
    first = []
    seen = set()
    for p in order:
        idx = p - 1
        if idx in allowed and idx not in seen:
            seen.add(idx)
            first.append(idx)
    return first + [i for i in pages if i not in seen]


def _raster_streamed(req, path, scale, crop, keys, previous, known, wanted, slot):
    """op_raster, one page at a time, most wanted first.

    Same pages, same pixels, same hashes as the one-reply path — the
    difference is when they arrive. Each page is rendered and encoded on
    its own and sent immediately as a partial frame, so the page the
    user is looking at reaches the screen without waiting for the ones
    behind it.

    The reply that follows lists every page in page order, with the PNGs
    left out of the ones already sent (marked "sent") so nothing crosses
    the pipe twice. A caller that misses a partial frame sees a page with
    neither a PNG nor `unchanged` and can ask again in full.

    Only reached when the page count is known up front (the slot's page
    keys), because rendering page by page means knowing how many there
    are before the first one is opened.
    """
    rid = req.get("id")
    page_count = len(keys)

    # The keys come from the file's own bytes, but they are read by
    # _pdf_page_keys rather than by pdfium, and the one-reply path below
    # refuses to trust them when the two disagree about how many pages
    # there are. This asks pdfium first — rendering nothing, which only
    # opens the document — and hands the whole request back to that path
    # if the count differs. Nothing is streamed before this is settled.
    if len(_rasterize(path, scale, crop, set())) != page_count:
        return None

    out = []
    current = {}
    rendered = 0
    items = {}
    order = _priority_order(page_count, wanted, req.get("order"))

    # Encoding happens beside the rendering, not after it.
    #
    # A page is rendered in this thread (pdfium is driven from one
    # thread, as it is everywhere else here) and then handed to the pool
    # to be turned into a PNG. zlib releases the GIL, so that encode runs
    # while the next page is being rendered — which is how sending the
    # pages one at a time costs no more in total than sending them
    # together used to. The bytes are the same either way.
    #
    # `waiting` holds the pages in the order they are to be sent. A page
    # is sent as soon as it and everything before it are done, so the
    # first page the caller asked for goes out the moment it is ready
    # rather than after the last one.
    waiting = []

    def emit(entry, block):
        idx, item, cached, future = entry
        if future is not None:
            if not block and not future.done():
                return False
            png = future.result()
            item["png"] = png
            cached["png"] = png
        current[keys[idx]] = cached
        items[idx] = item
        _send({"id": rid, "op": "raster", "partial": True,
               "result": {"scale": scale, "pageCount": page_count, "image": item},
               "log": [], "diagnostics": [], "ms": 0.0})
        return True

    with ThreadPoolExecutor(max_workers=max(1, min(len(order), os.cpu_count() or 1))) as pool:
        for idx in order:
            entry = previous.get(keys[idx]) if keys is not None else None
            if entry is not None and not (known.get(str(idx + 1)) == entry["hash"]
                                          or entry.get("png")):
                entry = None  # the caller needs a PNG this slot never encoded
            img = None
            if entry is None:
                img = _rasterize(path, scale, crop, {idx})[idx]
                rendered += 1
            item, cached = _item_for(idx, img, entry, keys, previous)
            future = pool.submit(_encode_png, img) if _fill_png(item, cached, known, idx) else None
            waiting.append((idx, item, cached, future))
            while waiting and emit(waiting[0], False):
                waiting.pop(0)
        while waiting:
            emit(waiting[0], True)
            waiting.pop(0)

    if slot is not None:
        _RENDERED[slot] = current

    for idx in sorted(items):
        item = items[idx]
        sent = dict(item)
        if sent.pop("png", None) is not None:
            sent["sent"] = True
        out.append(sent)

    return {"scale": scale, "pageCount": page_count, "images": out,
            "rendered": rendered, "streamed": True}


def op_raster(req):
    """Rasterize a PDF to PNGs for the preview pane.

    Uses snapshot_pdf.render_pdf_pages — the same rasterizer the visual
    regression test uses, so what the preview shows and what the test
    compares come from one code path.

    Request fields besides `path` and `scale`:
      pages  — 1-based page numbers to return, or null for all
      known  — {"<page>": "<hash>"} of images the caller already holds;
               a page whose pixels hash the same comes back without a
               PNG, marked unchanged
      crop   — "letter" to crop to US Letter in memory before rendering
               (the live preview renders Chromium's raw print this way);
               omitted, the file is rendered as it is
      slot   — a name for this stream of renders (the engine sends the
               document). With it, pages whose PDF-level key matches the
               slot's last render are not rendered again. See above.

    Imported lazily: pypdfium2 and Pillow are only needed for preview
    and snapshot work, and a worker used purely for building should not
    fail to start because they are missing.
    """
    path = Path(req["path"])
    if not path.exists():
        raise FileNotFoundError(f"no PDF at {path}")

    scale = float(req.get("scale") or 2.0)
    crop = req.get("crop") or None
    if crop not in (None, "letter"):
        raise ValueError(f"unknown crop {crop!r}; expected 'letter' or none")
    slot = req.get("slot") or None
    wanted = req.get("pages")
    # Pages the caller already holds, as {"<page>": "<hash>"}. A page whose
    # pixels hash the same is returned without a PNG, marked unchanged,
    # so an edit on page 1 does not re-encode page 2 or send it back.
    known = req.get("known") or {}

    keys = None
    previous = {}
    if slot is not None:
        keys = _slot_keys(path.read_bytes(), scale, crop)
        previous = _RENDERED.get(slot) or {}
        # Forget the slot until this render has finished, so a failure
        # halfway can never leave it describing a mix of two prints.
        _RENDERED.pop(slot, None)

    def reusable(index):
        """The previous render's entry for this page, if it may stand in."""
        if keys is None:
            return None
        entry = previous.get(keys[index])
        if entry is None:
            return None
        if known.get(str(index + 1)) == entry["hash"] or entry.get("png"):
            return entry
        return None  # the caller needs a PNG this slot never encoded

    # Streaming: each page sent as it is ready, most wanted first. Needs
    # the page count before the first page is rendered, which is what the
    # slot's keys give; without them this falls through to the one-reply
    # path below, which is also what every caller that does not ask for
    # streaming gets.
    if req.get("stream") and keys is not None and req.get("id") is not None:
        streamed = _raster_streamed(req, path, scale, crop, keys, previous,
                                    known, wanted, slot)
        # None: the keys do not describe this file after all. Nothing was
        # sent, so the one-reply path below answers it in full.
        if streamed is not None:
            return streamed

    # Which pages to render: every wanted page that has no reusable entry.
    # With no keys this is every wanted page, as before.
    page_count = None
    if keys is not None:
        page_count = len(keys)
        indices = [i for i in range(page_count) if not wanted or (i + 1) in wanted]
        only = {i for i in indices if reusable(i) is None}
    else:
        only = None if not wanted else {p - 1 for p in wanted}

    images = _rasterize(path, scale, crop, only)
    if keys is not None and len(images) != len(keys):
        # The key reader and pdfium disagree about the page count: trust
        # neither the keys nor the partial render, and render everything.
        keys = None
        page_count = None
        images = _rasterize(path, scale, crop, None)

    out = []
    to_encode = []
    current = {}
    for idx, img in enumerate(images):
        if wanted and (idx + 1) not in wanted:
            continue
        entry = reusable(idx) if img is None else None
        if img is None and entry is None:
            # Not rendered and nothing to stand in for it: cannot happen
            # unless the file changed under us. Render it now.
            img = _rasterize(path, scale, crop, {idx})[idx]
        item, cached = _item_for(idx, img, entry, keys, previous)
        if _fill_png(item, cached, known, idx):
            to_encode.append((item, cached, img))
        if keys is not None:
            current[keys[idx]] = cached
        out.append(item)

    # PNG encoding is most of the rasterize step, and zlib releases the
    # GIL, so the pages are encoded side by side. The bytes are the same
    # as encoding them one after another.
    if len(to_encode) > 1:
        workers = min(len(to_encode), os.cpu_count() or 1)
        with ThreadPoolExecutor(max_workers=workers) as pool:
            encoded = list(pool.map(_encode_png, (img for _, _, img in to_encode)))
    else:
        encoded = [_encode_png(img) for _, _, img in to_encode]
    for (item, cached, _), png in zip(to_encode, encoded):
        item["png"] = png
        cached["png"] = png

    if slot is not None and keys is not None:
        _RENDERED[slot] = current

    return {
        "scale": scale,
        "pageCount": page_count if page_count is not None else len(images),
        "images": out,
        "rendered": sum(1 for img in images if img is not None),
    }


def op_compare(req):
    """Pixel-diff two PDFs and report how much of each page differs.

    Exists so tests/test_engine_equivalence.js can get a real number
    without this project taking on a PNG-decoding dependency in Node
    just to compare two images. It reuses snapshot_pdf's rasterizer and
    its tolerance constants, so "different" means here exactly what it
    means to the visual-regression suite.

    Returns one entry per page. `fraction` is the share of pixels
    differing by more than PIXEL_RGB_TOLERANCE on any channel, and
    `pass` applies MAX_DIFF_FRACTION — the same threshold and the same
    arithmetic the snapshot test uses.
    """
    import pypdfium2 as pdfium
    import snapshot_pdf
    from PIL import ImageChops

    a_path, b_path = Path(req["a"]), Path(req["b"])
    for p in (a_path, b_path):
        if not p.exists():
            raise FileNotFoundError(f"no PDF at {p}")

    saved_scale = snapshot_pdf.SCALE
    snapshot_pdf.SCALE = float(req.get("scale") or snapshot_pdf.SCALE)
    try:
        a_pages = snapshot_pdf.render_pdf_pages(pdfium, a_path)
        b_pages = snapshot_pdf.render_pdf_pages(pdfium, b_path)
    finally:
        snapshot_pdf.SCALE = saved_scale

    if len(a_pages) != len(b_pages):
        return {
            "pageCountMatches": False,
            "aPages": len(a_pages),
            "bPages": len(b_pages),
            "pages": [],
        }

    pages = []
    for idx, (a_img, b_img) in enumerate(zip(a_pages, b_pages), 1):
        if a_img.size != b_img.size:
            pages.append({
                "page": idx, "pass": False, "fraction": 1.0,
                "note": f"{a_img.size} vs {b_img.size}",
            })
            continue
        diff = ImageChops.difference(a_img, b_img)
        differing = sum(
            1 for v in diff.convert("L").getdata()
            if v > snapshot_pdf.PIXEL_RGB_TOLERANCE
        )
        fraction = differing / (a_img.width * a_img.height)
        pages.append({
            "page": idx,
            "pass": fraction <= snapshot_pdf.MAX_DIFF_FRACTION,
            "fraction": fraction,
            "differingPixels": differing,
            "size": [a_img.width, a_img.height],
        })

    return {
        "pageCountMatches": True,
        "threshold": snapshot_pdf.MAX_DIFF_FRACTION,
        "pages": pages,
        "allPass": all(p["pass"] for p in pages),
    }


def op_shutdown(_req):
    return {"bye": True}


OPS = {
    "ping": op_ping,
    "build": op_build,
    "build_letter": op_build_letter,
    "crop": op_crop,
    "raster": op_raster,
    "compare": op_compare,
    "shutdown": op_shutdown,
}


# ---------------------------------------------------------------------------
# Dispatch
# ---------------------------------------------------------------------------

def handle(req):
    """Run one request and return its response frame.

    Everything the operation prints is captured and returned in the
    frame. Every failure mode — unknown op, bad YAML (which reaches
    here as the SystemExit that build.py's fail() raises), a locked
    output file, an unexpected crash — becomes an error frame. The
    worker does not exit on any of them.
    """
    rid = req.get("id")
    op_name = req.get("op")
    fn = OPS.get(op_name)

    out_buf, err_buf = io.StringIO(), io.StringIO()
    started = time.perf_counter()

    frame = {"id": rid, "op": op_name}

    if fn is None:
        frame.update({
            "ok": False,
            "error": {"kind": "unknown_op",
                      "message": f"unknown op {op_name!r}",
                      "detail": [f"known ops: {', '.join(sorted(OPS))}"]},
            "log": [], "diagnostics": [], "ms": 0.0,
        })
        return frame

    try:
        with redirect_stdout(out_buf), redirect_stderr(err_buf):
            result = fn(req)
        frame.update({"ok": True, "result": result})

    except SystemExit as e:
        # build.py's fail() ends in sys.exit(1). In a one-shot CLI run
        # that is the correct behavior; here it is a reportable error
        # and the worker survives it. The user-facing explanation fail()
        # already wrote is in err_buf and reaches the GUI as diagnostics.
        frame.update({
            "ok": False,
            "error": {"kind": "build_failed",
                      "message": _split_lines(err_buf.getvalue())[0]
                                 if _split_lines(err_buf.getvalue())
                                 else f"build exited with code {e.code}",
                      "detail": _split_lines(err_buf.getvalue())[1:],
                      "exitCode": e.code},
        })

    except FileNotFoundError as e:
        frame.update({
            "ok": False,
            "error": {"kind": "missing_file", "message": str(e), "detail": []},
        })

    except ValueError as e:
        frame.update({
            "ok": False,
            "error": {"kind": "bad_request", "message": str(e), "detail": []},
        })

    except PermissionError as e:
        frame.update({
            "ok": False,
            "error": {"kind": "locked_file",
                      "message": f"Permission denied writing {e.filename or 'output'}.",
                      "detail": ["Another program is holding the file open —",
                                 "most often a PDF viewer. Close it and retry."]},
        })

    except Exception as e:
        # A Jinja UndefinedError out of a 'final' build almost always
        # means dist/placement.json was solved for different data — see
        # the ordering invariant in op_build. Naming that explicitly
        # saves the caller from reading a template traceback.
        kind, detail = "internal", traceback.format_exc().splitlines()[-6:]
        if type(e).__name__ == "UndefinedError" and op_name == "build":
            kind = "stale_placement"
            detail = [
                "dist/placement.json does not match the data just loaded.",
                "Re-run the full sequence: build(measurement) -> measure ->",
                "solve -> build(final). Changing RESUME_DATA_SOURCE without",
                "re-solving is the usual cause.",
            ]
        frame.update({
            "ok": False,
            "error": {"kind": kind,
                      "message": f"{type(e).__name__}: {e}",
                      "detail": detail},
        })

    frame["log"] = _split_lines(out_buf.getvalue())
    frame["diagnostics"] = _split_lines(err_buf.getvalue())
    frame["ms"] = round((time.perf_counter() - started) * 1000, 1)
    return frame


def _warm_imports():
    """Import what the first preview will need, before it is asked for.

    The rasterizer's modules — pypdfium2, Pillow, snapshot_pdf — are
    imported lazily inside op_raster, deliberately: a worker that only
    ever builds HTML should not fail to start because a preview-only
    dependency is missing. That is still true. What was also true is
    that the FIRST preview of every session paid for those imports,
    while the caller sat there having just launched the app.

    So they are imported here instead, after the handshake and before
    the first request can be answered — which is exactly the window in
    which the Node side is launching Chromium and starting Sass in other
    processes. It costs nothing that was not already being spent, and if
    anything is missing or broken this is silent and the lazy import
    inside op_raster reports it, to the request that needed it, as
    before.
    """
    try:
        import pypdfium2  # noqa: F401
        import snapshot_pdf  # noqa: F401
        from PIL import ImageChops  # noqa: F401
    except Exception:
        pass  # op_raster's own import says what is wrong, when it matters


def main():
    # Line-buffered utf-8 on both ends. Without the explicit encoding a
    # Windows console code page would mangle the console symbols the
    # frames carry back as log text.
    try:
        sys.stdin.reconfigure(encoding="utf-8")
        _FRAME_OUT.reconfigure(encoding="utf-8", newline="\n")
    except AttributeError:
        pass  # Python < 3.7 reconfigure; the pinned floor is 3.10+.

    _send({"id": None, "op": "hello", "ok": True,
           "result": op_ping({}), "log": [], "diagnostics": [], "ms": 0.0})

    _warm_imports()

    for line in sys.stdin:
        line = line.strip()
        if not line:
            continue
        try:
            req = json.loads(line)
        except json.JSONDecodeError as e:
            _send({"id": None, "op": None, "ok": False,
                   "error": {"kind": "bad_request",
                             "message": f"malformed JSON request: {e}",
                             "detail": []},
                   "log": [], "diagnostics": [], "ms": 0.0})
            continue

        frame = handle(req)
        _send(frame)

        if req.get("op") == "shutdown":
            return 0

    return 0


if __name__ == "__main__":
    sys.exit(main())
