#!/usr/bin/env python3
"""
epub_reader.py — قارئ EPUB بسيط في ملف واحد، مع تكبير/تصغير ولوحة PDF للمقارنة.

التثبيت:
    pip install PyQt6 PyQt6-WebEngine pymupdf

التشغيل:
    python epub_reader.py                      # ثم Ctrl+O لفتح EPUB و Ctrl+P لفتح PDF
    python epub_reader.py book.epub
    python epub_reader.py book.epub book.pdf   # يعرضهما جنبًا إلى جنب

الفحص: عند فتح PDF تظهر صفحته بجانب صفحة EPUB. إن كان EPUB ناتج pdfsync
(page-0010.xhtml) فإن صفحة PDF تتبع رقم الملف تلقائيًا؛ وإلا تُستخدم رقم الصفحة في التسلسل
مع إمكانية ضبط "إزاحة" يدويًا.

الاختصارات:
    Ctrl +  /  Ctrl -  /  Ctrl 0     تكبير / تصغير / إعادة الحجم (للوحتين معًا)
    Ctrl + عجلة الفأرة                تكبير/تصغير اللوحة التي فوقها فقط
    ← أو PageDown                     الصفحة التالية (الكتاب عربي: السهم الأيسر = التالي)
    → أو PageUp                       الصفحة السابقة
    Home / End                        أول / آخر صفحة
    Ctrl+G                            انتقال لرقم صفحة      F2: إظهار/إخفاء PDF      F3: الفهرس
"""
from __future__ import annotations

import atexit
import os
import re
import shutil
import sys
import tempfile
import zipfile
from dataclasses import dataclass, field
from pathlib import Path
from urllib.parse import unquote, urldefrag
from xml.etree import ElementTree as ET

from PyQt6.QtCore import QEvent, QPointF, Qt, QUrl
from PyQt6.QtGui import QAction, QImage, QKeySequence, QPixmap
from PyQt6.QtWebEngineWidgets import QWebEngineView
from PyQt6.QtWidgets import (QApplication, QCheckBox, QDockWidget, QFileDialog, QInputDialog, QLabel,
                             QMainWindow, QMessageBox, QScrollArea, QSpinBox, QSplitter, QToolBar,
                             QTreeWidget, QTreeWidgetItem, QWidget)

try:
    import pymupdf as fitz  # type: ignore
except ImportError:
    try:
        import fitz  # type: ignore
    except ImportError:
        fitz = None

NS = {
    "c": "urn:oasis:names:tc:opendocument:xmlns:container",
    "o": "http://www.idpf.org/2007/opf",
    "x": "http://www.w3.org/1999/xhtml",
    "e": "http://www.idpf.org/2007/ops",
    "n": "http://www.daisy.org/z3986/2005/ncx/",
}
ZOOM_MIN, ZOOM_MAX, ZOOM_STEP = 0.4, 5.0, 0.1


# ---------------------------------------------------------------- نموذج EPUB

@dataclass
class TocEntry:
    title: str
    path: str | None
    frag: str | None
    children: list["TocEntry"] = field(default_factory=list)


@dataclass
class Book:
    title: str
    root: Path                       # مجلد الاستخراج المؤقت
    spine: list[Path]                # ملفات الصفحات بالترتيب
    toc: list[TocEntry]


def _resolve(base: Path, href: str) -> tuple[Path | None, str | None]:
    if not href:
        return None, None
    path, frag = urldefrag(href)
    if re.match(r"^[a-zA-Z][a-zA-Z0-9+.-]*:", path):
        return None, None
    target = base.parent / unquote(path) if path else base
    return Path(os.path.normpath(target)), (unquote(frag) or None)


def _text(el: ET.Element | None) -> str:
    return " ".join("".join(el.itertext()).split()) if el is not None else ""


def _parse_nav(nav_file: Path) -> list[TocEntry]:
    root = ET.parse(nav_file).getroot()
    for nav in root.iter(f"{{{NS['x']}}}nav"):
        if "toc" in (nav.get(f"{{{NS['e']}}}type") or "").split():
            ol = nav.find("x:ol", NS)
            return _nav_ol(ol, nav_file) if ol is not None else []
    return []


