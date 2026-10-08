# pdfsync v2 — مزامنة PDF مع EPUB (صفحة N في PDF = page-000N.xhtml)

## التركيب
استبدل مجلد `pdfsync/` القديم بالمجلد الجديد كاملًا (كل الملفات). لا يحتاج الآن إلى `epub_builder` ولا `validator`
إلا عند استخدام `--url`. المتطلبات: `pip install pymupdf lxml` (لا Tesseract ولا OCR).

## الاستخدام
    python main.py sync --pdf book.pdf --epub book.epub -o out.epub
    python main.py sync --pdf book.pdf --epub book.epub --pages 5-120
    python main.py sync --pdf book.pdf --epub book.epub --images off        # بدون نقل صور
    python main.py sync --pdf book.pdf --epub book.epub --no-fallback-image # لا صورة بديلة لصفحة نصها ناقص في EPUB

## ما الذي تغيّر
1. إزالة OCR كليًا. الصفحات المصورة (غلاف، لوحات) تُرسم كصورة من PDF نفسه وتوضع في ملف صفحتها.
2. محاذاة شاملة (مرتكزات + أطول سلسلة متزايدة) بدل المطابقة الجشعة التي كانت تنهار وتفسد ما بعدها.
3. الحواشي تُفصل عن المتن وتوضع أسفل صفحتها (في EPUB الأصلي تتجمع آخر الفصل).
4. اسم الملف = رقم صفحة PDF دائمًا. نص EPUB يُحفظ حرفيًا (يُتحقق منه آليًا).
5. صفحة PDF لا نص لها في EPUB (ناقصة من المصدر) تُدرج كصورة وتُذكر في التقرير بدل خلط الترتيب.
6. إصلاح: أرقام معكوسة، ألف مضاعفة في «الله»، صفحات PDF المدوّرة، ترويسة الكتاب.

## التقرير
`<output>.sync-report.json` لكل صفحة: الحالة، التشابه، بداية نص PDF مقابل بداية نص EPUB، وقائمة `pages_needing_review`.
