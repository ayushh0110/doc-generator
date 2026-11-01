import zipfile

import pytest
from docx import Document
from openpyxl import load_workbook
from pptx import Presentation

from app.documents import ENGINES
from app.documents.base import EditError, part_fingerprints
from app.documents.defaults import default_template
from app.documents.ops import (
    Block,
    ChartSeries,
    DocxAddRow,
    DocxDelete,
    DocxInsertAfter,
    DocxReplaceBody,
    DocxReplaceImage,
    DocxReplaceText,
    DocxSetCell,
    FindReplace,
    PlaceholderFill,
    PptxAddSlide,
    PptxDeleteSlide,
    PptxDuplicateSlide,
    PptxSetChartData,
    PptxSetText,
    SetSources,
    ShapeText,
    Source,
    XlsxAddSheet,
    XlsxDeleteRows,
    XlsxInsertRows,
    XlsxSetCell,
    XlsxWriteTable,
)


def assert_all_pass(engine, src, dest, result):
    failed = [c for c in engine.preservation_checks(src, dest, result) if not c.ok]
    assert not failed, failed


def test_docx_analysis_reports_the_structure(templates):
    profile = ENGINES["docx"].analyse(templates["docx"])
    page = profile["page_setup"][0]
    assert page["width_mm"] == 210.0
    assert page["margins_mm"]["left"] == 22.0
    assert profile["headings"][0]["text"] == "Q2 Network Review"
    assert any(s["name"] == "Heading 1" and s["font"] == "Trebuchet MS" for s in profile["styles_in_use"])
    table = profile["tables"][0]
    assert table["rows"] == 6 and table["header_fill"] == "#12344D"
    assert table["borders"]["top"]["color"] == "12344D"

    logo = next(i for i in profile["images"] if i["location"] == "header")
    assert logo["mode"] == "anchored" and logo["x_relative_to"] == "page" and logo["x_mm"] == 22.0
    footer = next(h for h in profile["headers_footers"] if h["kind"] == "footer")
    assert footer["has_page_number"]


def test_docx_edit_keeps_formatting_and_everything_else(templates, tmp_path):
    engine = ENGINES["docx"]
    outline = engine.outline(templates["docx"])
    summary = next(i for i in outline if i["text"].startswith("Volumes through"))
    dest = tmp_path / "out.docx"
    result = engine.apply(
        templates["docx"],
        [
            DocxReplaceText(target=summary["id"], text="Volumes grew again. **Margins held.**"),
            DocxSetCell(target="t0.r1.c1", text="50,000"),
            DocxAddRow(target="t0", after_row=4, values=["Hull", "9,000", "91.0", "£13.00"]),
        ],
        dest,
    )
    assert_all_pass(engine, templates["docx"], dest, result)

    doc = Document(str(dest))
    para = next(p for p in doc.paragraphs if p.text.startswith("Volumes grew"))
    assert para.style.name == "Normal"
    assert para.runs[-1].bold is True
    assert doc.tables[0].cell(1, 1).text == "50,000"
    assert doc.tables[0].cell(5, 0).text == "Hull"
    # The new row copies a body row's shading, not the header's.
    assert "12344D" not in doc.tables[0].rows[5]._tr.xml


def test_docx_replace_body_builds_from_the_templates_own_samples(templates, tmp_path):
    engine = ENGINES["docx"]
    dest = tmp_path / "out.docx"
    result = engine.apply(
        templates["docx"],
        [
            DocxReplaceBody(
                keep_through="p1",
                blocks=[
                    Block(kind="heading1", text="New section"),
                    Block(kind="paragraph", text="Body text here."),
                    Block(kind="bullet", text="A point."),
                    Block(kind="table", rows=[["A", "B"], ["1", "2"], ["3", "4"]]),
                ],
            )
        ],
        dest,
    )
    assert_all_pass(engine, templates["docx"], dest, result)

    doc = Document(str(dest))
    assert doc.paragraphs[0].text == "Q2 Network Review"
    assert doc.paragraphs[2].style.name == "Heading 1"
    assert doc.paragraphs[4].style.name == "List Bullet"
    table = doc.tables[0]
    assert (len(table.rows), len(table.columns)) == (3, 2)
    assert "12344D" in table.rows[0]._tr.xml
    assert table.cell(2, 1).text == "4"