def _nav_ol(ol: ET.Element, nav_file: Path) -> list[TocEntry]:
    out = []
    for li in ol.findall("x:li", NS):
        a = li.find("x:a", NS)
        label = a if a is not None else li.find("x:span", NS)
        path = frag = None
        if a is not None and a.get("href"):
            path, frag = _resolve(nav_file, a.get("href"))
        sub = li.find("x:ol", NS)
        out.append(TocEntry(_text(label), path, frag, _nav_ol(sub, nav_file) if sub is not None else []))
    return out


def _parse_ncx(ncx_file: Path) -> list[TocEntry]:
    def rec(parent: ET.Element) -> list[TocEntry]:
        out = []
        for np_ in parent.findall("n:navPoint", NS):
            content = np_.find("n:content", NS)
            path, frag = _resolve(ncx_file, content.get("src")) if content is not None else (None, None)
            out.append(TocEntry(_text(np_.find("n:navLabel/n:text", NS)), path, frag, rec(np_)))
        return out
    nm = ET.parse(ncx_file).getroot().find("n:navMap", NS)
    return rec(nm) if nm is not None else []


def load_epub(epub_path: Path) -> Book:
    tmp = Path(tempfile.mkdtemp(prefix="epubreader_"))
    atexit.register(shutil.rmtree, tmp, True)
    with zipfile.ZipFile(epub_path) as zf:
        zf.extractall(tmp)
    container = ET.parse(tmp / "META-INF" / "container.xml").getroot()
    opf_path = tmp / container.find(".//c:rootfile", NS).get("full-path")
    opf = ET.parse(opf_path).getroot()
    manifest = {it.get("id"): it for it in opf.findall(".//o:manifest/o:item", NS)}
    spine_el = opf.find(".//o:spine", NS)
    spine: list[Path] = []
    for ref in spine_el.findall("o:itemref", NS):
        it = manifest.get(ref.get("idref"))
        if it is not None:
            spine.append(Path(os.path.normpath(opf_path.parent / unquote(it.get("href")))))
    title = _text(opf.find(".//{http://purl.org/dc/elements/1.1/}title")) or epub_path.stem

    toc: list[TocEntry] = []
    nav_item = next((it for it in manifest.values() if "nav" in (it.get("properties") or "").split()), None)
    if nav_item is not None:
        try:
            toc = _parse_nav(opf_path.parent / unquote(nav_item.get("href")))
        except Exception:
            toc = []
    if not toc and spine_el is not None and spine_el.get("toc") in manifest:
        try:
            toc = _parse_ncx(opf_path.parent / unquote(manifest[spine_el.get("toc")].get("href")))
        except Exception:
            toc = []
    return Book(title, tmp, spine, toc)


# ---------------------------------------------------------------- لوحة PDF

