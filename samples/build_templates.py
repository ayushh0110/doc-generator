"""Builds the sample templates in samples/templates.

They belong to a made-up freight company and are deliberately fussy: anchored
logos, custom margins, merged cells, charts, formulas. If a template survives an
edit here, the preservation logic is doing its job.

    python samples/build_templates.py
"""
import copy
from io import BytesIO
from pathlib import Path

from docx import Document
from docx.enum.table import WD_TABLE_ALIGNMENT
from docx.enum.text import WD_ALIGN_PARAGRAPH
from docx.oxml import OxmlElement, parse_xml
from docx.oxml.ns import nsdecls, qn
from docx.shared import Cm, Mm, Pt, RGBColor
from lxml import etree
from openpyxl import Workbook
from openpyxl.chart import BarChart, Reference
from openpyxl.drawing.image import Image as XlImage
from openpyxl.formatting.rule import CellIsRule
from openpyxl.styles import Alignment, Border, Font, PatternFill, Side
from openpyxl.workbook.defined_name import DefinedName
from openpyxl.worksheet.datavalidation import DataValidation
from PIL import Image, ImageDraw
from pptx import Presentation
from pptx.chart.data import CategoryChartData
from pptx.dml.color import RGBColor as PptxRGB
from pptx.enum.chart import XL_CHART_TYPE, XL_LEGEND_POSITION
from pptx.oxml.ns import qn as pqn
from pptx.util import Emu, Inches
from pptx.util import Pt as PptxPt

OUT = Path(__file__).parent / "templates"
NAVY = "12344D"
RUST = "C2571A"
SLATE = "5B6B79"
PALE = "EEF2F5"


def logo_png(width: int = 480, height: int = 120) -> BytesIO:
    image = Image.new("RGBA", (width, height), (255, 255, 255, 0))
    draw = ImageDraw.Draw(image)
    draw.rectangle((0, 0, height, height), fill=f"#{NAVY}")
    draw.polygon([(18, height - 24), (height / 2, 22), (height - 18, height - 24)], fill=f"#{RUST}")
    for i in range(3):
        y = 34 + i * 26
        draw.rectangle((height + 22, y, width - 30 - i * 60, y + 12), fill=f"#{NAVY}")
    buffer = BytesIO()
    image.save(buffer, format="PNG")
    buffer.seek(0)
    return buffer


def photo_png(width: int = 900, height: int = 420) -> BytesIO:
    image = Image.new("RGB", (width, height), f"#{PALE}")
    draw = ImageDraw.Draw(image)
    draw.rectangle((0, height - 120, width, height), fill=f"#{SLATE}")
    x = 40
    for shade in (NAVY, RUST, SLATE, NAVY, RUST, NAVY):
        draw.rectangle((x, height - 210, x + 120, height - 120), fill=f"#{shade}")
        x += 138
    buffer = BytesIO()
    image.save(buffer, format="PNG")
    buffer.seek(0)
    return buffer


def _style(doc, name, font, size, color=None, bold=None, before=None, after=None):
    style = doc.styles[name]
    style.font.name = font
    rfonts = style.element.get_or_add_rPr().get_or_add_rFonts()
    for attr in ("w:ascii", "w:hAnsi", "w:cs", "w:eastAsia"):
        rfonts.set(qn(attr), font)
    for attr in ("w:asciiTheme", "w:hAnsiTheme", "w:cstheme", "w:eastAsiaTheme"):
        rfonts.attrib.pop(qn(attr), None)
    style.font.size = Pt(size)
    if color:
        style.font.color.rgb = RGBColor.from_string(color)
    if bold is not None:
        style.font.bold = bold
    if before is not None:
        style.paragraph_format.space_before = Pt(before)
    if after is not None:
        style.paragraph_format.space_after = Pt(after)


