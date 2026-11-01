"""Workbook edits are made on the XML, not through openpyxl.

openpyxl rebuilds the whole package when it saves, and shapes, some chart
formatting and anything it doesn't model can be lost on the way through. Patching
the sheet XML leaves every other part byte-for-byte as the user uploaded it.
openpyxl is only used for reading.
"""
import copy
import datetime as dt
import re
import subprocess
import tempfile
import zipfile
from pathlib import Path
from typing import Any, Callable, Iterable

from lxml import etree
from openpyxl import load_workbook
from openpyxl.formula import Tokenizer
from openpyxl.formula.translate import Translator
from openpyxl.utils import column_index_from_string, get_column_letter
from openpyxl.utils.cell import coordinate_from_string
from openpyxl.utils.exceptions import CellCoordinatesException

from app.documents.base import ApplyResult, Check, EditError, ImageResolver, sha256, theme_summary
from app.documents.preview import find_soffice

MAIN = "http://schemas.openxmlformats.org/spreadsheetml/2006/main"
REL = "http://schemas.openxmlformats.org/officeDocument/2006/relationships"
PKG_REL = "http://schemas.openxmlformats.org/package/2006/relationships"
CT = "http://schemas.openxmlformats.org/package/2006/content-types"
XDR = "http://schemas.openxmlformats.org/drawingml/2006/spreadsheetDrawing"
CHART = "http://schemas.openxmlformats.org/drawingml/2006/chart"
M = f"{{{MAIN}}}"

SOURCES_SHEET = "Sources"
STYLE_SHEET = "_styles"
OUTLINE_ROW_LIMIT = 300
MAX_COLUMN_WIDTH = 60
BUILTIN_DATE_FORMATS = {14, 15, 16, 17, 18, 19, 20, 21, 22, 45, 46, 47}
EXCEL_EPOCH = dt.date(1899, 12, 30)
RANGE_RE = re.compile(r"^(\$?)([A-Z]{1,3})(\$?)(\d+)(?::(\$?)([A-Z]{1,3})(\$?)(\d+))?$")
FULL_ROW_RE = re.compile(r"^(\$?)(\d+):(\$?)(\d+)$")


class _Package:
    def __init__(self, path: Path) -> None:
        self.parts: dict[str, bytes] = {}
        with zipfile.ZipFile(path) as archive:
            self.order = archive.namelist()
            for name in self.order:
                self.parts[name] = archive.read(name)
        self.trees: dict[str, Any] = {}

    def xml(self, name: str) -> Any:
        if name not in self.trees:
            self.trees[name] = etree.fromstring(self.parts[name])
        return self.trees[name]

    def save(self, path: Path) -> None:
        for name, tree in self.trees.items():
            # Parts that were only read keep their original bytes.
            original = self.parts.get(name)
            if original is not None and etree.tostring(tree, method="c14n") == etree.tostring(
                etree.fromstring(original), method="c14n"
            ):
                continue
            self.parts[name] = etree.tostring(tree, xml_declaration=True, encoding="UTF-8", standalone=True)
        with zipfile.ZipFile(path, "w", zipfile.ZIP_DEFLATED) as archive:
            for name in self.order:
                if name in self.parts:
                    archive.writestr(name, self.parts[name])
            for name in self.parts:
                if name not in self.order:
                    archive.writestr(name, self.parts[name])

    def remove(self, name: str) -> None:
        self.parts.pop(name, None)
        self.trees.pop(name, None)

    def rels_of(self, part: str) -> tuple[str, Any] | None:
        folder, _, leaf = part.rpartition("/")
        name = f"{folder}/_rels/{leaf}.rels"
        return (name, self.xml(name)) if name in self.parts else None

    def related(self, part: str, kind_suffix: str) -> list[str]:
        found = self.rels_of(part)
        if not found:
            return []
        folder = part.rpartition("/")[0]
        out = []
        for rel in found[1]:
            if rel.get("Type", "").endswith(kind_suffix) and rel.get("TargetMode") != "External":
                out.append(_resolve(folder, rel.get("Target")))
        return out


def _resolve(folder: str, target: str) -> str:
    if target.startswith("/"):
        return target.lstrip("/")
    parts = folder.split("/") if folder else []
    for bit in target.split("/"):
        if bit == "..":
            parts.pop()
        elif bit != ".":
            parts.append(bit)
    return "/".join(parts)


