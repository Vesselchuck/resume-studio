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
    raster  -> snapshot_pdf.render_pdf_pages      (the snapshot test's own rasterizer)

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
                 {env}  ......... optional {"RESUME_DATA_SOURCE": "mine"}
  build_letter-> {} .............. the single-page cover letter
  crop        -> {input, output, meta}
  raster      -> {path, scale, pages} -> base64 PNGs
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
import io
import json
import os
import sys
import time
import traceback
from contextlib import redirect_stdout, redirect_stderr
from pathlib import Path

# build/ on sys.path so sibling modules import the same way they do when
# run as scripts. Mirrors the `sys.path.insert` line at the top of
# build.py, crop_pdf.py and snapshot_pdf.py.
sys.path.insert(0, str(Path(__file__).parent))

import build as build_mod            # noqa: E402
import crop_pdf as crop_mod          # noqa: E402
import _console as c                 # noqa: E402

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

    # Per-request env overrides (RESUME_DATA_SOURCE), restored after.
    # build.py's load_data() reads os.environ at call time, so this is
    # the same switch the CLI gets from the shell.
    overrides = req.get("env") or {}
    saved = {k: os.environ.get(k) for k in overrides}
    for k, v in overrides.items():
        if v is None:
            os.environ.pop(k, None)
        else:
            os.environ[k] = str(v)

    # _console prefixes the first banner of a process with a newline.
    # Reset it per request so a warm build's captured log is formatted
    # identically to a cold run's.
    c._first_banner = True

    try:
        build_mod.build(mode=mode)
    finally:
        for k, v in saved.items():
            if v is None:
                os.environ.pop(k, None)
            else:
                os.environ[k] = v

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


def op_build_letter(_req):
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


def op_raster(req):
    """Rasterize a PDF to PNGs for the preview pane.

    Uses snapshot_pdf.render_pdf_pages — the same rasterizer the visual
    regression test uses, so what the preview shows and what the test
    compares come from one code path.

    Imported lazily: pypdfium2 and Pillow are only needed for preview
    and snapshot work, and a worker used purely for building should not
    fail to start because they are missing.
    """
    import pypdfium2 as pdfium
    import snapshot_pdf

    path = Path(req["path"])
    if not path.exists():
        raise FileNotFoundError(f"no PDF at {path}")

    scale = float(req.get("scale") or 2.0)

    # render_pdf_pages applies snapshot_pdf.SCALE. Swap it for the
    # requested preview scale and put it back, so the snapshot test's
    # own constant is never left modified.
    saved_scale = snapshot_pdf.SCALE
    snapshot_pdf.SCALE = scale
    try:
        images = snapshot_pdf.render_pdf_pages(pdfium, path)
    finally:
        snapshot_pdf.SCALE = saved_scale

    wanted = req.get("pages")
    out = []
    for idx, img in enumerate(images):
        if wanted and (idx + 1) not in wanted:
            continue
        buf = io.BytesIO()
        img.save(buf, format="PNG", optimize=False, compress_level=1)
        out.append({
            "page": idx + 1,
            "width": img.width,
            "height": img.height,
            "png": base64.b64encode(buf.getvalue()).decode("ascii"),
        })

    return {"scale": scale, "pageCount": len(images), "images": out}


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