def _anchor_picture(run) -> None:
    """Turn an inline picture into one pinned to the page corner, which is what real letterheads do."""
    inline = run._r.find(f".//{qn('wp:inline')}")
    cx, cy = inline.find(qn("wp:extent")).get("cx"), inline.find(qn("wp:extent")).get("cy")
    graphic = inline.find(qn("a:graphic"))
    doc_pr = inline.find(qn("wp:docPr"))
    anchor = parse_xml(
        f'<wp:anchor {nsdecls("wp", "a")} distT="0" distB="0" distL="0" distR="0" simplePos="0" '
        f'relativeHeight="251658240" behindDoc="0" locked="0" layoutInCell="1" allowOverlap="1">'
        f'<wp:simplePos x="0" y="0"/>'
        f'<wp:positionH relativeFrom="page"><wp:posOffset>{Mm(22)}</wp:posOffset></wp:positionH>'
        f'<wp:positionV relativeFrom="page"><wp:posOffset>{Mm(10)}</wp:posOffset></wp:positionV>'
        f'<wp:extent cx="{cx}" cy="{cy}"/><wp:wrapNone/>'
        f'<wp:docPr id="{doc_pr.get("id")}" name="Logo"/><wp:cNvGraphicFramePr/>'
        f"</wp:anchor>"
    )
    anchor.append(graphic)
    inline.getparent().replace(inline, anchor)


def _page_number_field(paragraph) -> None:
    for kind, text in (("begin", None), (None, " PAGE "), ("end", None)):
        run = paragraph.add_run()
        if kind:
            field = OxmlElement("w:fldChar")
            field.set(qn("w:fldCharType"), kind)
            run._r.append(field)
        else:
            instruction = OxmlElement("w:instrText")
            instruction.set("{http://www.w3.org/XML/1998/namespace}space", "preserve")
            instruction.text = text
            run._r.append(instruction)


def _shade(cell, fill: str) -> None:
    props = cell._tc.get_or_add_tcPr()
    shade = OxmlElement("w:shd")
    shade.set(qn("w:val"), "clear")
    shade.set(qn("w:color"), "auto")
    shade.set(qn("w:fill"), fill)
    props.append(shade)


def _table_borders(table, color: str) -> None:
    props = table._tbl.tblPr
    borders = OxmlElement("w:tblBorders")
    for edge, size, tint in (("top", 12, NAVY), ("bottom", 12, NAVY), ("insideH", 4, color)):
        node = OxmlElement(f"w:{edge}")
        node.set(qn("w:val"), "single")
        node.set(qn("w:sz"), str(size))
        node.set(qn("w:space"), "0")
        node.set(qn("w:color"), tint)
        borders.append(node)
    props.append(borders)