class PdfPane(QScrollArea):
    """عرض صفحة PDF واحدة مع تكبير/تصغير."""

    def __init__(self, on_zoom):
        super().__init__()
        self.doc = None
        self.page_no = 1
        self.zoom = 1.0
        self.on_zoom = on_zoom
        self.label = QLabel(alignment=Qt.AlignmentFlag.AlignHCenter | Qt.AlignmentFlag.AlignTop)
        self.label.setText("لم يُفتح PDF بعد (Ctrl+P)")
        self.setWidget(self.label)
        self.setWidgetResizable(False)          # يسمح بالتمرير الأفقي عند التكبير
        self.setAlignment(Qt.AlignmentFlag.AlignHCenter | Qt.AlignmentFlag.AlignTop)
        self.setLayoutDirection(Qt.LayoutDirection.LeftToRight)
        self.label.adjustSize()
        self.viewport().installEventFilter(self)

    def open(self, path: Path) -> int:
        if self.doc is not None:
            self.doc.close()
        self.doc = fitz.open(str(path))
        self.show_page(1)
        return len(self.doc)

    def set_zoom(self, z: float) -> None:
        self.zoom = max(ZOOM_MIN, min(ZOOM_MAX, z))
        self.show_page(self.page_no)

    def show_page(self, n: int) -> None:
        if self.doc is None:
            return
        n = max(1, min(len(self.doc), n))
        self.page_no = n
        ratio = self.devicePixelRatioF()
        # 1.0 = حجم الصفحة الأصلي (72 نقطة/بوصة)؛ كبّر بالأزرار أو Ctrl+عجلة
        scale = 1.0 * self.zoom * ratio
        pix = self.doc[n - 1].get_pixmap(matrix=fitz.Matrix(scale, scale), alpha=False)
        img = QImage(pix.samples, pix.width, pix.height, pix.stride, QImage.Format.Format_RGB888).copy()
        pm = QPixmap.fromImage(img)
        pm.setDevicePixelRatio(ratio)
        self.label.setPixmap(pm)
        self.label.adjustSize()
        self.verticalScrollBar().setValue(0)

    def eventFilter(self, obj, ev):  # Ctrl + عجلة => تكبير هذه اللوحة فقط
        if ev.type() == QEvent.Type.Wheel and ev.modifiers() & Qt.KeyboardModifier.ControlModifier:
            self.on_zoom(self, ev.angleDelta().y() > 0)
            return True
        return super().eventFilter(obj, ev)


class EpubView(QWebEngineView):
    def __init__(self, on_zoom):
        super().__init__()
        self.on_zoom = on_zoom
        self.zoom = 1.0
        self.loadFinished.connect(lambda _ok: self.setZoomFactor(self.zoom))

    def set_zoom(self, z: float) -> None:
        self.zoom = max(ZOOM_MIN, min(ZOOM_MAX, z))
        self.setZoomFactor(self.zoom)

    def wheelEvent(self, ev):
        if ev.modifiers() & Qt.KeyboardModifier.ControlModifier:
            self.on_zoom(self, ev.angleDelta().y() > 0)
            ev.accept()
        else:
            super().wheelEvent(ev)


# ---------------------------------------------------------------- النافذة

