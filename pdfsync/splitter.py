"""
pdfsync/splitter.py — تقسيم محتوى EPUB الأصلي إلى صفحات عند مواضع قص محددة.

المبدأ: النص والوسوم من EPUB فقط، ولا يُعاد كتابة أي حرف؛ نقصّ النص الأصلي عند حدود الصفحات.
إن عبر القصّ عنصرًا (فقرة/عنوانًا) يُستنسخ العنصر وآباؤه في كل صفحة يمتد إليها:

    <p id="m25">الجزء الأول ... | الجزء الثاني</p>
        صفحة 25: <p id="m25">الجزء الأول ...</p>
        صفحة 26: <p>الجزء الثاني</p>

تيارات النص:
  - تيار المتن: كل ما ليس حاشية، له حدود قص خاصة (los/his).
  - تيار الحواشي: عناصر class=footnote ... وهي تتجمع في EPUB الأصلي آخر الفصل، فتُقَصّ بحدود
    مستقلة (nlos/nhis) وتوضع أسفل الصفحة التي تخصها داخل <div class="pdf-footnotes">.

صفحات الصور: الصفحة التي لا نص لها في PDF (غلاف، لوحة، ...) أو التي لا يوجد لها نص مقابل في EPUB
تحمل صورة مرسومة من PDF نفسه (بلا OCR).
"""
from __future__ import annotations

import copy
import logging
import posixpath
from bisect import bisect_right
from dataclasses import dataclass, field

from lxml import etree

from .epub_source import OPS_NS, XHTML_NS, SourceDoc, SourceEpub, collect_text, resolve_path

logger = logging.getLogger("book2epub.pdfsync.splitter")

_X = f"{{{XHTML_NS}}}"
_XLINK = "{http://www.w3.org/1999/xlink}href"
_REF_ATTRS = ("href", "src", "data", "poster", _XLINK)

_PAGE_TMPL = (
    '<html xmlns="http://www.w3.org/1999/xhtml" xmlns:epub="http://www.idpf.org/2007/ops" '
    'xml:lang="{lang}" lang="{lang}" dir="rtl"><head><meta charset="utf-8"/><title/></head><body/></html>'
)


def page_filename(pdf_page_number: int) -> str:
    return f"page-{pdf_page_number:04d}.xhtml"


@dataclass
class PageAsset:
    """صورة تُضمَّن في الصفحة (مرسومة من PDF)."""
    zip_name: str                  # اسم داخل مجلد Images/
    media_type: str
    data: bytes
    alt: str = ""
    y_ratio: float | None = None   # للصور داخل صفحة نصية: نسبة النص الذي يسبقها (0..1)
    block: bool = True


@dataclass
class PageFragment:
    number: int                      # رقم صفحة PDF
    index: int                       # فهرس 0-based ضمن الصفحات المستهدفة
    root: etree._Element
    body: etree._Element
    ids: set[str] = field(default_factory=set)
    docs: list[int] = field(default_factory=list)
    assets: list[PageAsset] = field(default_factory=list)


def _slice(text: str, start: int, lo: int, hi: int) -> str:
    a = max(lo - start, 0)
    b = min(hi - start, len(text))
    return text[a:b] if b > a else ""


def _add_text(el: etree._Element, text: str) -> None:
    if not text:
        return
    if len(el):
        el[-1].tail = (el[-1].tail or "") + text
    else:
        el.text = (el.text or "") + text