def build_docx(path: Path) -> None:
    doc = Document()
    section = doc.sections[0]
    section.page_width, section.page_height = Mm(210), Mm(297)
    section.left_margin, section.right_margin = Mm(22), Mm(22)
    section.top_margin, section.bottom_margin = Mm(32), Mm(24)
    section.header_distance, section.footer_distance = Mm(10), Mm(10)

    _style(doc, "Normal", "Georgia", 10.5, after=7)
    doc.styles["Normal"].paragraph_format.line_spacing = 1.2
    _style(doc, "Title", "Trebuchet MS", 26, NAVY, True, after=2)
    title_ppr = doc.styles["Title"].element.get_or_add_pPr()
    for border in title_ppr.findall(qn("w:pBdr")):
        title_ppr.remove(border)
    _style(doc, "Subtitle", "Trebuchet MS", 12, SLATE, False, after=18)
    doc.styles["Subtitle"].font.italic = False
    _style(doc, "Heading 1", "Trebuchet MS", 15, NAVY, True, before=18, after=6)
    _style(doc, "Heading 2", "Trebuchet MS", 11.5, RUST, True, before=12, after=4)
    _style(doc, "List Bullet", "Georgia", 10.5, after=3)
    _style(doc, "Caption", "Trebuchet MS", 8.5, SLATE, False, before=2, after=10)

    header = section.header.paragraphs[0]
    header.alignment = WD_ALIGN_PARAGRAPH.RIGHT
    run = header.add_run("MERIDIAN FREIGHT  |  Internal")
    run.font.size = Pt(8)
    run.font.name = "Trebuchet MS"
    run.font.color.rgb = RGBColor.from_string(SLATE)
    logo_run = header.add_run()
    logo_run.add_picture(logo_png(), width=Mm(38))
    _anchor_picture(logo_run)

    footer = section.footer.paragraphs[0]
    footer.alignment = WD_ALIGN_PARAGRAPH.CENTER
    run = footer.add_run("Meridian Freight Ltd, 14 Dock Road, Felixstowe  |  Page ")
    run.font.size = Pt(8)
    run.font.color.rgb = RGBColor.from_string(SLATE)
    _page_number_field(footer)

    doc.add_paragraph("Q2 Network Review", style="Title")
    doc.add_paragraph("Prepared for the operations board, July", style="Subtitle")

    doc.add_paragraph("Summary", style="Heading 1")
    doc.add_paragraph(
        "Volumes through the eastern corridor grew for the third quarter running, while on-time performance "
        "slipped at two depots after the April timetable change. The network ended the quarter with more "
        "capacity than it used, which leaves room to consolidate the Thursday trunk runs."
    )
    doc.add_paragraph("Where we stand", style="Heading 2")
    for item in (
        "Consignments up 6.2% on Q1, led by Felixstowe and Immingham.",
        "On-time delivery at 93.1%, down from 95.4%.",
        "Fuel cost per trunk mile flat after the April hedge rolled over.",
    ):
        doc.add_paragraph(item, style="List Bullet")

    doc.add_paragraph("Depot performance", style="Heading 1")
    doc.add_paragraph(
        "The table below compares the four depots on the measures the board tracks each quarter. "
        "Immingham's dip is almost entirely the two weeks of berth closures in May."
    )
    rows = [
        ("Depot", "Consignments", "On-time %", "Cost per drop"),
        ("Felixstowe", "48,210", "94.6", "£11.20"),
        ("Immingham", "31,905", "89.8", "£12.75"),
        ("Tilbury", "27,440", "93.9", "£11.95"),
        ("Grangemouth", "12,118", "95.2", "£14.10"),
        ("Total", "119,673", "93.1", "£12.14"),
    ]
    table = doc.add_table(rows=len(rows), cols=4)
    table.alignment = WD_TABLE_ALIGNMENT.CENTER
    widths = (Mm(58), Mm(36), Mm(36), Mm(36))
    _table_borders(table, "C9D2DA")
    for r, values in enumerate(rows):
        for c, value in enumerate(values):
            cell = table.cell(r, c)
            cell.width = widths[c]
            paragraph = cell.paragraphs[0]
            paragraph.paragraph_format.space_after = Pt(2)
            paragraph.paragraph_format.space_before = Pt(2)
            if c:
                paragraph.alignment = WD_ALIGN_PARAGRAPH.RIGHT
            run = paragraph.add_run(value)
            run.font.name = "Trebuchet MS"
            run.font.size = Pt(9)
            if r == 0:
                run.font.bold = True
                run.font.color.rgb = RGBColor(255, 255, 255)
                _shade(cell, NAVY)
            elif r == len(rows) - 1:
                run.font.bold = True
                _shade(cell, PALE)

    doc.add_paragraph()
    doc.add_paragraph("Figure 1. Felixstowe yard, trailers staged for the Thursday trunk.", style="Caption").insert_paragraph_before().add_run().add_picture(photo_png(), width=Cm(14))

    doc.add_paragraph("Recommendations", style="Heading 1")
    doc.add_paragraph(
        "Merge the Thursday Immingham and Tilbury trunks from September, and keep the spare tractor unit at "
        "Felixstowe through peak. We should revisit the timetable change at Immingham before the winter schedule goes out."
    )
    doc.core_properties.title = "Q2 Network Review"
    doc.core_properties.author = "Meridian Freight"
    doc.core_properties.last_modified_by = "Meridian Freight"
    doc.core_properties.comments = ""
    doc.save(str(path))


