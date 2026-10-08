"""
pdfsync/normalize.py — تطبيع النص لأغراض المطابقة فقط.

قاعدة صارمة: هذا التطبيع يُستخدم للمقارنة بين نص PDF ونص EPUB فقط، ولا يُطبَّق أبدًا
على النص الذي يُكتب في EPUB الناتج. لذلك تُعيد `normalize_with_map` خريطة تربط كل
حرف مُطبَّع بموضعه الأصلي في النص الخام، لنتمكن من قص النص الأصلي (غير المُطبَّع)
عند حدود الصفحات.

ما يفعله التطبيع:
  - NFKC: يفك أشكال العرض العربية (ﻓ ﻻ ...) التي يُخرجها PDF إلى الحروف الأساسية.
  - حذف التشكيل والعلامات (Mn) والتطويل (ـ) وعلامات الاتجاه غير المرئية (Cf).
  - توحيد الأرقام العربية-الهندية والفارسية إلى 0-9.
  - توحيد الحروف المتغايرة بين العربية والفارسية (ي/ی/ى، ك/ک، ة/ه، ألف بهمزاتها...).
  - حذف المسافات وعلامات الترقيم والرموز (تختلف كثيرًا بين PDF وEPUB: فواصل الأسطر،
    الأقواس المعكوسة، الشرطات...). يبقى الحروف والأرقام فقط.
"""
from __future__ import annotations

import unicodedata
from array import array

# توحيد الحروف المتشابهة (للمطابقة فقط)
_LETTER_FOLD: dict[str, str] = {
    # الياء وأخواتها
    "ى": "ي", "ی": "ي", "ې": "ي", "ۍ": "ي", "ێ": "ي", "ے": "ي", "ئ": "ي", "ؠ": "ي",
    # الكاف وأخواتها
    "ک": "ك", "ڪ": "ك", "ګ": "ك", "گ": "ك",
    # التاء المربوطة والهاء بأنواعها
    "ة": "ه", "ہ": "ه", "ھ": "ه", "ە": "ه", "ۀ": "ه", "ۂ": "ه", "ۃ": "ه",
    # الألف بهمزاتها
    "أ": "ا", "إ": "ا", "آ": "ا", "ٱ": "ا", "ٲ": "ا", "ٳ": "ا",
    # الواو بهمزتها
    "ؤ": "و", "ۋ": "و", "ۆ": "و", "ۇ": "و", "ۈ": "و",
    # حروف فارسية شائعة
    "پ": "ب", "چ": "ج", "ژ": "ز", "ڤ": "ف", "ڨ": "ق",
}

_cache: dict[str, str] = {}


def _norm_char(ch: str) -> str:
    """تطبيع حرف واحد؛ يُعيد سلسلة (قد تكون فارغة أو أطول من حرف بعد NFKC)."""
    hit = _cache.get(ch)
    if hit is not None:
        return hit
    out: list[str] = []
    for c in unicodedata.normalize("NFKC", ch):
        cat = unicodedata.category(c)
        if c == "\u0640" or cat[0] == "M" or cat == "Cf":
            continue  # تطويل / تشكيل / علامات اتجاه
        if cat == "Nd":
            out.append(str(unicodedata.digit(c)))  # كل أنظمة الأرقام -> 0-9
            continue
        c = _LETTER_FOLD.get(c, c)
        if cat[0] == "L" or cat[0] == "N":
            out.append(c.lower())
        # غير ذلك (مسافات، ترقيم، رموز): يُحذف
    res = "".join(out)
    _cache[ch] = res
    return res


def normalize_for_match(text: str) -> str:
    """نسخة مضغوطة (حروف وأرقام فقط) من النص للمقارنة."""
    return "".join(_norm_char(ch) for ch in text)


def normalize_with_map(text: str) -> tuple[str, array]:
    """تطبيع النص مع خريطة: الفهرس في النص المُطبَّع -> الفهرس في النص الأصلي.

    يضمن أن `len(mapping) == len(normalized)`، وأن `text[mapping[i]]` هو الحرف
    الأصلي الذي نتج عنه الحرف المُطبَّع رقم i (أو بدايته إن كان ناتجًا عن تفكيك NFKC).
    """
    chars: list[str] = []
    mapping = array("I")
    for idx, ch in enumerate(text):
        rep = _norm_char(ch)
        if rep:
            chars.append(rep)
            if len(rep) == 1:
                mapping.append(idx)
            else:
                mapping.extend([idx] * len(rep))
    return "".join(chars), mapping


def similarity(a: str, b: str) -> float:
    """معامل Dice على ثلاثيات الأحرف بين نصين مُطبَّعين (0..1). خطي الزمن."""
    if not a or not b:
        return 0.0
    if len(a) < 3 or len(b) < 3:
        return 1.0 if a == b else 0.0
    from collections import Counter

    ca = Counter(a[i:i + 3] for i in range(len(a) - 2))
    cb = Counter(b[i:i + 3] for i in range(len(b) - 2))
    inter = sum((ca & cb).values())
    return 2.0 * inter / ((len(a) - 2) + (len(b) - 2))


# ---------------------------------------------------------------- أرقام PDF

import re as _re

_DIGIT_RUN = _re.compile(r"[\u0660-\u0669\u06F0-\u06F9]{2,}")
_DIGIT_VAL = {chr(0x0660 + i): i for i in range(10)} | {chr(0x06F0 + i): i for i in range(10)}


def reverse_digit_runs(text: str) -> str:
    """يعكس كل تتابع من الأرقام العربية-الهندية (٢+ خانتين).

    كثير من ملفات PDF العربية تُخرج الأعداد متعددة الخانات بترتيب بصري معكوس (١٠ تصبح ٠١).
    """
    return _DIGIT_RUN.sub(lambda m: m.group()[::-1], text)


def digit_run_values(text: str) -> list[tuple[int, int]]:
    """(القيمة كما هي، القيمة معكوسة) لكل تتابع أرقام متعدد الخانات — لاكتشاف اتجاه الأرقام."""
    out = []
    for m in _DIGIT_RUN.finditer(text):
        s = m.group()
        out.append((int("".join(str(_DIGIT_VAL[c]) for c in s)),
                    int("".join(str(_DIGIT_VAL[c]) for c in s[::-1]))))
    return out
