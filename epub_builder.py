"""
epub_builder.py — مولد ملفات EPUB 3 القياسية المتوافقة مع قارئات EPUB 2.

المواصفات المنفذة:
- معايير W3C EPUB 3.4 الرسمية مع دعم كامل للغة العربية (RTL و UTF-8).
- ملف mimetype غير مضغوط (Stored) في أول بايتات الأرشيف.
- META-INF/container.xml سليم.
- OEBPS/content.opf كامل مع معرّف UUID موحد وبيانات وصفية دقيقة.
- OEBPS/nav.xhtml كوثيقة تنقل رسمية لـ EPUB 3 مع فهرس هرمي.
- OEBPS/toc.ncx لدعم القارئات القديمة (EPUB 2) مع تطابق تام للمعرّف.
- OEBPS/Styles/style.css مدمج لتنسيق النصوص والخطوط العربية.
- OEBPS/Images/ للصور والغلاف مع تسجيل MIME الصحيح.
- تقسيم الصفحات (حسب الباب الرئيسي أو حسب الصفحة المنفردة) مع روابط داخلية سليمة.
"""
from __future__ import annotations

import html
import io
import logging
import uuid
import zipfile
from collections import OrderedDict
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import TYPE_CHECKING

from config import Config
from urlutils import safe_filename

if TYPE_CHECKING:
    from models import Book, ImageAsset, Page

logger = logging.getLogger("book2epub.builder")


@dataclass
class TocNode:
    title: str
    target_href: str | None = None
    children: dict[str, TocNode] = field(default_factory=OrderedDict)


@dataclass
class SectionDoc:
    filename: str            # مثل Text/part_01.xhtml
    item_id: str             # مثل sec_0001
    title: str
    html_content: str
    size_bytes: int = 0