def _retheme(prs) -> None:
    master = prs.slide_master
    theme_part = next(r.target_part for r in master.part.rels.values() if r.reltype.endswith("/theme"))
    root = etree.fromstring(theme_part.blob)
    a = "{http://schemas.openxmlformats.org/drawingml/2006/main}"
    root.set("name", "Meridian")
    scheme = root.find(f".//{a}clrScheme")
    scheme.set("name", "Meridian")
    palette = {"dk2": NAVY, "lt2": PALE, "accent1": NAVY, "accent2": RUST, "accent3": SLATE, "accent4": "8A9BA8", "accent5": "E1A070", "accent6": "2F6F8F"}
    for slot, value in palette.items():
        node = scheme.find(f"{a}{slot}")
        for child in list(node):
            node.remove(child)
        etree.SubElement(node, f"{a}srgbClr").set("val", value)
    fonts = root.find(f".//{a}fontScheme")
    fonts.set("name", "Meridian")
    fonts.find(f"{a}majorFont/{a}latin").set("typeface", "Trebuchet MS")
    fonts.find(f"{a}minorFont/{a}latin").set("typeface", "Calibri")
    theme_part._blob = etree.tostring(root, xml_declaration=True, encoding="UTF-8", standalone=True)


def _scale_to_widescreen(prs) -> None:
    factor = 13.333 / 10
    prs.slide_width, prs.slide_height = Inches(13.333), Inches(7.5)
    holders = [prs.slide_master] + list(prs.slide_layouts)
    for holder in holders:
        for shape in holder.shapes:
            xfrm = shape._element.find(f".//{pqn('a:xfrm')}")
            if xfrm is None:
                continue
            shape.left = Emu(int(shape.left * factor))
            shape.width = Emu(int(shape.width * factor))


def _master_furniture(prs) -> None:
    master = prs.slide_master
    bar = master.shapes._spTree
    xml = (
        f'<p:sp xmlns:p="http://schemas.openxmlformats.org/presentationml/2006/main" '
        f'xmlns:a="http://schemas.openxmlformats.org/drawingml/2006/main">'
        f'<p:nvSpPr><p:cNvPr id="90" name="Rust rule"/><p:cNvSpPr/><p:nvPr userDrawn="1"/></p:nvSpPr>'
        f'<p:spPr><a:xfrm><a:off x="0" y="0"/><a:ext cx="{prs.slide_width}" cy="{Inches(0.12)}"/></a:xfrm>'
        f'<a:prstGeom prst="rect"><a:avLst/></a:prstGeom><a:solidFill><a:schemeClr val="accent2"/></a:solidFill>'
        f"<a:ln><a:noFill/></a:ln></p:spPr>"
        f'<p:txBody><a:bodyPr/><a:lstStyle/><a:p><a:endParaRPr lang="en-GB"/></a:p></p:txBody></p:sp>'
    )
    bar.insert(2, etree.fromstring(xml))

    image_part, rid = master.part.get_or_add_image_part(logo_png())
    del image_part
    pic = (
        f'<p:pic xmlns:p="http://schemas.openxmlformats.org/presentationml/2006/main" '
        f'xmlns:a="http://schemas.openxmlformats.org/drawingml/2006/main" '
        f'xmlns:r="http://schemas.openxmlformats.org/officeDocument/2006/relationships">'
        f'<p:nvPicPr><p:cNvPr id="91" name="Logo"/><p:cNvPicPr><a:picLocks noChangeAspect="1"/></p:cNvPicPr>'
        f'<p:nvPr userDrawn="1"/></p:nvPicPr><p:blipFill><a:blip r:embed="{rid}"/><a:stretch><a:fillRect/></a:stretch></p:blipFill>'
        f'<p:spPr><a:xfrm><a:off x="{Inches(11.45)}" y="{Inches(0.28)}"/><a:ext cx="{Inches(1.5)}" cy="{Inches(0.375)}"/></a:xfrm>'
        f'<a:prstGeom prst="rect"><a:avLst/></a:prstGeom></p:spPr></p:pic>'
    )
    bar.append(etree.fromstring(pic))

    # Titles left-aligned and heavier than the python-pptx default.
    styles = master._element.find(pqn("p:txStyles"))
    title = styles.find(pqn("p:titleStyle")).find(pqn("a:lvl1pPr"))
    title.set("algn", "l")
    defaults = title.find(pqn("a:defRPr"))
    defaults.set("sz", "3000")
    defaults.set("b", "1")
    fill = defaults.find(pqn("a:solidFill"))
    if fill is not None:
        for child in list(fill):
            fill.remove(child)
        etree.SubElement(fill, pqn("a:schemeClr")).set("val", "tx2")


