"""
pdfsync/writer.py — كتابة EPUB 3 الناتج (صفحة PDF = ملف XHTML)، مع إعادة استخدام EPUBBuilder.

يرث من EPUBBuilder الحالي ليستعير توليد شجرة الفهرس (nav) وملف NCX كما هما، ويضيف:
  - Text/page-NNNN.xhtml لكل صفحة PDF (بالترتيب، عددها = عدد صفحات PDF المستهدفة).
  - nav.xhtml: فهرس EPUB الأصلي مُعاد توجيهه إلى الصفحات الجديدة + page-list لكل الصفحات.
  - toc.ncx للتوافق مع القارئات القديمة.
  - بقية موارد EPUB الأصلي (صور/CSS/خطوط) بمساراتها الأصلية.
"""
from __future__ import annotations

import html
import logging
import posixpath
import uuid
import zipfile
from pathlib import Path
from types import SimpleNamespace

from lxml import etree

from config import Config
from epub_builder import EPUBBuilder, TocNode

from .epub_source import SourceEpub
from .splitter import PageFragment, page_filename

logger = logging.getLogger("book2epub.pdfsync.writer")

_DC_EXTRA_OK = {"date", "description", "subject", "rights", "source", "contributor", "type",
                "format", "relation", "coverage"}


def _serialize(root: etree._Element) -> bytes:
    return etree.tostring(root, encoding="utf-8", xml_declaration=True, doctype="<!DOCTYPE html>")


