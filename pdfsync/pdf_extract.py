"""
pdfsync/pdf_extract.py — استخراج نص PDF وبنيته عبر PyMuPDF (بلا OCR إطلاقًا).

لكل صفحة نُنتج:
  - كتل نصية بإحداثياتها وخطوطها، مصنَّفة: body | header | footer | page_number | footnote
      * الترويسة: شريط أعلى الصفحة + توقيع متكرر (نفس الخط ونفس الارتفاع عبر الصفحات).
      * الحواشي: كل ما تحت الخط الفاصل الأفقي القصير (إن وُجد)، وإلا بحسب صغر الخط.
  - صور الصفحة (إحداثيات) ونوع الصفحة: text | image | mixed | blank
      * صفحة "image": لا نص متن لكن فيها صور/رسوم → تُنقل كصورة (عرض مطابق للأصل).
      * صفحة "blank": فارغة فعلًا.

النص هنا للمطابقة فقط؛ لا يُكتب في الناتج النهائي (النص يأتي من EPUB).
"""
from __future__ import annotations

import logging
import re
from collections import Counter
from dataclasses import dataclass, field
from pathlib import Path

try:  # الاسم الجديد للحزمة
    import pymupdf as fitz
except ImportError:  # pragma: no cover
    import fitz  # type: ignore

from .normalize import digit_run_values, normalize_for_match, reverse_digit_runs

logger = logging.getLogger("book2epub.pdfsync.pdf")

_ALLAH_FIX = re.compile("[\u0627\ufe8d\ufe8e]\ufdf2")   # «ا»+«ﷲ» => «ﷲ» (يمنع تضاعف الألف بعد NFKC)


@dataclass
class PdfImage:
    x0: float
    y0: float
    x1: float
    y1: float
    xref: int = 0
    width: int = 0
    height: int = 0

    @property
    def rect(self) -> tuple[float, float, float, float]:
        return (self.x0, self.y0, self.x1, self.y1)

    @property
    def area(self) -> float:
        return max(0.0, self.x1 - self.x0) * max(0.0, self.y1 - self.y0)


@dataclass
class PdfTextBlock:
    page_number: int
    lines: list[str]
    x0: float
    y0: float
    x1: float
    y1: float
    font_size: float = 0.0
    bold: bool = False
    fonts: tuple[str, ...] = ()
    kind: str = "body"      # body | header | footer | page_number | footnote
    text: str = ""

    def apply_order(self, mode: str, reverse_digits: bool = False) -> None:
        lines = self.lines
        if mode == "reverse_chars":
            lines = [line[::-1] for line in lines]
        elif mode == "reverse_words":
            lines = [" ".join(reversed(line.split())) for line in lines]
        lines = [_ALLAH_FIX.sub("\ufdf2", line) for line in lines]
        if reverse_digits:
            lines = [reverse_digit_runs(line) for line in lines]
        self.text = "\n".join(lines)


@dataclass
class PdfPage:
    number: int
    width: float
    height: float
    blocks: list[PdfTextBlock] = field(default_factory=list)
    images: list[PdfImage] = field(default_factory=list)       # الصور المعتد بها فقط
    drawing_rect: tuple[float, float, float, float] | None = None
    separator_y: float | None = None
    raw_char_count: int = 0
    kind: str = "text"             # text | image | mixed | blank
    notes: list[str] = field(default_factory=list)
    rm: object = None

    def _text(self, kind: str) -> str:
        return "\n".join(b.text for b in self.blocks if b.kind == kind and b.text)

    def body_text(self) -> str:
        return self._text("body")

    def note_text(self) -> str:
        return self._text("footnote")

    @property
    def has_text(self) -> bool:
        return self.raw_char_count > 0

    def image_region(self) -> tuple[float, float, float, float] | None:
        """المستطيل الذي يُرسَم للصفحات/الأجزاء المصورة (اتحاد الصور والرسوم)."""
        rects = [im.rect for im in self.images]
        if self.drawing_rect:
            rects.append(self.drawing_rect)
        if not rects:
            return None
        x0 = max(0.0, min(r[0] for r in rects))
        y0 = max(0.0, min(r[1] for r in rects))
        x1 = min(self.width, max(r[2] for r in rects))
        y1 = min(self.height, max(r[3] for r in rects))
        return (x0, y0, x1, y1) if x1 > x0 and y1 > y0 else None


