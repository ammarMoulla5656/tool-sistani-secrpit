"""
pdfsync/writer.py — كتابة EPUB 3 الناتج (صفحة PDF = ملف XHTML) بشكل مستقل تمامًا.

يُنتج:
  - Text/page-NNNN.xhtml لكل صفحة PDF؛ الرقم = رقم صفحة PDF نفسه (صفحة 10 في PDF = page-0010.xhtml).
  - nav.xhtml: فهرس EPUB الأصلي مُعاد توجيهه إلى الصفحات الجديدة + page-list لكل الصفحات.
  - toc.ncx للتوافق مع القارئات القديمة.
  - Images/: صور الصفحات المرسومة من PDF (بلا OCR) + موارد EPUB الأصلي بمساراتها.
  - Styles/pdfsync.css: تنسيق الحواشي وصور الصفحات.
"""
from __future__ import annotations

import html
import logging
import posixpath
import uuid
import zipfile
from datetime import datetime, timezone
from pathlib import Path

from lxml import etree

from .epub_source import SourceEpub
from .splitter import PageFragment, page_filename
from .toc import TocNode

logger = logging.getLogger("book2epub.pdfsync.writer")

_DC_EXTRA_OK = {"date", "description", "subject", "rights", "source", "contributor", "type",
                "format", "relation", "coverage"}

PDFSYNC_CSS = """\
/* pdfsync — تنسيق إضافي لصفحات PDF */
.pdf-page-image { text-align: center; margin: 0; padding: 0; }
.pdf-page-image img { max-width: 100%; height: auto; }
.pdf-footnotes { margin-top: 1.6em; font-size: 0.88em; }
.pdf-footnote-separator { width: 33%; margin: 0.6em 0 0.7em auto; border: 0; border-top: 1px solid currentColor; }
.pdf-footnotes p { margin: 0.25em 0; text-indent: 0; }
"""


def _serialize(root: etree._Element) -> bytes:
    return etree.tostring(root, encoding="utf-8", xml_declaration=True, doctype="<!DOCTYPE html>")


def _nav_ol(node: TocNode, indent: int = 2) -> str:
    pad = "  " * indent
    items = []
    for child in node.children.values():
        label = html.escape(child.title)
        inner = (f'<a href="{html.escape(child.target_href)}">{label}</a>' if child.target_href
                 else f"<span>{label}</span>")
        sub = _nav_ol(child, indent + 2) if child.children else ""
        items.append(f"{pad}  <li>{inner}{sub}</li>")
    return f"\n{pad}<ol>\n" + "\n".join(items) + f"\n{pad}</ol>\n" if items else ""


def _ncx_points(node: TocNode, state: dict, fallback: str, indent: int = 2) -> str:
    """navPoints؛ الأهداف المتطابقة تأخذ playOrder واحدًا (شرط EPUBCheck)."""
    out = []
    pad = "  " * indent
    for child in node.children.values():
        state["n"] += 1
        n = state["n"]
        src = child.target_href or fallback
        order = state["orders"].setdefault(src, len(state["orders"]) + 1)
        out.append(f'{pad}<navPoint id="np{n}" playOrder="{order}">\n{pad}  <navLabel><text>{html.escape(child.title)}</text></navLabel>\n'
                   f'{pad}  <content src="{html.escape(src)}"/>\n'
                   f'{_ncx_points(child, state, fallback, indent + 1)}{pad}</navPoint>\n')
    return "".join(out)


