"""
pdfsync/splitter.py — تقسيم محتوى EPUB الأصلي إلى صفحات عند مواضع قص محددة في النص الخام.

المبدأ: النص والوسوم تأتي من EPUB فقط، ولا يُعاد كتابة أي حرف؛ نقصّ النص الأصلي عند
مواضع الصفحات فقط. إن عبر القصّ عنصرًا (فقرة/حكمًا/عنوانًا) يُستنسخ العنصر وسلسلة
آبائه في كل صفحة يمتد إليها، ويحمل كل جزء الجزء الخاص به من النص:

    <p id="m25">الجزء الأول ... | الجزء الثاني</p>
        صفحة 25: <p id="m25">الجزء الأول ...</p>
        صفحة 26: <p>الجزء الثاني</p>          (يُحذف الـ id من الاستنساخ التكميلي)

قواعد الملكية (متسقة بين العناصر والروابط):
  - يملك الصفحةَ k العنصرُ الذي يقع موضع بدايته في [lo_k, hi_k).
  - العنصر الذي بدأ قبل lo_k ويحمل نصًا داخل الصفحة يُستنسخ بلا id (استمرار).
  - العناصر الفارغة (br, img, مراسي الـ id) تُنسب للصفحة التي يقع فيها موضعها.
بعد بناء كل الصفحات تُعاد كتابة الروابط الداخلية لتشير إلى ملف الصفحة الذي يملك الهدف.
"""
from __future__ import annotations

import copy
import logging
import posixpath
from bisect import bisect_right
from dataclasses import dataclass, field

from lxml import etree

from .epub_source import (OPS_NS, XHTML_NS, SourceDoc, SourceEpub, collect_text, resolve_path)

logger = logging.getLogger("book2epub.pdfsync.splitter")

_X = f"{{{XHTML_NS}}}"
_XLINK = "{http://www.w3.org/1999/xlink}href"
_REF_ATTRS = ("href", "src", "data", "poster", _XLINK)

_PAGE_TMPL = (
    '<html xmlns="http://www.w3.org/1999/xhtml" xmlns:epub="http://www.idpf.org/2007/ops" '
    'xml:lang="{lang}" lang="{lang}" dir="rtl"><head><meta charset="utf-8"/><title/></head><body/></html>'
)


def page_filename(n: int) -> str:
    return f"page-{n:04d}.xhtml"


@dataclass
class PageFragment:
    number: int                      # 1-based
    root: etree._Element
    body: etree._Element
    ids: set[str] = field(default_factory=set)
    docs: list[int] = field(default_factory=list)   # فهارس المستندات المصدرية المساهِمة


