"""Starting points for "make me a new document" when there's no template to follow."""
from pathlib import Path
from typing import Any

from docx import Document
from docx.shared import Pt, RGBColor
from openpyxl import Workbook
from openpyxl.styles import Alignment, Border, Font, PatternFill, Side
from pptx import Presentation
from pptx.oxml.ns import qn
from pptx.util import Emu, Inches

from app.documents.xlsx_engine import STYLE_SHEET

INK = "1F2933"


def _clear_author(properties: Any) -> None:
    # Otherwise new files say they were written by the library's author.
    properties.author = ""
    properties.last_modified_by = ""
    properties.comments = ""


def _default_docx(path: Path) -> None:
    doc = Document()
    normal = doc.styles["Normal"]
    normal.font.name = "Calibri"
    normal.font.size = Pt(11)
    for name, size in (("Heading 1", 16), ("Heading 2", 13), ("Heading 3", 11)):
        style = doc.styles[name]
        style.font.name = "Calibri"
        style.font.size = Pt(size)
        style.font.color.rgb = RGBColor.from_string(INK)
    _clear_author(doc.core_properties)
    doc.save(str(path))


def _default_pptx(path: Path) -> None:
    prs = Presentation()
    factor = 13.333 / 10
    prs.slide_width, prs.slide_height = Inches(13.333), Inches(7.5)
    for holder in [prs.slide_master, *prs.slide_layouts]:
        for shape in holder.shapes:
            if shape._element.find(f".//{qn('a:xfrm')}") is not None:
                shape.left = Emu(int(shape.left * factor))
                shape.width = Emu(int(shape.width * factor))
    _clear_author(prs.core_properties)
    prs.save(str(path))


def _default_xlsx(path: Path) -> None:
    wb = Workbook()
    ws = wb.active
    ws.title = "Data"
    for column in range(1, 13):
        ws.column_dimensions[ws.cell(row=1, column=column).column_letter].width = 18

    # write_table copies its styles from existing cells, and a blank workbook has
    # none. They live on a hidden sheet so sample text can't end up in the result.
    styles = wb.create_sheet(STYLE_SHEET)
    styles.sheet_state = "veryHidden"
    for column in range(1, 3):
        header = styles.cell(row=1, column=column, value="Header")
        header.font = Font(bold=True, color="FFFFFF")
        header.fill = PatternFill("solid", fgColor=INK)
        header.alignment = Alignment(vertical="center")
        body = styles.cell(row=2, column=column, value="Body")
        body.border = Border(bottom=Side(style="thin", color="D5DBE1"))
    wb.properties.creator = ""
    wb.save(path)


BUILDERS = {"docx": _default_docx, "pptx": _default_pptx, "xlsx": _default_xlsx}


def default_template(kind: str, folder: Path) -> Path:
    folder.mkdir(parents=True, exist_ok=True)
    path = folder / f"default.{kind}"
    if not path.exists():
        BUILDERS[kind](path)
    return path