def test_docx_find_replace_crosses_runs_and_reaches_the_header(templates, tmp_path):
    engine = ENGINES["docx"]
    dest = tmp_path / "out.docx"
    result = engine.apply(templates["docx"], [FindReplace(find="Meridian Freight Ltd", replace="Northline Ltd")], dest)
    assert_all_pass(engine, templates["docx"], dest, result)
    assert "Northline Ltd" in Document(str(dest)).sections[0].footer.paragraphs[0].text

    dest2 = tmp_path / "out2.docx"
    result = engine.apply(templates["docx"], [FindReplace(find="MERIDIAN FREIGHT", replace="NORTHLINE")], dest2)
    assert_all_pass(engine, templates["docx"], dest2, result)
    assert "NORTHLINE" in Document(str(dest2)).sections[0].header.paragraphs[0].text


def test_docx_image_swap_keeps_the_anchor(templates, tmp_path):
    from PIL import Image

    engine = ENGINES["docx"]
    new_logo = tmp_path / "new.png"
    Image.new("RGB", (480, 120), "#008000").save(new_logo)
    profile = engine.analyse(templates["docx"])
    logo = next(i for i in profile["images"] if i["location"] == "header")

    dest = tmp_path / "out.docx"
    result = engine.apply(templates["docx"], [DocxReplaceImage(target=logo["id"], file_id="x")], dest, lambda _: new_logo)
    assert_all_pass(engine, templates["docx"], dest, result)

    after = next(i for i in engine.analyse(dest)["images"] if i["location"] == "header")
    assert after["sha256"] != logo["sha256"]
    for key in ("x_mm", "y_mm", "width_mm", "height_mm", "mode"):
        assert after[key] == logo[key]


def test_docx_validator_catches_tampering(templates, tmp_path):
    engine = ENGINES["docx"]
    dest = tmp_path / "out.docx"
    result = engine.apply(templates["docx"], [DocxDelete(target="p3")], dest)
    # Pretend the delete wasn't declared, as if an engine bug had changed it.
    result.touched.clear()
    failed = [c.name for c in engine.preservation_checks(templates["docx"], dest, result) if not c.ok]
    assert "Untouched content is byte-identical" in failed


def test_docx_bad_ids_raise_a_message_the_writer_can_use(templates, tmp_path):
    with pytest.raises(EditError, match="p999 does not exist"):
        ENGINES["docx"].apply(templates["docx"], [DocxReplaceText(target="p999", text="x")], tmp_path / "o.docx")


def test_docx_sources_section_is_replaced_not_stacked(templates, tmp_path):
    engine = ENGINES["docx"]
    first = tmp_path / "one.docx"
    engine.apply(
        templates["docx"],
        [SetSources(sources=[Source(key="S1", title="Annual report", locator="p. 4", url="https://example.com/a")])],
        first,
    )
    second = tmp_path / "two.docx"
    result = engine.apply(first, [SetSources(sources=[Source(key="S2", title="Port data")])], second)
    assert_all_pass(engine, first, second, result)
    text = [p.text for p in Document(str(second)).paragraphs]
    assert text.count("Sources") == 1
    assert "[S2] Port data" in text and not any("Annual report" in t for t in text)


def test_pptx_analysis_reports_masters_layouts_and_positions(templates):
    profile = ENGINES["pptx"].analyse(templates["pptx"])
    assert profile["slide_size_mm"]["width"] == pytest.approx(338.7, abs=0.2)
    assert profile["theme"]["major_font"] == "Trebuchet MS"
    assert profile["theme"]["colors"]["accent2"] == "#C2571A"
    master = profile["masters"][0]
    assert any(s["name"] == "Logo" and s["kind"] == "picture" for s in master["shapes"])
    assert "Title Slide" in [l["name"] for l in master["layouts"]]
    kinds = {s["kind"] for slide in profile["slides"] for s in slide["shapes"]}
    assert {"chart", "table", "picture", "placeholder"} <= kinds