def _slice(text: str, start: int, lo: int, hi: int) -> str:
    """الجزء من text (الذي يبدأ عند الموضع start) الواقع داخل [lo, hi)."""
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
    def __init__(self, src: SourceEpub, los: list[int], his: list[int], language: str = "ar",
                 page_labels: list[str] | None = None):
        self.src = src
        self.los, self.his = los, his
        self.lang = language
        self.labels = page_labels or [str(i + 1) for i in range(len(los))]
        self.page_dir = posixpath.join(src.opf_dir, "Text") if src.opf_dir else "Text"
        self.nav_new_path = posixpath.join(src.opf_dir, "nav.xhtml") if src.opf_dir else "nav.xhtml"
        self.docs_by_path = src.doc_by_path()
        self.idmap: dict[tuple[int, str], str] = {}
        self.pending: list[tuple[etree._Element, str, str, str | None]] = []
        self.unresolved: list[str] = []
        self._used: set[str] = set()
        self._cur_doc: SourceDoc | None = None

    # ------------------------------------------------------------ مساعدات

    def page_of(self, offset: int) -> int:
        """فهرس الصفحة (0-based) التي تملك موضعًا خامًا معيّنًا."""
        k = bisect_right(self.los, offset) - 1
        return min(max(k, 0), len(self.los) - 1)

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
                continue  # رابط خارجي: يبقى كما هو
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

    def _fill(self, doc: SourceDoc, src_el: etree._Element, new_el: etree._Element, lo: int, hi: int) -> None:
        s, _ = doc.spans[src_el]
        if src_el.text:
            _add_text(new_el, _slice(src_el.text, s, lo, hi))
        pos = s + len(src_el.text or "")
        for c in src_el:
            if not isinstance(c.tag, str):                      # تعليق/تعليمة معالجة: يُهمل مع حفظ الذيل
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
                if cs >= lo and ce <= hi:                       # داخل المجال بالكامل: نسخ سريع
                    nc = copy.deepcopy(c)
                    nc.tail = None
                    new_el.append(nc)
                    self._register_subtree(nc, doc)
                else:                                           # يعبر الحدّ: تفصيل
                    nc = etree.SubElement(new_el, c.tag, attrib=dict(c.attrib))
                    self._fill(doc, c, nc, lo, hi)
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

    # ------------------------------------------------------------ بناء صفحة

    def build_page(self, k: int, stylesheets_fallback: bool = True) -> PageFragment:
        lo, hi = self.los[k], self.his[k]
        number = k + 1
        root = etree.fromstring(_PAGE_TMPL.format(lang=self.lang).encode("utf-8"))
        head = root.find(f"{_X}head")
        body = root.find(f"{_X}body")
        self._used = set()

        marker_id = f"pg-{number:04d}"
        label = self.labels[k]
        etree.SubElement(body, f"{_X}span", {
            "id": marker_id, f"{{{OPS_NS}}}type": "pagebreak", "role": "doc-pagebreak",
            "aria-label": label, "title": label,
        })
        self._used.add(marker_id)

        frag = PageFragment(number=number, root=root, body=body)
        css: list[str] = []
        for doc in self.src.docs:
            if doc.end > doc.start:
                contrib = not (doc.end <= lo or doc.start >= hi)
            else:
                contrib = lo <= doc.start < hi
            if not contrib:
                continue
            frag.docs.append(doc.index)
            for sheet in doc.stylesheets:
                if sheet in self.src.resources and sheet not in css:
                    css.append(sheet)
            self._fill(doc, doc.body, body, lo, hi)

        for sheet in css:
            etree.SubElement(head, f"{_X}link", {
                "rel": "stylesheet", "type": "text/css",
                "href": posixpath.relpath(sheet, self.page_dir),
            })
        if not css and stylesheets_fallback:
            etree.SubElement(head, f"{_X}link", {
                "rel": "stylesheet", "type": "text/css", "href": "../Styles/style.css",
            })
        frag.ids = set(self._used)
        return frag

    def build_all(self) -> list[PageFragment]:
        pages = [self.build_page(k) for k in range(len(self.los))]
        self._resolve_pending(pages)
        for p in pages:
            title = p.root.find(f"{_X}head/{_X}title")
            title.text = f"{self.src.metadata.get('title') or ''} — {self.labels[p.number - 1]}".strip(" —")
            etree.cleanup_namespaces(p.root)
        return pages

    # ------------------------------------------------------------ الروابط

    def href_for(self, path: str | None, frag: str | None, pages_ids: list[set[str]] | None = None) -> str | None:
        """رابط (نسبةً لمجلد OPF) إلى موضع (مستند، fragment) في الصفحات الجديدة."""
        if not path or path not in self.docs_by_path:
            return None
        doc = self.docs_by_path[path]
        if frag and frag in doc.ids:
            off = doc.ids[frag]
            k = self.page_of(off)
            final = self.idmap.get((doc.index, frag))
            ok = final is not None and (pages_ids is None or final in pages_ids[k])
            tail = f"#{final}" if ok else ""
        else:
            k = self.page_of(doc.start)
            tail = ""
        base = posixpath.join("Text", page_filename(k + 1))
        return base + tail

    def _resolve_pending(self, pages: list[PageFragment]) -> None:
        ids = [p.ids for p in pages]
        broken = 0
        for el, attr, target, frag in self.pending:
            href = self.href_for(target, frag, ids)
            if href is None:
                broken += 1
                continue
            # الروابط من صفحة إلى أخرى داخل Text/ نسبية لاسم الملف فقط
            el.set(attr, href.split("/", 1)[1])
            if frag and "#" not in href:
                self.unresolved.append(f"fragment غير موجود: {target}#{frag}")
        if broken:
            logger.warning("روابط داخلية تعذّر حلها: %d", broken)
        self.pending.clear()

    # ------------------------------------------------------------ تحقق

    def total_text(self, pages: list[PageFragment]) -> str:
        return "".join(collect_text(p.body) for p in pages)
