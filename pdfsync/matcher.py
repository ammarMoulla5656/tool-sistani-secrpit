"""
pdfsync/matcher.py — المطابقة التسلسلية الشاملة (Global Sequential Matching).

الفكرة: كتاب واحد بصفحات مرتبة، فنحرّك مؤشرًا (cursor) داخل النص المُطبَّع للـEPUB:
    صفحة PDF 1 -> نجد مجالها في EPUB
    صفحة PDF 2 -> نبحث بعد نهاية مجال الصفحة 1  ...وهكذا
فلا تُطابَق أي صفحة بمعزل عن بقية الكتاب، وتنخفض جدًا احتمالات التطابق الخاطئ.

لكل صفحة نستخدم نقاط ارتكاز متعددة (Anchors): بداية، منتصف (وربع/ثلاثة أرباع للصفحات
الطويلة)، نهاية. كل نقطة تصوّت على موضع بداية الصفحة (diagonal = موضع الإصابة − موضع
النقطة داخل الصفحة)، وتُجمَّع الأصوات في عناقيد؛ يُختار العنقود الأكثر دعمًا ثم يُتحقق
منه بدرجة تشابه على كامل نص الصفحة.

سلّم الاحتياط (لا نستخدم fuzzy matching بشكل أعمى):
  1) تطابق تام بعد التطبيع بنقاط ارتكاز متعددة داخل نافذة بحث حول المؤشر.
  2) تصويت q-gram على كامل نص الصفحة داخل النافذة، مع حد أدنى لدرجة التشابه.
  3) توسيع النافذة ثم إعادة المزامنة الشاملة (بشروط أشد).
  4) استيفاء داخلي (Interpolation) تناسبيًا بين صفحتين مطابقتين، مع وسم الثقة "منخفضة".
"""
from __future__ import annotations

import difflib
import logging
from collections import Counter
from dataclasses import dataclass, field

from .normalize import normalize_for_match, similarity
from .pdf_extract import PdfDocument

logger = logging.getLogger("book2epub.pdfsync.matcher")


@dataclass
class MatchSettings:
    anchor_len: int = 24            # طول نقطة الارتكاز (أحرف مُطبَّعة ≈ 5 كلمات)
    min_page_chars: int = 12        # أقل طول لنص صفحة قابل للمطابقة المستقلة
    accept_score: float = 0.55      # أدنى تشابه لقبول مطابقة غير تامة
    exact_score: float = 0.85       # تشابه يُعدّ معه التطابق "تامًا/عالي الثقة"
    window_factor: float = 2.5      # حجم نافذة البحث نسبةً لطول الصفحة
    base_window: int = 3000         # حد أدنى ثابت لنافذة البحث (أحرف)
    qgram: int = 6
    qgram_step: int = 3
    qgram_min_ratio: float = 0.18
    global_resync: bool = True
    include_footnotes: bool = False


@dataclass
class PageMatch:
    page: int                       # رقم صفحة PDF
    pdf_chars: int = 0
    status: str = "unmatched"       # exact | fuzzy | interpolated | empty | unmatched
    start: int | None = None        # فهرس البداية في النص المُطبَّع E
    end: int | None = None
    score: float = 0.0
    anchors_hit: int = 0
    anchors_total: int = 0
    boundary: int = 0               # فهرس القص النهائي (بداية الصفحة) في E
    raw_start: int = 0              # موضع القص في النص الخام
    raw_end: int = 0
    notes: list[str] = field(default_factory=list)

    @property
    def confidence(self) -> str:
        if self.status == "exact" and self.score >= 0.85:
            return "high"
        if self.status in ("exact", "fuzzy") and self.score >= 0.55:
            return "medium"
        if self.status == "empty":
            return "n/a"
        return "low"


# ------------------------------------------------------------------ أدوات البحث

def _find_all(text: str, needle: str, lo: int, hi: int, limit: int = 12) -> list[int]:
    out: list[int] = []
    p = text.find(needle, lo, hi)
    while p != -1 and len(out) < limit:
        out.append(p)
        p = text.find(needle, p + 1, hi)
    return out


