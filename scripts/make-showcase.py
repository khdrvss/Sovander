"""Make the documents the README's screenshots show: docs/showcase/{report.docx,budget.xlsx,talk.pptx}.

Built through LibreOffice's own API, so they are real Office files of this project's own (invented)
content, and anyone can make them again:   python3 scripts/make-showcase.py
"""

from __future__ import annotations

import sys
import tempfile
from pathlib import Path

REPO = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(REPO))

from sovander.engine.process import Engine  # noqa: E402

OUT = REPO / "docs" / "showcase"
BLUE, DARK, LIGHT, AMBER, GREEN, RED = 0x1A5FB4, 0x241F31, 0xDEECFB, 0xF6D32D, 0x2EC27E, 0xE01B24
WHITE, GREY = 0xFFFFFF, 0x77767B


def prop(name, value):
    from com.sun.star.beans import PropertyValue

    p = PropertyValue()
    p.Name, p.Value = name, value
    return p


def new(engine: Engine, kind: str):
    hidden = (prop("Hidden", True),)
    return engine.desktop.loadComponentFromURL(f"private:factory/{kind}", "_blank", 0, hidden)


def save(doc, name: str, filter_name: str) -> None:
    doc.storeToURL((OUT / name).as_uri(), (prop("FilterName", filter_name),))
    doc.close(True)
    print(OUT / name)


def report(engine: Engine) -> None:
    doc = new(engine, "swriter")
    text, cursor = doc.Text, doc.Text.createTextCursor()

    def para(words: str, style: str = "Text body", bullet: bool = False) -> None:
        cursor.ParaStyleName = style
        cursor.NumberingStyleName = "List 1" if bullet else ""
        text.insertString(cursor, words, False)
        text.insertControlCharacter(cursor, 0, False)  # PARAGRAPH_BREAK

    para("Riverside Library", "Title")
    para("Annual report 2026", "Subtitle")
    para("The year in short", "Heading 1")
    para("Riverside Library welcomed more readers than in any year before. The new reading room opened in "
         "March, the mobile library reached four more villages, and the digital collection doubled.")
    for line in ("31,400 visits, up 18% on last year", "2,150 new library cards",
                 "48 evening talks and workshops", "Reading room open seven days a week"):
        para(line, bullet=True)
    para("Visits by quarter", "Heading 1")
    rows = [("Quarter", "Visits", "Loans", "Events"), ("Q1", "6,820", "9,410", "9"),
            ("Q2", "7,540", "10,230", "12"), ("Q3", "8,110", "11,050", "11"), ("Q4", "8,930", "12,480", "16")]
    table = doc.createInstance("com.sun.star.text.TextTable")
    table.initialize(len(rows), len(rows[0]))
    text.insertTextContent(cursor, table, False)
    for r, row in enumerate(rows):
        for c, value in enumerate(row):
            cell = table.getCellByPosition(c, r)
            cell.setString(value)
            if r == 0:
                cell.BackColor = BLUE
                words = cell.createTextCursor()
                words.gotoEnd(True)
                words.CharColor, words.CharWeight = WHITE, 150
            elif r % 2 == 0:
                cell.BackColor = LIGHT
    para("")
    para("What comes next", "Heading 1")
    para("In 2027 the library will open a children's corner, lend laptops for a week at a time, and keep the "
         "reading room open until nine in the evening.")
    save(doc, "report.docx", "MS Word 2007 XML")


