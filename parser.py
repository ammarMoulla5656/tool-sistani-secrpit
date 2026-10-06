"""
parser.py — استخراج وتنظيف محتوى الصفحات وتحويله إلى كتل XHTML قياسية.

القواعد الصارمة:
1. الحفاظ المطلق على النص العربي حرفيًا دون تصحيح أو تلخيص أو تعديل.
2. الحفاظ على ترقيم المسائل وعناوين الفصول والأبواب.
3. استبعاد نصوص وعناصر الموقع الدخيلة (البحث، القوائم، المشاركة).
4. تحويل الروابط الداخلية للكتاب إلى روابط EPUB داخلية.
5. استخراج روابط الصور الصالحة داخل المتن لإدراجها محليًا.
6. ضمان توافق المخرجات مع معايير XHTML الصارمة لـ EPUB 3 و EPUB 2.
"""
from __future__ import annotations

import html
import re
from typing import TYPE_CHECKING
from urllib.parse import urljoin, urlsplit

from bs4 import BeautifulSoup, Comment, NavigableString, Tag

from urlutils import normalize_url

if TYPE_CHECKING:
    from models import PageContent


# العناصر المسموح بها في محتوى متون كتب EPUB
_ALLOWED_TAGS = {
    "h1", "h2", "h3", "h4", "h5", "h6",
    "p", "div", "blockquote", "pre", "hr",
    "b", "strong", "i", "em", "u", "s", "span", "sub", "sup",
    "ol", "ul", "li", "dl", "dt", "dd",
    "table", "thead", "tbody", "tfoot", "tr", "th", "td", "caption",
    "a", "img", "br",
}

# سمات غير مقبولة أو خطرة
_FORBIDDEN_ATTRS = {
    "onclick", "onmouseover", "onload", "onerror", "onfocus",
    "style", "width", "height", "color", "bgcolor", "border",
}


def _clean_node_recursively(tag: Tag) -> None:
    """تنظيف الشجرة من العناصر غير المرغوبة والسمات الملوثة."""
    for comment in list(tag.find_all(text=lambda t: isinstance(t, Comment))):
        comment.extract()

    for child in list(tag.find_all(True)):
        name = child.name.lower()
        if name not in _ALLOWED_TAGS:
            # تفريغ المحتوى مع حذف الوسم نفسه
            child.unwrap()
            continue

        # تنظيف السمات
        attrs_to_remove = []
        for attr in child.attrs:
            attr_lower = attr.lower()
            if attr_lower in _FORBIDDEN_ATTRS or attr_lower.startswith("on"):
                attrs_to_remove.append(attr)

        for attr in attrs_to_remove:
            del child[attr]


def segment_into_paragraphs(container: Tag) -> list[Tag]:
    """تقسيم محتوى الحاوية المفصول بـ <br> إلى فقرات <p> نظيفة متناسقة مع معايير XHTML."""
    soup = BeautifulSoup("", "lxml")
    paragraphs: list[Tag] = []
    current_p = soup.new_tag("p")

    def flush_current():
        nonlocal current_p
        # إذا كانت الفقرة تحتوي نصًا أو صورًا
        text = current_p.get_text(strip=True)
        has_media = bool(current_p.find(["img", "table", "ul", "ol"]))
        if text or has_media:
            paragraphs.append(current_p)
        current_p = soup.new_tag("p")

    for child in list(container.contents):
        if isinstance(child, Tag):
            name = child.name.lower()
            if name == "br":
                flush_current()
            elif name in {"h1", "h2", "h3", "h4", "h5", "h6", "hr", "table", "ul", "ol", "blockquote"}:
                flush_current()
                paragraphs.append(child)
            else:
                # وسوم نصية عادية مثل b, span, a, strong
                current_p.append(child)
        elif isinstance(child, NavigableString):
            s = str(child)
            # إزالة علامات الأسطر الفارغة الفائضة مع الحفاظ على الكلمات
            if "\n" in s:
                parts = s.split("\n")
                for i, part in enumerate(parts):
                    if i > 0 and not part.strip():
                        # سطر فارغ
                        flush_current()
                    else:
                        if part:
                            current_p.append(NavigableString(part))
            else:
                if s:
                    current_p.append(NavigableString(s))

    flush_current()
    return paragraphs


class PageParser:
    def __init__(self, book_scope: str):
        self.book_scope = book_scope

    def parse_page(
        self,
        page_content: PageContent,
        page_url: str,
        image_resolver: callable | None = None,
        link_resolver: callable | None = None,
    ) -> tuple[str, list[str], set[str], list[str]]:
        """تحويل PageContent إلى قطع XHTML نظيفة.

        يعيد:
        - clean_title: عنوان الصفحة المستخرج
        - html_blocks: قائمة سلاسل XHTML تمثل فقرات وترويسات متن الصفحة
        - anchors: قائمة أسماء المعرّفات الموجودة (id / name)
        - found_image_urls: روابط الصور المكتشفة داخل المتن لتنزيلها
        """
        title = page_content.title or ""
        body_tag = page_content.body
        footnotes_tag = page_content.footnotes

        # تنظيف العناصر والسمات
        _clean_node_recursively(body_tag)
        if footnotes_tag:
            _clean_node_recursively(footnotes_tag)

        found_image_urls: list[str] = []
        anchors: set[str] = set()

        # معالجة الصور
        for img in body_tag.find_all("img"):
            src = img.get("src")
            if not src:
                img.decompose()
                continue
            full_src = urljoin(page_url, src)
            found_image_urls.append(full_src)
            if image_resolver:
                local_src = image_resolver(full_src)
                img["src"] = local_src
            if not img.get("alt"):
                img["alt"] = ""

        # معالجة الروابط
        for a in body_tag.find_all("a"):
            aid = a.get("id") or a.get("name")
            if aid:
                anchors.add(aid)

            href = a.get("href")
            if not href:
                continue

            full_href = urljoin(page_url, href)
            # رابط داخلي لنفس الكتاب
            if full_href.startswith(self.book_scope):
                if link_resolver:
                    new_href = link_resolver(full_href)
                    a["href"] = new_href
            else:
                # رابط خارجي
                a["rel"] = "noopener noreferrer"

        # تقسيم إلى فقرات منتظمة
        blocks_tags = segment_into_paragraphs(body_tag)

        # تحويل الوسوم إلى نصوص XHTML سليمة
        html_blocks: list[str] = []
        for tag in blocks_tags:
            # تحويل الوسم إلى XHTML مع إغلاق الوسوم الذاتية
            tag_xml = tag.decode(formatter="minimal")
            html_blocks.append(tag_xml)

        # معالجة الحواشي إن وجدت
        if footnotes_tag and footnotes_tag.get_text(strip=True):
            fn_blocks = segment_into_paragraphs(footnotes_tag)
            if fn_blocks:
                html_blocks.append('<hr class="footnote-separator" />')
                for ftag in fn_blocks:
                    if "class" not in ftag.attrs:
                        ftag["class"] = ["footnote"]
                    else:
                        ftag["class"].append("footnote")
                    html_blocks.append(ftag.decode(formatter="minimal"))

        return title, html_blocks, anchors, found_image_urls