def _anchor_offsets(L: int, K: int) -> list[tuple[int, str]]:
    offs: list[tuple[int, str]] = [(0, "start")]
    if L > K:
        offs.append((L - K, "end"))
    span = max(0, L - K)
    mids = [span // 2]
    if L > 1200:
        mids += [span // 4, (3 * span) // 4]
    for m in mids:
        if all(m != o for o, _ in offs):
            offs.append((m, "mid"))
    return offs


def _refine_start(P: str, E: str, s_est: int, tol: int) -> int | None:
    """تدقيق موضع البداية موضعيًا عبر محاذاة رأس الصفحة مع جوار التقدير."""
    head = P[: min(len(P), 48)]
    lo = max(0, s_est - tol)
    hi = min(len(E), s_est + tol + len(head))
    win = E[lo:hi]
    if not win or not head:
        return None
    sm = difflib.SequenceMatcher(None, win, head, autojunk=False)
    for blk in sm.get_matching_blocks():
        if blk.size >= 5:
            cand = lo + blk.a - blk.b
            return max(0, cand)
    return None


@dataclass
class _Candidate:
    start: int
    end: int
    score: float
    support: int
    total: int
    method: str


def _locate_exact(P: str, E: str, lo: int, hi: int, st: MatchSettings) -> _Candidate | None:
    L = len(P)
    K = min(st.anchor_len, L)
    anchors = _anchor_offsets(L, K)
    hits: list[tuple[int, str, int, int]] = []        # (diag, label, pos, a)
    for a, label in anchors:
        needle = P[a:a + K]
        for pos in _find_all(E, needle, lo, hi):
            hits.append((pos - a, label, pos, a))
    if not hits:
        return None
    hits.sort()
    tol = max(30, int(0.06 * L))

    clusters: list[list[tuple[int, str, int, int]]] = []
    for h in hits:
        if clusters and h[0] - clusters[-1][0][0] <= tol:
            clusters[-1].append(h)
        else:
            clusters.append([h])

    cands: list[_Candidate] = []
    for cl in clusters:
        support = len({h[3] for h in cl})
        start_hit = next((h for h in cl if h[1] == "start"), None)
        end_hit = next((h for h in cl if h[1] == "end"), None)
        diags = sorted(h[0] for h in cl)
        median = diags[len(diags) // 2]
        if start_hit is not None:
            s = start_hit[2]
        else:
            s = max(lo, median)
            refined = _refine_start(P, E, s, tol)
            if refined is not None and abs(refined - s) <= tol:
                s = refined
        e = (end_hit[2] + K) if end_hit is not None else min(len(E), s + L)
        if e <= s:
            e = min(len(E), s + L)
        cands.append(_Candidate(s, e, similarity(P, E[s:e]), support, len(anchors), "exact"))

    cands.sort(key=lambda c: (-c.support, -c.score, c.start))
    for c in cands[:4]:
        ok = (c.support >= 2 and c.score >= st.accept_score) or (c.support == 1 and c.score >= 0.75)
        if ok:
            return c
    return None


def _locate_qgram(P: str, E: str, lo: int, hi: int, st: MatchSettings) -> _Candidate | None:
    q, L = st.qgram, len(P)
    if L < q + 6 or hi - lo < q:
        return None
    B = 24
    votes: Counter[int] = Counter()
    gram_hits: list[tuple[int, int]] = []             # (j, pos)
    grams = 0
    for j in range(0, L - q + 1, st.qgram_step):
        grams += 1
        for pos in _find_all(E, P[j:j + q], lo, hi, limit=4):
            votes[(pos - j) // B] += 1
            gram_hits.append((j, pos))
    if not votes:
        return None
    best_b = max(votes, key=lambda b: votes[b] + votes.get(b + 1, 0))
    total_votes = votes[best_b] + votes.get(best_b + 1, 0)
    if total_votes / max(1, grams) < st.qgram_min_ratio:
        return None
    near = [(j, pos) for j, pos in gram_hits if (pos - j) // B in (best_b, best_b + 1)]
    diags = sorted(pos - j for j, pos in near)
    median = diags[len(diags) // 2]
    s = max(lo, median)
    refined = _refine_start(P, E, s, 3 * B)
    if refined is not None and abs(refined - s) <= 3 * B:
        s = refined
    tail_hits = [pos + q for j, pos in near if j >= int(L * 0.8)]
    e = max(tail_hits) if tail_hits else min(len(E), s + L)
    if e <= s:
        e = min(len(E), s + L)
    score = similarity(P, E[s:e])
    if score < st.accept_score:
        return None
    return _Candidate(s, e, score, 0, grams, "fuzzy")


def _to_match(page: int, L: int, c: _Candidate) -> PageMatch:
    return PageMatch(
        page=page, pdf_chars=L,
        status="exact" if c.method == "exact" and c.score >= 0.7 else "fuzzy",
        start=c.start, end=c.end, score=round(c.score, 4),
        anchors_hit=c.support, anchors_total=c.total,
    )


# ------------------------------------------------------------------ اتجاه نص PDF

def page_match_text(page, st: MatchSettings) -> str:
    return normalize_for_match(page.match_text(st.include_footnotes))


def choose_text_order(pdf: PdfDocument, E: str, st: MatchSettings, sample: int = 8) -> tuple[str, dict[str, float]]:
    """اكتشاف تلقائي لاتجاه الاستخراج (منطقي/بصري) بقياس إصابات مقاطع قصيرة في EPUB."""
    rich = sorted((p for p in pdf.pages if p.has_text), key=lambda p: -p.raw_char_count)[:sample]
    scores: dict[str, float] = {}
    for mode in ("logical", "reverse_chars", "reverse_words"):
        pdf.set_text_order(mode)
        hits = total = 0
        for p in rich:
            P = page_match_text(p, st)
            if len(P) < 60:
                continue
            for k in range(6):
                off = (len(P) - 14) * k // 5
                total += 1
                if P[off:off + 14] in E:
                    hits += 1
        scores[mode] = hits / total if total else 0.0
    best = max(scores, key=lambda m: scores[m])
    if scores["logical"] >= max(0.5 * scores[best], 0.05) or scores[best] < 0.1:
        best = "logical"
    pdf.set_text_order(best)
    return best, scores


# ------------------------------------------------------------------ المطابقة التسلسلية

def match_pages(page_texts: list[tuple[int, str]], E: str, st: MatchSettings) -> list[PageMatch]:
    """مطابقة كل صفحة (رقمها، نصها المُطبَّع) بشكل تسلسلي. يُعيد PageMatch لكل صفحة بالترتيب."""
    matches: list[PageMatch] = []
    cursor = 0
    streak = 0
    ratio = 1.0
    n_e = len(E)

    for page_no, P in page_texts:
        L = len(P)
        m = PageMatch(page=page_no, pdf_chars=L)
        if L == 0:
            m.status = "empty"
            m.notes.append("لا نص في هذه الصفحة (فارغة أو مصورة)")
            matches.append(m)
            continue

        found: _Candidate | None = None
        if L < st.min_page_chars:
            hi = min(n_e, cursor + st.base_window + streak * 1500)
            occ = _find_all(E, P, cursor, hi, limit=2)
            if len(occ) == 1:
                found = _Candidate(occ[0], occ[0] + L, 1.0, 1, 1, "exact")
        else:
            slack = streak * (3 * L + 1500)
            hi1 = min(n_e, cursor + int(L * st.window_factor * max(ratio, 1.0)) + st.base_window + slack)
            for lo, hi, strict in (
                (cursor, hi1, False),
                (cursor, min(n_e, cursor + 10 * L + 30000), False),
                (cursor, n_e, True) if st.global_resync and streak >= 1 else (None, None, True),
            ):
                if lo is None:
                    continue
                cand = _locate_exact(P, E, lo, hi, st) or _locate_qgram(P, E, lo, hi, st)
                if cand and strict and not (cand.score >= 0.7):
                    cand = None
                if cand:
                    found = cand
                    break

        if found is None:
            streak += 1
            m.notes.append("لم يُعثر على مطابقة موثوقة")
            matches.append(m)
            continue

        m = _to_match(page_no, L, found)
        anchored_end = found.method == "exact" and found.support >= 2
        back = int(0.05 * L) if anchored_end else int(0.2 * L)
        cursor = max(found.start + 1, found.end - back)
        if anchored_end:
            ratio = 0.7 * ratio + 0.3 * min(2.0, max(0.5, (found.end - found.start) / L))
        streak = 0
        matches.append(m)
    return matches


# ------------------------------------------------------------------ حدود الصفحات

def compute_boundaries(matches: list[PageMatch], n_e: int, st: MatchSettings) -> list[int]:
    """حساب فهرس بداية كل صفحة في E، مع استيفاء الصفحات غير المطابقة وفرض التزايد الرتيب.

    قاعدة: لا يُحذف أي نص من EPUB؛ أي نص بين نهاية صفحة وبداية التالية يبقى مع الصفحة السابقة.
    """
    N = len(matches)
    b: list[int | None] = [m.start for m in matches]
    i = 0
    while i < N:
        if b[i] is not None:
            i += 1
            continue
        j = i
        while j < N and b[j] is None:
            j += 1
        if i == 0:
            left = 0
        else:
            prev = matches[i - 1]
            left = max(prev.end if prev.end is not None else (b[i - 1] or 0), b[i - 1] or 0)
        right = b[j] if j < N else n_e
        right = max(right, left)
        weights = [matches[k].pdf_chars for k in range(i, j)]
        total = sum(weights)
        gap = right - left
        if total > 0 and gap >= 0.3 * total:
            cum = 0
            for k, w in zip(range(i, j), weights):
                b[k] = left + round(gap * cum / total)
                cum += w
                matches[k].status = "interpolated"
                matches[k].notes.append("موضع الصفحة مُستوفى تناسبيًا (ثقة منخفضة) — راجعها")
        else:
            for k in range(i, j):
                b[k] = right
                if matches[k].status != "empty":
                    matches[k].notes.append("لم يوجد لهذه الصفحة نص مقابل في EPUB؛ تُركت فارغة")
        i = j

    out: list[int] = []
    prev = 0
    for v in b:
        v = min(n_e, max(prev, int(v or 0)))
        out.append(v)
        prev = v

    # ملاحظات الفجوات: نص زائد في EPUB بين نهاية صفحة وبداية التالية
    for k in range(N - 1):
        m = matches[k]
        if m.end is not None and m.start is not None and matches[k + 1].start is not None:
            gap = matches[k + 1].start - m.end
            if gap > max(80, 0.25 * m.pdf_chars):
                m.notes.append(f"{gap} حرفًا إضافيًا من EPUB بعد آخر نقطة مطابقة (ربما حواشٍ) أُلحقت بهذه الصفحة")
    return out