def budget(engine: Engine) -> None:
    doc = new(engine, "scalc")
    sheet = doc.Sheets.getByIndex(0)
    sheet.Name = "Budget 2027"
    rows = [("Item", "2026", "2027", "Change"),
            ("Books and journals", 18400, 19800, None), ("Digital collection", 6200, 9100, None),
            ("Events and workshops", 4300, 5200, None), ("Mobile library", 7900, 8400, None),
            ("Children's corner", 0, 6500, None), ("Building and utilities", 21500, 22100, None)]
    money = doc.NumberFormats.queryKey("#,##0", doc.CharLocale, False)
    if money < 0:
        money = doc.NumberFormats.addNew("#,##0", doc.CharLocale)
    percent = doc.NumberFormats.queryKey("0.0%", doc.CharLocale, False)
    if percent < 0:
        percent = doc.NumberFormats.addNew("0.0%", doc.CharLocale)
    for r, row in enumerate(rows):
        for c, value in enumerate(row):
            cell = sheet.getCellByPosition(c, r)
            if r == 0:
                cell.setString(value)
                cell.CellBackColor, cell.CharColor, cell.CharWeight = BLUE, WHITE, 150
            elif c == 0:
                cell.setString(value)
            elif c == 3:
                cell.setFormula(f"=IF(B{r + 1}=0;0;C{r + 1}/B{r + 1}-1)")
                cell.NumberFormat = percent
            else:
                cell.setValue(value)
                cell.NumberFormat = money
    last = len(rows) + 1
    for c, formula in enumerate(("Total", f"=SUM(B2:B{last - 1})", f"=SUM(C2:C{last - 1})",
                                 f"=C{last}/B{last}-1")):
        cell = sheet.getCellByPosition(c, last - 1)
        cell.setFormula(formula) if c else cell.setString(formula)
        cell.CellBackColor, cell.CharWeight = LIGHT, 150
        if c:
            cell.NumberFormat = percent if c == 3 else money
    sheet.getCellByPosition(3, 5).CellBackColor = AMBER  # the new children's corner
    for c, width in enumerate((6200, 2600, 2600, 2400)):
        sheet.Columns.getByIndex(c).Width = width
    doc.Sheets.insertNewByName("Notes", 1)
    notes = doc.Sheets.getByName("Notes")
    notes.getCellByPosition(0, 0).setString("Figures in euros. 2027 is the board's proposal of 12 October.")
    notes.Columns.getByIndex(0).Width = 16000
    save(doc, "budget.xlsx", "Calc MS Excel 2007 XML")


def talk(engine: Engine) -> None:
    doc = new(engine, "simpress")
    pages = doc.DrawPages
    first = pages.getByIndex(0)
    width, height = first.Width, first.Height

    def box(page, x, y, w, h, fill=None, words="", size=0, colour=DARK, bold=False):
        from com.sun.star.awt import Point, Size

        kind = "com.sun.star.drawing.RectangleShape" if fill is not None else "com.sun.star.drawing.TextShape"
        shape = doc.createInstance(kind)
        page.add(shape)
        shape.Position, shape.Size = Point(x, y), Size(w, h)
        if fill is not None:
            shape.FillColor, shape.LineStyle = fill, 0  # LineStyle NONE
        if words:
            shape.setString(words)
            cursor = shape.createTextCursor()
            cursor.gotoEnd(True)
            cursor.CharHeight, cursor.CharColor, cursor.CharWeight = size, colour, 150 if bold else 100
        return shape

    for page in (first,):
        while page.Count:
            page.remove(page.getByIndex(0))
    box(first, 0, 0, width, height, fill=BLUE)
    box(first, 0, height - 900, width, 900, fill=AMBER)
    box(first, 1800, 4200, width - 3600, 2600, words="A library for everyone", size=44, colour=WHITE,
        bold=True)
    box(first, 1800, 7300, width - 3600, 1600, words="Plans for 2027 · Riverside Library", size=22,
        colour=0xC0D7F5)

    second = pages.insertNewByIndex(0)
    while second.Count:
        second.remove(second.getByIndex(0))
    box(second, 0, 0, width, 2600, fill=BLUE)
    box(second, 1500, 700, width - 3000, 1500, words="Three things we will do", size=32, colour=WHITE,
        bold=True)
    items = [("Children's corner", "Books, games and a reading hour every Saturday", GREEN),
             ("Laptops to borrow", "For a week at a time, with a library card", AMBER),
             ("Open until nine", "The reading room, every evening", RED)]
    for k, (title, line, colour) in enumerate(items):
        y = 3800 + k * 3600
        box(second, 1500, y, 600, 2400, fill=colour)
        box(second, 2600, y, width - 4500, 1200, words=title, size=26, bold=True)
        box(second, 2600, y + 1200, width - 4500, 1200, words=line, size=18, colour=GREY)
    save(doc, "talk.pptx", "Impress MS PowerPoint 2007 XML")