def _shape_id(engine, path, slide, predicate):
    item = engine.outline(path)[slide]
    return next(s["id"] for s in item["shapes"] if predicate(s))


def test_pptx_text_edit_leaves_other_slides_and_design_alone(templates, tmp_path):
    engine = ENGINES["pptx"]
    target = _shape_id(engine, templates["pptx"], 1, lambda s: s.get("placeholder_idx") == 1)
    dest = tmp_path / "out.pptx"
    result = engine.apply(
        templates["pptx"],
        [PptxSetText(target=target, paragraphs=["First point", "Second point", "\tA sub-point", "Fourth point"])],
        dest,
    )
    assert_all_pass(engine, templates["pptx"], dest, result)
    body = Presentation(str(dest)).slides[1].placeholders[1].text_frame
    assert [p.text for p in body.paragraphs] == ["First point", "Second point", "A sub-point", "Fourth point"]
    assert body.paragraphs[2].level == 1


def test_pptx_new_slide_uses_the_layout_and_keeps_footers(templates, tmp_path):
    engine = ENGINES["pptx"]
    dest = tmp_path / "out.pptx"
    result = engine.apply(
        templates["pptx"],
        [
            PptxAddSlide(
                layout="Title and Content",
                after="s1",
                content=[
                    PlaceholderFill(placeholder="title", paragraphs=["Added slide"]),
                    PlaceholderFill(placeholder="body", paragraphs=["One", "Two"]),
                ],
                notes="Speaker note",
            )
        ],
        dest,
    )
    assert_all_pass(engine, templates["pptx"], dest, result)
    prs = Presentation(str(dest))
    assert len(prs.slides) == 6
    new = prs.slides[2]
    assert new.shapes.title.text == "Added slide"
    assert new.slide_layout.name == "Title and Content"
    assert new.notes_slide.notes_text_frame.text == "Speaker note"
    types = {str(p.placeholder_format.type) for p in new.placeholders}
    assert any("SLIDE_NUMBER" in t for t in types)


def test_pptx_slides_land_where_asked_and_in_the_order_asked(templates, tmp_path):
    engine = ENGINES["pptx"]

    def slide(title):
        return PptxAddSlide(
            layout="Title Only", content=[PlaceholderFill(placeholder="title", paragraphs=[title])], after=None
        )

    dest = tmp_path / "end.pptx"
    engine.apply(templates["pptx"], [slide("First new"), slide("Second new")], dest)
    titles = [s.shapes.title.text for s in Presentation(str(dest)).slides]
    assert titles[-2:] == ["First new", "Second new"]

    dest = tmp_path / "middle.pptx"
    engine.apply(
        templates["pptx"],
        [slide("A").model_copy(update={"after": "s1"}), slide("B").model_copy(update={"after": "s1"})],
        dest,
    )
    titles = [s.shapes.title.text for s in Presentation(str(dest)).slides]
    assert titles[1:4] == ["Eastern corridor keeps growing", "A", "B"]


def test_typed_bullet_characters_do_not_show_up_twice(templates, tmp_path):
    engine = ENGINES["pptx"]
    target = _shape_id(engine, templates["pptx"], 1, lambda s: s.get("placeholder_idx") == 1)
    dest = tmp_path / "out.pptx"
    engine.apply(templates["pptx"], [PptxSetText(target=target, paragraphs=["• One", "- Two", "\t• Three", "4 million TEU"])], dest)
    body = Presentation(str(dest)).slides[1].placeholders[1].text_frame
    assert [p.text for p in body.paragraphs] == ["One", "Two", "Three", "4 million TEU"]

    dest = tmp_path / "out.docx"
    ENGINES["docx"].apply(templates["docx"], [DocxInsertAfter(target="p7", blocks=[Block(kind="bullet", text="• Point")])], dest)
    assert "Point" in [p.text for p in Document(str(dest)).paragraphs]