class EPUBBuilder:
    def __init__(self, book: Book, config: Config):
        self.book = book
        self.config = config
        self.book_uuid = f"urn:uuid:{uuid.uuid4()}"
        self.now_utc = datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")

    def _build_toc_tree(
        self,
        page_targets: dict[str, str],
        group_targets: dict[str, str] | None = None,
    ) -> TocNode:
        """بناء شجرة الفهرس الهرمية استنادًا إلى مسارات الصفحات.

        page_targets: خريطة من url الصفحة إلى رابط EPUB الداخلي (مثال: 'Text/part_01.xhtml#anchor-1')
        """
        if group_targets is None:
            group_targets = {}

        root = TocNode(title="Root")
        for page in self.book.pages:
            href = page_targets.get(page.url)
            if not href:
                continue

            current = root
            path = page.path or [page.source_title or "صفحة"]
            for i, part in enumerate(path):
                clean_part = part.strip()
                if not clean_part:
                    continue
                if clean_part not in current.children:
                    node = TocNode(title=clean_part)
                    if i == 0 and clean_part in group_targets:
                        node.target_href = group_targets[clean_part]
                    current.children[clean_part] = node
                current = current.children[clean_part]
                # الرابط يرتبط بأعمق عقدة (الورقة) أو إذا كانت العقدة تمثل هذه الصفحة
                if i == len(path) - 1:
                    current.target_href = href

        return root

    def _render_nav_ol(self, node: TocNode) -> str:
        """توليد قائمة HTML <ol> لعقد الفهرس في nav.xhtml."""
        lines = ["<ol>"]
        for child in node.children.values():
            safe_title = html.escape(child.title)
            if child.target_href:
                item_html = f'<a href="{child.target_href}">{safe_title}</a>'
            else:
                # إذا كانت عقدة مجلد لا تملك رابطًا مباشرًا نربطها بأول ابن يملك رابطًا
                first_href = self._get_first_href(child)
                if first_href:
                    item_html = f'<a href="{first_href}">{safe_title}</a>'
                else:
                    item_html = f"<span>{safe_title}</span>"

            if child.children:
                sub_ol = self._render_nav_ol(child)
                lines.append(f"<li>{item_html}\n{sub_ol}</li>")
            else:
                lines.append(f"<li>{item_html}</li>")
        lines.append("</ol>")
        return "\n".join(lines)

    def _get_first_href(self, node: TocNode) -> str | None:
        if node.target_href:
            return node.target_href
        for child in node.children.values():
            href = self._get_first_href(child)
            if href:
                return href
        return None

    def _render_ncx_navpoints(
        self,
        node: TocNode,
        play_order_start: int = 1,
        seen_targets: dict[str, int] | None = None,
    ) -> tuple[str, int]:
        """توليد عناصر <navPoint> لـ toc.ncx مع الحفاظ على الترقيم playOrder وتفادي تعارض الأهداف المكررة."""
        if seen_targets is None:
            seen_targets = {}

        xml_chunks = []
        play_order = play_order_start
        for idx, child in enumerate(node.children.values()):
            safe_title = html.escape(child.title)
            href = child.target_href or self._get_first_href(child) or "Text/title.xhtml"

            if href in seen_targets:
                current_order = seen_targets[href]
            else:
                current_order = play_order
                seen_targets[href] = current_order
                play_order += 1

            nav_id = f"navPoint-{current_order}_{idx}"

            sub_xml = ""
            if child.children:
                sub_xml, play_order = self._render_ncx_navpoints(child, play_order, seen_targets)

            chunk = (
                f'    <navPoint id="{nav_id}" playOrder="{current_order}">\n'
                f'      <navLabel><text>{safe_title}</text></navLabel>\n'
                f'      <content src="{href}"/>\n'
                f"{sub_xml}"
                f"    </navPoint>\n"
            )
            xml_chunks.append(chunk)

        return "".join(xml_chunks), play_order

    def _prepare_sections(self) -> tuple[list[SectionDoc], dict[str, str]]:
        """تجهيز ملفات XHTML وتقسيمها إما حسب الباب (part) أو حسب الصفحة (page).

        يعيد (sections_list, url_to_epub_href_map)
        """
        sections: list[SectionDoc] = []
        url_to_target: dict[str, str] = {}

        split_mode = self.config.split_mode  # 'part' أو 'page'

        if split_mode == "page":
            # ملف منفرد لكل صفحة كما في الملف المرجعي
            for idx, page in enumerate(self.book.pages, start=1):
                filename = f"Text/Section{idx:04d}.xhtml"
                item_id = f"sec_{idx:04d}"
                anchor_id = f"page_{idx}"
                url_to_target[page.url] = f"{filename}#{anchor_id}"

                doc_title = page.source_title or (page.path[-1] if page.path else f"صفحة {idx}")

                body_lines = [
                    f'<h2 class="chapter-title" id="{anchor_id}">{html.escape(doc_title)}</h2>',
                ]
                body_lines.extend(page.blocks)

                content = self._wrap_xhtml(doc_title, "\n".join(body_lines))
                sections.append(SectionDoc(
                    filename=filename,
                    item_id=item_id,
                    title=doc_title,
                    html_content=content,
                    size_bytes=len(content.encode("utf-8")),
                ))
            return sections, url_to_target, {}
        else:
            # تجميع الصفحات حسب الباب الرئيسي الأول
            groups: OrderedDict[str, list[Page]] = OrderedDict()
            for page in self.book.pages:
                main_group = page.path[0] if page.path else "المقدمة"
                groups.setdefault(main_group, []).append(page)

            sec_idx = 1
            group_targets: dict[str, str] = {}
            for group_name, pages in groups.items():
                filename = f"Text/part_{sec_idx:03d}.xhtml"
                item_id = f"part_{sec_idx:03d}"
                part_anchor = f"part_{sec_idx}"
                group_targets[group_name] = f"{filename}#{part_anchor}"

                body_lines = [
                    f'<h1 class="part-title" id="{part_anchor}">{html.escape(group_name)}</h1>',
                ]

                for p_idx, page in enumerate(pages, start=1):
                    anchor_id = f"chap_{sec_idx}_{p_idx}"
                    url_to_target[page.url] = f"{filename}#{anchor_id}"

                    sub_title = " » ".join(page.path[1:]) if len(page.path) > 1 else (page.source_title or group_name)
                    if sub_title and sub_title != group_name:
                        body_lines.append(f'<h2 class="chapter-title" id="{anchor_id}">{html.escape(sub_title)}</h2>')
                    else:
                        body_lines.append(f'<div id="{anchor_id}"></div>')

                    body_lines.extend(page.blocks)

                content = self._wrap_xhtml(group_name, "\n".join(body_lines))
                sections.append(SectionDoc(
                    filename=filename,
                    item_id=item_id,
                    title=group_name,
                    html_content=content,
                    size_bytes=len(content.encode("utf-8")),
                ))
                sec_idx += 1

        return sections, url_to_target, group_targets

    def _wrap_xhtml(self, title: str, body_inner: str) -> str:
        """تغليف المحتوى في قالب XHTML متوافق مع معايير EPUB 3 و W3C."""
        safe_title = html.escape(title or self.book.meta.title)
        return (
            '<?xml version="1.0" encoding="utf-8"?>\n'
            '<!DOCTYPE html>\n'
            '<html xmlns="http://www.w3.org/1999/xhtml" xmlns:epub="http://www.idpf.org/2007/ops" xml:lang="ar" dir="rtl">\n'
            '<head>\n'
            '  <meta charset="utf-8" />\n'
            f'  <title>{safe_title}</title>\n'
            '  <link rel="stylesheet" type="text/css" href="../Styles/style.css" />\n'
            '</head>\n'
            '<body>\n'
            f'{body_inner}\n'
            '</body>\n'
            '</html>'
        )

    def _build_title_page(self) -> SectionDoc:
        """إنشاء صفحة بيانات الكتاب (Title Page)."""
        meta = self.book.meta
        lines = [
            '<div class="title-page">',
            f'  <h1 class="book-title">{html.escape(meta.title)}</h1>',
        ]
        if meta.author:
            lines.append(f'  <div class="meta-author">تأليف: <strong>{html.escape(meta.author)}</strong></div>')
        if meta.publisher:
            lines.append(f'  <div class="meta-publisher">{html.escape(meta.publisher)}</div>')
        if meta.source_url:
            lines.append(f'  <div class="meta-date">المصدر: <a href="{html.escape(meta.source_url)}">{html.escape(meta.source_url)}</a></div>')
        if meta.extracted_at:
            dt_str = meta.extracted_at.strftime("%Y-%m-%d")
            lines.append(f'  <div class="meta-date">تاريخ الاستخراج: {dt_str}</div>')
        lines.append('</div>')

        content = self._wrap_xhtml(meta.title, "\n".join(lines))
        return SectionDoc(
            filename="Text/title.xhtml",
            item_id="title_page",
            title="صفحة العنوان",
            html_content=content,
            size_bytes=len(content.encode("utf-8")),
        )

    def _build_cover_page(self, cover_asset: ImageAsset) -> SectionDoc:
        """إنشاء صفحة غلاف الكتاب."""
        body = (
            '<div class="cover-wrapper">\n'
            f'  <img class="cover-image" src="../Images/{cover_asset.filename}" alt="{html.escape(self.book.meta.title)}" />\n'
            '</div>'
        )
        content = self._wrap_xhtml("الغلاف", body)
        return SectionDoc(
            filename="Text/cover.xhtml",
            item_id="cover_page",
            title="الغلاف",
            html_content=content,
            size_bytes=len(content.encode("utf-8")),
        )

    def build(self, output_path: Path | None = None) -> Path:
        """بناء ملف الـ EPUB كاملًا وحفظه على القرص."""
        meta = self.book.meta
        if output_path is None:
            filename = safe_filename(meta.title) + ".epub"
            output_path = self.config.output_dir / filename

        output_path.parent.mkdir(parents=True, exist_ok=True)

        logger.info(f"بدء بناء كتاب EPUB: {meta.title} -> {output_path}")

        # 1. تجهيز المقاطع وربط الروابط
        sections, page_targets, group_targets = self._prepare_sections()

        # 2. بناء شجرة الفهرس
        toc_root = self._build_toc_tree(page_targets, group_targets)

        # 3. تجهيز صفحة العنوان وصفحة الغلاف
        title_doc = self._build_title_page()
        cover_doc = None
        if self.book.cover and self.config.include_cover:
            cover_doc = self._build_cover_page(self.book.cover)

        # 4. بناء وثيقة التنقل nav.xhtml
        nav_ol_html = self._render_nav_ol(toc_root)
        nav_body = (
            '<nav epub:type="toc" id="toc" class="epub-toc">\n'
            f'  <h1 class="book-title">فهرس المحتويات</h1>\n'
            f'{nav_ol_html}\n'
            '</nav>'
        )
        nav_doc = SectionDoc(
            filename="nav.xhtml",
            item_id="nav",
            title="الفهرس",
            html_content=(
                '<?xml version="1.0" encoding="utf-8"?>\n'
                '<!DOCTYPE html>\n'
                '<html xmlns="http://www.w3.org/1999/xhtml" xmlns:epub="http://www.idpf.org/2007/ops" xml:lang="ar" dir="rtl">\n'
                '<head>\n'
                '  <meta charset="utf-8" />\n'
                '  <title>فهرس المحتويات</title>\n'
                '  <link rel="stylesheet" type="text/css" href="Styles/style.css" />\n'
                '</head>\n'
                '<body>\n'
                f'{nav_body}\n'
                '</body>\n'
                '</html>'
            ),
        )

        # 5. بناء toc.ncx لدعم EPUB 2
        ncx_navpoints, _ = self._render_ncx_navpoints(toc_root, play_order_start=1)
        ncx_content = (
            '<?xml version="1.0" encoding="utf-8"?>\n'
            '<!DOCTYPE ncx PUBLIC "-//NISO//DTD ncx 2005-1//EN" "http://www.daisy.org/z3986/2005/ncx-2005-1.dtd">\n'
            '<ncx xmlns="http://www.daisy.org/z3986/2005/ncx/" version="2005-1" xml:lang="ar" dir="rtl">\n'
            '  <head>\n'
            f'    <meta name="dtb:uid" content="{self.book_uuid}"/>\n'
            '    <meta name="dtb:depth" content="3"/>\n'
            '    <meta name="dtb:totalPageCount" content="0"/>\n'
            '    <meta name="dtb:maxPageNumber" content="0"/>\n'
            '  </head>\n'
            f'  <docTitle><text>{html.escape(meta.title)}</text></docTitle>\n'
            f'  <docAuthor><text>{html.escape(meta.author or "")}</text></docAuthor>\n'
            '  <navMap>\n'
            f'{ncx_navpoints}'
            '  </navMap>\n'
            '</ncx>'
        )

        # 6. قراءة ملف التنسيق CSS
        css_path = self.config.templates_dir / "style.css"
        css_data = css_path.read_text(encoding="utf-8") if css_path.exists() else "body { direction: rtl; text-align: right; }"

        # 7. بناء content.opf
        manifest_items: list[str] = [
            '    <item id="ncx" href="toc.ncx" media-type="application/x-dtbncx+xml"/>',
            '    <item id="nav" href="nav.xhtml" media-type="application/xhtml+xml" properties="nav"/>',
            '    <item id="style" href="Styles/style.css" media-type="text/css"/>',
            f'    <item id="{title_doc.item_id}" href="{title_doc.filename}" media-type="application/xhtml+xml"/>',
        ]
        spine_items: list[str] = []

        if cover_doc and self.book.cover:
            manifest_items.append(
                f'    <item id="cover-image" href="Images/{self.book.cover.filename}" media-type="{self.book.cover.media_type}" properties="cover-image"/>'
            )
            manifest_items.append(
                f'    <item id="{cover_doc.item_id}" href="{cover_doc.filename}" media-type="application/xhtml+xml"/>'
            )
            spine_items.append(f'    <itemref idref="{cover_doc.item_id}"/>')

        spine_items.append(f'    <itemref idref="{title_doc.item_id}"/>')
        spine_items.append('    <itemref idref="nav"/>')

        for sec in sections:
            manifest_items.append(
                f'    <item id="{sec.item_id}" href="{sec.filename}" media-type="application/xhtml+xml"/>'
            )
            spine_items.append(f'    <itemref idref="{sec.item_id}"/>')

        # إضافة باقي الصور إلى الـ manifest
        for img in self.book.images:
            img_id = f"img_{safe_filename(img.filename, max_len=30)}"
            manifest_items.append(
                f'    <item id="{img_id}" href="Images/{img.filename}" media-type="{img.media_type}"/>'
            )

        meta_cover_tag = '<meta name="cover" content="cover-image"/>' if cover_doc else ""

        opf_content = (
            '<?xml version="1.0" encoding="utf-8"?>\n'
            '<package xmlns="http://www.idpf.org/2007/opf" version="3.0" unique-identifier="pub-id" dir="rtl" xml:lang="ar">\n'
            '  <metadata xmlns:dc="http://purl.org/dc/elements/1.1/">\n'
            f'    <dc:identifier id="pub-id">{self.book_uuid}</dc:identifier>\n'
            f'    <dc:title>{html.escape(meta.title)}</dc:title>\n'
            '    <dc:language>ar</dc:language>\n'
            f'    <dc:creator>{html.escape(meta.author or "غير محدد")}</dc:creator>\n'
            f'    <dc:publisher>{html.escape(meta.publisher or "")}</dc:publisher>\n'
            f'    <meta property="dcterms:modified">{self.now_utc}</meta>\n'
            f'    {meta_cover_tag}\n'
            '  </metadata>\n'
            '  <manifest>\n'
            + "\n".join(manifest_items) + "\n"
            '  </manifest>\n'
            '  <spine toc="ncx" page-progression-direction="rtl">\n'
            + "\n".join(spine_items) + "\n"
            '  </spine>\n'
            '</package>'
        )

        # 8. كتابة حزمة الـ ZIP وفق المعيار القياسي لـ EPUB
        # ملاحظة هامة: mimetype يجب أن يكون أول ملف وبدون أي ضغط (Stored)
        with zipfile.ZipFile(output_path, "w") as zf:
            # mimetype (Uncompressed, exactly 20 bytes)
            mimetype_info = zipfile.ZipInfo("mimetype")
            mimetype_info.compress_type = zipfile.ZIP_STORED
            mimetype_info.file_size = 20
            mimetype_info.flag_bits = 0
            zf.writestr(mimetype_info, b"application/epub+zip")

            # META-INF/container.xml
            container_xml = (
                '<?xml version="1.0" encoding="UTF-8"?>\n'
                '<container version="1.0" xmlns="urn:oasis:names:tc:opendocument:xmlns:container">\n'
                '  <rootfiles>\n'
                '    <rootfile full-path="OEBPS/content.opf" media-type="application/oebps-package+xml"/>\n'
                '  </rootfiles>\n'
                '</container>'
            )
            zf.writestr("META-INF/container.xml", container_xml.encode("utf-8"), compress_type=zipfile.ZIP_DEFLATED)

            # OEBPS/content.opf
            zf.writestr("OEBPS/content.opf", opf_content.encode("utf-8"), compress_type=zipfile.ZIP_DEFLATED)

            # OEBPS/toc.ncx
            zf.writestr("OEBPS/toc.ncx", ncx_content.encode("utf-8"), compress_type=zipfile.ZIP_DEFLATED)

            # OEBPS/nav.xhtml
            zf.writestr("OEBPS/nav.xhtml", nav_doc.html_content.encode("utf-8"), compress_type=zipfile.ZIP_DEFLATED)

            # OEBPS/Styles/style.css
            zf.writestr("OEBPS/Styles/style.css", css_data.encode("utf-8"), compress_type=zipfile.ZIP_DEFLATED)

            # OEBPS/Text/title.xhtml
            zf.writestr(f"OEBPS/{title_doc.filename}", title_doc.html_content.encode("utf-8"), compress_type=zipfile.ZIP_DEFLATED)

            # الغلاف إن وجد
            if cover_doc and self.book.cover:
                zf.writestr(f"OEBPS/{cover_doc.filename}", cover_doc.html_content.encode("utf-8"), compress_type=zipfile.ZIP_DEFLATED)
                zf.writestr(f"OEBPS/Images/{self.book.cover.filename}", self.book.cover.data, compress_type=zipfile.ZIP_DEFLATED)

            # ملفات المتن
            for sec in sections:
                zf.writestr(f"OEBPS/{sec.filename}", sec.html_content.encode("utf-8"), compress_type=zipfile.ZIP_DEFLATED)

            # باقي الصور
            for img in self.book.images:
                zf.writestr(f"OEBPS/Images/{img.filename}", img.data, compress_type=zipfile.ZIP_DEFLATED)

        logger.info(f"تم بنجاح إنشاء حزمة EPUB بحجم {output_path.stat().st_size:,} بايت")
        return output_path