def _with_footer(prs, slide, layout, text: str) -> None:
    from pptx.enum.shapes import PP_PLACEHOLDER

    for shape in layout.placeholders:
        kind = shape.placeholder_format.type
        if kind in (PP_PLACEHOLDER.FOOTER, PP_PLACEHOLDER.SLIDE_NUMBER):
            clone = copy.deepcopy(shape._element)
            slide.shapes._spTree.append(clone)
            if kind == PP_PLACEHOLDER.FOOTER:
                for t in clone.iter(pqn("a:t")):
                    t.text = text
                if not list(clone.iter(pqn("a:t"))):
                    paragraph = clone.find(f".//{pqn('a:p')}")
                    run = etree.Element(pqn("a:r"))
                    etree.SubElement(run, pqn("a:t")).text = text
                    paragraph.insert(0, run)


def build_pptx(path: Path) -> None:
    prs = Presentation()
    _retheme(prs)
    _scale_to_widescreen(prs)
    _master_furniture(prs)
    title_layout, content_layout, two_layout, title_only = (
        prs.slide_layouts[0],
        prs.slide_layouts[1],
        prs.slide_layouts[3],
        prs.slide_layouts[5],
    )
    footer = "Meridian Freight  |  Board pack"

    slide = prs.slides.add_slide(title_layout)
    slide.shapes.title.text = "Q2 Network Review"
    slide.placeholders[1].text = "Operations board, July"

    slide = prs.slides.add_slide(content_layout)
    slide.shapes.title.text = "Eastern corridor keeps growing"
    frame = slide.placeholders[1].text_frame
    frame.text = "Consignments up 6.2% on Q1"
    for line in ("Felixstowe and Immingham drove most of the rise", "On-time delivery slipped to 93.1%"):
        frame.add_paragraph().text = line
    sub = frame.add_paragraph()
    sub.text = "Immingham's dip traces to the May berth closures"
    sub.level = 1
    slide.notes_slide.notes_text_frame.text = "Mention the April timetable change before anyone asks."
    _with_footer(prs, slide, content_layout, footer)

    slide = prs.slides.add_slide(title_only)
    slide.shapes.title.text = "On-time delivery by depot"
    data = CategoryChartData()
    data.categories = ["Felixstowe", "Immingham", "Tilbury", "Grangemouth"]
    data.add_series("Q1", (95.8, 94.2, 95.0, 96.1))
    data.add_series("Q2", (94.6, 89.8, 93.9, 95.2))
    chart = slide.shapes.add_chart(
        XL_CHART_TYPE.COLUMN_CLUSTERED, Inches(0.8), Inches(1.6), Inches(11.7), Inches(4.9), data
    ).chart
    chart.has_legend = True
    chart.legend.position = XL_LEGEND_POSITION.BOTTOM
    chart.legend.include_in_layout = False
    _with_footer(prs, slide, title_only, footer)

    slide = prs.slides.add_slide(title_only)
    slide.shapes.title.text = "Depot scorecard"
    rows = [
        ("Depot", "Consignments", "On-time %", "Cost per drop"),
        ("Felixstowe", "48,210", "94.6", "£11.20"),
        ("Immingham", "31,905", "89.8", "£12.75"),
        ("Tilbury", "27,440", "93.9", "£11.95"),
        ("Grangemouth", "12,118", "95.2", "£14.10"),
    ]
    table = slide.shapes.add_table(len(rows), 4, Inches(0.8), Inches(1.7), Inches(11.7), Inches(3.2)).table
    for r, values in enumerate(rows):
        for c, value in enumerate(values):
            table.cell(r, c).text = value
    _with_footer(prs, slide, title_only, footer)

    slide = prs.slides.add_slide(two_layout)
    slide.shapes.title.text = "Next steps"
    slide.placeholders[1].text = "Merge the Thursday Immingham and Tilbury trunks from September"
    picture = slide.placeholders[2]
    left, top, width = picture.left, picture.top, picture.width
    picture._element.getparent().remove(picture._element)
    slide.shapes.add_picture(photo_png(), left, top, width=width)
    _with_footer(prs, slide, two_layout, footer)

    for shape in prs.slides[0].shapes:
        if shape.has_text_frame:
            for paragraph in shape.text_frame.paragraphs:
                for run in paragraph.runs:
                    if shape == prs.slides[0].shapes.title:
                        run.font.size = PptxPt(40)
                        run.font.color.rgb = PptxRGB.from_string(NAVY)

    prs.core_properties.title = "Q2 Network Review"
    prs.core_properties.author = "Meridian Freight"
    prs.core_properties.last_modified_by = "Meridian Freight"
    prs.save(str(path))


