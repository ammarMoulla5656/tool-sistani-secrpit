"""
pdfsync/epub_source.py — قراءة EPUB الأصلي (مصدر الحقيقة للنص والتنسيق) وفهرسته.

لكل مستند XHTML في الـ spine نبني:
  - النص الخام التسلسلي: تجميع نصوص العقد (text + tail) بترتيب المستند، بالضبط كما هي.
  - spans: لكل عنصر مجال النص [بداية، نهاية) داخل النص الخام الكلي.
  - ids: موضع بداية كل عنصر له id (لإعادة توجيه الروابط الداخلية لاحقًا).

ثم يُبنى النص المُطبَّع E مع خريطة تعيد كل فهرس مُطبَّع إلى موضعه الأصلي، ليُقصَّ
النص الأصلي (وليس المُطبَّع) عند حدود الصفحات.
"""
from __future__ import annotations

import logging
import posixpath
import zipfile
from array import array
from dataclasses import dataclass, field
from pathlib import Path
from urllib.parse import unquote, urlsplit

from lxml import etree

from .normalize import normalize_with_map

logger = logging.getLogger("book2epub.pdfsync.epub")

XHTML_NS = "http://www.w3.org/1999/xhtml"
OPF_NS = "http://www.idpf.org/2007/opf"
DC_NS = "http://purl.org/dc/elements/1.1/"
OPS_NS = "http://www.idpf.org/2007/ops"
CONTAINER_NS = "urn:oasis:names:tc:opendocument:xmlns:container"
NCX_NS = "http://www.daisy.org/z3986/2005/ncx/"

_X = f"{{{XHTML_NS}}}"


@dataclass
class TocItem:
    title: str
    path: str | None = None        # مسار المستند داخل ZIP
    fragment: str | None = None
    children: list["TocItem"] = field(default_factory=list)


@dataclass
class Resource:
    path: str                      # مسار داخل ZIP
    media_type: str
    item_id: str
    properties: str
    data: bytes


@dataclass
class SourceDoc:
    index: int
    path: str
    item_id: str
    root: etree._Element
    body: etree._Element
    spans: dict = field(default_factory=dict)     # عنصر -> (بداية، نهاية) في تيار المتن أو تيار الحواشي
    ids: dict[str, tuple[int, int]] = field(default_factory=dict)   # id -> (تيار 0=متن/1=حواشٍ، موضع)
    note_roots: list = field(default_factory=list)  # جذور عناصر الحواشي بترتيبها
    skipped: set = field(default_factory=set)       # عناصر فاصلة (hr حواشٍ) تُهمل
    start: int = 0
    end: int = 0
    nstart: int = 0
    nend: int = 0
    stylesheets: list[str] = field(default_factory=list)
    title: str = ""


@dataclass
class SourceEpub:
    path: Path
    opf_path: str
    opf_dir: str
    nav_path: str | None
    docs: list[SourceDoc]
    resources: dict[str, Resource]
    metadata: dict
    toc: list[TocItem]
    raw_text: str = ""
    norm_text: str = ""
    norm_map: array = field(default_factory=lambda: array("I"))
    note_raw: str = ""                 # تيار الحواشي (إن وُجدت) — منفصل عن المتن
    note_norm: str = ""
    note_map: array = field(default_factory=lambda: array("I"))

    def doc_by_path(self) -> dict[str, SourceDoc]:
        return {d.path: d for d in self.docs}


# ---------------------------------------------------------------- أدوات عامة

def resolve_path(base_path: str, href: str) -> tuple[str | None, str | None]:
    """حل رابط نسبي إلى (مسار داخل ZIP، fragment). يُعيد (None، None) للروابط الخارجية."""
    if not href:
        return None, None
    parts = urlsplit(href)
    if parts.scheme or parts.netloc:
        return None, None
    frag = unquote(parts.fragment) if parts.fragment else None
    if not parts.path:
        return base_path, frag
    target = posixpath.normpath(posixpath.join(posixpath.dirname(base_path), unquote(parts.path)))
    return target, frag


def collect_text(el: etree._Element) -> str:
    """نص العنصر الخام بنفس قواعد الفهرسة (يتجاهل التعليقات ويحتفظ بذيولها)."""
    out: list[str] = []

    def rec(e: etree._Element) -> None:
        if e.text:
            out.append(e.text)
        for c in e:
            if isinstance(c.tag, str):
                rec(c)
            if c.tail:
                out.append(c.tail)

    rec(el)
    return "".join(out)


def _parse_xml(data: bytes, name: str) -> etree._Element:
    try:
        return etree.fromstring(data, etree.XMLParser(resolve_entities=False, huge_tree=True))
    except etree.XMLSyntaxError as e:
        logger.warning("تعذّر تحليل %s بصرامة (%s)؛ المحاولة بوضع التسامح — قد يضيع جزء من النص", name, e)
        return etree.fromstring(data, etree.XMLParser(resolve_entities=False, huge_tree=True, recover=True))