class PageSplitter:
    def __init__(self, src: SourceEpub, los: list[int], his: list[int],
                 nlos: list[int], nhis: list[int], page_numbers: list[int],
                 language: str = "ar", page_labels: list[str] | None = None,
                 page_assets: dict[int, list[PageAsset]] | None = None):
        self.src = src
        self.los, self.his = los, his
        self.nlos, self.nhis = nlos, nhis
        self.numbers = page_numbers
        self.lang = language
        self.labels = page_labels or [str(n) for n in page_numbers]
        self.assets = page_assets or {}
        self.page_dir = posixpath.join(src.opf_dir, "Text") if src.opf_dir else "Text"
        self.nav_new_path = posixpath.join(src.opf_dir, "nav.xhtml") if src.opf_dir else "nav.xhtml"
        self.img_dir = posixpath.join(src.opf_dir, "Images") if src.opf_dir else "Images"
        self.docs_by_path = src.doc_by_path()
        self.idmap: dict[tuple[int, str], str] = {}
        self.pending: list[tuple[etree._Element, str, str, str | None]] = []
        self.unresolved: list[str] = []
        self._used: set[str] = set()
        self._note_sets = {d.index: set(d.note_roots) for d in src.docs}
        # الأسلاف الذين يحتوون حواشي: لا يجوز نسخهم دفعة واحدة (وإلا تتكرر الحواشي داخل المتن)
        self._note_anc: dict[int, set] = {}
        for d in src.docs:
            anc: set = set()
            for r in list(d.note_roots) + list(d.skipped):
                a = r.getparent()
                while a is not None and a is not d.body.getparent():
                    anc.add(a)
                    a = a.getparent()
            self._note_anc[d.index] = anc

    # ------------------------------------------------------------ مساعدات

    def page_of(self, offset: int, stream: int = 0) -> int:
        cuts = self.los if stream == 0 else self.nlos
        k = bisect_right(cuts, offset) - 1
        return min(max(k, 0), len(cuts) - 1)

    def _process_attrs(self, el: etree._Element, doc: SourceDoc, strip_id: bool) -> None:
        eid = el.get("id")
        if eid is not None:
            if strip_id:
                del el.attrib["id"]
            else:
                final = eid
                n = 0
                while final in self._used:
                    n += 1
                    final = f"{eid}-d{doc.index}" if n == 1 else f"{eid}-d{doc.index}-{n}"
                self._used.add(final)
                if final != eid:
                    el.set("id", final)
                self.idmap[(doc.index, eid)] = final

        for attr in _REF_ATTRS:
            val = el.get(attr)
            if not val:
                continue
            target, frag = resolve_path(doc.path, val)
            if target is None:
                continue
            if target in self.docs_by_path:
                self.pending.append((el, attr, target, frag))
            elif target == self.src.nav_path:
                rel = posixpath.relpath(self.nav_new_path, self.page_dir)
                el.set(attr, rel + (f"#{frag}" if frag else ""))
            elif target in self.src.resources:
                rel = posixpath.relpath(target, self.page_dir)
                el.set(attr, rel + (f"#{frag}" if frag else ""))
            else:
                self.unresolved.append(f"{doc.path}: {attr}={val}")

    def _register_subtree(self, root: etree._Element, doc: SourceDoc) -> None:
        for e in root.iter():
            if isinstance(e.tag, str):
                self._process_attrs(e, doc, strip_id=False)

    # ------------------------------------------------------------ التعبئة

    def _fill(self, doc: SourceDoc, src_el: etree._Element, new_el: etree._Element, lo: int, hi: int,
              skip_notes: bool) -> None:
        s, _ = doc.spans[src_el]
        notes = self._note_sets[doc.index]
        anc = self._note_anc[doc.index]
        if src_el.text:
            _add_text(new_el, _slice(src_el.text, s, lo, hi))
        pos = s + len(src_el.text or "")
        for c in src_el:
            if not isinstance(c.tag, str):                      # تعليق/تعليمة معالجة
                if c.tail:
                    _add_text(new_el, _slice(c.tail, pos, lo, hi))
                    pos += len(c.tail)
                continue
            if skip_notes and (c in notes or c in doc.skipped):  # الحواشي لا تدخل تيار المتن
                if c.tail:
                    _add_text(new_el, _slice(c.tail, pos, lo, hi))
                    pos += len(c.tail)
                continue
            cs, ce = doc.spans[c]
            if ce > cs:
                visible = not (ce <= lo or cs >= hi)
            else:
                visible = lo <= cs < hi
            nc: etree._Element | None = None
            if visible:
                if cs >= lo and ce <= hi and not (skip_notes and c in anc):
                    nc = copy.deepcopy(c)
                    nc.tail = None
                    new_el.append(nc)
                    self._register_subtree(nc, doc)
                else:
                    nc = etree.SubElement(new_el, c.tag, attrib=dict(c.attrib))
                    self._fill(doc, c, nc, lo, hi, skip_notes)
                    if (lo <= cs < hi) or (nc.text or "").strip() or len(nc):
                        self._process_attrs(nc, doc, strip_id=(cs < lo))
                    else:
                        new_el.remove(nc)
                        nc = None
            if c.tail:
                piece = _slice(c.tail, ce, lo, hi)
                if nc is not None:
                    nc.tail = piece or None
                else:
                    _add_text(new_el, piece)
            pos = ce + len(c.tail or "")

    def _fill_notes(self, doc: SourceDoc, container: etree._Element, lo: int, hi: int) -> bool:
        wrote = False
        for root in doc.note_roots:
            cs, ce = doc.spans[root]
            if ce > cs:
                visible = not (ce <= lo or cs >= hi)
            else:
                visible = lo <= cs < hi
            if not visible:
                continue
            if cs >= lo and ce <= hi:
                nc = copy.deepcopy(root)
                nc.tail = "\n"
                container.append(nc)
                self._register_subtree(nc, doc)
            else:
                nc = etree.SubElement(container, root.tag, attrib=dict(root.attrib))
                self._fill(doc, root, nc, lo, hi, skip_notes=False)
                self._process_attrs(nc, doc, strip_id=(cs < lo))
                nc.tail = "\n"
            wrote = True
        return wrote

    # ------------------------------------------------------------ الصور

    def _img_element(self, asset: PageAsset) -> etree._Element:
        rel = posixpath.relpath(posixpath.join(self.img_dir, asset.zip_name), self.page_dir)
        div = etree.Element(f"{_X}div", {"class": "pdf-page-image"})
        etree.SubElement(div, f"{_X}img", {"src": rel, "alt": asset.alt or ""})
        return div

    def _place_assets(self, body: etree.Element, assets: list[PageAsset], marker: etree._Element) -> None:
        """صور الصفحات: الكاملة في مكانها (قبل/بعد النص) والمنسوبة بنسبة y بين عناصر المتن."""
        top = [c for c in body if c is not marker and isinstance(c.tag, str)]
        lengths = [len("".join(c.itertext()).strip()) for c in top]
        total = sum(lengths) or 1
        for a in sorted(assets, key=lambda x: (x.y_ratio if x.y_ratio is not None else -1)):
            el = self._img_element(a)
            if a.y_ratio is None or not top:
                body.append(el)
                continue
            target = a.y_ratio * total
            cum = 0
            idx = len(top)
            for n, ln in enumerate(lengths):
                if cum + ln > target:
                    idx = n
                    break
                cum += ln
            if idx >= len(top):
                body.append(el)
            else:
                top[idx].addprevious(el)

    # ------------------------------------------------------------ بناء صفحة

    def build_page(self, k: int) -> PageFragment:
        lo, hi = self.los[k], self.his[k]
        nlo, nhi = self.nlos[k], self.nhis[k]
        number = self.numbers[k]
        root = etree.fromstring(_PAGE_TMPL.format(lang=self.lang).encode("utf-8"))
        head = root.find(f"{_X}head")
        body = root.find(f"{_X}body")
        self._used = set()

        marker_id = f"pg-{number:04d}"
        label = self.labels[k]
        marker = etree.SubElement(body, f"{_X}span", {
            "id": marker_id, f"{{{OPS_NS}}}type": "pagebreak", "role": "doc-pagebreak",
            "aria-label": label, "title": label,
        })
        self._used.add(marker_id)

        frag = PageFragment(number=number, index=k, root=root, body=body)
        css: list[str] = []
        for doc in self.src.docs:
            body_contrib = (not (doc.end <= lo or doc.start >= hi)) if doc.end > doc.start else (lo <= doc.start < hi)
            if hi > lo and body_contrib:
                frag.docs.append(doc.index)
                for sheet in doc.stylesheets:
                    if sheet in self.src.resources and sheet not in css:
                        css.append(sheet)
                self._fill(doc, doc.body, body, lo, hi, skip_notes=True)

        assets = self.assets.get(number, [])
        if assets:
            self._place_assets(body, assets, marker)
            frag.assets = assets

        if nhi > nlo:
            box = etree.Element(f"{_X}div", {"class": "pdf-footnotes"})
            etree.SubElement(box, f"{_X}hr", {"class": "pdf-footnote-separator"})
            wrote = False
            for doc in self.src.docs:
                if doc.nend > doc.nstart and not (doc.nend <= nlo or doc.nstart >= nhi):
                    if doc.index not in frag.docs:
                        frag.docs.append(doc.index)
                    for sheet in doc.stylesheets:
                        if sheet in self.src.resources and sheet not in css:
                            css.append(sheet)
                    wrote |= self._fill_notes(doc, box, nlo, nhi)
            if wrote:
                body.append(box)

        for sheet in css:
            etree.SubElement(head, f"{_X}link", {
                "rel": "stylesheet", "type": "text/css", "href": posixpath.relpath(sheet, self.page_dir)})
        etree.SubElement(head, f"{_X}link", {
            "rel": "stylesheet", "type": "text/css",
            "href": posixpath.relpath(posixpath.join(self.src.opf_dir or "", "Styles/pdfsync.css"), self.page_dir)})
        frag.ids = set(self._used)
        return frag

    def build_all(self) -> list[PageFragment]:
        pages = [self.build_page(k) for k in range(len(self.los))]
        self._resolve_pending(pages)
        for p in pages:
            title = p.root.find(f"{_X}head/{_X}title")
            title.text = f"{self.src.metadata.get('title') or ''} — {self.labels[p.index]}".strip(" —")
            etree.cleanup_namespaces(p.root)
        return pages

    # ------------------------------------------------------------ الروابط

    def href_for(self, path: str | None, frag: str | None, pages_ids: list[set[str]] | None = None) -> str | None:
        """رابط (نسبةً لمجلد OPF) إلى موضع (مستند، fragment) في الصفحات الجديدة."""
        if not path or path not in self.docs_by_path:
            return None
        doc = self.docs_by_path[path]
        if frag and frag in doc.ids:
            stream, off = doc.ids[frag]
            k = self.page_of(off, stream)
            final = self.idmap.get((doc.index, frag))
            ok = final is not None and (pages_ids is None or final in pages_ids[k])
            tail = f"#{final}" if ok else ""
        else:
            k = self.page_of(doc.start)
            tail = ""
        return posixpath.join("Text", page_filename(self.numbers[k])) + tail

    def _resolve_pending(self, pages: list[PageFragment]) -> None:
        ids = [p.ids for p in pages]
        broken = 0
        for el, attr, target, frag in self.pending:
            href = self.href_for(target, frag, ids)
            if href is None:
                broken += 1
                continue
            el.set(attr, href.split("/", 1)[1])
            if frag and "#" not in href:
                self.unresolved.append(f"fragment غير موجود: {target}#{frag}")
        if broken:
            logger.warning("روابط داخلية تعذّر حلها: %d", broken)
        self.pending.clear()

    # ------------------------------------------------------------ تحقق

    def total_body_text(self, pages: list[PageFragment]) -> str:
        out = []
        for p in pages:
            clone = copy.deepcopy(p.body)
            for e in list(clone.iter(f"{_X}div")):
                if e.get("class") in ("pdf-footnotes", "pdf-page-image"):
                    tail = e.tail or ""
                    parent = e.getparent()
                    prev = e.getprevious()
                    parent.remove(e)
                    if prev is not None:
                        prev.tail = (prev.tail or "") + tail
                    else:
                        parent.text = (parent.text or "") + tail
            out.append(collect_text(clone))
        return "".join(out)

    def total_note_text(self, pages: list[PageFragment]) -> str:
        out = []
        for p in pages:
            for box in p.body.iter(f"{_X}div"):
                if box.get("class") == "pdf-footnotes":
                    for c in box:
                        if c.tag == f"{_X}hr":
                            continue
                        out.append(collect_text(c))
        return "".join(out)