def build_xlsx(path: Path) -> None:
    wb = Workbook()
    ws = wb.active
    ws.title = "Budget"
    thin = Side(style="thin", color="C9D2DA")
    heavy = Side(style="medium", color=NAVY)
    label = Font(name="Trebuchet MS", size=10)
    money = '#,##0;[Red](#,##0)'

    ws.merge_cells("A1:F1")
    ws["A1"] = "Meridian Freight: FY depot budget"
    ws["A1"].font = Font(name="Trebuchet MS", size=15, bold=True, color=NAVY)
    ws["A1"].alignment = Alignment(vertical="center")
    ws.row_dimensions[1].height = 42
    ws.merge_cells("A2:F2")
    ws["A2"] = "All figures in GBP thousands. Variance is budget minus actual."
    ws["A2"].font = Font(name="Trebuchet MS", size=9, italic=True, color=SLATE)

    headers = ["Depot", "Region", "Budget", "Actual", "Variance", "Status"]
    for c, title in enumerate(headers, start=1):
        cell = ws.cell(row=4, column=c, value=title)
        cell.font = Font(name="Trebuchet MS", size=10, bold=True, color="FFFFFF")
        cell.fill = PatternFill("solid", fgColor=NAVY)
        cell.alignment = Alignment(horizontal="center" if c > 2 else "left", vertical="center")
        cell.border = Border(bottom=heavy)
    ws.row_dimensions[4].height = 24

    data = [
        ("Felixstowe", "East", 1820, 1764, "On track"),
        ("Immingham", "East", 1310, 1402, "Over"),
        ("Tilbury", "South", 1145, 1120, "On track"),
        ("Grangemouth", "North", 690, 701, "Watch"),
    ]
    first = 5
    for offset, (depot, region, budget, actual, status) in enumerate(data):
        r = first + offset
        ws.cell(r, 1, depot).font = label
        ws.cell(r, 2, region).font = label
        ws.cell(r, 3, budget).number_format = money
        ws.cell(r, 4, actual).number_format = money
        ws.cell(r, 5, f"=C{r}-D{r}").number_format = money
        ws.cell(r, 6, status).alignment = Alignment(horizontal="center")
        for c in range(1, 7):
            ws.cell(r, c).border = Border(bottom=thin)
            if c in (3, 4, 5, 6):
                ws.cell(r, c).font = label
    last = first + len(data) - 1
    total = last + 1
    ws.cell(total, 1, "Total")
    ws.cell(total, 3, f"=SUM(C{first}:C{last})").number_format = money
    ws.cell(total, 4, f"=SUM(D{first}:D{last})").number_format = money
    ws.cell(total, 5, f"=C{total}-D{total}").number_format = money
    for c in range(1, 7):
        ws.cell(total, c).font = Font(name="Trebuchet MS", size=10, bold=True)
        ws.cell(total, c).fill = PatternFill("solid", fgColor=PALE)
        ws.cell(total, c).border = Border(top=heavy, bottom=heavy)

    for column, width in zip("ABCDEF", (20, 12, 12, 12, 12, 12)):
        ws.column_dimensions[column].width = width
    ws.freeze_panes = "A5"

    ws.conditional_formatting.add(
        f"E{first}:E{last}", CellIsRule(operator="lessThan", formula=["0"], fill=PatternFill("solid", bgColor="F6D5C3"))
    )
    validation = DataValidation(type="list", formula1='"On track,Watch,Over"', allow_blank=False)
    validation.add(f"F{first}:F{last}")
    ws.add_data_validation(validation)
    wb.defined_names["BudgetTotal"] = DefinedName("BudgetTotal", attr_text=f"Budget!$C${total}")

    chart = BarChart()
    chart.type = "col"
    chart.title = "Budget against actual"
    chart.height, chart.width = 7.5, 16
    chart.add_data(Reference(ws, min_col=3, max_col=4, min_row=4, max_row=last), titles_from_data=True)
    chart.set_categories(Reference(ws, min_col=1, min_row=first, max_row=last))
    # openpyxl leaves this unset, and Excel then hides both axes.
    chart.x_axis.delete = False
    chart.y_axis.delete = False
    ws.add_chart(chart, "A12")

    logo = XlImage(logo_png())
    logo.width, logo.height = 150, 37
    ws.add_image(logo, "G1")

    ws.page_setup.orientation = "landscape"
    ws.page_setup.fitToWidth = 1
    ws.page_setup.fitToHeight = 0
    ws.sheet_properties.pageSetUpPr.fitToPage = True
    ws.oddHeader.right.text = "Meridian Freight"
    ws.oddFooter.center.text = "Page &P of &N"

    summary = wb.create_sheet("Summary")
    summary["A1"] = "Board summary"
    summary["A1"].font = Font(name="Trebuchet MS", size=13, bold=True, color=NAVY)
    summary["A3"] = "Total budget"
    summary["B3"] = "=BudgetTotal"
    summary["A4"] = "Total actual"
    summary["B4"] = f"=Budget!D{total}"
    summary["A5"] = "Variance"
    summary["B5"] = "=B3-B4"
    for cell in ("B3", "B4", "B5"):
        summary[cell].number_format = money
    summary.column_dimensions["A"].width = 18

    wb.properties.title = "FY depot budget"
    wb.properties.creator = "Meridian Freight"
    wb.save(path)


def build_all(out: Path = OUT) -> dict[str, Path]:
    out.mkdir(parents=True, exist_ok=True)
    paths = {
        "docx": out / "meridian_quarterly_report.docx",
        "pptx": out / "meridian_board_deck.pptx",
        "xlsx": out / "meridian_depot_budget.xlsx",
    }
    build_docx(paths["docx"])
    build_pptx(paths["pptx"])
    build_xlsx(paths["xlsx"])
    return paths


if __name__ == "__main__":
    for kind, path in build_all().items():
        print(f"{kind}: {path}")
