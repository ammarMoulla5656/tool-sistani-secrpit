"""
pdfsync/glyph_text.py — فكّ ترميز نص PDF من الخطوط المدمجة نفسها عندما تكون طبقة النص تالفة.

المشكلة: ملفات PDF الناتجة عن Word بخطوط عربية قديمة (مثل almohsin) تحوي جدول ToUnicode خاطئًا،
فيُستخرج نص مثل «ٕقمٚمؿ ذم هذه اعم» بدل «الأعلم في هذه المسألة»، ولا يمكن مطابقته مع أي EPUB.

الحل: الرسم نفسه سليم، فكل حرف مرسوم برقم glyph داخل الخط المدمج، والخط يعرف الحرف الأصلي:
  - جدول cmap يربط الحروف الأساسية بالـ glyph،
  - وجدول GSUB يربط صور الحرف (أول/وسط/آخر/ربط) بالحرف الأساسي.
نعكس هذه الجداول: glyph -> حرف/حروف. الـ glyph ذات العرض الصفري التي لا تقابل حرفًا (حشو/وصلات) تُهمل.
ثم نبني الأسطر من مواضع الأحرف المرسومة (بترتيب يمين←يسار للعربية).
"""
from __future__ import annotations

import io
import logging
import re
from dataclasses import dataclass, field

logger = logging.getLogger("book2epub.pdfsync.glyph")

_ARABIC = re.compile(r"[\u0600-\u06FF\u0750-\u077F\u08A0-\u08FF\uFB50-\uFDFF\uFE70-\uFEFF]")


def build_glyph_map(buf: bytes) -> dict[int, str]:
    """glyph id -> نص (قد يكون فارغًا للحشو صفري العرض)."""
    from fontTools.ttLib import TTFont

    tt = TTFont(io.BytesIO(buf), lazy=True)
    order = tt.getGlyphOrder()
    cmap = tt.getBestCmap() or {}
    base: dict[str, str] = {gn: chr(cp) for cp, gn in cmap.items()}
    if "GSUB" in tt and tt["GSUB"].table.LookupList:
        changed, it = True, 0
        while changed and it < 8:
            changed, it = False, it + 1
            for lk in tt["GSUB"].table.LookupList.Lookup:
                for st in lk.SubTable:
                    if lk.LookupType == 7:
                        st = st.ExtSubTable
                    mapping = getattr(st, "mapping", None)
                    if mapping:
                        for a, b in mapping.items():
                            if a in base and b not in base:
                                base[b] = base[a]
                                changed = True
                    ligs = getattr(st, "ligatures", None)
                    if ligs:
                        for first, items in ligs.items():
                            for lg in items:
                                comps = [first] + list(lg.Component)
                                if lg.LigGlyph not in base and all(c in base for c in comps):
                                    base[lg.LigGlyph] = "".join(base[c] for c in comps)
                                    changed = True
    hmtx = tt["hmtx"].metrics if "hmtx" in tt else {}
    out: dict[int, str] = {}
    for i, name in enumerate(order):
        if name in base:
            out[i] = base[name]
        elif hmtx.get(name, (1, 0))[0] == 0:
            out[i] = ""                      # حشو/وصل صفري العرض
        else:
            out[i] = "\ufffd"                # حرف غير معروف
    return out


@dataclass
class GlyphDecoder:
    mdoc: object
    maps: dict[str, dict[int, str] | None] = field(default_factory=dict)
    stats: dict[str, int] = field(default_factory=lambda: {"decoded": 0, "unknown": 0, "native": 0})

    def _font_map(self, name: str, page) -> dict[int, str] | None:
        key = name.split("+")[-1]
        if key in self.maps:
            return self.maps[key]
        gm = None
        try:
            for f in page.get_fonts(full=False):
                if f[3].split("+")[-1] == key:
                    _n, ext, _t, buf = self.mdoc.extract_font(f[0])
                    if buf and ext in ("ttf", "otf", "cff"):
                        gm = build_glyph_map(buf)
                        break
        except Exception as e:  # الخط غير قابل للقراءة
            logger.debug("تعذّر قراءة الخط %s: %s", key, e)
        self.maps[key] = gm
        return gm

    def char(self, font: str, page, uni: int, gid: int) -> str:
        gm = self._font_map(font, page)
        if gm is not None and gid < 0:       # حشو صفري العرض (لا glyph له) — يُهمل
            return ""
        if gm is not None and gid in gm:
            s = gm[gid]
            if s == "\ufffd":
                self.stats["unknown"] += 1
                return ""
            self.stats["decoded"] += 1
            return s
        self.stats["native"] += 1
        return chr(uni) if uni > 0 else ""


def glyph_lines(page, decoder: GlyphDecoder, rot) -> list[dict]:
    """أسطر الصفحة المُفكّكة: [{text, x0,y0,x1,y1, size, fonts}] من مواضع الأحرف المرسومة."""
    import pymupdf as fitz  # type: ignore

    chars: list[tuple[float, float, float, str, str, tuple]] = []   # (xc, yc, size, text, font, bbox)
    for span in page.get_texttrace():
        font = str(span.get("font", ""))
        size = float(span.get("size", 0.0))
        for ch in span.get("chars", ()):
            uni, gid, _origin, bbox = ch[0], ch[1], ch[2], ch[3]
            text = decoder.char(font, page, uni, gid)
            if not text:
                continue
            r = fitz.Rect(bbox)
            if rot is not None:
                r = r * rot
                r.normalize()
            chars.append(((r.x0 + r.x1) / 2, (r.y0 + r.y1) / 2, size, text, font, (r.x0, r.y0, r.x1, r.y1)))
    if not chars:
        return []

    chars.sort(key=lambda c: c[1])
    lines: list[list] = []
    for c in chars:
        if lines:
            ys = [x[1] for x in lines[-1]]
            ref = sorted(ys)[len(ys) // 2]
            if abs(c[1] - ref) <= 0.55 * max(c[2], 6.0):
                lines[-1].append(c)
                continue
        lines.append([c])

    out = []
    for ln in lines:
        arabic = sum(1 for c in ln if _ARABIC.search(c[3]))
        rtl = arabic >= 0.5 * max(1, sum(1 for c in ln if c[3].strip()))
        ln.sort(key=lambda c: c[0], reverse=rtl)
        text = "".join(c[3] for c in ln)
        if not text.strip():
            continue
        out.append({
            "text": text,
            "x0": min(c[5][0] for c in ln), "y0": min(c[5][1] for c in ln),
            "x1": max(c[5][2] for c in ln), "y1": max(c[5][3] for c in ln),
            "size": sum(c[2] for c in ln) / len(ln),
            "fonts": tuple(sorted({c[4] for c in ln})),
        })
    return out
