"""
_marked_content.py — what a PDF/UA checker asks of a tagged PDF's
content: everything drawn is tagged or an artifact, nothing tagged is
inside an artifact, the content's operators are balanced, and every
MCID belongs to the structure tree. For the suites that check
build/crop_pdf.py's artifact marking.

WHY FORMS
─────────
Reading only each page's own content stream misses what Chromium puts
in form XObjects: a group (opacity, a blend mode) is drawn as a form,
and its content tagged inside it. A page-only check sees one `Do`
operator. Counted as drawing, it passes when the `Do` is wrapped in
/Artifact, which is exactly the case where the tagged content inside
becomes an artifact. So this walks into every form a stream draws,
carrying the marked-content sequences open at its `Do`, the way a
viewer reads it (ISO 32000 14.6: a form's content is inside whatever
sequence its `Do` is in).

WHAT COVERS DRAWING
───────────────────
Only two kinds of marked-content sequence make drawing tagged or an
artifact: an /Artifact, and a sequence with an MCID, given inline
(<< /MCID 0 >>) or as a named property list in the stream's
/Properties (/P /MC0 BDC). Drawing inside any other sequence, optional
content (/OC) or a bare /Span, is neither, and is loose.
"""

from collections import namedtuple

DRAWING = {b"f", b"F", b"f*", b"S", b"s", b"B", b"B*", b"b", b"b*",
           b"Tj", b"TJ", b"'", b'"', b"sh", b"INLINE IMAGE"}

#: What check() found, each a list of "page N: …" or "page N /X1: …"
#: (inside a form) strings, in the order met.
Report = namedtuple("Report", "loose hidden unbalanced orphans")


def problems(pdf):
    """(loose, hidden) for every page of `pdf` (a PdfReader or PdfWriter).

    loose   drawing outside every /Artifact and every tagged sequence
    hidden  tagged content (a BDC with an MCID) inside an /Artifact,
            including one left open around the `Do` of the form it is in
    """
    report = check(pdf)
    return report.loose, report.hidden