class _Book:
    def __init__(self, package: _Package) -> None:
        self.package = package
        self.path = "xl/workbook.xml"
        self.root = package.xml(self.path)
        self.touched_sheets: set[str] = set()
        self.touched_parts: set[str] = set()

    def sheets(self) -> dict[str, str]:
        rels = self.package.rels_of(self.path)[1]
        targets = {r.get("Id"): r.get("Target") for r in rels}
        out = {}
        for sheet in self.root.find(f"{M}sheets"):
            rid = sheet.get(f"{{{REL}}}id")
            out[sheet.get("name")] = _resolve("xl", targets[rid])
        return out

    def sheet(self, name: str) -> Any:
        sheets = self.sheets()
        if name not in sheets:
            raise EditError(f'No sheet called "{name}". Sheets: {", ".join(sheets)}')
        return self.package.xml(sheets[name])

    def date_styles(self) -> set[int]:
        if "xl/styles.xml" not in self.package.parts:
            return set()
        root = etree.fromstring(self.package.parts["xl/styles.xml"])
        custom = {}
        formats = root.find(f"{M}numFmts")
        if formats is not None:
            for fmt in formats:
                code = re.sub(r'"[^"]*"|\[[^\]]*\]', "", fmt.get("formatCode", "")).lower()
                custom[int(fmt.get("numFmtId"))] = any(c in code for c in "ymd") or "h:" in code
        out = set()
        xfs = root.find(f"{M}cellXfs")
        if xfs is not None:
            for index, xf in enumerate(xfs):
                num = int(xf.get("numFmtId", 0))
                if num in BUILTIN_DATE_FORMATS or custom.get(num):
                    out.add(index)
        return out


def _split_ref(ref: str) -> tuple[int, int]:
    # The sheet is a separate field, but "Budget!A9" is a natural thing to write.
    cell = ref.rpartition("!")[2].replace("$", "")
    try:
        letters, row = coordinate_from_string(cell)
    except (ValueError, CellCoordinatesException) as exc:
        raise EditError(f'"{ref}" is not a cell reference. Use the form A9.') from exc
    return column_index_from_string(letters), row


def _get_row(sheet: Any, number: int, create: bool = True) -> Any:
    data = sheet.find(f"{M}sheetData")
    for row in data:
        current = int(row.get("r"))
        if current == number:
            return row
        if current > number:
            if not create:
                return None
            new = etree.Element(f"{M}row", r=str(number))
            row.addprevious(new)
            return new
    if not create:
        return None
    return etree.SubElement(data, f"{M}row", r=str(number))


def _get_cell(row: Any, column: int, create: bool = True) -> Any:
    ref = f"{get_column_letter(column)}{row.get('r')}"
    for cell in row:
        current = _split_ref(cell.get("r"))[0]
        if current == column:
            return cell
        if current > column:
            if not create:
                return None
            new = etree.Element(f"{M}c", r=ref)
            cell.addprevious(new)
            return new
    if not create:
        return None
    return etree.SubElement(row, f"{M}c", r=ref)


def _write_value(cell: Any, value: Any, date_styles: set[int]) -> None:
    for child in list(cell):
        cell.remove(child)
    cell.attrib.pop("t", None)
    if value is None or value == "":
        return
    if isinstance(value, bool):
        cell.set("t", "b")
        etree.SubElement(cell, f"{M}v").text = "1" if value else "0"
    elif isinstance(value, (int, float)):
        etree.SubElement(cell, f"{M}v").text = repr(value) if isinstance(value, float) else str(value)
    elif isinstance(value, str) and value.startswith("=") and len(value) > 1:
        etree.SubElement(cell, f"{M}f").text = value[1:]
    else:
        text = str(value)
        if int(cell.get("s", 0)) in date_styles and re.fullmatch(r"\d{4}-\d{2}-\d{2}", text):
            serial = (dt.date.fromisoformat(text) - EXCEL_EPOCH).days
            etree.SubElement(cell, f"{M}v").text = str(serial)
            return
        cell.set("t", "inlineStr")
        inline = etree.SubElement(cell, f"{M}is")
        t = etree.SubElement(inline, f"{M}t")
        t.text = text
        t.set("{http://www.w3.org/XML/1998/namespace}space", "preserve")


def _expand_shared_formulas(sheet: Any) -> None:
    """Moving rows around a shared formula breaks it, so spell each one out first."""
    masters: dict[str, tuple[str, str]] = {}
    for cell in sheet.iter(f"{M}c"):
        f = cell.find(f"{M}f")
        if f is not None and f.get("t") == "shared" and f.text:
            masters[f.get("si")] = (f.text, cell.get("r"))
    for cell in sheet.iter(f"{M}c"):
        f = cell.find(f"{M}f")
        if f is None or f.get("t") != "shared":
            continue
        text, origin = masters[f.get("si")]
        if not f.text:
            f.text = Translator("=" + text, origin=origin).translate_formula(cell.get("r"))[1:]
        for attr in ("t", "ref", "si"):
            f.attrib.pop(attr, None)


def _shift_row(row: int, at: int, delta: int) -> int | None:
    if delta > 0:
        return row + delta if row >= at else row
    gone_until = at - delta
    if at <= row < gone_until:
        return None
    return row + delta if row >= gone_until else row


def _shift_range(text: str, at: int, delta: int, extend_abutting: bool) -> str | None:
    match = RANGE_RE.match(text)
    if match:
        d1, c1, d2, r1, d3, c2, d4, r2 = match.groups()
        first = _shift_row(int(r1), at, delta)
        if r2 is None:
            return None if first is None else f"{d1}{c1}{d2}{first}"
        last = _shift_row(int(r2), at, delta)
        if delta > 0 and extend_abutting and int(r2) == at - 1 and int(r1) < int(r2):
            last = int(r2) + delta
        if first is None and last is None:
            return None
        # A range that only partly survives a delete shrinks to what's left.
        first = at if first is None else first
        last = at - 1 if last is None else last
        return f"{d1}{c1}{d2}{first}:{d3}{c2}{d4}{last}"
    match = FULL_ROW_RE.match(text)
    if match:
        d1, r1, d2, r2 = match.groups()
        first = _shift_row(int(r1), at, delta)
        last = _shift_row(int(r2), at, delta)
        if first is None or last is None:
            return None
        return f"{d1}{first}:{d2}{last}"
    return text


