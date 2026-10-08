"""
pdfsync/matcher.py — المطابقة الشاملة بسلسلة المرتكزات (Global Anchor-Chain Alignment).

المشكلة: نص PDF المستخرج مشوّش (أرقام معكوسة، كلمات مقطّعة، رموز شاذة، ترويسات)،
والمطابقة الجشعة صفحةً بصفحة تنهار عند أول خلل ثم تُفسد كل ما بعده.

الحل — نعالج الكتاب كله دفعة واحدة:
  1) نبني فهرس k-gram (افتراضيًا 8 أحرف مُطبَّعة ≈ كلمتان) لنص EPUB، ونحتفظ بالمقاطع
     النادرة فقط (تتكرر ≤ max_occ مرة).
  2) نجمع نص كل صفحات PDF في تيار واحد، ونأخذ مقاطعه التي ظهرت مرة واحدة في PDF ونادرة في EPUB:
     كل مقطع يعطي نقطة (موضعه في PDF، موضعه في EPUB).
  3) نختار أطول سلسلة متزايدة (LIS) من هذه النقاط: أي إصابة شاذة (مطابقة عرَضية في مكان آخر)
     تسقط تلقائيًا لأنها تخالف الترتيب العام، ثم نحذف ما يخالف الانحراف المحلي.
  4) نشتق بداية كل صفحة من أقرب نقطة ارتكاز (مع استيفاء خطي عند البعد) ثم نصقلها محليًا.

النتيجة: حدود صفحات متسقة ومرتبة حتمًا، وتتحمل مقاطع كاملة من التشويش دون انهيار متسلسل.
تُطبَّق الخوارزمية نفسها على تيارين مستقلين: المتن، والحواشي.
"""
from __future__ import annotations

import difflib
import logging
from bisect import bisect_left
from collections import Counter
from dataclasses import dataclass, field

from .normalize import normalize_for_match, similarity
from .pdf_extract import PdfDocument

logger = logging.getLogger("book2epub.pdfsync.matcher")


@dataclass
class MatchSettings:
    anchor_len: int = 8             # طول k-gram (أحرف مُطبَّعة)
    max_occ: int = 3                # أقصى تكرار للمقطع في EPUB ليصلح مرتكزًا
    accept_score: float = 0.55      # أدنى تشابه لاعتبار صفحة "موثوقة"
    exact_score: float = 0.85       # تشابه عالي الثقة
    diag_window: int = 5            # نافذة مرشّح الانحراف (عدد نقاط على كل جهة)
    diag_tol: int = 80              # أقصى انحراف مسموح عن الوسيط المحلي (أحرف)
    near_anchor: int = 90           # أقصى بعد عن مرتكز لاعتماد الانحراف المحلي مباشرة
    refine_radius: int = 60
    include_footnotes: bool = False  # (للتوافق) لا أثر له: الحواشي تُطابَق في تيار مستقل


@dataclass
class PageMatch:
    page: int
    pdf_chars: int = 0
    status: str = "unmatched"       # exact | fuzzy | partial | interpolated | missing | empty | unmatched
    start: int | None = None        # بداية الصفحة في النص المُطبَّع (قبل الاستيفاء)
    end: int | None = None
    score: float = 0.0
    anchors_hit: int = 0
    anchors_total: int = 0
    boundary: int = 0               # بداية القص النهائية في النص المُطبَّع
    raw_start: int = 0
    raw_end: int = 0
    notes: list[str] = field(default_factory=list)

    @property
    def confidence(self) -> str:
        if self.status == "empty":
            return "n/a"
        if self.status == "exact" and self.score >= 0.85:
            return "high"
        if self.status in ("exact", "fuzzy") and self.score >= 0.55:
            return "medium"
        return "low"      # interpolated | partial | missing | unmatched


# ------------------------------------------------------------------ بناء المرتكزات

def _build_index(E: str, k: int, max_occ: int) -> dict[str, list[int]]:
    idx: dict[str, list[int]] = {}
    cap = max_occ + 1
    for i in range(len(E) - k + 1):
        g = E[i:i + k]
        lst = idx.get(g)
        if lst is None:
            idx[g] = [i]
        elif len(lst) < cap:
            lst.append(i)
    return {g: l for g, l in idx.items() if len(l) <= max_occ}