def test_pptx_unknown_layout_lists_the_real_ones(templates, tmp_path):
    with pytest.raises(EditError, match="Available:.*Title Slide"):
        ENGINES["pptx"].apply(
            templates["pptx"], [PptxAddSlide(layout="Nope", content=[])], tmp_path / "o.pptx"
        )


def test_pptx_duplicate_keeps_pictures_and_edits_the_copy_only(templates, tmp_path):
    engine = ENGINES["pptx"]
    title = _shape_id(engine, templates["pptx"], 4, lambda s: s.get("placeholder_idx") == 0)
    dest = tmp_path / "out.pptx"
    result = engine.apply(
        templates["pptx"],
        [PptxDuplicateSlide(target="s4", edits=[ShapeText(shape=title, paragraphs=["Copy of next steps"])])],
        dest,
    )
    assert_all_pass(engine, templates["pptx"], dest, result)
    prs = Presentation(str(dest))
    assert prs.slides[4].shapes.title.text == "Next steps"
    assert prs.slides[5].shapes.title.text == "Copy of next steps"
    assert any(s.shape_type == 13 for s in prs.slides[5].shapes)


def test_pptx_chart_data_swap_keeps_the_chart(templates, tmp_path):
    engine = ENGINES["pptx"]
    chart = _shape_id(engine, templates["pptx"], 2, lambda s: s["kind"] == "chart")
    dest = tmp_path / "out.pptx"
    result = engine.apply(
        templates["pptx"],
        [PptxSetChartData(target=chart, categories=["A", "B"], series=[ChartSeries(name="Q3", values=[1, 2])])],
        dest,
    )
    assert_all_pass(engine, templates["pptx"], dest, result)
    updated = next(s for s in Presentation(str(dest)).slides[2].shapes if s.has_chart).chart
    assert list(updated.plots[0].categories) == ["A", "B"]


def test_pptx_delete_and_overflow_check(templates, tmp_path):
    engine = ENGINES["pptx"]
    dest = tmp_path / "out.pptx"
    result = engine.apply(templates["pptx"], [PptxDeleteSlide(target="s4")], dest)
    assert_all_pass(engine, templates["pptx"], dest, result)
    assert len(Presentation(str(dest)).slides) == 4

    title = _shape_id(engine, templates["pptx"], 1, lambda s: s.get("placeholder_idx") == 0)
    dest2 = tmp_path / "out2.pptx"
    result = engine.apply(templates["pptx"], [PptxSetText(target=title, paragraphs=["word " * 200])], dest2)
    failed = [c.name for c in engine.preservation_checks(templates["pptx"], dest2, result) if not c.ok]
    assert failed == ["Text fits its boxes"]


def test_xlsx_analysis_reports_formats_and_objects(templates):
    profile = ENGINES["xlsx"].analyse(templates["xlsx"])
    budget = profile["sheets"][0]
    assert budget["freeze_panes"] == "A5"
    assert "A1:F1" in budget["merged_cells"]
    assert budget["conditional_formats"] and budget["data_validations"]
    assert budget["header"]["fill"] == "#12344D"
    assert profile["named_ranges"] == ["BudgetTotal"]
    assert profile["objects"]["charts"] == 1 and profile["objects"]["images"] == 1


def test_xlsx_cell_edit_leaves_the_other_parts_byte_identical(templates, tmp_path):
    engine = ENGINES["xlsx"]
    dest = tmp_path / "out.xlsx"
    result = engine.apply(templates["xlsx"], [XlsxSetCell(sheet="Budget", ref="D6", value=1500)], dest)
    assert_all_pass(engine, templates["xlsx"], dest, result)

    with zipfile.ZipFile(templates["xlsx"]) as a, zipfile.ZipFile(dest) as b:
        for name in a.namelist():
            if name.startswith(("xl/styles", "xl/theme", "xl/charts", "xl/media", "xl/drawings")):
                assert a.read(name) == b.read(name), name

    wb = load_workbook(dest)
    assert wb["Budget"]["D6"].value == 1500
    assert wb["Budget"]["D6"].number_format == load_workbook(templates["xlsx"])["Budget"]["D6"].number_format