def _shift_operand(operand: str, own_sheet: str, edited: str, at: int, delta: int, extend: bool) -> str:
    sheet_part, bang, ref = operand.rpartition("!")
    target = sheet_part.strip("'").replace("''", "'") if bang else own_sheet
    if target != edited:
        return operand
    shifted = _shift_range(ref, at, delta, extend)
    return f"{sheet_part}{bang}{shifted}" if shifted is not None else "#REF!"


def _shift_formula(formula: str, own_sheet: str, edited: str, at: int, delta: int, extend: bool) -> str:
    try:
        tokens = Tokenizer("=" + formula).items
    except Exception:
        return formula
    out = []
    for token in tokens:
        if token.type == "OPERAND" and token.subtype == "RANGE":
            out.append(_shift_operand(token.value, own_sheet, edited, at, delta, extend))
        else:
            out.append(token.value)
    return "".join(out)


def _shift_sqref(value: str, at: int, delta: int) -> str:
    kept = []
    for piece in value.split():
        shifted = _shift_range(piece, at, delta, True)
        if shifted is not None:
            kept.append(shifted)
    return " ".join(kept)


def _shift_sheet_structure(sheet: Any, sheet_name: str, at: int, delta: int) -> None:
    for tag, attr in (("mergeCell", "ref"), ("hyperlink", "ref"), ("autoFilter", "ref"), ("dataValidation", "sqref"), ("conditionalFormatting", "sqref")):
        for node in list(sheet.iter(f"{M}{tag}")):
            shifted = _shift_sqref(node.get(attr, ""), at, delta)
            if shifted:
                node.set(attr, shifted)
            else:
                node.getparent().remove(node)
    for node in sheet.iter(f"{M}formula", f"{M}formula1", f"{M}formula2"):
        if node.text:
            node.text = _shift_formula(node.text, sheet_name, sheet_name, at, delta, True)
    merges = sheet.find(f"{M}mergeCells")
    if merges is not None:
        merges.set("count", str(len(merges)))


def _shift_everything(book: _Book, edited: str, at: int, delta: int) -> None:
    package = book.package
    sheets = book.sheets()

    for name, part in sheets.items():
        tree = package.xml(part)
        _expand_shared_formulas(tree)
        for cell in tree.iter(f"{M}c"):
            f = cell.find(f"{M}f")
            if f is None or not f.text:
                continue
            own_row = _split_ref(cell.get("r"))[1]
            # A total under a SUM should pick up rows added directly above it.
            extend = name == edited and own_row >= at
            updated = _shift_formula(f.text, name, edited, at, delta, extend)
            if updated != f.text:
                f.text = updated
                book.touched_sheets.add(name)

    names = book.root.find(f"{M}definedNames")
    if names is not None:
        for defined in names:
            if defined.text:
                defined.text = _shift_formula(defined.text, "", edited, at, delta, True)

    part = sheets[edited]
    _shift_sheet_structure(package.xml(part), edited, at, delta)

    for table_part in package.related(part, "/table"):
        table = package.xml(table_part)
        shifted = _shift_range(table.get("ref"), at, delta, True)
        table.set("ref", shifted or table.get("ref"))
        auto = table.find(f"{M}autoFilter")
        if auto is not None:
            auto.set("ref", _shift_range(auto.get("ref"), at, delta, True) or auto.get("ref"))
        book.touched_parts.add(table_part)

    for drawing_part in package.related(part, "/drawing"):
        drawing = package.xml(drawing_part)
        for row_node in drawing.iter(f"{{{XDR}}}row"):
            # Anchors are zero-based.
            moved = _shift_row(int(row_node.text) + 1, at, delta)
            if moved is not None:
                row_node.text = str(moved - 1)
        book.touched_parts.add(drawing_part)
        for chart_part in package.related(drawing_part, "/chart"):
            chart = package.xml(chart_part)
            for formula in chart.iter(f"{{{CHART}}}f"):
                if formula.text:
                    formula.text = _shift_formula(formula.text, "", edited, at, delta, True)
            book.touched_parts.add(chart_part)


def _renumber_rows(sheet: Any, at: int, delta: int) -> None:
    data = sheet.find(f"{M}sheetData")
    for row in list(data):
        moved = _shift_row(int(row.get("r")), at, delta)
        if moved is None:
            data.remove(row)
            continue
        if moved != int(row.get("r")):
            row.set("r", str(moved))
            for cell in row:
                column = _split_ref(cell.get("r"))[0]
                cell.set("r", f"{get_column_letter(column)}{moved}")