def _local(tag: str) -> str:
    return tag.rsplit("}", 1)[-1] if isinstance(tag, str) else ""


# ---------------------------------------------------------------- الفهرسة

NOTE_CLASS_PREFIXES = ("footnote", "endnote", "fn")
NOTE_EPUB_TYPES = {"footnote", "footnotes", "endnote", "endnotes", "rearnote", "rearnotes"}


def make_note_detector(extra_classes: tuple[str, ...] = ()):
    prefixes = tuple(c.lower() for c in NOTE_CLASS_PREFIXES + tuple(extra_classes))

    def is_note(el: etree._Element) -> bool:
        for c in (el.get("class") or "").split():
            cl = c.lower()
            if cl == "fn" or cl.startswith(prefixes):
                return True
        types = (el.get(f"{{{OPS_NS}}}type") or "").split()
        return any(t in NOTE_EPUB_TYPES for t in types)

    return is_note


def _index_doc(doc: SourceDoc, base: int, nbase: int, parts: list[str], nparts: list[str],
               is_note) -> tuple[int, int]:
    """يملأ spans/ids لمستند. المتن يذهب إلى تيار، والحواشي (class=footnote ...) إلى تيار آخر.

    الحواشي في مواقع المواقع الأصلية تتجمع عادةً في آخر الفصل؛ لذلك نفصلها لتُطابَق على صفحاتها
    الصحيحة بدل أن تخلط ترتيب المتن.
    """
    pos = [base, nbase]

    def rec(el: etree._Element, s: int) -> None:
        sink = parts if s == 0 else nparts
        start = pos[s]
        eid = el.get("id")
        if eid and eid not in doc.ids:
            doc.ids[eid] = (s, start)
        if el.text:
            sink.append(el.text)
            pos[s] += len(el.text)
        for c in el:
            if isinstance(c.tag, str):
                if s == 0 and is_note(c):
                    if _local(c.tag) == "hr":
                        doc.skipped.add(c)
                        doc.spans[c] = (pos[1], pos[1])
                    else:
                        doc.note_roots.append(c)
                        rec(c, 1)
                else:
                    rec(c, s)
            if c.tail:
                sink.append(c.tail)
                pos[s] += len(c.tail)
        doc.spans[el] = (start, pos[s])

    rec(doc.body, 0)
    doc.start, doc.end = base, pos[0]
    doc.nstart, doc.nend = nbase, pos[1]
    return pos[0], pos[1]


# ---------------------------------------------------------------- الفهرس (TOC)

def _parse_nav_ol(ol: etree._Element, nav_path: str) -> list[TocItem]:
    items: list[TocItem] = []
    for li in ol.findall(f"{_X}li"):
        a = li.find(f"{_X}a")
        label = a if a is not None else li.find(f"{_X}span")
        title = " ".join("".join(label.itertext()).split()) if label is not None else ""
        path = frag = None
        if a is not None and a.get("href"):
            path, frag = resolve_path(nav_path, a.get("href"))
        sub = li.find(f"{_X}ol")
        items.append(TocItem(title, path, frag, _parse_nav_ol(sub, nav_path) if sub is not None else []))
    return items


def _parse_nav(root: etree._Element, nav_path: str) -> list[TocItem]:
    for nav in root.iter(f"{_X}nav"):
        types = (nav.get(f"{{{OPS_NS}}}type") or "").split()
        if "toc" in types:
            ol = nav.find(f"{_X}ol")
            if ol is not None:
                return _parse_nav_ol(ol, nav_path)
    return []


def _parse_ncx(root: etree._Element, ncx_path: str) -> list[TocItem]:
    ns = f"{{{NCX_NS}}}"

    def rec(parent: etree._Element) -> list[TocItem]:
        out: list[TocItem] = []
        for np_ in parent.findall(f"{ns}navPoint"):
            label = np_.find(f"{ns}navLabel/{ns}text")
            content = np_.find(f"{ns}content")
            title = " ".join((label.text or "").split()) if label is not None else ""
            path, frag = resolve_path(ncx_path, content.get("src")) if content is not None else (None, None)
            out.append(TocItem(title, path, frag, rec(np_)))
        return out

    nav_map = root.find(f"{ns}navMap")
    return rec(nav_map) if nav_map is not None else []


# ---------------------------------------------------------------- القراءة الرئيسية