def test_xlsx_insert_above_total_moves_and_extends_references(templates, tmp_path):
    engine = ENGINES["xlsx"]
    dest = tmp_path / "out.xlsx"
    result = engine.apply(
        templates["xlsx"],
        [XlsxInsertRows(sheet="Budget", before_row=9, rows=[["Hull", "East", 400, 390, None, "On track"]])],
        dest,
    )
    assert_all_pass(engine, templates["xlsx"], dest, result)

    wb = load_workbook(dest)
    ws = wb["Budget"]
    assert ws["A9"].value == "Hull"
    # The variance formula came down from the row above, for the new row.
    assert ws["E9"].value == "=C9-D9"
    assert ws["A10"].value == "Total"
    assert ws["C10"].value == "=SUM(C5:C9)"
    assert wb["Summary"]["B4"].value == "=Budget!D10"
    assert wb.defined_names["BudgetTotal"].attr_text == "Budget!$C$10"
    assert ws["A9"].font.name == ws["A8"].font.name
    assert ws["D9"].number_format == ws["D8"].number_format
    assert "E5:E9" in [str(r.sqref) for r in ws.conditional_formatting]
    assert "F5:F9" in [str(d.sqref) for d in ws.data_validations.dataValidation]

    with zipfile.ZipFile(dest) as package:
        chart = package.read("xl/charts/chart1.xml").decode()
        drawing = package.read("xl/drawings/drawing1.xml").decode()
    assert "$8" not in chart and "$9" in chart
    # The chart was anchored at A12, zero-based row 11, and moves with the rows.
    assert "<xdr:row>12</xdr:row>" in drawing or "<row>12</row>" in drawing


def test_xlsx_accepts_a_sheet_prefix_and_explains_bad_references(templates, tmp_path):
    engine = ENGINES["xlsx"]
    dest = tmp_path / "out.xlsx"
    result = engine.apply(templates["xlsx"], [XlsxSetCell(sheet="Budget", ref="Budget!D6", value=1500)], dest)
    assert_all_pass(engine, templates["xlsx"], dest, result)
    assert load_workbook(dest)["Budget"]["D6"].value == 1500

    with pytest.raises(EditError, match="not a cell reference"):
        engine.apply(templates["xlsx"], [XlsxSetCell(sheet="Budget", ref="the total", value=1)], tmp_path / "x.xlsx")


def test_sample_chart_shows_its_axes_in_excel(templates):
    with zipfile.ZipFile(templates["xlsx"]) as package:
        chart = package.read("xl/charts/chart1.xml").decode()
    assert chart.count('delete val="0"') == 2


def test_xlsx_delete_rows_shrinks_ranges_and_flags_nothing_when_clean(templates, tmp_path):
    engine = ENGINES["xlsx"]
    dest = tmp_path / "out.xlsx"
    result = engine.apply(templates["xlsx"], [XlsxDeleteRows(sheet="Budget", start_row=6)], dest)
    assert_all_pass(engine, templates["xlsx"], dest, result)
    ws = load_workbook(dest)["Budget"]
    assert ws["A6"].value == "Tilbury"
    assert ws["C8"].value == "=SUM(C5:C7)"


def test_xlsx_write_table_and_sources_sheet(templates, tmp_path):
    engine = ENGINES["xlsx"]
    dest = tmp_path / "out.xlsx"
    result = engine.apply(
        templates["xlsx"],
        [
            XlsxAddSheet(name="Forecast"),
            XlsxWriteTable(sheet="Forecast", start="A1", headers=["Quarter", "Spend"], rows=[["Q3", 100], ["Q4", 120]]),
            SetSources(sources=[Source(key="S1", title="Port stats", url="https://example.com/p")]),
        ],
        dest,
    )
    assert_all_pass(engine, templates["xlsx"], dest, result)
    wb = load_workbook(dest)
    assert wb.sheetnames == ["Budget", "Summary", "Forecast", "Sources"]
    assert wb["Forecast"]["B3"].value == 120
    assert wb["Sources"]["B2"].value == "Port stats"
    assert wb["Forecast"]["A1"].font.b
    # New sheets fit their text and print one page wide.
    assert wb["Sources"].column_dimensions["B"].width >= 12
    assert wb["Sources"].sheet_properties.pageSetUpPr.fitToPage


