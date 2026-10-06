"""
urlutils.py — تطبيع الروابط ومساعدات أسماء الملفات.

التطبيع ضروري لمنع التكرار: الروابط التالية تُعتبر صفحة واحدة
    https://www.sistani.org/arabic/book/13/530
    https://www.sistani.org/arabic/book/13/530/
    http://sistani.org//arabic/book/13/530/?x=1#top
"""
from __future__ import annotations

import re
from urllib.parse import parse_qsl, quote, unquote, urlencode, urljoin, urlsplit, urlunsplit

_MULTI_SLASH = re.compile(r"/{2,}")
_SAFE_PATH_CHARS = "/:@!$&'()*+,;=-._~"
_DEFAULT_PORTS = {"http": 80, "https": 443}


def normalize_url(
    url: str,
    base: str | None = None,
    *,
    host_aliases: dict[str, str] | None = None,
    keep_query: tuple[str, ...] | set[str] = (),
    trailing_slash: bool = True,
) -> str:
    """يعيد رابطًا مطبّعًا (بدون fragment).

    - يحلّ الروابط النسبية مقابل base.
    - يوحّد المخطط والمضيف إلى أحرف صغيرة، ويحذف المنفذ الافتراضي.
    - يحوّل أسماء المضيف البديلة إلى المضيف القانوني (ويفرض https لها).
    - يدمج الشرطات المائلة المتكررة.
    - يوحّد ترميز النسبة المئوية (percent-encoding).
    - يضيف / في النهاية للمسارات التي لا تبدو ملفات (إن طُلب).
    - يحذف معاملات الاستعلام إلا المسموح بها، ويرتّبها.
    """
    url = (url or "").strip()
    if base:
        url = urljoin(base, url)
    parts = urlsplit(url)
    scheme = parts.scheme.lower()
    host = (parts.hostname or "").lower()
    port = parts.port

    if host_aliases and host in host_aliases:
        host = host_aliases[host]
        scheme = "https"
        port = None

    netloc = host
    if port and _DEFAULT_PORTS.get(scheme) != port:
        netloc = f"{host}:{port}"

    path = _MULTI_SLASH.sub("/", parts.path or "/")
    path = quote(unquote(path), safe=_SAFE_PATH_CHARS)
    if trailing_slash and not path.endswith("/"):
        last = path.rsplit("/", 1)[-1]
        if "." not in last:
            path += "/"

    query = ""
    if keep_query and parts.query:
        kept = sorted((k, v) for k, v in parse_qsl(parts.query, keep_blank_values=True) if k in keep_query)
        query = urlencode(kept)

    return urlunsplit((scheme, netloc, path, query, ""))


def fragment_of(url: str) -> str:
    return urlsplit(url).fragment


_WIN_RESERVED = {
    "CON", "PRN", "AUX", "NUL",
    *(f"COM{i}" for i in range(1, 10)),
    *(f"LPT{i}" for i in range(1, 10)),
}
_BAD_FS_CHARS = re.compile(r'[<>:"/\\|?*\x00-\x1f]')


def safe_filename(name: str, max_len: int = 120, fallback: str = "book") -> str:
    """اسم ملف آمن على Windows/Linux/macOS مع الحفاظ على الحروف العربية."""
    name = _BAD_FS_CHARS.sub("_", name or "")
    name = re.sub(r"\s+", " ", name).strip(" .")
    if not name:
        name = fallback
    if name.upper() in _WIN_RESERVED:
        name += "_"
    if len(name) > max_len:
        name = name[:max_len].rstrip(" .")
    return name
