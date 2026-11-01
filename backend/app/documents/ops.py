"""The edit language the writer agent speaks.

The model never touches XML. It emits these operations, and the engines apply
them deterministically. Anything it can't express here, it can't break.
"""
from typing import Literal, Union

from pydantic import BaseModel, Field


class Source(BaseModel):
    """Written into the document itself by the set_sources op."""

    key: str
    title: str
    locator: str = ""
    url: str = ""
    retrieved_at: str = ""


class Block(BaseModel):
    kind: Literal["heading1", "heading2", "heading3", "paragraph", "bullet", "numbered", "table"]
    text: str = ""
    rows: list[list[str]] = Field(default_factory=list, description="For tables. First row is the header.")


class FindReplace(BaseModel):
    op: Literal["find_replace"] = "find_replace"
    find: str
    replace: str


class SetSources(BaseModel):
    """Appended by the pipeline, never by the model."""

    op: Literal["set_sources"] = "set_sources"
    sources: list[Source]


class DocxReplaceText(BaseModel):
    op: Literal["replace_text"] = "replace_text"
    target: str = Field(description="Paragraph id, e.g. p14")
    text: str


class DocxInsertAfter(BaseModel):
    op: Literal["insert_after"] = "insert_after"
    target: str = Field(description="Paragraph or table id to insert after")
    blocks: list[Block]


class DocxDelete(BaseModel):
    op: Literal["delete"] = "delete"
    target: str


class DocxSetCell(BaseModel):
    op: Literal["set_cell"] = "set_cell"
    target: str = Field(description="Cell id, e.g. t0.r1.c2")
    text: str


class DocxAddRow(BaseModel):
    op: Literal["add_row"] = "add_row"
    target: str = Field(description="Table id, e.g. t0")
    after_row: int | None = Field(default=None, description="Row index to insert after. Omit to add at the end.")
    values: list[str]


class DocxDeleteRow(BaseModel):
    op: Literal["delete_row"] = "delete_row"
    target: str = Field(description="Row id, e.g. t0.r2")


class DocxReplaceBody(BaseModel):
    op: Literal["replace_body"] = "replace_body"
    keep_through: str | None = Field(
        default=None,
        description="Keep everything up to and including this element, such as a cover page. Omit to replace it all.",
    )
    blocks: list[Block]


class DocxReplaceImage(BaseModel):
    op: Literal["replace_image"] = "replace_image"
    target: str = Field(description="Image id, e.g. img0")
    file_id: str = Field(description="Id of an image the user attached")


DocxOp = Union[
        DocxReplaceText,
        DocxInsertAfter,
        DocxDelete,
        DocxSetCell,
        DocxAddRow,
        DocxDeleteRow,
        DocxReplaceBody,
        DocxReplaceImage,
        FindReplace,
]


class PlaceholderFill(BaseModel):
    placeholder: str = Field(description='"title", "body", "subtitle", or a placeholder idx like "13"')
    paragraphs: list[str]


class ShapeText(BaseModel):
    shape: str = Field(description="Shape id on the slide, e.g. sh5")
    paragraphs: list[str]


class PptxSetText(BaseModel):
    op: Literal["set_text"] = "set_text"
    target: str = Field(description="Shape id, e.g. s2.sh5")
    paragraphs: list[str] = Field(description="One entry per paragraph. A leading tab means one indent level.")


class PptxAddSlide(BaseModel):
    op: Literal["add_slide"] = "add_slide"
    layout: str = Field(description="Exact layout name from the template")
    after: str | None = Field(default=None, description="Slide id to insert after, e.g. s2. Omit to add at the end.")
    content: list[PlaceholderFill]
    notes: str = ""


class PptxDuplicateSlide(BaseModel):
    op: Literal["duplicate_slide"] = "duplicate_slide"
    target: str = Field(description="Slide id to copy, e.g. s2")
    after: str | None = None
    edits: list[ShapeText] = Field(default_factory=list)
    notes: str = ""


class PptxDeleteSlide(BaseModel):
    op: Literal["delete_slide"] = "delete_slide"
    target: str


class PptxMoveSlide(BaseModel):
    op: Literal["move_slide"] = "move_slide"
    target: str
    after: str | None = Field(default=None, description="Slide id to place it after. Omit to move it to the front.")


class PptxSetCell(BaseModel):
    op: Literal["set_cell"] = "set_cell"
    target: str = Field(description="Cell id, e.g. s2.sh5.r1.c2")
    text: str


class PptxSetNotes(BaseModel):
    op: Literal["set_notes"] = "set_notes"
    target: str = Field(description="Slide id")
    text: str


class ChartSeries(BaseModel):
    name: str
    values: list[float]


class PptxSetChartData(BaseModel):
    op: Literal["set_chart_data"] = "set_chart_data"
    target: str = Field(description="Chart shape id, e.g. s2.sh5")
    categories: list[str]
    series: list[ChartSeries]


class PptxReplaceImage(BaseModel):
    op: Literal["replace_image"] = "replace_image"
    target: str = Field(description="Picture shape id, e.g. s0.sh4")
    file_id: str


PptxOp = Union[
        PptxSetText,
        PptxAddSlide,
        PptxDuplicateSlide,
        PptxDeleteSlide,
        PptxMoveSlide,
        PptxSetCell,
        PptxSetNotes,
        PptxSetChartData,
        PptxReplaceImage,
        FindReplace,
]


CellValue = Union[str, float, bool, None]


class XlsxSetCell(BaseModel):
    op: Literal["set_cell"] = "set_cell"
    sheet: str
    ref: str = Field(description="A1-style reference")
    value: CellValue = Field(description='Text, number, or a formula starting with "="')


class XlsxInsertRows(BaseModel):
    op: Literal["insert_rows"] = "insert_rows"
    sheet: str
    before_row: int = Field(description="New rows go above this row. Use the row after the last data row to append.")
    rows: list[list[CellValue]] = Field(
        description="A null keeps the formula from the row above, adjusted to the new row."
    )


class XlsxDeleteRows(BaseModel):
    op: Literal["delete_rows"] = "delete_rows"
    sheet: str
    start_row: int
    count: int = 1


class XlsxWriteTable(BaseModel):
    op: Literal["write_table"] = "write_table"
    sheet: str
    start: str = Field(default="A1", description="Top-left cell of the table")
    headers: list[str]
    rows: list[list[CellValue]]


class XlsxAddSheet(BaseModel):
    op: Literal["add_sheet"] = "add_sheet"
    name: str


XlsxOp = Union[XlsxSetCell, XlsxInsertRows, XlsxDeleteRows, XlsxWriteTable, XlsxAddSheet, FindReplace
]