@dataclass
class PdfDocument:
    path: Path
    total_pages: int
    pages: list[PdfPage]
    metadata: dict = field(default_factory=dict)
    text_order: str = "logical"
    reverse_digits: bool = False
    body_font_size: float = 0.0
    removed_headers: dict[str, int] = field(default_factory=dict)
    glyph_mode: bool = False
    _mdoc: object = None

    def set_text_order(self, mode: str) -> None:
        self.text_order = mode
        self._reapply()

    def set_reverse_digits(self, flag: bool) -> None:
        self.reverse_digits = flag
        self._reapply()

    def _reapply(self) -> None:
        for page in self.pages:
            for block in page.blocks:
                block.apply_order(self.text_order, self.reverse_digits)

    # ---- رسم مناطق الصور
    def render(self, page_number: int, rect: tuple[float, float, float, float], dpi: int = 170,
               prefer_jpeg: bool = False, jpeg_quality: int = 88) -> tuple[bytes, str]:
        """يرسم منطقة من صفحة ويُعيد (البايتات، الامتداد)."""
        page = self._mdoc[page_number - 1]
        pix = page.get_pixmap(clip=fitz.Rect(*rect), dpi=dpi, alpha=False)
        if prefer_jpeg:
            return pix.tobytes("jpeg", jpg_quality=jpeg_quality), "jpg"
        return pix.tobytes("png"), "png"

    def close(self) -> None:
        if self._mdoc is not None:
            try:
                self._mdoc.close()
            except Exception:
                pass
            self._mdoc = None


# ------------------------------------------------------------------ التحليل

def _rot(bbox, rm):
    """الصفحات المدوّرة: إحداثيات PyMuPDF للنص/الصور/الرسوم غير مدوّرة، نحوّلها لإحداثيات العرض."""
    if rm is None:
        return tuple(float(v) for v in bbox)
    r = fitz.Rect(bbox) * rm
    r.normalize()
    return (r.x0, r.y0, r.x1, r.y1)


def _parse_blocks(page_dict: dict, page_number: int, rm=None) -> tuple[list[PdfTextBlock], list[PdfImage]]:
    blocks: list[PdfTextBlock] = []
    images: list[PdfImage] = []
    for b in page_dict.get("blocks", []):
        x0, y0, x1, y1 = _rot(b["bbox"], rm)
        if b.get("type") == 1:
            images.append(PdfImage(x0, y0, x1, y1, 0, int(b.get("width", 0)), int(b.get("height", 0))))
            continue
        lines: list[str] = []
        size_chars: list[tuple[float, int]] = []
        fonts: set[str] = set()
        bold_chars = total_chars = 0
        for line in b.get("lines", []):
            spans = line.get("spans", [])
            text = "".join(s.get("text", "") for s in spans)
            if text.strip():
                lines.append(text)
            for s in spans:
                n = len(s.get("text", "").strip())
                if n:
                    size_chars.append((float(s.get("size", 0.0)), n))
                    fonts.add(str(s.get("font", "")))
                    total_chars += n
                    if int(s.get("flags", 0)) & 16:
                        bold_chars += n
        if not lines:
            continue
        weight = sum(n for _, n in size_chars) or 1
        avg = sum(sz * n for sz, n in size_chars) / weight
        blocks.append(PdfTextBlock(
            page_number=page_number, lines=lines, x0=x0, y0=y0, x1=x1, y1=y1,
            font_size=round(avg, 2), bold=bold_chars > total_chars * 0.5, fonts=tuple(sorted(fonts)),
        ))
    return blocks, images


def _separator_line(mp, width: float, height: float, blocks: list[PdfTextBlock], rm=None) -> float | None:
    """الخط الأفقي القصير الفاصل بين المتن والحواشي: أدنى خط يقع تحته نص."""
    cands: list[float] = []
    try:
        drawings = mp.get_drawings()
    except Exception:
        return None
    for d in drawings:
        r = fitz.Rect(_rot(d["rect"], rm))
        if r.height <= 3.0 and 25.0 <= r.width <= 0.8 * width and 0.30 * height < r.y0 < 0.985 * height:
            cands.append(float(r.y0))
    for y in sorted(set(round(c, 1) for c in cands), reverse=True):
        if any(b.y0 >= y - 3 for b in blocks):
            return y
    return None