def _lis(points: list[tuple[int, int]]) -> list[tuple[int, int]]:
    """أطول سلسلة متزايدة (بدقة) في (j, pos). تُرتَّب النقاط (j تصاعدي، pos تنازلي عند التساوي)."""
    if not points:
        return []
    points = sorted(points, key=lambda t: (t[0], -t[1]))
    tails: list[int] = []        # أصغر pos ينهي سلسلة بطول i+1
    tails_idx: list[int] = []    # فهرس النقطة المقابلة
    prev = [-1] * len(points)
    for n, (_, pos) in enumerate(points):
        i = bisect_left(tails, pos)
        if i == len(tails):
            tails.append(pos)
            tails_idx.append(n)
        else:
            tails[i] = pos
            tails_idx[i] = n
        prev[n] = tails_idx[i - 1] if i > 0 else -1
    out: list[tuple[int, int]] = []
    n = tails_idx[-1]
    while n != -1:
        out.append(points[n])
        n = prev[n]
    out.reverse()
    return out


def _filter_diagonal(chain: list[tuple[int, int]], window: int, tol: int) -> list[tuple[int, int]]:
    """حذف النقاط المنحرفة عن الانحراف المحلي (الفرق pos - j) لجيرانها."""
    if len(chain) < 3:
        return chain
    kept = chain
    for _ in range(2):
        d = [q - j for j, q in kept]
        out = []
        for i, pt in enumerate(kept):
            lo, hi = max(0, i - window), min(len(kept), i + window + 1)
            nb = sorted(d[lo:i] + d[i + 1:hi])
            if not nb:
                out.append(pt)
                continue
            med = nb[len(nb) // 2]
            if abs(d[i] - med) <= tol:
                out.append(pt)
        if len(out) == len(kept):
            break
        kept = out
    return kept


def _prefer_latest(chain: list[tuple[int, int]], P: str, idx: dict[str, list[int]], k: int) -> list[tuple[int, int]]:
    """عند تكرار فقرة في EPUB (نسختان متطابقتان) نفضّل النسخة الأحدث التي تبقي السلسلة متزايدة،
    فيبقى النص المكرر الزائد قبل بداية الصفحة لا داخلها."""
    out = list(chain)
    bound = None
    for n in range(len(out) - 1, -1, -1):
        j, q = out[n]
        alts = idx.get(P[j:j + k], ())
        best = q
        for a in alts:
            if a > best and (bound is None or a < bound):
                best = a
        out[n] = (j, best)
        bound = best
    return out


@dataclass
class StreamAlignment:
    matches: list[PageMatch]
    anchors: int
    chain_J: list[int]
    chain_Q: list[int]


def _refine_start(head: str, E: str, s_est: int, radius: int) -> int | None:
    """صقل بداية الصفحة بمحاذاة رأسها مع جوار التقدير (أطول مقطع مشترك).

    إن كان في رأس PDF بادئة غير موجودة في EPUB (عنوان/ترويسة زائدة) نبدأ عند المقطع المطابق،
    وإلا (ضجيج استخراج فقط) نمدّ البداية للخلف بطول البادئة.
    """
    if len(head) < 6:
        return None
    lo = max(0, s_est - radius)
    hi = min(len(E), s_est + radius + len(head))
    win = E[lo:hi]
    if len(win) < 6:
        return None
    sm = difflib.SequenceMatcher(None, win, head, autojunk=False)
    m = sm.find_longest_match(0, len(win), 0, len(head))
    if m.size < 6:
        return None
    start = lo + m.a
    if m.b > 0:
        prefix = head[:m.b]
        before = E[max(0, start - m.b):start]
        if before and similarity(prefix, before) >= 0.45:
            start = max(0, start - m.b)
    return start if abs(start - s_est) <= radius + 40 else None


def align_stream(page_texts: list[tuple[int, str]], E: str, st: MatchSettings) -> StreamAlignment:
    """يحاذي نصوص الصفحات (رقم، نص مُطبَّع) على نص EPUB المُطبَّع E."""
    k = st.anchor_len
    n_pages = len(page_texts)
    matches = [PageMatch(page=no, pdf_chars=len(t)) for no, t in page_texts]
    offs: list[int] = []
    acc = 0
    for _, t in page_texts:
        offs.append(acc)
        acc += len(t) + 1
    P = "\n".join(t for _, t in page_texts)

    if not E or len(P) < k + 1:
        for m in matches:
            m.status = "empty" if m.pdf_chars == 0 else "unmatched"
        return StreamAlignment(matches, 0, [], [])

    idx = _build_index(E, k, st.max_occ)
    pcount: Counter[str] = Counter(P[i:i + k] for i in range(len(P) - k + 1) if "\n" not in P[i:i + k])
    hits: list[tuple[int, int]] = []
    for j in range(len(P) - k + 1):
        g = P[j:j + k]
        if pcount.get(g) == 1:
            occ = idx.get(g)
            if occ:
                for pos in occ:
                    hits.append((j, pos))

    chain = _filter_diagonal(_lis(hits), st.diag_window, st.diag_tol)
    chain = _prefer_latest(chain, P, idx, k)
    J = [j for j, _ in chain]
    Q = [q for _, q in chain]
    logger.info("مرتكزات: %d إصابة | %d في السلسلة بعد الترشيح (نص PDF %d حرفًا)", len(hits), len(chain), len(P))

    for p, (no, t) in enumerate(page_texts):
        m = matches[p]
        L = len(t)
        if L == 0:
            m.status = "empty"
            m.notes.append("لا نص في هذه الصفحة")
            continue
        if not chain:
            m.notes.append("لا مرتكزات")
            continue
        a, b = offs[p], offs[p] + L
        i = bisect_left(J, a)
        m.anchors_hit = bisect_left(J, b) - i
        m.anchors_total = max(1, (L - k + 1))
        if m.anchors_hit == 0:
            m.notes.append("لا مرتكزات داخل الصفحة")   # يُقرَّر لاحقًا: استيفاء أو "ناقصة من EPUB"
            continue
        cand = []
        if i < len(J):
            cand.append(i)
        if i > 0:
            cand.append(i - 1)
        near = min(cand, key=lambda c: abs(J[c] - a))
        dist = abs(J[near] - a)
        if dist <= st.near_anchor or not (i > 0 and i < len(J)):
            s = Q[near] - (J[near] - a)
        else:
            j0, j1, q0, q1 = J[i - 1], J[i], Q[i - 1], Q[i]
            slope = (q1 - q0) / max(1, (j1 - j0))
            s = int(round(q0 + (a - j0) * slope))
            s = min(max(s, q0), q1)
        if 0 < dist <= st.near_anchor:
            ref = _refine_start(t[:48], E, s, st.refine_radius)
            if ref is not None:
                s = ref
        # قيد منطقي: بداية الصفحة تقع بين المرتكز السابق والمرتكز الأول داخل الصفحة
        lower = Q[i - 1] + 1 if i > 0 else 0
        upper = Q[i] if i < len(J) and J[i] < b else len(E)
        s = min(max(s, lower), max(lower, upper))
        m.start = max(0, min(len(E), s))
        last = bisect_left(J, b) - 1
        m.end = min(len(E), Q[last] + k)
        if m.end <= m.start:
            m.end = min(len(E), m.start + L)
        if dist > 4 * k + st.near_anchor:
            m.status = "fuzzy"
    return StreamAlignment(matches, len(chain), J, Q)


# ------------------------------------------------------------------ الحدود النهائية

def compute_boundaries(matches: list[PageMatch], n_e: int) -> list[int]:
    """فهرس بداية كل صفحة في E: رتيب متزايد، والصفحات الفارغة صفرية الطول، والمجهولة تُستوفى.

    لا يُحذف نص بين صفحتين: كل ما بين بداية صفحة وبداية التالية يخصّها.
    """
    N = len(matches)
    b: list[int | None] = [m.start if m.pdf_chars > 0 else None for m in matches]

    # استيفاء الصفحات النصية التي بلا موضع (بنسبة طول نصها بين أقرب معلومين)
    i = 0
    while i < N:
        if b[i] is not None or matches[i].pdf_chars == 0:
            i += 1
            continue
        j = i
        while j < N and (b[j] is None):
            j += 1
        left = 0
        for k in range(i - 1, -1, -1):
            if b[k] is not None:
                left = max(b[k], matches[k].end or b[k])
                break
        right = next((b[k] for k in range(j, N) if b[k] is not None), n_e)
        right = max(right, left)
        group = [k for k in range(i, j) if matches[k].pdf_chars > 0]
        total = sum(matches[k].pdf_chars for k in group) or 1
        gap = right - left
        if gap >= 0.3 * total and total >= 20:
            cum = 0
            for k in group:
                b[k] = left + round(gap * cum / total)
                cum += matches[k].pdf_chars
                matches[k].status = "interpolated"
                matches[k].notes.append("موضع الصفحة مستوفى تناسبيًا (ثقة منخفضة) — راجعها")
        else:
            for k in group:
                b[k] = right
                matches[k].status = "missing"
                matches[k].notes.append("لا يوجد نص مقابل لهذه الصفحة في EPUB")
        i = j

    # الصفحات الفارغة: تأخذ بداية أول صفحة نصية بعدها (طول صفري)
    nxt = n_e
    for k in range(N - 1, -1, -1):
        if b[k] is None:
            b[k] = nxt
        else:
            nxt = b[k]

    out: list[int] = []
    prev = 0
    for v in b:
        v = min(n_e, max(prev, int(v or 0)))
        out.append(v)
        prev = v

    # ملاحظة: نص زائد في EPUB بين نهاية صفحة وبداية التالية يلحق بالصفحة السابقة
    text_pages = [k for k in range(N) if matches[k].pdf_chars > 0]
    for a, c in zip(text_pages, text_pages[1:]):
        m = matches[a]
        gap = out[c] - (m.end if m.end is not None else out[a])
        if m.status != "interpolated" and gap > max(120, 0.35 * m.pdf_chars):
            m.notes.append(f"{gap} حرفًا إضافيًا من EPUB بعد آخر مرتكز أُلحقت بهذه الصفحة (ربما نص غير موجود في PDF)")
    return out


def score_pages(matches: list[PageMatch], page_texts: list[tuple[int, str]], boundaries: list[int],
                E: str, st: MatchSettings) -> None:
    """تشابه (Dice) بين نص كل صفحة وما قُصّ لها من EPUB، ثم تحديد الحالة."""
    n = len(matches)
    for k, m in enumerate(matches):
        m.boundary = boundaries[k]
        if m.pdf_chars == 0:
            m.status = "empty"
            continue
        e = boundaries[k + 1] if k + 1 < n else (m.end if m.end is not None else len(E))
        seg = E[boundaries[k]:e]
        m.score = round(similarity(page_texts[k][1], seg), 4)
        if m.status == "missing":
            continue
        if m.pdf_chars >= 60 and len(seg) < 0.15 * m.pdf_chars and m.score < 0.3:
            m.status = "missing"
            m.notes.append("لا يوجد نص مقابل لهذه الصفحة في EPUB")
            continue
        if m.status == "interpolated":
            continue
        if m.anchors_hit >= 1 and m.score < 0.6 and len(seg) < 0.7 * m.pdf_chars:
            m.status = "partial"
            m.notes.append(f"نص EPUB أقصر بكثير من نص PDF ({len(seg)} مقابل {m.pdf_chars}) — جزء من الصفحة غير موجود في EPUB")
            continue
        if m.anchors_hit >= 2 and m.score >= st.exact_score:
            m.status = "exact"
        elif m.score >= st.accept_score:
            m.status = "fuzzy" if m.anchors_hit < 2 else "exact"
        else:
            m.status = "fuzzy"
            m.notes.append(f"تشابه منخفض ({m.score}) — راجع هذه الصفحة")


# ------------------------------------------------------------------ اتجاه نص PDF

def page_texts_for(pdf: PdfDocument, which: str) -> list[tuple[int, str]]:
    getter = (lambda p: p.body_text()) if which == "body" else (lambda p: p.note_text())
    return [(p.number, normalize_for_match(getter(p))) for p in pdf.pages]


def choose_text_order(pdf: PdfDocument, E: str, sample: int = 8) -> tuple[str, dict[str, float]]:
    """كشف تلقائي لاتجاه الاستخراج (منطقي/بصري) بإصابات مقاطع قصيرة في EPUB."""
    rich = sorted((p for p in pdf.pages if p.kind in ("text", "mixed")), key=lambda p: -p.raw_char_count)[:sample]
    scores: dict[str, float] = {}
    for mode in ("logical", "reverse_chars", "reverse_words"):
        pdf.set_text_order(mode)
        hits = total = 0
        for p in rich:
            P = normalize_for_match(p.body_text())
            if len(P) < 60:
                continue
            for q in range(6):
                off = (len(P) - 14) * q // 5
                total += 1
                if P[off:off + 14] in E:
                    hits += 1
        scores[mode] = hits / total if total else 0.0
    best = max(scores, key=lambda m: scores[m])
    if scores["logical"] >= max(0.5 * scores[best], 0.05) or scores[best] < 0.1:
        best = "logical"
    pdf.set_text_order(best)
    return best, scores