class PageEPUBWriter(EPUBBuilder):
    def __init__(self, config: Config, title: str):
        # لا نستدعي EPUBBuilder.__init__ لأنه يتطلب Book من الزاحف؛ نضبط ما تحتاجه الدوال المستعارة فقط.
        self.config = config
        self.book = SimpleNamespace(meta=SimpleNamespace(title=title))
        self.book_uuid = f"urn:uuid:{uuid.uuid4()}"
        from datetime import datetime, timezone
        self.now_utc = datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")

    def write(
        self,
        output_path: Path,
        src: SourceEpub,
        pages: list[PageFragment],
        toc_root: TocNode,
        page_labels: list[str],
        source_note: str,
    ) -> Path:
        output_path = Path(output_path)
        output_path.parent.mkdir(parents=True, exist_ok=True)
        opf_dir = src.opf_dir

        def P(rel: str) -> str:          # مسار داخل الأرشيف نسبةً لمجلد OPF
            return posixpath.join(opf_dir, rel) if opf_dir else rel

        md = src.metadata
        title = md.get("title") or "كتاب"

        # ---- CSS افتراضي عند غياب أي ورقة أنماط في المصدر
        default_css_path = P("Styles/style.css")
        needs_default_css = default_css_path not in src.resources and any(
            (lk.get("href") == "../Styles/style.css")
            for pg in pages for lk in pg.root.iter("{http://www.w3.org/1999/xhtml}link")
        )
        css_bytes = b""
        if needs_default_css:
            tpl = self.config.templates_dir / "style.css"
            css_bytes = (tpl.read_text(encoding="utf-8") if tpl.exists()
                         else "body { text-align: right; }").encode("utf-8")

        first_css = next((r.path for r in src.resources.values() if r.media_type == "text/css"), None)
        nav_css = (posixpath.relpath(first_css, opf_dir or ".") if first_css
                   else ("Styles/style.css" if needs_default_css else None))

        # ---- nav.xhtml: toc + page-list
        toc_ol = self._render_nav_ol(toc_root)
        page_items = "\n".join(
            f'    <li><a href="Text/{page_filename(pg.number)}#pg-{pg.number:04d}">{html.escape(page_labels[pg.number - 1])}</a></li>'
            for pg in pages
        )
        css_link = f'  <link rel="stylesheet" type="text/css" href="{nav_css}" />\n' if nav_css else ""
        nav_xhtml = (
            '<?xml version="1.0" encoding="utf-8"?>\n<!DOCTYPE html>\n'
            '<html xmlns="http://www.w3.org/1999/xhtml" xmlns:epub="http://www.idpf.org/2007/ops" '
            'xml:lang="ar" lang="ar" dir="rtl">\n<head>\n  <meta charset="utf-8" />\n'
            f'  <title>{html.escape(title)} — الفهرس</title>\n{css_link}</head>\n<body>\n'
            '<nav epub:type="toc" id="toc" class="epub-toc">\n  <h1 class="book-title">فهرس المحتويات</h1>\n'
            f'{toc_ol}\n</nav>\n'
            '<nav epub:type="page-list" id="page-list" hidden="hidden">\n  <ol>\n'
            f'{page_items}\n  </ol>\n</nav>\n</body>\n</html>'
        )

        # ---- toc.ncx
        navpoints, _ = self._render_ncx_navpoints(toc_root, play_order_start=1)
        creators = md.get("creators") or []
        ncx = (
            '<?xml version="1.0" encoding="utf-8"?>\n'
            '<!DOCTYPE ncx PUBLIC "-//NISO//DTD ncx 2005-1//EN" "http://www.daisy.org/z3986/2005/ncx-2005-1.dtd">\n'
            '<ncx xmlns="http://www.daisy.org/z3986/2005/ncx/" version="2005-1" xml:lang="ar" dir="rtl">\n'
            f'  <head>\n    <meta name="dtb:uid" content="{self.book_uuid}"/>\n'
            '    <meta name="dtb:depth" content="3"/>\n'
            '    <meta name="dtb:totalPageCount" content="0"/>\n'
            '    <meta name="dtb:maxPageNumber" content="0"/>\n  </head>\n'
            f'  <docTitle><text>{html.escape(title)}</text></docTitle>\n'
            f'  <docAuthor><text>{html.escape(creators[0] if creators else "")}</text></docAuthor>\n'
            f'  <navMap>\n{navpoints}  </navMap>\n</ncx>'
        )

        # ---- content.opf
        used_ids = {"ncx", "nav"} | {f"page_{pg.number:04d}" for pg in pages}
        manifest = [
            '    <item id="ncx" href="toc.ncx" media-type="application/x-dtbncx+xml"/>',
            '    <item id="nav" href="nav.xhtml" media-type="application/xhtml+xml" properties="nav"/>',
        ]
        if needs_default_css:
            used_ids.add("pdfsync_style")
            manifest.append('    <item id="pdfsync_style" href="Styles/style.css" media-type="text/css"/>')
        id_map: dict[str, str] = {}
        for r in src.resources.values():
            rid = r.item_id if r.item_id not in used_ids else f"res_{r.item_id}"
            used_ids.add(rid)
            id_map[r.item_id] = rid
            props = f' properties="{html.escape(r.properties)}"' if r.properties else ""
            href = html.escape(posixpath.relpath(r.path, opf_dir or "."))
            manifest.append(f'    <item id="{html.escape(rid)}" href="{href}" media-type="{r.media_type}"{props}/>')
        spine = []
        for pg in pages:
            iid = f"page_{pg.number:04d}"
            manifest.append(f'    <item id="{iid}" href="Text/{page_filename(pg.number)}" media-type="application/xhtml+xml"/>')
            spine.append(f'    <itemref idref="{iid}"/>')

        meta_lines = [
            f'    <dc:identifier id="pub-id">{self.book_uuid}</dc:identifier>',
            f'    <dc:title>{html.escape(title)}</dc:title>',
            f'    <dc:language>{html.escape(md.get("language") or "ar")}</dc:language>',
        ]
        for c in creators or ["غير محدد"]:
            meta_lines.append(f"    <dc:creator>{html.escape(c)}</dc:creator>")
        if md.get("publisher"):
            meta_lines.append(f'    <dc:publisher>{html.escape(md["publisher"])}</dc:publisher>')
        for name, text in md.get("extra", []):
            if name in _DC_EXTRA_OK and text:
                meta_lines.append(f"    <dc:{name}>{html.escape(text)}</dc:{name}>")
        meta_lines.append(f"    <dc:source>{html.escape(source_note)}</dc:source>")
        meta_lines.append(f'    <meta property="dcterms:modified">{self.now_utc}</meta>')
        cover_ref = md.get("cover_meta")
        if cover_ref and cover_ref in id_map:
            meta_lines.append(f'    <meta name="cover" content="{html.escape(id_map[cover_ref])}"/>')

        opf = (
            '<?xml version="1.0" encoding="utf-8"?>\n'
            '<package xmlns="http://www.idpf.org/2007/opf" version="3.0" unique-identifier="pub-id" dir="rtl" xml:lang="ar">\n'
            '  <metadata xmlns:dc="http://purl.org/dc/elements/1.1/">\n' + "\n".join(meta_lines) + "\n  </metadata>\n"
            "  <manifest>\n" + "\n".join(manifest) + "\n  </manifest>\n"
            '  <spine toc="ncx" page-progression-direction="rtl">\n' + "\n".join(spine) + "\n  </spine>\n</package>"
        )

        container = (
            '<?xml version="1.0" encoding="UTF-8"?>\n'
            '<container version="1.0" xmlns="urn:oasis:names:tc:opendocument:xmlns:container">\n'
            f'  <rootfiles>\n    <rootfile full-path="{P("content.opf")}" media-type="application/oebps-package+xml"/>\n'
            '  </rootfiles>\n</container>'
        )

        with zipfile.ZipFile(output_path, "w") as zf:
            mi = zipfile.ZipInfo("mimetype")
            mi.compress_type = zipfile.ZIP_STORED
            zf.writestr(mi, b"application/epub+zip")
            deflate = zipfile.ZIP_DEFLATED
            zf.writestr("META-INF/container.xml", container.encode("utf-8"), compress_type=deflate)
            zf.writestr(P("content.opf"), opf.encode("utf-8"), compress_type=deflate)
            zf.writestr(P("toc.ncx"), ncx.encode("utf-8"), compress_type=deflate)
            zf.writestr(P("nav.xhtml"), nav_xhtml.encode("utf-8"), compress_type=deflate)
            if needs_default_css:
                zf.writestr(default_css_path, css_bytes, compress_type=deflate)
            for r in src.resources.values():
                zf.writestr(r.path, r.data, compress_type=deflate)
            for pg in pages:
                zf.writestr(P(f"Text/{page_filename(pg.number)}"), _serialize(pg.root), compress_type=deflate)

        logger.info("تم إنشاء %s (%d صفحة) بحجم %s بايت", output_path.name, len(pages),
                    f"{output_path.stat().st_size:,}")
        return output_path