def _drawings_region(mp, width: float, height: float, top_limit: float, rm=None) -> tuple[float, float, float, float] | None:
    """اتحاد الرسوم المتجهة الجوهرية (غير الخطوط الرفيعة) أسفل الترويسة."""
    try:
        drawings = mp.get_drawings()
    except Exception:
        return None
    rects = [fitz.Rect(_rot(d["rect"], rm)) for d in drawings]
    rects = [r for r in rects
             if r.height > 4 and r.width > 4 and r.y0 >= top_limit
             and r.width * r.height < 0.95 * width * height]
    if len(rects) < 8:
        return None
    x0 = min(r.x0 for r in rects); y0 = min(r.y0 for r in rects)
    x1 = max(r.x1 for r in rects); y1 = max(r.y1 for r in rects)
    if (x1 - x0) * (y1 - y0) < 0.03 * width * height:
        return None
    return (x0, y0, x1, y1)


def _classify(doc: PdfDocument, header_frac: float, footer_frac: float, footnote_size_ratio: float = 0.9) -> None:
    pages = doc.pages
    n = len(pages)

    # 1) الترويسة/التذييل بالشريط + بالتوقيع المتكرر (الخط + الارتفاع)
    sig_count: Counter = Counter()
    top_sig: dict[int, tuple] = {}
    for pg in pages:
        tops = [b for b in pg.blocks if b.y0 < 0.25 * pg.height]
        if tops:
            b = min(tops, key=lambda x: x.y0)
            sig = (b.fonts, round(b.y0 / 4))
            top_sig[pg.number] = sig
            sig_count[sig] += 1
    min_rep = max(3, int(0.3 * n)) if n > 4 else 2

    for pg in pages:
        for b in pg.blocks:
            norm = normalize_for_match(" ".join(b.lines))
            letters = re.sub(r"[0-9]", "", norm)
            in_top = b.y1 <= pg.height * header_frac
            in_bottom = b.y0 >= pg.height * (1.0 - footer_frac)
            sig_hit = top_sig.get(pg.number) is not None and sig_count[top_sig[pg.number]] >= min_rep \
                and (b.fonts, round(b.y0 / 4)) == top_sig[pg.number]
            if in_top or in_bottom or sig_hit:
                if not letters:
                    b.kind = "page_number"
                else:
                    b.kind = "header" if (in_top or sig_hit) else "footer"
                    doc.removed_headers[re.sub(r"\d+", "", norm)[:30]] = doc.removed_headers.get(
                        re.sub(r"\d+", "", norm)[:30], 0) + 1

    # 2) الحواشي بالخط الفاصل
    for pg in pages:
        body = [b for b in pg.blocks if b.kind == "body"]
        if pg.separator_y is not None:
            for b in body:
                if (b.y0 + b.y1) / 2 >= pg.separator_y:
                    b.kind = "footnote"

    # 3) حجم خط المتن (الأكثر وزنًا بين كتل المتن)
    weight: Counter = Counter()
    for pg in pages:
        for b in pg.blocks:
            if b.kind == "body":
                weight[round(b.font_size * 2) / 2] += sum(len(x) for x in b.lines)
    doc.body_font_size = weight.most_common(1)[0][0] if weight else 0.0

    # 4) احتياط: صفحات بلا خط فاصل — الحاشية = خط أصغر بوضوح في النصف السفلي
    if doc.body_font_size:
        limit = doc.body_font_size * footnote_size_ratio
        for pg in pages:
            if pg.separator_y is not None:
                continue
            for b in pg.blocks:
                if b.kind == "body" and b.font_size <= limit and b.y0 > pg.height * 0.45:
                    b.kind = "footnote"


def _page_kind(pg: PdfPage, min_body_chars: int = 25) -> None:
    body_chars = len(re.sub(r"\s+", "", pg.body_text()))
    note_chars = len(re.sub(r"\s+", "", pg.note_text()))
    has_art = bool(pg.images) or pg.drawing_rect is not None
    if has_art and body_chars < min_body_chars:
        pg.kind = "image"
    elif has_art:
        pg.kind = "mixed"
    elif body_chars + note_chars == 0:
        pg.kind = "blank"
    else:
        pg.kind = "text"


