"""Runs fixed edit plans through the document engines and saves the results.

No model is involved. These files show what the engines preserve, not what a model
would write. The plans are the same shape the writer agent produces.

    python samples/generate_outputs.py
"""
import json
import shutil
import subprocess
import sys
import tempfile
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path[:0] = [str(ROOT / "backend"), str(ROOT / "samples")]

from build_templates import build_all  # noqa: E402

from app.documents import ENGINES  # noqa: E402
from app.documents.ops import (  # noqa: E402
    Block,
    DocxAddRow,
    DocxReplaceBody,
    DocxReplaceText,
    DocxSetCell,
    FindReplace,
    PlaceholderFill,
    PptxAddSlide,
    PptxSetChartData,
    PptxSetText,
    SetSources,
    Source,
    XlsxAddSheet,
    XlsxInsertRows,
    XlsxSetCell,
    XlsxWriteTable,
    ChartSeries,
)
from app.documents.preview import find_soffice  # noqa: E402
from app.documents.validate import blocking_failures, validate_plan  # noqa: E402

OUT = ROOT / "samples" / "outputs"
HULL_NOTE = Source(key="S1", title="hull_depot_notes.txt", locator="Full text", retrieved_at="2026-11-01")


def find_id(outline, starts_with):
    for item in outline:
        if item.get("text", "").startswith(starts_with):
            return item["id"]
    raise LookupError(starts_with)


def shape_id(outline, slide, idx):
    for shape in outline[slide]["shapes"]:
        if shape.get("placeholder_idx") == idx:
            return shape["id"]
    raise LookupError((slide, idx))


def docx_edit(template):
    outline = ENGINES["docx"].outline(template)
    return [
        DocxReplaceText(target=find_id(outline, "Q2 Network"), text="Q3 Network Review"),
        DocxReplaceText(target=find_id(outline, "Prepared for"), text="Prepared for the operations board, October"),
        DocxReplaceText(
            target=find_id(outline, "Volumes through"),
            text="The Hull depot opened on 4 August and handled 9,120 consignments in its first eight weeks, with "
            "on-time delivery at 96.2% [S1]. **The eastern corridor now carries most of the network's growth.**",
        ),
        DocxSetCell(target="t0.r5.c1", text="128,793"),
        DocxSetCell(target="t0.r5.c2", text="93.4"),
        DocxSetCell(target="t0.r5.c3", text="£12.17"),
        DocxAddRow(target="t0", after_row=4, values=["Hull", "9,120", "96.2", "£13.40"]),
        FindReplace(find="Meridian Freight Ltd", replace="Meridian Freight Group Ltd"),
        SetSources(sources=[HULL_NOTE]),
    ]


def docx_new(template):
    outline = ENGINES["docx"].outline(template)
    subtitle = find_id(outline, "Prepared for")
    return [
        DocxReplaceText(target=find_id(outline, "Q2 Network"), text="Winter Readiness Plan"),
        DocxReplaceText(target=subtitle, text="Prepared for the safety committee, November"),
        DocxReplaceBody(
            keep_through=subtitle,
            blocks=[
                Block(kind="heading1", text="Why this matters"),
                Block(
                    kind="paragraph",
                    text="Winter disruption costs more when it catches depots unprepared. This plan sets out what "
                    "each site should have ready by the end of November, and who checks it.",
                ),
                Block(kind="heading2", text="What each depot needs"),
                Block(kind="bullet", text="A gritting contract confirmed in writing."),
                Block(kind="bullet", text="Cold-start checks added to the daily vehicle inspection."),
                Block(kind="bullet", text="A named duty manager for each weather warning level."),
                Block(kind="heading1", text="Who checks what"),
                Block(
                    kind="table",
                    rows=[
                        ["Check", "Owner", "Due"],
                        ["Gritting contracts", "Site managers", "14 November"],
                        ["Vehicle checks", "Fleet lead", "21 November"],
                        ["Duty rota", "Operations", "28 November"],
                    ],
                ),
            ],
        ),
    ]