def _refresh_dimension(sheet: Any) -> None:
    dimension = sheet.find(f"{M}dimension")
    if dimension is None:
        return
    columns, rows = [], []
    for cell in sheet.iter(f"{M}c"):
        column, row = _split_ref(cell.get("r"))
        columns.append(column)
        rows.append(row)
    if rows:
        dimension.set("ref", f"{get_column_letter(min(columns))}{min(rows)}:{get_column_letter(max(columns))}{max(rows)}")


def _drop_calc_chain(package: _Package) -> None:
    """Excel reports a corrupt file if the calc chain names a cell with no formula."""
    name = "xl/calcChain.xml"
    if name not in package.parts:
        return
    package.remove(name)
    rels = package.xml("xl/_rels/workbook.xml.rels")
    for rel in list(rels):
        if rel.get("Type", "").endswith("/calcChain"):
            rels.remove(rel)
    types = package.xml("[Content_Types].xml")
    for node in list(types):
        if node.get("PartName") == "/xl/calcChain.xml":
            types.remove(node)


def _mark_for_recalc(book: _Book) -> None:
    calc = book.root.find(f"{M}calcPr")
    if calc is None:
        calc = etree.Element(f"{M}calcPr")
        for tag in ("definedNames", "externalReferences", "functionGroups", "sheets"):
            anchor = book.root.find(f"{M}{tag}")
            if anchor is not None:
                anchor.addnext(calc)
                break
    calc.set("fullCalcOnLoad", "1")


def _clear_cached_values(book: _Book) -> None:
    for part in book.sheets().values():
        for cell in book.package.xml(part).iter(f"{M}c"):
            if cell.find(f"{M}f") is not None:
                value = cell.find(f"{M}v")
                if value is not None:
                    cell.remove(value)
                cell.attrib.pop("t", None)


def _inject_calculated_values(dest: Path) -> None:
    """Fill in cached results so previews and non-Excel readers aren't left with blanks.

    LibreOffice does the arithmetic on a throwaway copy. Only the values come
    back, never its rewritten file.
    """
    soffice = find_soffice()
    if soffice is None:
        return
    with tempfile.TemporaryDirectory() as tmp:
        tmp_path = Path(tmp)
        out = tmp_path / "out"
        out.mkdir()
        try:
            subprocess.run(
                [
                    soffice,
                    f"-env:UserInstallation=file:///{(tmp_path / 'profile').as_posix()}",
                    "--headless",
                    "--convert-to",
                    "xlsx:Calc MS Excel 2007 XML",
                    "--outdir",
                    str(out),
                    str(dest),
                ],
                check=True,
                capture_output=True,
                timeout=120,
            )
        except (subprocess.SubprocessError, OSError):
            return
        converted = out / dest.name
        if not converted.exists():
            return
        values = load_workbook(converted, data_only=True)

        package = _Package(dest)
        book = _Book(package)
        for name, part in book.sheets().items():
            if name not in values.sheetnames:
                continue
            sheet_values = values[name]
            for cell in package.xml(part).iter(f"{M}c"):
                if cell.find(f"{M}f") is None:
                    continue
                result = sheet_values[cell.get("r")].value
                if result is None:
                    continue
                node = etree.SubElement(cell, f"{M}v")
                if isinstance(result, bool):
                    cell.set("t", "b")
                    node.text = "1" if result else "0"
                elif isinstance(result, (int, float)):
                    node.text = repr(result)
                elif isinstance(result, (dt.datetime, dt.date)):
                    day = result.date() if isinstance(result, dt.datetime) else result
                    node.text = str((day - EXCEL_EPOCH).days)
                elif isinstance(result, str) and result.startswith("#"):
                    cell.set("t", "e")
                    node.text = result
                else:
                    cell.set("t", "str")
                    node.text = str(result)
        package.save(dest)