CHAPTERS = [
    ("The reading room", [
        "The reading room opens at eight, before the town is quite awake. Morning light comes in low "
        "through the river windows and lies across the long oak tables in bright bands.",
        "Mrs Halloran is always first. She takes the seat by the radiator, unfolds yesterday's paper and "
        "reads it slowly, as if it were a letter that might still change its mind.",
        "By nine the students arrive with their laptops and their coffee, and the room fills with the "
        "small sounds of people thinking: pages turning, a pencil tapping, someone sighing at a sum.",
    ]),
    ("The mobile library", [
        "On Tuesdays the blue van leaves the yard at seven and follows the river road north. Four new "
        "villages joined the round this year; in one of them the stop is a bus shelter beside a pond.",
        "Children wait on the bench with their cards held up like tickets. The driver knows them by the "
        "books they ask for: the dinosaur boy, the girl who reads only about horses, the twins who argue.",
    ]),
]
BOOK_CSS = """body { font-family: serif; line-height: 1.6; margin: 0 6%; }
h1 { font-variant: small-caps; letter-spacing: .04em; margin-top: 2em; }
p { text-indent: 1.5em; margin: 0; } h1 + p { text-indent: 0; }
h1 + p::first-letter { font-size: 2.6em; float: left; line-height: 1; margin: .05em .08em 0 0; }"""


def book() -> None:
    """An EPUB 3 is a zip of XHTML: no engine needed."""
    import zipfile

    def page(title: str, body: str) -> str:
        return ('<?xml version="1.0" encoding="UTF-8"?>\n<html xmlns="http://www.w3.org/1999/xhtml" '
                'xmlns:epub="http://www.idpf.org/2007/ops" xml:lang="en"><head><meta charset="utf-8"/>'
                f'<title>{title}</title><link rel="stylesheet" href="book.css"/></head>'
                f'<body>{body}</body></html>')

    items, spine, nav = [], [], []
    files = {"book.css": BOOK_CSS}
    for n, (title, paragraphs) in enumerate(CHAPTERS, 1):
        name = f"chapter{n}.xhtml"
        files[name] = page(title, f"<h1>{title}</h1>" + "".join(f"<p>{p}</p>" for p in paragraphs))
        items.append(f'<item id="c{n}" href="{name}" media-type="application/xhtml+xml"/>')
        spine.append(f'<itemref idref="c{n}"/>')
        nav.append(f'<li><a href="{name}">{title}</a></li>')
    toc = f'<nav epub:type="toc"><h1>Contents</h1><ol>{"".join(nav)}</ol></nav>'
    files["nav.xhtml"] = page("Contents", toc)
    files["content.opf"] = (
        '<?xml version="1.0" encoding="UTF-8"?>\n<package xmlns="http://www.idpf.org/2007/opf" version="3.0" '
        'unique-identifier="id"><metadata xmlns:dc="http://purl.org/dc/elements/1.1/">'
        '<dc:identifier id="id">urn:uuid:5d3f6a52-7c1e-4b8e-9a51-6e0a2c4b7d19</dc:identifier>'
        '<dc:title>The Riverside Almanac</dc:title><dc:creator>Riverside Library</dc:creator>'
        '<dc:language>en</dc:language><meta property="dcterms:modified">2026-10-18T00:00:00Z</meta>'
        '</metadata><manifest><item id="nav" href="nav.xhtml" media-type="application/xhtml+xml" '
        'properties="nav"/><item id="css" href="book.css" media-type="text/css"/>' + "".join(items)
        + '</manifest><spine>' + "".join(spine) + '</spine></package>')
    container = ('<?xml version="1.0" encoding="UTF-8"?>\n<container version="1.0" '
                 'xmlns="urn:oasis:names:tc:opendocument:xmlns:container"><rootfiles>'
                 '<rootfile full-path="OEBPS/content.opf" media-type="application/oebps-package+xml"/>'
                 '</rootfiles></container>')
    with zipfile.ZipFile(OUT / "almanac.epub", "w") as zf:
        zf.writestr("mimetype", "application/epub+zip", compress_type=zipfile.ZIP_STORED)  # first, unpacked
        zf.writestr("META-INF/container.xml", container, compress_type=zipfile.ZIP_DEFLATED)
        for name, text in files.items():
            zf.writestr(f"OEBPS/{name}", text, compress_type=zipfile.ZIP_DEFLATED)
    print(OUT / "almanac.epub")


def main() -> None:
    OUT.mkdir(parents=True, exist_ok=True)
    book()
    with tempfile.TemporaryDirectory(dir=REPO / "build") as home:
        engine = Engine(connect="pipe,name=sovander-showcase", profile=Path(home) / "profile")
        engine.start()
        try:
            report(engine)
            budget(engine)
            talk(engine)
        finally:
            engine.stop()


if __name__ == "__main__":
    main()