def detect_reverse_digits(pdf: PdfDocument) -> bool:
    """هل الأعداد متعددة الخانات معكوسة في PDF؟ نقيس تتابع الأعداد (n, n+1) في الاتجاهين."""
    asis = rev = 0
    prev_a = prev_r = None
    for pg in pdf.pages:
        for b in pg.blocks:
            if b.kind not in ("body", "footnote"):
                continue
            for line in b.lines:
                for a, r in digit_run_values(line):
                    if prev_a is not None and a == prev_a + 1:
                        asis += 1
                    if prev_r is not None and r == prev_r + 1:
                        rev += 1
                    prev_a, prev_r = a, r
    return rev >= 5 and rev > 1.3 * asis


def extract_pdf(
    path: Path,
    page_range: tuple[int, int] | None = None,
    *,
    header_frac: float = 0.12,
    footer_frac: float = 0.06,
    min_image_frac: float = 0.004,
    glyph_decode: bool = False,
) -> PdfDocument:
    """استخراج بنية PDF صفحةً صفحة. page_range: (أول، آخر) بترقيم يبدأ من 1 وشامل الطرفين."""
    path = Path(path)
    mdoc = fitz.open(str(path))
    total = len(mdoc)
    first, last = (1, total) if page_range is None else (max(1, page_range[0]), min(total, page_range[1]))
    if first > last:
        mdoc.close()
        raise ValueError(f"نطاق الصفحات غير صالح: {page_range} (عدد صفحات الملف {total})")

    decoder = None
    if glyph_decode:
        from .glyph_text import GlyphDecoder, glyph_lines
        decoder = GlyphDecoder(mdoc)
    pages: list[PdfPage] = []
    for pno in range(first, last + 1):
        mp = mdoc[pno - 1]
        rect = mp.rect
        pg = PdfPage(number=pno, width=float(rect.width), height=float(rect.height))
        rm = mp.rotation_matrix if mp.rotation else None
        pg.rm = rm
        if decoder is not None:
            blocks = [PdfTextBlock(page_number=pno, lines=[ln["text"]], x0=ln["x0"], y0=ln["y0"], x1=ln["x1"],
                                   y1=ln["y1"], font_size=round(ln["size"], 2), fonts=ln["fonts"])
                      for ln in glyph_lines(mp, decoder, rm)]
        else:
            blocks, _ = _parse_blocks(mp.get_text("dict"), pno, rm)
        pg.blocks = blocks
        pg.raw_char_count = len(re.sub(r"\s+", "", "".join("".join(b.lines) for b in blocks)))
        pg.separator_y = _separator_line(mp, pg.width, pg.height, blocks, rm)

        page_area = max(1.0, pg.width * pg.height)
        for info in mp.get_image_info(xrefs=True):
            x0, y0, x1, y1 = _rot(info["bbox"], rm)
            im = PdfImage(x0, y0, x1, y1, int(info.get("xref", 0)), int(info.get("width", 0)), int(info.get("height", 0)))
            if im.area >= min_image_frac * page_area:
                pg.images.append(im)
        pages.append(pg)

    doc = PdfDocument(path=path, total_pages=total, pages=pages, metadata=dict(mdoc.metadata or {}), _mdoc=mdoc)
    _classify(doc, header_frac, footer_frac)

    for pg in pages:
        # صورة تغطي الصفحة كاملة مع نص حقيقي = خلفية ممسوحة بطبقة نص → لا تُنقل
        full_bg = [im for im in pg.images if im.area >= 0.85 * pg.width * pg.height]
        body_raw = sum(len(re.sub(r"\s+", "", "".join(b.lines))) for b in pg.blocks if b.kind == "body")
        if full_bg and body_raw >= 40:
            pg.images = [im for im in pg.images if im not in full_bg]
            pg.notes.append("صورة خلفية بحجم الصفحة مع طبقة نص — لم تُنقل")
        if body_raw == 0:
            top = pg.height * header_frac
            mp = mdoc[pg.number - 1]
            pg.drawing_rect = None if pg.images else _drawings_region(mp, pg.width, pg.height, top, getattr(pg, 'rm', None))

    doc.glyph_mode = decoder is not None
    if decoder is not None:
        logger.info("فك الترميز من الخطوط: %s", decoder.stats)
    doc.set_text_order("logical")
    for pg in pages:
        _page_kind(pg)
    logger.info("PDF: %d صفحة مستهدفة من %d | نصية: %d | صور: %d | مختلطة: %d | فارغة: %d",
                len(pages), total,
                sum(p.kind == "text" for p in pages), sum(p.kind == "image" for p in pages),
                sum(p.kind == "mixed" for p in pages), sum(p.kind == "blank" for p in pages))
    return doc