def read_epub(path: Path, split_notes: bool = True, note_classes: tuple[str, ...] = ()) -> SourceEpub:
    path = Path(path)
    with zipfile.ZipFile(path) as zf:
        container = _parse_xml(zf.read("META-INF/container.xml"), "container.xml")
        rootfile = container.find(f".//{{{CONTAINER_NS}}}rootfile")
        if rootfile is None:
            raise ValueError("container.xml لا يحتوي rootfile")
        opf_path = rootfile.get("full-path")
        opf_dir = posixpath.dirname(opf_path)
        opf = _parse_xml(zf.read(opf_path), opf_path)

        # --- manifest
        manifest: dict[str, dict] = {}
        for it in opf.iterfind(f".//{{{OPF_NS}}}manifest/{{{OPF_NS}}}item"):
            href = unquote(it.get("href", ""))
            full = posixpath.normpath(posixpath.join(opf_dir, href)) if opf_dir else posixpath.normpath(href)
            manifest[it.get("id")] = {
                "path": full, "type": it.get("media-type", ""), "props": it.get("properties", ""),
            }

        spine_el = opf.find(f".//{{{OPF_NS}}}spine")
        spine_ids = [r.get("idref") for r in spine_el.iterfind(f"{{{OPF_NS}}}itemref")] if spine_el is not None else []
        ncx_id = spine_el.get("toc") if spine_el is not None else None

        nav_path = next((m["path"] for m in manifest.values() if "nav" in m["props"].split()), None)

        # --- مستندات المحتوى (بدون nav)
        docs: list[SourceDoc] = []
        spine_paths: set[str] = set()
        parts: list[str] = []
        nparts: list[str] = []
        pos = npos = 0
        is_note = make_note_detector(note_classes) if split_notes else (lambda e: False)
        for iid in spine_ids:
            m = manifest.get(iid)
            if not m or m["path"] == nav_path:
                continue
            if m["type"] not in ("application/xhtml+xml", "text/html"):
                continue
            root = _parse_xml(zf.read(m["path"]), m["path"])
            body = root.find(f"{_X}body")
            if body is None:
                body = root.find("body")
            if body is None:
                logger.warning("مستند بلا body: %s (تم تخطيه)", m["path"])
                continue
            doc = SourceDoc(index=len(docs), path=m["path"], item_id=iid, root=root, body=body)
            for link in root.iter(f"{_X}link"):
                if "stylesheet" in (link.get("rel") or "").split() and link.get("href"):
                    target, _ = resolve_path(doc.path, link.get("href"))
                    if target:
                        doc.stylesheets.append(target)
            title_el = root.find(f"{_X}head/{_X}title")
            doc.title = " ".join((title_el.text or "").split()) if title_el is not None and title_el.text else ""
            pos, npos = _index_doc(doc, pos, npos, parts, nparts, is_note)
            docs.append(doc)
            spine_paths.add(doc.path)

        # --- الموارد (كل ما ليس مستند محتوى ولا nav ولا ncx)
        resources: dict[str, Resource] = {}
        ncx_path = manifest[ncx_id]["path"] if ncx_id in manifest else None
        for iid, m in manifest.items():
            if m["path"] in spine_paths or m["path"] in (nav_path, ncx_path):
                continue
            if m["type"] == "application/x-dtbncx+xml":
                ncx_path = ncx_path or m["path"]
                continue
            try:
                data = zf.read(m["path"])
            except KeyError:
                logger.warning("مورد مذكور في manifest وغير موجود في الأرشيف: %s", m["path"])
                continue
            resources[m["path"]] = Resource(m["path"], m["type"], iid, m["props"], data)

        # --- البيانات الوصفية
        md = opf.find(f"{{{OPF_NS}}}metadata")
        metadata: dict = {"title": "", "creators": [], "publisher": "", "language": "ar", "extra": [],
                          "cover_meta": None}
        if md is not None:
            for el in md:
                if not isinstance(el.tag, str):
                    continue
                name = _local(el.tag)
                text = (el.text or "").strip()
                if el.tag == f"{{{DC_NS}}}title" and not metadata["title"]:
                    metadata["title"] = text
                elif el.tag == f"{{{DC_NS}}}creator":
                    metadata["creators"].append(text)
                elif el.tag == f"{{{DC_NS}}}publisher":
                    metadata["publisher"] = text
                elif el.tag == f"{{{DC_NS}}}language":
                    metadata["language"] = text or "ar"
                elif el.tag.startswith(f"{{{DC_NS}}}") and name not in ("identifier",):
                    metadata["extra"].append((name, text))
                elif name == "meta" and el.get("name") == "cover":
                    metadata["cover_meta"] = el.get("content")

        # --- الفهرس
        toc: list[TocItem] = []
        if nav_path:
            toc = _parse_nav(_parse_xml(zf.read(nav_path), nav_path), nav_path)
        if not toc and ncx_path:
            toc = _parse_ncx(_parse_xml(zf.read(ncx_path), ncx_path), ncx_path)

    src = SourceEpub(path=path, opf_path=opf_path, opf_dir=opf_dir, nav_path=nav_path, docs=docs,
                     resources=resources, metadata=metadata, toc=toc)
    src.raw_text = "".join(parts)
    src.norm_text, src.norm_map = normalize_with_map(src.raw_text)
    src.note_raw = "".join(nparts)
    src.note_norm, src.note_map = normalize_with_map(src.note_raw)
    logger.info("EPUB: %d مستند محتوى | متن: %d حرفًا خامًا / %d مُطبَّعًا | حواشٍ: %d / %d | %d مورد",
                len(docs), len(src.raw_text), len(src.norm_text), len(src.note_raw), len(src.note_norm),
                len(resources))
    return src