def pptx_edit(template):
    outline = ENGINES["pptx"].outline(template)
    chart = next(s["id"] for s in outline[2]["shapes"] if s["kind"] == "chart")
    return [
        PptxSetText(target=shape_id(outline, 0, 0), paragraphs=["Q3 Network Review"]),
        PptxSetText(target=shape_id(outline, 0, 1), paragraphs=["Operations board, October"]),
        PptxSetText(
            target=shape_id(outline, 1, 1),
            paragraphs=[
                "Hull opened in August: 9,120 consignments in eight weeks [S1]",
                "On-time delivery at Hull reached 96.2% [S1]",
                "\tThe best of any depot this quarter",
            ],
        ),
        PptxSetChartData(
            target=chart,
            categories=["Felixstowe", "Immingham", "Tilbury", "Grangemouth", "Hull"],
            series=[
                ChartSeries(name="Q2", values=[94.6, 89.8, 93.9, 95.2, 0]),
                ChartSeries(name="Q3", values=[95.1, 92.4, 94.0, 95.5, 96.2]),
            ],
        ),
        PptxAddSlide(
            layout="Title and Content",
            after="s3",
            content=[
                PlaceholderFill(placeholder="title", paragraphs=["What Hull changes"]),
                PlaceholderFill(
                    placeholder="body",
                    paragraphs=["Takes pressure off Immingham at peak", "Shortens the northern trunk by a day"],
                ),
            ],
            notes="Added slide. Layout, footer and page number come from the template.",
        ),
        SetSources(sources=[HULL_NOTE]),
    ]


def xlsx_edit(template):
    return [
        XlsxInsertRows(sheet="Budget", before_row=9, rows=[["Hull", "East", 400, 412, None, "Watch"]]),
        XlsxSetCell(sheet="Budget", ref="D5", value=1790),
        XlsxAddSheet(name="Depot notes"),
        XlsxWriteTable(
            sheet="Depot notes",
            start="A1",
            headers=["Depot", "Note"],
            rows=[["Hull", "Opened on 4 August [S1]"], ["Immingham", "Over budget after the May berth closures"]],
        ),
        SetSources(sources=[HULL_NOTE]),
    ]


JOBS = [
    ("docx", "report_rolled_forward_to_q3.docx", docx_edit),
    ("docx", "new_document_from_the_same_template.docx", docx_new),
    ("pptx", "deck_rolled_forward_to_q3.pptx", pptx_edit),
    ("xlsx", "budget_with_new_depot.xlsx", xlsx_edit),
]


def to_pdf(path: Path, folder: Path) -> None:
    soffice = find_soffice()
    if soffice is None:
        return
    with tempfile.TemporaryDirectory(ignore_cleanup_errors=True) as tmp:
        try:
            subprocess.run(
                [soffice, f"-env:UserInstallation=file:///{Path(tmp).as_posix()}", "--headless", "--convert-to", "pdf", "--outdir", str(folder), str(path)],
                check=True,
                capture_output=True,
                timeout=180,
            )
        except subprocess.SubprocessError as exc:
            print(f"  No PDF for {path.name}: {exc}")


def main() -> int:
    templates = build_all()
    if OUT.exists():
        shutil.rmtree(OUT)
    (OUT / "pdf").mkdir(parents=True)
    report = {}
    failed = False
    for kind, name, plan in JOBS:
        template = templates[kind]
        ops = plan(template)
        dest = OUT / name
        result = ENGINES[kind].apply(template, ops, dest)
        checks = validate_plan(template, dest, result, ops, {"S1"})
        failures = blocking_failures(checks)
        failed = failed or bool(failures)
        report[name] = {
            "template": template.name,
            "edits": [op.model_dump() for op in ops],
            "log": result.log,
            "checks": [c.to_dict() for c in checks],
        }
        print(f"{name}: {len(checks) - len(failures)}/{len(checks)} checks passed")
        for failure in failures:
            print(f"  FAILED {failure.name}: {failure.detail}")
        to_pdf(dest, OUT / "pdf")
    for path in templates.values():
        to_pdf(path, OUT / "pdf")
    (OUT / "report.json").write_text(json.dumps(report, indent=2, ensure_ascii=False), encoding="utf-8")
    return 1 if failed else 0


if __name__ == "__main__":
    raise SystemExit(main())