class XlsxEngine:
    extension = "xlsx"

    def analyse(self, path: Path) -> dict[str, Any]:
        wb = load_workbook(path)
        fonts: dict[str, int] = {}
        fills: dict[str, int] = {}
        sheets = []
        for ws in wb.worksheets:
            if ws.title == STYLE_SHEET:
                continue
            formats: dict[str, int] = {}
            header_style = None
            formulas = 0
            for row in ws.iter_rows():
                bold_cells = [c for c in row if c.value is not None and c.font.b]
                # A lone bold cell is usually a title. A header row has several.
                if header_style is None and len(bold_cells) >= 2:
                    first = bold_cells[0]
                    rgb = first.fill.fgColor.rgb if first.fill.fill_type else None
                    header_style = {
                        "row": first.row,
                        "font": first.font.name,
                        "size": first.font.sz,
                        "fill": "#" + rgb[-6:] if isinstance(rgb, str) else None,
                    }
                for cell in row:
                    if cell.value is None and not cell.has_style:
                        continue
                    if isinstance(cell.value, str) and cell.value.startswith("="):
                        formulas += 1
                    if cell.has_style:
                        key = f"{cell.font.name} {cell.font.sz}"
                        fonts[key] = fonts.get(key, 0) + 1
                        rgb = cell.fill.fgColor.rgb if cell.fill.fill_type else None
                        if isinstance(rgb, str):
                            fills["#" + rgb[-6:]] = fills.get("#" + rgb[-6:], 0) + 1
                        if cell.number_format != "General":
                            formats[cell.number_format] = formats.get(cell.number_format, 0) + 1
            sheets.append(
                {
                    "name": ws.title,
                    "state": ws.sheet_state,
                    "dimensions": ws.dimensions,
                    "freeze_panes": ws.freeze_panes,
                    "column_widths": {k: round(v.width, 1) for k, v in ws.column_dimensions.items() if v.width},
                    "merged_cells": [str(r) for r in ws.merged_cells.ranges],
                    "conditional_formats": [str(r.sqref) for r in ws.conditional_formatting],
                    "data_validations": [str(d.sqref) for d in ws.data_validations.dataValidation]
                    if ws.data_validations
                    else [],
                    "tables": {name: table.ref for name, table in ws.tables.items()},
                    "number_formats": formats,
                    "formula_cells": formulas,
                    "header": header_style,
                    "page_setup": {
                        "orientation": ws.page_setup.orientation,
                        "fit_to_width": ws.page_setup.fitToWidth,
                        "paper_size": ws.page_setup.paperSize,
                    },
                    "print_header": ws.oddHeader.center.text or ws.oddHeader.left.text or ws.oddHeader.right.text,
                    "print_footer": ws.oddFooter.center.text or ws.oddFooter.left.text or ws.oddFooter.right.text,
                }
            )

        with zipfile.ZipFile(path) as package:
            names = package.namelist()
            theme = next((n for n in names if n.startswith("xl/theme/") and n.endswith(".xml")), None)
            theme_info = theme_summary(package.read(theme)) if theme else None

        return {
            "format": "xlsx",
            "theme": theme_info,
            "fonts": fonts,
            "fills": fills,
            "named_ranges": sorted(wb.defined_names.keys()),
            "sheets": sheets,
            "objects": {
                "charts": sum(1 for n in names if re.fullmatch(r"xl/charts/chart\d+\.xml", n)),
                "images": sum(1 for n in names if n.startswith("xl/media/")),
                "pivot_tables": sum(1 for n in names if re.fullmatch(r"xl/pivotTables/pivotTable\d+\.xml", n)),
            },
            "stats": {"sheets": len(sheets), "formula_cells": sum(s["formula_cells"] for s in sheets)},
        }

    def outline(self, path: Path) -> list[dict[str, Any]]:
        formulas = load_workbook(path)
        values = load_workbook(path, data_only=True)
        items = []
        for ws in formulas.worksheets:
            if ws.title == STYLE_SHEET:
                continue
            computed = values[ws.title]
            rows = []
            total_rows = 0
            for row in ws.iter_rows():
                cells = []
                for cell in row:
                    if cell.value is None:
                        continue
                    entry = {"ref": cell.coordinate, "value": _plain(computed[cell.coordinate].value)}
                    if isinstance(cell.value, str) and cell.value.startswith("="):
                        entry["formula"] = cell.value
                    cells.append(entry)
                if cells:
                    total_rows += 1
                    if len(rows) < OUTLINE_ROW_LIMIT:
                        rows.append({"row": row[0].row, "cells": cells})
            items.append(
                {
                    "id": ws.title,
                    "kind": "sheet",
                    "rows": rows,
                    "rows_total": total_rows,
                    "rows_omitted": max(total_rows - OUTLINE_ROW_LIMIT, 0),
                }
            )
        return items

    def apply(self, src: Path, ops: list[Any], dest: Path, resolve_image: ImageResolver | None = None) -> ApplyResult:
        package = _Package(src)
        book = _Book(package)
        result = ApplyResult()
        dates = book.date_styles()

        for op in ops:
            handler: Callable[..., None] | None = getattr(self, f"_op_{op.op}", None)
            if handler is None:
                raise EditError(f"{op.op} is not an XLSX operation")
            handler(book, op, result, dates)

        for name in book.touched_sheets:
            _refresh_dimension(book.sheet(name))
        _drop_calc_chain(package)
        _mark_for_recalc(book)
        _clear_cached_values(book)
        result.touched_parts |= book.touched_parts
        package.save(dest)
        _inject_calculated_values(dest)
        return result

    def _op_set_cell(self, book, op, result, dates) -> None:
        sheet = book.sheet(op.sheet)
        _expand_shared_formulas(sheet)
        column, row_number = _split_ref(op.ref)
        row = _get_row(sheet, row_number)
        cell = _get_cell(row, column)
        _write_value(cell, op.value, dates)
        book.touched_sheets.add(op.sheet)
        result.touched.add(f"{op.sheet}!{cell.get('r')}")
        result.log.append(f"Set {op.sheet}!{op.ref}")

    def _op_insert_rows(self, book, op, result, dates) -> None:
        sheet = book.sheet(op.sheet)
        if op.before_row < 1:
            raise EditError("before_row starts at 1")
        count = len(op.rows)
        if not count:
            return
        _expand_shared_formulas(sheet)
        proto_number = op.before_row - 1
        proto = _get_row(sheet, proto_number, create=False) if proto_number >= 1 else None
        proto_copy = copy.deepcopy(proto) if proto is not None else None

        _renumber_rows(sheet, op.before_row, count)
        _shift_everything(book, op.sheet, op.before_row, count)

        for offset, values in enumerate(op.rows):
            number = op.before_row + offset
            row = _get_row(sheet, number)
            if proto_copy is not None:
                for attr in ("ht", "customHeight", "s", "customFormat", "spans"):
                    if proto_copy.get(attr) is not None:
                        row.set(attr, proto_copy.get(attr))
            columns = _columns_for(proto_copy, values)
            for column in columns:
                value = values[column - 1] if column - 1 < len(values) else None
                cell = _get_cell(row, column)
                source = _get_cell(proto_copy, column, create=False) if proto_copy is not None else None
                if source is not None and source.get("s"):
                    cell.set("s", source.get("s"))
                if value is None and source is not None and source.find(f"{M}f") is not None:
                    translated = Translator("=" + source.find(f"{M}f").text, origin=source.get("r")).translate_formula(cell.get("r"))
                    _write_value(cell, translated, dates)
                else:
                    _write_value(cell, value, dates)

        book.touched_sheets.add(op.sheet)
        result.row_shifts.setdefault(op.sheet, []).append((op.before_row, count))
        result.touched.add(f"{op.sheet}!formulas")
        result.log.append(f"Inserted {count} row(s) at {op.sheet} row {op.before_row}")

    def _op_delete_rows(self, book, op, result, dates) -> None:
        sheet = book.sheet(op.sheet)
        if op.count < 1 or op.start_row < 1:
            raise EditError("delete_rows needs a start_row and count of at least 1")
        _expand_shared_formulas(sheet)
        _renumber_rows(sheet, op.start_row, -op.count)
        _shift_everything(book, op.sheet, op.start_row, -op.count)
        book.touched_sheets.add(op.sheet)
        result.row_shifts.setdefault(op.sheet, []).append((op.start_row, -op.count))
        result.touched.add(f"{op.sheet}!formulas")
        result.log.append(f"Deleted {op.count} row(s) at {op.sheet} row {op.start_row}")

    def _op_write_table(self, book, op, result, dates) -> None:
        sheet = book.sheet(op.sheet)
        _expand_shared_formulas(sheet)
        start_col, start_row = _split_ref(op.start)
        header_styles, body_styles = _table_styles(book, sheet)
        for offset, title in enumerate(op.headers):
            self._write_styled(sheet, start_row, start_col + offset, title, header_styles, offset, dates, result, op.sheet)
        for r, values in enumerate(op.rows, start=1):
            for offset, value in enumerate(values):
                self._write_styled(sheet, start_row + r, start_col + offset, value, body_styles, offset, dates, result, op.sheet)
        _fit_columns(sheet)
        book.touched_sheets.add(op.sheet)
        result.log.append(f"Wrote a {len(op.rows)}-row table to {op.sheet}!{op.start}")

    def _write_styled(self, sheet, row_number, column, value, styles, offset, dates, result, sheet_name) -> None:
        row = _get_row(sheet, row_number)
        cell = _get_cell(row, column)
        if cell.get("s") is None and styles:
            cell.set("s", styles[min(offset, len(styles) - 1)])
        _write_value(cell, value, dates)
        result.touched.add(f"{sheet_name}!{cell.get('r')}")

    def _op_add_sheet(self, book, op, result, dates) -> None:
        if op.name in book.sheets():
            raise EditError(f'A sheet called "{op.name}" already exists')
        _add_sheet(book, op.name)
        result.log.append(f"Added sheet {op.name}")

    def _op_find_replace(self, book, op, result, dates) -> None:
        total = 0
        package = book.package
        if "xl/sharedStrings.xml" in package.parts:
            root = package.xml("xl/sharedStrings.xml")
            for t in root.iter(f"{M}t"):
                if t.text and op.find in t.text:
                    total += t.text.count(op.find)
                    t.text = t.text.replace(op.find, op.replace)
            if total:
                book.touched_parts.add("xl/sharedStrings.xml")
        for name, part in book.sheets().items():
            tree = package.xml(part)
            for t in tree.iter(f"{M}t"):
                if t.text and op.find in t.text:
                    total += t.text.count(op.find)
                    t.text = t.text.replace(op.find, op.replace)
                    book.touched_sheets.add(name)
            for tag in ("oddHeader", "oddFooter", "evenHeader", "evenFooter", "firstHeader", "firstFooter"):
                for node in tree.iter(f"{M}{tag}"):
                    if node.text and op.find in node.text:
                        total += node.text.count(op.find)
                        node.text = node.text.replace(op.find, op.replace)
                        book.touched_sheets.add(name)
        if not total:
            raise EditError(f'"{op.find}" does not appear in the workbook')
        result.touched.add("*text*")
        result.log.append(f'Replaced "{op.find}" {total} time(s)')

    def _op_set_sources(self, book, op, result, dates) -> None:
        if not op.sources:
            return
        if SOURCES_SHEET not in book.sheets():
            _add_sheet(book, SOURCES_SHEET)
        sheet = book.sheet(SOURCES_SHEET)
        data = sheet.find(f"{M}sheetData")
        for row in list(data):
            data.remove(row)
        first_sheet = next(iter(book.sheets()))
        header_styles, _ = _table_styles(book, book.sheet(first_sheet))
        header_style = header_styles[0] if header_styles else None
        rows = [["Key", "Source", "Where", "URL", "Retrieved"]] + [
            [s.key, s.title, s.locator, s.url, s.retrieved_at] for s in op.sources
        ]
        for r, values in enumerate(rows, start=1):
            row = _get_row(sheet, r)
            for c, value in enumerate(values, start=1):
                cell = _get_cell(row, c)
                if r == 1 and header_style:
                    cell.set("s", header_style)
                _write_value(cell, value, dates)
        _fit_columns(sheet)
        _refresh_dimension(sheet)
        result.log.append(f"Listed {len(op.sources)} source(s) on the {SOURCES_SHEET} sheet")

    def preservation_checks(self, src: Path, dest: Path, result: ApplyResult) -> list[Check]:
        allowed_prefixes = ("xl/worksheets/", "docProps/")
        allowed_exact = {
            "xl/workbook.xml",
            "xl/_rels/workbook.xml.rels",
            "[Content_Types].xml",
            "xl/calcChain.xml",
        } | result.touched_parts
        changed = []
        missing = []
        with zipfile.ZipFile(src) as before, zipfile.ZipFile(dest) as after:
            names_after = set(after.namelist())
            for name in before.namelist():
                if name in allowed_exact or name.startswith(allowed_prefixes):
                    if name not in names_after and name != "xl/calcChain.xml":
                        missing.append(name)
                    continue
                if name not in names_after:
                    missing.append(name)
                elif sha256(before.read(name)) != sha256(after.read(name)):
                    changed.append(name)
            charts_before = sum(1 for n in before.namelist() if n.startswith("xl/charts/chart"))
            charts_after = sum(1 for n in names_after if n.startswith("xl/charts/chart"))

        checks = [
            Check(
                "Styles, theme, charts and images unchanged",
                not changed,
                "Changed: " + ", ".join(changed) if changed else "Compared byte for byte",
            ),
            Check("No parts dropped", not missing, "Missing: " + ", ".join(missing) if missing else ""),
            Check("Charts kept", charts_after >= charts_before, f"{charts_before} chart(s) in the original"),
        ]

        src_book = _Book(_Package(src))
        dest_book = _Book(_Package(dest))
        problems: list[str] = []
        for name, part in src_book.sheets().items():
            if name not in dest_book.sheets():
                problems.append(f'Sheet "{name}" is gone')
                continue
            problems.extend(_compare_sheet(name, src_book.package.xml(part), dest_book.sheet(name), result))
        checks.append(
            Check(
                "Untouched cells keep their values and styles",
                not problems,
                "; ".join(problems[:5]) if problems else f"{len(src_book.sheets())} sheet(s) compared",
            )
        )

        errors = formula_errors(dest)
        checks.append(
            Check(
                "No formula errors",
                not errors,
                "; ".join(errors[:5]) if errors else "",
            )
        )
        return checks