def test_xlsx_find_replace_and_bad_sheet(templates, tmp_path):
    engine = ENGINES["xlsx"]
    dest = tmp_path / "out.xlsx"
    result = engine.apply(templates["xlsx"], [FindReplace(find="Meridian Freight", replace="Northline")], dest)
    assert_all_pass(engine, templates["xlsx"], dest, result)
    assert load_workbook(dest)["Budget"]["A1"].value.startswith("Northline")

    with pytest.raises(EditError, match='No sheet called "Nope"'):
        engine.apply(templates["xlsx"], [XlsxSetCell(sheet="Nope", ref="A1", value=1)], tmp_path / "x.xlsx")


def test_fingerprints_ignore_serialisation_only_differences(templates, tmp_path):
    copy_path = tmp_path / "copy.docx"
    Document(str(templates["docx"])).save(str(copy_path))
    before, after = part_fingerprints(templates["docx"]), part_fingerprints(copy_path)
    assert before["word/styles.xml"] == after["word/styles.xml"]


def test_new_docx_from_the_blank_template(tmp_path):
    engine = ENGINES["docx"]
    blank = default_template("docx", tmp_path / "defaults")
    dest = tmp_path / "out.docx"
    result = engine.apply(
        blank,
        [
            DocxReplaceBody(
                blocks=[
                    Block(kind="heading1", text="Title"),
                    Block(kind="paragraph", text="Body."),
                    Block(kind="bullet", text="Point."),
                    Block(kind="numbered", text="Step."),
                    Block(kind="table", rows=[["A", "B"], ["1", "2"]]),
                ]
            )
        ],
        dest,
    )
    assert_all_pass(engine, blank, dest, result)
    doc = Document(str(dest))
    assert [p.style.name for p in doc.paragraphs] == ["Heading 1", "Normal", "List Bullet", "List Number"]
    assert doc.tables[0].style.name == "Table Grid"


def test_new_deck_from_the_blank_template(tmp_path):
    engine = ENGINES["pptx"]
    blank = default_template("pptx", tmp_path / "defaults")
    dest = tmp_path / "out.pptx"
    result = engine.apply(
        blank,
        [
            PptxAddSlide(
                layout="Title Slide",
                content=[
                    PlaceholderFill(placeholder="title", paragraphs=["Hello"]),
                    PlaceholderFill(placeholder="subtitle", paragraphs=["World"]),
                ],
            )
        ],
        dest,
    )
    assert_all_pass(engine, blank, dest, result)
    assert Presentation(str(dest)).slides[0].shapes.title.text == "Hello"


def test_new_workbook_from_the_blank_template_has_no_sample_text(tmp_path):
    engine = ENGINES["xlsx"]
    blank = default_template("xlsx", tmp_path / "defaults")
    dest = tmp_path / "out.xlsx"
    result = engine.apply(
        blank,
        [XlsxWriteTable(sheet="Data", headers=["Quarter", "Spend"], rows=[["Q3", 100], ["Q4", "=B2*2"]])],
        dest,
    )
    assert_all_pass(engine, blank, dest, result)
    wb = load_workbook(dest)
    ws = wb["Data"]
    assert ws["A1"].value == "Quarter" and ws["A1"].font.b and ws["C1"].value is None
    assert ws["B3"].value == "=B2*2"
    assert wb["_styles"].sheet_state == "veryHidden"
    assert [s["id"] for s in engine.outline(dest)] == ["Data"]
    assert [s["name"] for s in engine.analyse(dest)["sheets"]] == ["Data"]