def check(pdf):
    """A Report for every page of `pdf`: problems()' loose and hidden, and

    unbalanced  an EMC, Q or ET with nothing of its kind open in the same
                stream, a BT inside a BT, or a stream that ends with a
                marked-content sequence, q or BT of its own still open
    orphans     an MCID the structure tree does not lead back to: its
                stream (the page, or the form) has no /StructParents,
                the ParentTree has no structure element at that MCID, or
                that element does not list the MCID among its kids
    """
    from pypdf.generic import ContentStream, IndirectObject
    loose, hidden, unbalanced, orphans = [], [], [], []
    root = pdf.root_object if hasattr(pdf, "root_object") else pdf.trailer["/Root"]
    tree = root.get("/StructTreeRoot")
    tree = tree.get_object() if tree is not None else {}
    parents = _number_tree(tree.get("/ParentTree"))

    def properties(operands, op, resources):
        """The property list of a BDC, inline or named, or None."""
        props = operands[1] if op == b"BDC" and len(operands) > 1 else None
        if isinstance(props, str):   # a NameObject: look it up
            named = resources.get("/Properties")
            props = named.get_object().get(props) if named is not None else None
        return props.get_object() if props is not None else None

    def lists(element, mcid, page, form):
        """Whether structure element `element` lists `mcid` in `page`,
        or in `form` (a reference) when it is not None."""
        kids = element.get("/K")
        for kid in kids if isinstance(kids, list) else [kids]:
            if isinstance(kid, IndirectObject):
                kid = kid.get_object()
            if form is None and isinstance(kid, int) and not isinstance(kid, bool):
                if kid == mcid:
                    return True
            elif hasattr(kid, "get") and kid.get("/MCID") == mcid:
                stm = kid.get("/Stm")
                if form is None and stm is None:
                    return True
                if (form is not None and isinstance(stm, IndirectObject)
                        and stm.idnum == form.idnum):
                    return True
        return False

    def mcid_belongs(mcid, owner, page, form, where):
        parent = owner.get("/StructParents")
        if parent is None:
            orphans.append(f"{where}: MCID {mcid}, in a stream without /StructParents")
            return
        entry = parents.get(int(parent))
        entry = entry.get_object() if entry is not None else None
        element = entry[mcid] if isinstance(entry, list) and 0 <= mcid < len(entry) else None
        element = element.get_object() if element is not None else None
        if not hasattr(element, "get") or "/S" not in element:
            orphans.append(f"{where}: MCID {mcid}, with no structure element at "
                           f"ParentTree {int(parent)}")
        elif not lists(element, mcid, page, form):
            orphans.append(f"{where}: MCID {mcid}, which its structure element "
                           f"({element['/S']}) does not list")

    def walk(stream, owner, form, resources, open_marks, where, page, depth):
        if depth > 16:   # a form that draws itself
            return
        resources = resources.get_object() if resources is not None else {}
        stack = list(open_marks)   # (tag, covers) of each open sequence
        q = 0
        in_text = False
        for operands, op in ContentStream(stream, pdf).operations:
            if op in (b"BDC", b"BMC"):
                kind = str(operands[0]) if operands else ""
                props = properties(operands, op, resources)
                mcid = props.get("/MCID") if hasattr(props, "get") else None
                if mcid is not None:
                    if any(tag == "/Artifact" for tag, _ in stack):
                        hidden.append(f"{where}: MCID {mcid}")
                    mcid_belongs(int(mcid), owner, page, form, where)
                stack.append((kind, kind == "/Artifact" or mcid is not None))
            elif op == b"EMC":
                if len(stack) > len(open_marks):
                    stack.pop()
                else:
                    unbalanced.append(f"{where}: EMC with no BMC or BDC open")
            elif op == b"q":
                q += 1
            elif op == b"Q":
                if q:
                    q -= 1
                else:
                    unbalanced.append(f"{where}: Q with no q open")
            elif op == b"BT":
                if in_text:
                    unbalanced.append(f"{where}: BT inside BT")
                in_text = True
            elif op == b"ET":
                if not in_text:
                    unbalanced.append(f"{where}: ET with no BT open")
                in_text = False
            elif op == b"Do":
                covered = any(covers for _, covers in stack)
                xobjects = resources.get("/XObject")
                ref = xobjects.get_object().get(operands[0]) if xobjects else None
                xobject = ref.get_object() if ref is not None else None
                if xobject is not None and xobject.get("/Subtype") == "/Form":
                    walk(xobject, xobject, ref if isinstance(ref, IndirectObject) else None,
                         xobject.get("/Resources") or resources, stack,
                         f"{where.split(' /')[0]} {operands[0]}", page, depth + 1)
                elif not covered:
                    loose.append(f"{where}: Do")
            elif op in DRAWING and not any(covers for _, covers in stack):
                loose.append(f"{where}: {op.decode('latin-1')}")
        if len(stack) > len(open_marks):
            unbalanced.append(f"{where}: {len(stack) - len(open_marks)} marked-content "
                              f"sequence(s) left open")
        if q:
            unbalanced.append(f"{where}: {q} q left open")
        if in_text:
            unbalanced.append(f"{where}: BT left open")

    for number, page in enumerate(pdf.pages, 1):
        contents = page.get_contents()
        if contents is not None:
            walk(contents, page, None, page.get("/Resources"), [], f"page {number}",
                 page, 0)
    # A form drawn twice is walked twice: say what is wrong with its
    # stream once. Drawing is listed as met, each operator.
    return Report(loose, hidden, list(dict.fromkeys(unbalanced)),
                  list(dict.fromkeys(orphans)))


def _number_tree(node, out=None):
    """A number tree (ISO 32000 7.9.7) as {key: value}."""
    out = {} if out is None else out
    node = node.get_object() if node is not None else None
    if not hasattr(node, "get"):
        return out
    nums = node.get("/Nums") or []
    for key, value in zip(nums[::2], nums[1::2]):
        out[int(key)] = value
    for kid in node.get("/Kids") or []:
        _number_tree(kid, out)
    return out