def _plain(value: Any) -> Any:
    if isinstance(value, (dt.datetime, dt.date)):
        return value.isoformat()
    return value


def _columns_for(proto: Any, values: list[Any]) -> list[int]:
    columns = set(range(1, len(values) + 1))
    if proto is not None:
        columns |= {_split_ref(c.get("r"))[0] for c in proto}
    return sorted(columns)


def _table_styles(book: _Book, sheet: Any) -> tuple[list[str], list[str]]:
    """Find a header row and the data row under it, for their cell styles.

    A new sheet has nothing to copy from, so the rest of the workbook is tried too.
    """
    others = [book.package.xml(part) for part in book.sheets().values()]
    for candidate in [sheet, *others]:
        rows = list(candidate.find(f"{M}sheetData"))
        for index, row in enumerate(rows):
            styled = [c for c in row if c.get("s") and (c.find(f"{M}v") is not None or c.find(f"{M}is") is not None)]
            if len(styled) >= 2 and all(c.get("t") in ("s", "inlineStr") for c in styled):
                header = [c.get("s") for c in row if c.get("s")]
                body: list[str] = []
                if index + 1 < len(rows):
                    body = [c.get("s") for c in rows[index + 1] if c.get("s")]
                return header, body
    return [], []


def _add_sheet(book: _Book, name: str) -> None:
    package = book.package
    existing = {n for n in package.parts if re.fullmatch(r"xl/worksheets/sheet\d+\.xml", n)}
    number = 1
    while f"xl/worksheets/sheet{number}.xml" in existing:
        number += 1
    part = f"xl/worksheets/sheet{number}.xml"
    package.parts[part] = (
        f'<?xml version="1.0" encoding="UTF-8" standalone="yes"?>'
        f'<worksheet xmlns="{MAIN}"><sheetPr><pageSetUpPr fitToPage="1"/></sheetPr><sheetData/>'
        f'<pageSetup orientation="landscape" fitToWidth="1" fitToHeight="0"/></worksheet>'
    ).encode()

    types = package.xml("[Content_Types].xml")
    etree.SubElement(
        types,
        f"{{{CT}}}Override",
        PartName=f"/{part}",
        ContentType="application/vnd.openxmlformats-officedocument.spreadsheetml.worksheet+xml",
    )

    rels = package.xml("xl/_rels/workbook.xml.rels")
    rid_numbers = [int(m[1]) for r in rels if (m := re.fullmatch(r"rId(\d+)", r.get("Id", "")))]
    rid = f"rId{max(rid_numbers, default=0) + 1}"
    etree.SubElement(
        rels,
        f"{{{PKG_REL}}}Relationship",
        Id=rid,
        Type=f"{REL}/worksheet",
        Target=f"worksheets/sheet{number}.xml",
    )

    sheets = book.root.find(f"{M}sheets")
    sheet_ids = [int(s.get("sheetId")) for s in sheets]
    entry = etree.SubElement(sheets, f"{M}sheet", name=name, sheetId=str(max(sheet_ids, default=0) + 1))
    entry.set(f"{{{REL}}}id", rid)