class Reader(QMainWindow):
    def __init__(self):
        super().__init__()
        self.setLayoutDirection(Qt.LayoutDirection.RightToLeft)
        self.setWindowTitle("قارئ EPUB")
        self.resize(1400, 850)
        self.book: Book | None = None
        self.index = 0

        self.view = EpubView(self.zoom_wheel)
        self.pdf = PdfPane(self.zoom_wheel) if fitz else None
        self.split = QSplitter(Qt.Orientation.Horizontal)
        self.split.setLayoutDirection(Qt.LayoutDirection.LeftToRight)
        self.split.addWidget(self.view)
        if self.pdf:
            self.split.addWidget(self.pdf)
            self.pdf.hide()
        self.setCentralWidget(self.split)

        self.tree = QTreeWidget()
        self.tree.setHeaderHidden(True)
        self.tree.itemClicked.connect(self._toc_clicked)
        self.dock = QDockWidget("الفهرس", self)
        self.dock.setWidget(self.tree)
        self.addDockWidget(Qt.DockWidgetArea.RightDockWidgetArea, self.dock)

        self._build_toolbar()
        self.statusBar().showMessage("افتح ملف EPUB (Ctrl+O)")

    # ---- واجهة
    def _act(self, text, slot, shortcut=None, tip=None):
        a = QAction(text, self)
        a.triggered.connect(lambda *_: slot())
        if shortcut:
            a.setShortcut(QKeySequence(shortcut))
        if tip:
            a.setToolTip(tip)
        return a

    def _build_toolbar(self):
        tb = QToolBar("الأدوات")
        tb.setMovable(False)
        self.addToolBar(tb)
        tb.addAction(self._act("📖 EPUB", self.open_epub_dialog, "Ctrl+O", "فتح EPUB"))
        tb.addAction(self._act("📄 PDF", self.open_pdf_dialog, "Ctrl+P", "فتح PDF للمقارنة"))
        tb.addSeparator()
        tb.addAction(self._act("◀ السابق", self.prev_page, tip="الصفحة السابقة"))
        self.spin = QSpinBox()
        self.spin.setMinimum(1)
        self.spin.setKeyboardTracking(False)
        self.spin.valueChanged.connect(lambda v: self.goto(v - 1))
        tb.addWidget(self.spin)
        self.total_lbl = QLabel(" / 0 ")
        tb.addWidget(self.total_lbl)
        tb.addAction(self._act("التالي ▶", self.next_page, tip="الصفحة التالية"))
        tb.addSeparator()
        tb.addAction(self._act("➖", lambda: self.zoom_all(-ZOOM_STEP), "Ctrl+-", "تصغير"))
        self.zoom_lbl = QLabel(" 100% ")
        tb.addWidget(self.zoom_lbl)
        tb.addAction(self._act("➕", lambda: self.zoom_all(ZOOM_STEP), "Ctrl+=", "تكبير"))
        tb.addAction(self._act("100%", lambda: self.zoom_all(None), "Ctrl+0", "إعادة الحجم"))
        tb.addSeparator()
        self.sync_chk = QCheckBox("مزامنة PDF")
        self.sync_chk.setChecked(True)
        self.sync_chk.toggled.connect(lambda _: self.sync_pdf())
        tb.addWidget(self.sync_chk)
        tb.addWidget(QLabel(" إزاحة: "))
        self.offset = QSpinBox()
        self.offset.setRange(-2000, 2000)
        self.offset.setToolTip("صفحة PDF = رقم صفحة EPUB + الإزاحة")
        self.offset.valueChanged.connect(lambda _: self.sync_pdf())
        tb.addWidget(self.offset)
        self.pdf_lbl = QLabel("")
        tb.addWidget(self.pdf_lbl)
        # اختصارات بلا أزرار
        for text, slot, key in (("go", self.goto_dialog, "Ctrl+G"), ("pdf", self.toggle_pdf, "F2"),
                                ("toc", lambda: self.dock.setVisible(not self.dock.isVisible()), "F3"),
                                ("first", lambda: self.goto(0), "Home"),
                                ("last", lambda: self.goto(10**9), "End"),
                                ("n1", self.next_page, "Left"), ("n2", self.next_page, "PageDown"),
                                ("p1", self.prev_page, "Right"), ("p2", self.prev_page, "PageUp"),
                                ("z+", lambda: self.zoom_all(ZOOM_STEP), "Ctrl++")):
            a = self._act(text, slot, key)
            self.addAction(a)

    # ---- فتح الملفات
    def open_epub_dialog(self):
        p, _ = QFileDialog.getOpenFileName(self, "اختر EPUB", "", "EPUB (*.epub)")
        if p:
            self.open_epub(Path(p))

    def open_pdf_dialog(self):
        if not fitz:
            QMessageBox.warning(self, "PDF", "ثبّت المكتبة أولًا:  pip install pymupdf")
            return
        p, _ = QFileDialog.getOpenFileName(self, "اختر PDF", "", "PDF (*.pdf)")
        if p:
            self.open_pdf(Path(p))

    def open_epub(self, path: Path):
        try:
            self.book = load_epub(path)
        except Exception as e:
            QMessageBox.critical(self, "خطأ", f"تعذّر فتح EPUB:\n{e}")
            return
        self.setWindowTitle(f"قارئ EPUB — {self.book.title}")
        self.spin.blockSignals(True)
        self.spin.setMaximum(max(1, len(self.book.spine)))
        self.spin.blockSignals(False)
        self.total_lbl.setText(f" / {len(self.book.spine)} ")
        self.tree.clear()
        self._fill_tree(self.book.toc, self.tree.invisibleRootItem())
        self.tree.expandToDepth(0)
        self.goto(0)

    def open_pdf(self, path: Path):
        try:
            n = self.pdf.open(path)
        except Exception as e:
            QMessageBox.critical(self, "خطأ", f"تعذّر فتح PDF:\n{e}")
            return
        self.pdf.show()
        self.split.setSizes([self.width() // 2, self.width() // 2])
        self.statusBar().showMessage(f"PDF: {path.name} ({n} صفحة)")
        self.sync_pdf()

    def toggle_pdf(self):
        if self.pdf:
            self.pdf.setVisible(not self.pdf.isVisible())

    # ---- الفهرس
    def _fill_tree(self, entries: list[TocEntry], parent):
        for e in entries:
            item = QTreeWidgetItem(parent, [e.title or "—"])
            item.setData(0, Qt.ItemDataRole.UserRole, (str(e.path) if e.path else None, e.frag))
            self._fill_tree(e.children, item)

    def _toc_clicked(self, item):
        path, frag = item.data(0, Qt.ItemDataRole.UserRole) or (None, None)
        if not path or not self.book:
            return
        p = Path(path)
        if p in self.book.spine:
            self.goto(self.book.spine.index(p), frag)

    # ---- التنقل
    def goto(self, i: int, frag: str | None = None):
        if not self.book or not self.book.spine:
            return
        self.index = max(0, min(len(self.book.spine) - 1, i))
        url = QUrl.fromLocalFile(str(self.book.spine[self.index]))
        if frag:
            url.setFragment(frag)
        self.view.load(url)
        self.spin.blockSignals(True)
        self.spin.setValue(self.index + 1)
        self.spin.blockSignals(False)
        self.sync_pdf()

    def next_page(self):
        self.goto(self.index + 1)

    def prev_page(self):
        self.goto(self.index - 1)

    def goto_dialog(self):
        if not self.book:
            return
        n, ok = QInputDialog.getInt(self, "انتقال", "رقم الصفحة (ترتيب الملف):", self.index + 1, 1, len(self.book.spine))
        if ok:
            self.goto(n - 1)

    def current_page_number(self) -> int:
        """رقم الصفحة: من اسم الملف page-0010.xhtml إن وُجد، وإلا ترتيبه في الكتاب."""
        name = self.book.spine[self.index].stem
        m = re.search(r"(\d+)$", name) if name.lower().startswith("page") else None
        return int(m.group(1)) if m else self.index + 1

    def sync_pdf(self):
        if not (self.pdf and self.pdf.doc and self.book and self.pdf.isVisible() and self.sync_chk.isChecked()):
            return
        target = self.current_page_number() + self.offset.value()
        total = len(self.pdf.doc)
        self.pdf.show_page(target)
        ok = 1 <= target <= total
        self.pdf_lbl.setText(f"  PDF: {self.pdf.page_no}/{total}" + ("" if ok else "  ⚠ خارج النطاق"))
        self.statusBar().showMessage(f"EPUB: {self.book.spine[self.index].name}   |   PDF: صفحة {self.pdf.page_no} من {total}")

    # ---- التكبير
    def _update_zoom_label(self):
        self.zoom_lbl.setText(f" {round(self.view.zoom * 100)}% ")

    def zoom_all(self, delta: float | None):
        z = 1.0 if delta is None else self.view.zoom + delta
        self.view.set_zoom(z)
        if self.pdf:
            self.pdf.set_zoom(z)
        self._update_zoom_label()

    def zoom_wheel(self, pane, zoom_in: bool):
        d = ZOOM_STEP if zoom_in else -ZOOM_STEP
        pane.set_zoom(pane.zoom + d)
        self._update_zoom_label()


def main() -> int:
    os.environ.setdefault("QTWEBENGINE_CHROMIUM_FLAGS", "--disable-gpu" if sys.platform.startswith("linux") else "")
    app = QApplication(sys.argv)
    w = Reader()
    w.show()
    args = [Path(a) for a in sys.argv[1:]]
    for a in args:
        if a.suffix.lower() == ".epub" and a.exists():
            w.open_epub(a)
    for a in args:
        if a.suffix.lower() == ".pdf" and a.exists() and fitz:
            w.open_pdf(a)
    return app.exec()


if __name__ == "__main__":
    sys.exit(main())