class PageEPUBWriter:
    def __init__(self, title: str):
        self.title = title
        self.book_uuid = f"urn:uuid:{uuid.uuid4()}"
        self.now_utc = datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")

    def write(self, output_path: Path, src: SourceEpub, pages: list[PageFragment], toc_root: TocNode,
              page_labels: list[str], source_note: str) -> Path:
        output_path = Path(output_path)
        output_path.parent.mkdir(parents=True, exist_ok=True)
        opf_dir = src.opf_dir

        def P(rel: str) -> str:
            return posixpath.join(opf_dir, rel) if opf_dir else rel

        md = src.metadata
        title = md.get("title") or self.title or "كتاب"
        first_page = f"Text/{page_filename(pages[0].number)}"

        # ---- nav.xhtml
        toc_html = _nav_ol(toc_root) or (
            f'\n  <ol>\n    <li><a href="{first_page}">{html.escape(title)}</a></li>\n  </ol>\n')
        page_items = "\n".join(
            f'    <li><a href="Text/{page_filename(pg.number)}#pg-{pg.number:04d}">{html.escape(page_labels[pg.index])}</a></li>'
            for pg in pages)
        nav_xhtml = (
            '<?xml version="1.0" encoding="utf-8"?>\n<!DOCTYPE html>\n'
            '<html xmlns="http://www.w3.org/1999/xhtml" xmlns:epub="http://www.idpf.org/2007/ops" '
            'xml:lang="ar" lang="ar" dir="rtl">\n<head>\n  <meta charset="utf-8" />\n'
            f'  <title>{html.escape(title)} — الفهرس</title>\n</head>\n<body>\n'
            '<nav epub:type="toc" id="toc">\n  <h1>فهرس المحتويات</h1>'
            f'{toc_html}</nav>\n'
            '<nav epub:type="page-list" id="page-list" hidden="hidden">\n  <ol>\n'
            f'{page_items}\n  </ol>\n</nav>\n</body>\n</html>')

        # ---- toc.ncx
        points = _ncx_points(toc_root, {"n": 0, "orders": {}}, first_page)
        if not points:
            points = (f'    <navPoint id="np1" playOrder="1"><navLabel><text>{html.escape(title)}</text></navLabel>'
                      f'<content src="{first_page}"/></navPoint>\n')
        creators = md.get("creators") or []
        ncx = (
            '<?xml version="1.0" encoding="utf-8"?>\n'
            '<!DOCTYPE ncx PUBLIC "-//NISO//DTD ncx 2005-1//EN" "http://www.daisy.org/z3986/2005/ncx-2005-1.dtd">\n'
            '<ncx xmlns="http://www.daisy.org/z3986/2005/ncx/" version="2005-1" xml:lang="ar" dir="rtl">\n'
            f'  <head>\n    <meta name="dtb:uid" content="{self.book_uuid}"/>\n'
            '    <meta name="dtb:depth" content="3"/>\n    <meta name="dtb:totalPageCount" content="0"/>\n'
            '    <meta name="dtb:maxPageNumber" content="0"/>\n  </head>\n'
            f'  <docTitle><text>{html.escape(title)}</text></docTitle>\n'
            f'  <docAuthor><text>{html.escape(creators[0] if creators else "")}</text></docAuthor>\n'
            f'  <navMap>\n{points}  </navMap>\n</ncx>')

        # ---- content.opf
        used_ids = {"ncx", "nav", "pdfsync_css"} | {f"page_{pg.number:04d}" for pg in pages}
        manifest = [
            '    <item id="ncx" href="toc.ncx" media-type="application/x-dtbncx+xml"/>',
            '    <item id="nav" href="nav.xhtml" media-type="application/xhtml+xml" properties="nav"/>',
            '    <item id="pdfsync_css" href="Styles/pdfsync.css" media-type="text/css"/>',
        ]
        id_map: dict[str, str] = {}
        reserved = {P("Styles/pdfsync.css"), P("nav.xhtml"), P("toc.ncx"), P("content.opf")}
        resources = [r for r in src.resources.values() if r.path not in reserved]
        for r in resources:
            rid = r.item_id if r.item_id not in used_ids else f"res_{r.item_id}"
            used_ids.add(rid)
            id_map[r.item_id] = rid
            props = f' properties="{html.escape(r.properties)}"' if r.properties else ""
            href = html.escape(posixpath.relpath(r.path, opf_dir or "."))
            manifest.append(f'    <item id="{html.escape(rid)}" href="{href}" media-type="{r.media_type}"{props}/>')
        asset_files: dict[str, bytes] = {}
        for pg in pages:
            for a in pg.assets:
                if a.zip_name in asset_files:
                    continue
                asset_files[a.zip_name] = a.data
                aid = f"pgimg_{len(asset_files):04d}"
                manifest.append(f'    <item id="{aid}" href="Images/{html.escape(a.zip_name)}" media-type="{a.media_type}"/>')
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
            if name in _DC_EXTRA_OK and text and name != "source":
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
            '  <spine toc="ncx" page-progression-direction="rtl">\n' + "\n".join(spine) + "\n  </spine>\n</package>")

        container = (
            '<?xml version="1.0" encoding="UTF-8"?>\n'
            '<container version="1.0" xmlns="urn:oasis:names:tc:opendocument:xmlns:container">\n'
            f'  <rootfiles>\n    <rootfile full-path="{P("content.opf")}" media-type="application/oebps-package+xml"/>\n'
            '  </rootfiles>\n</container>')

        with zipfile.ZipFile(output_path, "w") as zf:
            mi = zipfile.ZipInfo("mimetype")
            mi.compress_type = zipfile.ZIP_STORED
            zf.writestr(mi, b"application/epub+zip")
            deflate = zipfile.ZIP_DEFLATED
            zf.writestr("META-INF/container.xml", container.encode("utf-8"), compress_type=deflate)
            zf.writestr(P("content.opf"), opf.encode("utf-8"), compress_type=deflate)
            zf.writestr(P("toc.ncx"), ncx.encode("utf-8"), compress_type=deflate)
            zf.writestr(P("nav.xhtml"), nav_xhtml.encode("utf-8"), compress_type=deflate)
            zf.writestr(P("Styles/pdfsync.css"), PDFSYNC_CSS.encode("utf-8"), compress_type=deflate)
            for r in resources:
                zf.writestr(r.path, r.data, compress_type=deflate)
            for name, data in asset_files.items():
                zf.writestr(P(f"Images/{name}"), data, compress_type=zipfile.ZIP_STORED)
            for pg in pages:
                zf.writestr(P(f"Text/{page_filename(pg.number)}"), _serialize(pg.root), compress_type=deflate)

        logger.info("تم إنشاء %s (%d صفحة) بحجم %s بايت", output_path.name, len(pages),
                    f"{output_path.stat().st_size:,}")
        return output_path