def _fit_columns(sheet: Any) -> None:
    """Size a sheet's columns to its text, unless it already has widths."""
    if sheet.find(f"{M}cols") is not None:
        return
    longest: dict[int, int] = {}
    for cell in sheet.iter(f"{M}c"):
        column = _split_ref(cell.get("r"))[0]
        text = "".join(cell.find(f"{M}is").itertext()) if cell.find(f"{M}is") is not None else ""
        value = cell.find(f"{M}v")
        length = len(text) or len(value.text or "") if value is not None or text else 0
        longest[column] = max(longest.get(column, 0), length)
    if not longest:
        return
    cols = etree.Element(f"{M}cols")
    for column, length in sorted(longest.items()):
        width = str(min(max(length * 1.15 + 2, 10), MAX_COLUMN_WIDTH))
        etree.SubElement(cols, f"{M}col", min=str(column), max=str(column), width=width, customWidth="1")
    sheet.find(f"{M}sheetData").addprevious(cols)


def _original_row(row: int, shifts: list[tuple[int, int]]) -> int | None:
    """Where a row of the source ends up after the row edits, in order."""
    current: int | None = row
    for at, delta in shifts:
        if current is None:
            return None
        current = _shift_row(current, at, delta)
    return current


def _cell_map(sheet: Any) -> dict[str, tuple[str | None, str | None, str | None, bool]]:
    out = {}
    for cell in sheet.iter(f"{M}c"):
        value = cell.find(f"{M}v")
        inline = cell.find(f"{M}is")
        text = value.text if value is not None else ("".join(inline.itertext()) if inline is not None else None)
        out[cell.get("r")] = (cell.get("s"), cell.get("t"), text, cell.find(f"{M}f") is not None)
    return out


def _compare_sheet(name: str, before: Any, after: Any, result: ApplyResult) -> Iterable[str]:
    problems = []
    shifts = result.row_shifts.get(name, [])
    text_changed = "*text*" in result.touched
    after_cells = _cell_map(after)
    for ref, (style, kind, text, is_formula) in _cell_map(before).items():
        column, row = _split_ref(ref)
        moved = _original_row(row, shifts)
        if moved is None:
            continue
        new_ref = f"{get_column_letter(column)}{moved}"
        if f"{name}!{new_ref}" in result.touched or f"{name}!{ref}" in result.touched:
            continue
        if new_ref not in after_cells:
            if text is not None:
                problems.append(f"{name}!{ref} disappeared")
            continue
        new_style, new_kind, new_text, new_is_formula = after_cells[new_ref]
        if new_style != style:
            problems.append(f"{name}!{ref} lost its style")
        elif is_formula != new_is_formula:
            problems.append(f"{name}!{ref} changed between formula and value")
        elif not is_formula and not text_changed and (kind, text) != (new_kind, new_text):
            problems.append(f"{name}!{ref} changed value")

    for tag in ("cols", "sheetViews", "sheetFormatPr", "pageMargins", "pageSetup", "headerFooter"):
        old = before.find(f"{M}{tag}")
        new = after.find(f"{M}{tag}")
        if old is None and new is None:
            continue
        if text_changed and tag == "headerFooter":
            continue
        if old is None or new is None or etree.tostring(old, method="c14n") != etree.tostring(new, method="c14n"):
            problems.append(f"{name}: {tag} changed")
    for tag in ("mergeCell", "conditionalFormatting", "dataValidation"):
        if not shifts and sum(1 for _ in before.iter(f"{M}{tag}")) != sum(1 for _ in after.iter(f"{M}{tag}")):
            problems.append(f"{name}: {tag} count changed")
    return problems


def formula_errors(path: Path) -> list[str]:
    errors = []
    values = load_workbook(path, data_only=True)
    for ws in values.worksheets:
        for row in ws.iter_rows():
            for cell in row:
                if isinstance(cell.value, str) and re.fullmatch(r"#(REF|NAME\?|VALUE|DIV/0|N/A|NUM|NULL)!?", cell.value):
                    errors.append(f"{ws.title}!{cell.coordinate} is {cell.value}")
    return errors
