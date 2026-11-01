import copy
import re
import zipfile
from collections import Counter
from pathlib import Path
from typing import Any, Iterable

from docx import Document
from docx.opc.constants import RELATIONSHIP_TYPE as RT
from docx.oxml import OxmlElement
from docx.oxml.ns import qn
from docx.styles import BabelFish

from app.documents.base import (
    ApplyResult,
    Check,
    EditError,
    ImageResolver,
    element_hash,
    emu_to_mm,
    part_fingerprints,
    sha256,
    strip_list_marker,
    theme_summary,
    twips_to_mm,
)
from app.documents.ops import Block

WP = "{http://schemas.openxmlformats.org/drawingml/2006/wordprocessingDrawing}"
A = "{http://schemas.openxmlformats.org/drawingml/2006/main}"
R = "{http://schemas.openxmlformats.org/officeDocument/2006/relationships}"

ALWAYS_REWRITTEN = {"word/document.xml", "docProps/core.xml", "[Content_Types].xml", "word/_rels/document.xml.rels"}
SOURCES_HEADING = "Sources"


def para_text(p: Any) -> str:
    parts = []
    for node in p.iter(qn("w:t"), qn("w:tab"), qn("w:br")):
        if node.tag == qn("w:t"):
            parts.append(node.text or "")
        elif node.getparent().tag == qn("w:r"):  # w:tab also appears in w:tabs
            parts.append("\t" if node.tag == qn("w:tab") else "\n")
    return "".join(parts)


def is_text_only_run(run: Any) -> bool:
    allowed = {qn("w:rPr"), qn("w:t"), qn("w:tab"), qn("w:br"), qn("w:cr"), qn("w:noBreakHyphen")}
    return all(child.tag in allowed for child in run)


def set_para_text(p: Any, text: str) -> None:
    """Swap the text, keep the first run's formatting and anything that isn't text.

    `**bold**` in the text becomes a bold copy of that run, so a writer can still
    produce a bold lead-in.
    """
    runs = [r for r in p.findall(qn("w:r")) if is_text_only_run(r)]
    for link in p.findall(qn("w:hyperlink")):
        runs.extend(link.findall(qn("w:r")))
    with_text = [r for r in runs if r.find(qn("w:t")) is not None]

    template = None
    if with_text:
        template = copy.deepcopy(with_text[0].find(qn("w:rPr")))
        anchor = with_text[0]
        insert_at = list(anchor.getparent()).index(anchor)
        parent = anchor.getparent()
        if parent.tag == qn("w:hyperlink"):
            insert_at = list(p).index(parent)
            parent = p
    else:
        mark = p.find(f"{qn('w:pPr')}/{qn('w:rPr')}")
        template = copy.deepcopy(mark) if mark is not None else None
        parent = p
        insert_at = 1 if p.find(qn("w:pPr")) is not None else 0

    for run in runs:
        run.getparent().remove(run)
    for link in p.findall(qn("w:hyperlink")):
        if not len(link):
            p.remove(link)

    for offset, (chunk, bold) in enumerate(_bold_chunks(text)):
        parent.insert(insert_at + offset, _make_run(chunk, template, bold))


def _bold_chunks(text: str) -> list[tuple[str, bool]]:
    pieces = re.split(r"\*\*(.+?)\*\*", text)
    return [(piece, i % 2 == 1) for i, piece in enumerate(pieces) if piece] or [("", False)]


def _make_run(text: str, rpr: Any, bold: bool) -> Any:
    run = OxmlElement("w:r")
    if rpr is not None:
        run.append(copy.deepcopy(rpr))
    if bold:
        props = run.find(qn("w:rPr"))
        if props is None:
            props = OxmlElement("w:rPr")
            run.insert(0, props)
        if props.find(qn("w:b")) is None:
            # w:rStyle and w:rFonts are the only rPr children allowed ahead of w:b.
            position = sum(1 for c in props if c.tag in (qn("w:rStyle"), qn("w:rFonts")))
            props.insert(position, OxmlElement("w:b"))
    t = OxmlElement("w:t")
    t.text = text
    t.set("{http://www.w3.org/XML/1998/namespace}space", "preserve")
    run.append(t)
    return run


def find_replace_in_paragraph(p: Any, find: str, replace: str) -> int:
    """Replace across run boundaries. Whichever run the match starts in keeps its formatting."""
    nodes = list(p.iter(qn("w:t")))
    joined = "".join(n.text or "" for n in nodes)
    if find not in joined:
        return 0

    count = 0
    position = joined.rfind(find)
    while position != -1:
        count += 1
        end = position + len(find)
        cursor = 0
        placed = False
        for node in nodes:
            text = node.text or ""
            start, stop = cursor, cursor + len(text)
            cursor = stop
            if stop <= position or start >= end:
                continue
            left = text[: max(position - start, 0)]
            right = text[max(end - start, 0):] if end < stop else ""
            node.text = left + (replace if not placed else "") + right
            node.set("{http://www.w3.org/XML/1998/namespace}space", "preserve")
            placed = True
        joined = "".join(n.text or "" for n in nodes)
        position = joined.rfind(find, 0, position)
    return count


class DocxEngine:
    extension = "docx"

    def analyse(self, path: Path) -> dict[str, Any]:
        doc = Document(str(path))
        body = list(_body_items(doc))
        paragraphs = [el for _, el in body if el.tag == qn("w:p")]

        style_counts: Counter[str] = Counter()
        fonts: Counter[str] = Counter()
        colors: Counter[str] = Counter()
        headings = []
        for item_id, el in body:
            if el.tag != qn("w:p"):
                continue
            name = _style_name(doc, el)
            style_counts[name] += 1
            level = _heading_level(doc, el)
            if level is not None and para_text(el).strip():
                headings.append({"id": item_id, "level": level, "text": para_text(el).strip()})
            for run in el.iter(qn("w:r")):
                props = run.find(qn("w:rPr"))
                if props is None:
                    continue
                font = props.find(qn("w:rFonts"))
                if font is not None and font.get(qn("w:ascii")):
                    fonts[font.get(qn("w:ascii"))] += 1
                color = props.find(qn("w:color"))
                if color is not None and color.get(qn("w:val"), "auto") != "auto":
                    colors["#" + color.get(qn("w:val")).upper()] += 1

        with zipfile.ZipFile(path) as package:
            names = package.namelist()
            theme = next((n for n in names if n.startswith("word/theme/") and n.endswith(".xml")), None)
            theme_info = theme_summary(package.read(theme)) if theme else None

        return {
            "format": "docx",
            "page_setup": [_section_info(s) for s in doc.sections],
            "theme": theme_info,
            "defaults": _doc_defaults(doc),
            "styles_in_use": [
                _style_info(doc, name, count) for name, count in style_counts.most_common()
            ],
            "direct_fonts": dict(fonts),
            "direct_colors": dict(colors),
            "headings": headings,
            "tables": [_table_info(doc, item_id, el) for item_id, el in body if el.tag == qn("w:tbl")],
            "images": _images(doc),
            "headers_footers": _headers_footers(doc),
            "stats": {
                "paragraphs": len(paragraphs),
                "words": sum(len(para_text(p).split()) for p in paragraphs),
                "tables": sum(1 for _, el in body if el.tag == qn("w:tbl")),
                "sections": len(doc.sections),
            },
        }

    def outline(self, path: Path) -> list[dict[str, Any]]:
        doc = Document(str(path))
        items = []
        for item_id, el in _body_items(doc):
            if el.tag == qn("w:p"):
                text = para_text(el)
                has_image = el.find(f".//{qn('w:drawing')}") is not None
                if not text.strip() and not has_image:
                    continue
                entry = {"id": item_id, "kind": "paragraph", "style": _style_name(doc, el), "text": text}
                if has_image:
                    entry["has_image"] = True
                items.append(entry)
            else:
                rows = []
                for r, row in enumerate(el.findall(qn("w:tr"))):
                    rows.append(
                        [
                            {"id": f"{item_id}.r{r}.c{c}", "text": _cell_text(cell)}
                            for c, cell in enumerate(row.findall(qn("w:tc")))
                        ]
                    )
                items.append({"id": item_id, "kind": "table", "rows": rows})
        return items

    def apply(self, src: Path, ops: list[Any], dest: Path, resolve_image: ImageResolver | None = None) -> ApplyResult:
        doc = Document(str(src))
        items = dict(_body_items(doc))
        protos = _Prototypes(doc)
        result = ApplyResult()

        # Ids are resolved against the original document, so later ops in the
        # batch still mean what the writer saw.
        for op in ops:
            handler = getattr(self, f"_op_{op.op}", None)
            if handler is None:
                raise EditError(f"{op.op} is not a DOCX operation")
            handler(doc, items, protos, op, result, resolve_image)

        doc.save(str(dest))
        return result

    def _op_replace_text(self, doc, items, protos, op, result, _images_fn) -> None:
        el = _lookup(items, op.target, qn("w:p"))
        set_para_text(el, op.text)
        result.touched.add(op.target)
        result.log.append(f"Rewrote {op.target}")

    def _op_insert_after(self, doc, items, protos, op, result, _images_fn) -> None:
        anchor = _lookup(items, op.target)
        for new in reversed(protos.build(op.blocks)):
            anchor.addnext(new)
        result.log.append(f"Added {len(op.blocks)} block(s) after {op.target}")

    def _op_delete(self, doc, items, protos, op, result, _images_fn) -> None:
        el = _lookup(items, op.target)
        if el.find(f"{qn('w:pPr')}/{qn('w:sectPr')}") is not None:
            raise EditError(f"{op.target} ends a page section, so deleting it would change the page layout")
        el.getparent().remove(el)
        result.touched.add(op.target)
        result.log.append(f"Deleted {op.target}")

    def _op_set_cell(self, doc, items, protos, op, result, _images_fn) -> None:
        match = re.fullmatch(r"(t\d+)\.r(\d+)\.c(\d+)", op.target)
        if not match:
            raise EditError(f"{op.target} is not a cell id. Use the form t0.r1.c2")
        table = _lookup(items, match[1], qn("w:tbl"))
        cell = _cell_at(table, int(match[2]), int(match[3]), op.target)
        paragraphs = cell.findall(qn("w:p"))
        set_para_text(paragraphs[0], op.text)
        for extra in paragraphs[1:]:
            cell.remove(extra)
        result.touched.add(match[1])
        result.log.append(f"Updated cell {op.target}")

    def _op_add_row(self, doc, items, protos, op, result, _images_fn) -> None:
        table = _lookup(items, op.target, qn("w:tbl"))
        rows = table.findall(qn("w:tr"))
        index = len(rows) - 1 if op.after_row is None else op.after_row
        if not 0 <= index < len(rows):
            raise EditError(f"{op.target} has no row {index}")
        # Copy a body row where possible, so a new row after the header doesn't look like a header.
        source = rows[max(index, 1)] if len(rows) > 1 else rows[index]
        new_row = copy.deepcopy(source)
        _fill_row(new_row, op.values)
        rows[index].addnext(new_row)
        result.touched.add(op.target)
        result.log.append(f"Added a row to {op.target}")

    def _op_delete_row(self, doc, items, protos, op, result, _images_fn) -> None:
        match = re.fullmatch(r"(t\d+)\.r(\d+)", op.target)
        if not match:
            raise EditError(f"{op.target} is not a row id. Use the form t0.r2")
        table = _lookup(items, match[1], qn("w:tbl"))
        rows = table.findall(qn("w:tr"))
        index = int(match[2])
        if index >= len(rows):
            raise EditError(f"{match[1]} has no row {index}")
        table.remove(rows[index])
        result.touched.add(match[1])
        result.log.append(f"Deleted row {op.target}")

    def _op_replace_body(self, doc, items, protos, op, result, _images_fn) -> None:
        body = doc.element.body
        keep_until = -1
        children = list(body)
        if op.keep_through:
            keep_until = children.index(_lookup(items, op.keep_through))

        inverse = {id(el): item_id for item_id, el in items.items()}
        insert_before = None
        for position, child in enumerate(children):
            if position <= keep_until or child.tag == qn("w:sectPr"):
                continue
            if child.find(f"{qn('w:pPr')}/{qn('w:sectPr')}") is not None:
                # Section breaks hold margins and orientation. They stay put.
                if insert_before is None:
                    insert_before = child
                continue
            if id(child) in inverse:
                result.touched.add(inverse[id(child)])
            body.remove(child)

        if insert_before is None:
            insert_before = body.find(qn("w:sectPr"))
        for new in protos.build(op.blocks):
            if insert_before is not None:
                insert_before.addprevious(new)
            else:
                body.append(new)
        result.log.append(f"Rebuilt the body with {len(op.blocks)} block(s)")

    def _op_find_replace(self, doc, items, protos, op, result, _images_fn) -> None:
        total = 0
        for item_id, el in list(items.items()):
            if el.getparent() is None:
                continue
            hits = sum(find_replace_in_paragraph(p, op.find, op.replace) for p in el.iter(qn("w:p")))
            if hits:
                result.touched.add(item_id)
                total += hits
        for part in _header_footer_parts(doc):
            hits = sum(find_replace_in_paragraph(p, op.find, op.replace) for p in part.element.iter(qn("w:p")))
            if hits:
                result.touched_parts.add(str(part.partname).lstrip("/"))
                total += hits
        if not total:
            raise EditError(f'"{op.find}" does not appear in the document')
        result.log.append(f'Replaced "{op.find}" {total} time(s)')

    def _op_replace_image(self, doc, items, protos, op, result, resolve_image) -> None:
        if resolve_image is None:
            raise EditError("No image file is available for this edit")
        found = _images(doc, with_elements=True)
        match = next((img for img in found if img["id"] == op.target), None)
        if match is None:
            raise EditError(f"{op.target} is not an image in this document")
        blip = match["_blip"]
        part = match["_part"]
        rid, _ = part.get_or_add_image(str(resolve_image(op.file_id)))
        blip.set(f"{R}embed", rid)
        if match["location"] == "body":
            owner = _owning_item(items, blip)
            if owner:
                result.touched.add(owner)
        else:
            result.touched_parts.add(str(part.partname).lstrip("/"))
            result.touched_parts.add(f"word/_rels/{Path(str(part.partname)).name}.rels")
        result.log.append(f"Swapped image {op.target}; position and size unchanged")

    def _op_set_sources(self, doc, items, protos, op, result, _images_fn) -> None:
        if not op.sources:
            return
        body = doc.element.body
        children = list(body)
        start = None
        for position, child in enumerate(children):
            if child.tag == qn("w:p") and para_text(child).strip() == SOURCES_HEADING:
                start = position
        inverse = {id(el): item_id for item_id, el in items.items()}
        if start is not None:
            for child in children[start:]:
                if child.tag == qn("w:sectPr") or child.find(f"{qn('w:pPr')}/{qn('w:sectPr')}") is not None:
                    continue
                if id(child) in inverse:
                    result.touched.add(inverse[id(child)])
                body.remove(child)

        blocks = [Block(kind="heading2", text=SOURCES_HEADING)]
        for source in op.sources:
            bits = [source.title, source.locator, source.url]
            if source.retrieved_at:
                bits.append(f"Retrieved {source.retrieved_at}")
            line = ". ".join(b.rstrip(".") for b in bits if b)
            blocks.append(Block(kind="paragraph", text=f"[{source.key}] {line}"))

        insert_before = body.find(qn("w:sectPr"))
        for new in protos.build(blocks):
            if insert_before is not None:
                insert_before.addprevious(new)
            else:
                body.append(new)
        result.log.append(f"Listed {len(op.sources)} source(s)")

    def preservation_checks(self, src: Path, dest: Path, result: ApplyResult) -> list[Check]:
        before = part_fingerprints(src)
        after = part_fingerprints(dest)
        allowed = ALWAYS_REWRITTEN | result.touched_parts

        changed = [n for n, h in before.items() if n in after and after[n] != h and n not in allowed]
        missing = [n for n in before if n not in after]
        media_before = {h for n, h in before.items() if n.startswith("word/media/")}
        media_after = {h for n, h in after.items() if n.startswith("word/media/")}

        checks = [
            Check(
                "Styles, theme, headers and footers unchanged",
                not changed,
                "Changed: " + ", ".join(changed) if changed else f"{len(before) - len(allowed & set(before))} parts identical",
            ),
            Check("No parts dropped", not missing, "Missing: " + ", ".join(missing) if missing else ""),
            Check(
                "Logos and images kept",
                media_before <= media_after or bool(result.touched_parts) or _replaced_images(result),
                f"{len(media_before)} image(s) in the original",
            ),
        ]

        src_doc = Document(str(src))
        dest_doc = Document(str(dest))
        src_sections = [_section_info(s) for s in src_doc.sections]
        dest_sections = [_section_info(s) for s in dest_doc.sections]
        checks.append(
            Check(
                "Page size and margins unchanged",
                src_sections == dest_sections,
                "" if src_sections == dest_sections else f"Was {src_sections}, now {dest_sections}",
            )
        )

        kept = [element_hash(el) for item_id, el in _body_items(src_doc) if item_id not in result.touched]
        final = [element_hash(el) for _, el in _body_items(dest_doc)]
        out_of_place = _first_missing_in_order(kept, final)
        checks.append(
            Check(
                "Untouched content is byte-identical",
                out_of_place is None,
                f"{len(kept)} element(s) checked" if out_of_place is None else f"{out_of_place + 1}th untouched element changed or moved",
            )
        )
        return checks


def _replaced_images(result: ApplyResult) -> bool:
    return any(line.startswith("Swapped image") for line in result.log)


def _first_missing_in_order(needles: list[str], haystack: list[str]) -> int | None:
    position = 0
    for index, needle in enumerate(needles):
        try:
            position = haystack.index(needle, position) + 1
        except ValueError:
            return index
    return None


def _body_items(doc: Any) -> Iterable[tuple[str, Any]]:
    p_index = t_index = 0
    for child in doc.element.body:
        if child.tag == qn("w:p"):
            yield f"p{p_index}", child
            p_index += 1
        elif child.tag == qn("w:tbl"):
            yield f"t{t_index}", child
            t_index += 1


def _lookup(items: dict[str, Any], item_id: str, tag: str | None = None) -> Any:
    el = items.get(item_id)
    if el is None:
        raise EditError(f"{item_id} does not exist in this document")
    if el.getparent() is None:
        raise EditError(f"{item_id} was already removed earlier in this plan")
    if tag and el.tag != tag:
        raise EditError(f"{item_id} is the wrong kind of element for this operation")
    return el


def _owning_item(items: dict[str, Any], node: Any) -> str | None:
    by_id = {id(el): item_id for item_id, el in items.items()}
    while node is not None:
        if id(node) in by_id:
            return by_id[id(node)]
        node = node.getparent()
    return None


def _cell_text(cell: Any) -> str:
    return "\n".join(para_text(p) for p in cell.findall(qn("w:p")))


def _cell_at(table: Any, row: int, column: int, label: str) -> Any:
    rows = table.findall(qn("w:tr"))
    if row >= len(rows):
        raise EditError(f"{label}: the table only has {len(rows)} rows")
    cells = rows[row].findall(qn("w:tc"))
    if column >= len(cells):
        raise EditError(f"{label}: row {row} only has {len(cells)} cells")
    return cells[column]


def _fill_row(row: Any, values: list[str]) -> None:
    cells = row.findall(qn("w:tc"))
    for position, cell in enumerate(cells):
        paragraphs = cell.findall(qn("w:p"))
        set_para_text(paragraphs[0], values[position] if position < len(values) else "")
        for extra in paragraphs[1:]:
            cell.remove(extra)


def _style_name(doc: Any, p: Any) -> str:
    props = p.find(qn("w:pPr"))
    style = props.find(qn("w:pStyle")) if props is not None else None
    if style is None:
        return _default_style_name(doc)
    style_id = style.get(qn("w:val"))
    for candidate in doc.styles.element.findall(qn("w:style")):
        if candidate.get(qn("w:styleId")) == style_id:
            # Word stores "heading 1" but shows "Heading 1".
            return BabelFish.internal2ui(candidate.find(qn("w:name")).get(qn("w:val")))
    return style_id


def _default_style_name(doc: Any) -> str:
    try:
        return doc.styles.default(1).name  # WD_STYLE_TYPE.PARAGRAPH
    except (AttributeError, ValueError):
        return "Normal"


def _heading_level(doc: Any, p: Any) -> int | None:
    match = re.fullmatch(r"heading (\d)", _style_name(doc, p).lower())
    if match:
        return int(match[1])
    if _style_name(doc, p).lower() == "title":
        return 0
    return None


def _section_info(section: Any) -> dict[str, Any]:
    sect = section._sectPr
    cols = sect.find(qn("w:cols"))
    return {
        "width_mm": emu_to_mm(section.page_width),
        "height_mm": emu_to_mm(section.page_height),
        "orientation": "landscape" if section.orientation == 1 else "portrait",
        "margins_mm": {
            "top": emu_to_mm(section.top_margin),
            "bottom": emu_to_mm(section.bottom_margin),
            "left": emu_to_mm(section.left_margin),
            "right": emu_to_mm(section.right_margin),
            "header": emu_to_mm(section.header_distance),
            "footer": emu_to_mm(section.footer_distance),
            "gutter": emu_to_mm(section.gutter),
        },
        "different_first_page": section.different_first_page_header_footer,
        "columns": int(cols.get(qn("w:num"), 1)) if cols is not None else 1,
    }


def _doc_defaults(doc: Any) -> dict[str, Any]:
    props = doc.styles.element.find(f"{qn('w:docDefaults')}/{qn('w:rPrDefault')}/{qn('w:rPr')}")
    if props is None:
        return {}
    fonts = props.find(qn("w:rFonts"))
    size = props.find(qn("w:sz"))
    return {
        "font": fonts.get(qn("w:ascii")) or fonts.get(qn("w:asciiTheme")) if fonts is not None else None,
        "size_pt": int(size.get(qn("w:val"))) / 2 if size is not None else None,
    }


def _style_info(doc: Any, name: str, count: int) -> dict[str, Any]:
    info: dict[str, Any] = {"name": name, "paragraphs": count}
    try:
        style = doc.styles[name]
    except KeyError:
        return info
    font = style.font
    info.update(
        font=font.name,
        size_pt=font.size.pt if font.size else None,
        bold=font.bold,
        italic=font.italic,
        color=f"#{font.color.rgb}" if font.color is not None and font.color.rgb is not None else None,
    )
    fmt = getattr(style, "paragraph_format", None)
    if fmt is not None:
        info.update(
            alignment=str(fmt.alignment).split(" ")[0] if fmt.alignment is not None else None,
            space_before_pt=fmt.space_before.pt if fmt.space_before is not None else None,
            space_after_pt=fmt.space_after.pt if fmt.space_after is not None else None,
        )
    return info


def _table_info(doc: Any, item_id: str, table: Any) -> dict[str, Any]:
    rows = table.findall(qn("w:tr"))
    props = table.find(qn("w:tblPr"))
    style = props.find(qn("w:tblStyle")) if props is not None else None
    borders = {}
    border_el = props.find(qn("w:tblBorders")) if props is not None else None
    if border_el is not None:
        for edge in border_el:
            borders[edge.tag.split("}")[1]] = {
                "style": edge.get(qn("w:val")),
                "size": edge.get(qn("w:sz")),
                "color": edge.get(qn("w:color")),
            }
    grid = table.find(qn("w:tblGrid"))
    widths = [twips_to_mm(c.get(qn("w:w"))) for c in grid.findall(qn("w:gridCol"))] if grid is not None else []
    fills = []
    if rows:
        for shade in rows[0].iter(qn("w:shd")):
            if shade.get(qn("w:fill")) not in (None, "auto"):
                fills.append("#" + shade.get(qn("w:fill")).upper())
    merged = any(True for _ in table.iter(qn("w:gridSpan"))) or any(True for _ in table.iter(qn("w:vMerge")))
    return {
        "id": item_id,
        "rows": len(rows),
        "columns": len(widths),
        "style": style.get(qn("w:val")) if style is not None else None,
        "column_widths_mm": widths,
        "borders": borders,
        "header_fill": fills[0] if fills else None,
        "has_merged_cells": merged,
    }


def _header_footer_parts(doc: Any) -> list[Any]:
    parts = []
    for rel in doc.part.rels.values():
        if rel.reltype in (RT.HEADER, RT.FOOTER):
            parts.append(rel.target_part)
    return parts


def _images(doc: Any, with_elements: bool = False) -> list[dict[str, Any]]:
    found = []
    containers = [("body", doc.part, doc.element)]
    for part in _header_footer_parts(doc):
        kind = "header" if "header" in str(part.partname) else "footer"
        containers.append((kind, part, part.element))

    index = 0
    for location, part, root in containers:
        for drawing in root.iter(qn("w:drawing")):
            for holder in drawing:
                blip = holder.find(f".//{A}blip")
                if blip is None:
                    continue
                extent = holder.find(f"{WP}extent")
                info: dict[str, Any] = {
                    "id": f"img{index}",
                    "location": location,
                    "mode": "anchored" if holder.tag == f"{WP}anchor" else "inline",
                    "width_mm": emu_to_mm(int(extent.get("cx"))) if extent is not None else None,
                    "height_mm": emu_to_mm(int(extent.get("cy"))) if extent is not None else None,
                }
                if holder.tag == f"{WP}anchor":
                    info.update(_anchor_position(holder))
                rid = blip.get(f"{R}embed")
                if rid and rid in part.rels:
                    image = part.rels[rid].target_part
                    info["file"] = Path(str(image.partname)).name
                    info["content_type"] = image.content_type
                    info["sha256"] = sha256(image.blob)
                if with_elements:
                    info["_blip"] = blip
                    info["_part"] = part
                found.append(info)
                index += 1
    return found


def _anchor_position(anchor: Any) -> dict[str, Any]:
    position: dict[str, Any] = {"behind_text": anchor.get("behindDoc") == "1"}
    for axis, tag in (("x", "positionH"), ("y", "positionV")):
        el = anchor.find(f"{WP}{tag}")
        if el is None:
            continue
        position[f"{axis}_relative_to"] = el.get("relativeFrom")
        offset = el.find(f"{WP}posOffset")
        align = el.find(f"{WP}align")
        if offset is not None:
            position[f"{axis}_mm"] = emu_to_mm(int(offset.text))
        elif align is not None:
            position[f"{axis}_align"] = align.text
    wrap = [c.tag.split("}")[1] for c in anchor if c.tag.split("}")[1].startswith("wrap")]
    if wrap:
        position["wrap"] = wrap[0]
    return position


def _headers_footers(doc: Any) -> list[dict[str, Any]]:
    out = []
    for part in _header_footer_parts(doc):
        root = part.element
        out.append(
            {
                "part": str(part.partname).lstrip("/"),
                "kind": "header" if "header" in str(part.partname) else "footer",
                "text": " ".join(para_text(p) for p in root.iter(qn("w:p"))).strip(),
                "images": sum(1 for _ in root.iter(qn("w:drawing"))),
                "has_page_number": any("PAGE" in (i.text or "") for i in root.iter(qn("w:instrText")))
                or any("PAGE" in (f.get(qn("w:instr")) or "") for f in root.iter(qn("w:fldSimple"))),
            }
        )
    return out


class _Prototypes:
    """New content is cloned from what the template already uses.

    Captured up front, because replace_body deletes the originals.
    """

    def __init__(self, doc: Any) -> None:
        self.doc = doc
        self.by_kind: dict[str, Any] = {}
        self.table: Any = None
        for el in doc.element.body:
            if el.tag == qn("w:tbl"):
                self.table = self.table if self.table is not None else copy.deepcopy(el)
                continue
            if el.tag != qn("w:p") or not para_text(el).strip():
                continue
            self._consider(el)

    def _consider(self, p: Any) -> None:
        level = _heading_level(self.doc, p)
        if level in (1, 2, 3):
            self.by_kind.setdefault(f"heading{level}", copy.deepcopy(p))
            return
        if level == 0:
            return
        kind = self._list_kind(p)
        if kind:
            self.by_kind.setdefault(kind, copy.deepcopy(p))
            return
        text = para_text(p).strip()
        current = self.by_kind.get("paragraph")
        # A longer paragraph is a safer sample of body text than a short caption.
        if current is None or (len(text) > 80 and len(para_text(current).strip()) <= 80):
            self.by_kind["paragraph"] = copy.deepcopy(p)

    def _list_kind(self, p: Any) -> str | None:
        name = _style_name(self.doc, p).lower()
        if "bullet" in name:
            return "bullet"
        if "number" in name:
            return "numbered"
        props = p.find(qn("w:pPr"))
        num = props.find(qn("w:numPr")) if props is not None else None
        if num is None:
            return None
        return "bullet" if self._is_bullet(num) else "numbered"

    def _is_bullet(self, num_pr: Any) -> bool:
        num_id = num_pr.find(qn("w:numId"))
        try:
            numbering = self.doc.part.numbering_part.element
        except (KeyError, NotImplementedError):
            return True
        for num in numbering.findall(qn("w:num")):
            if num_id is not None and num.get(qn("w:numId")) == num_id.get(qn("w:val")):
                abstract_id = num.find(qn("w:abstractNumId")).get(qn("w:val"))
                for abstract in numbering.findall(qn("w:abstractNum")):
                    if abstract.get(qn("w:abstractNumId")) == abstract_id:
                        level = abstract.find(qn("w:lvl"))
                        fmt = level.find(qn("w:numFmt")) if level is not None else None
                        return fmt is None or fmt.get(qn("w:val")) == "bullet"
        return True

    def build(self, blocks: list[Block]) -> list[Any]:
        return [self._table(b) if b.kind == "table" else self._paragraph(b) for b in blocks]

    def _paragraph(self, block: Block) -> Any:
        if block.kind in ("bullet", "numbered"):
            block = block.model_copy(update={"text": strip_list_marker(block.text)})
        proto = self.by_kind.get(block.kind)
        if proto is not None:
            new = copy.deepcopy(proto)
            for tag in ("w:bookmarkStart", "w:bookmarkEnd"):
                for mark in new.findall(qn(tag)):
                    new.remove(mark)
            # A cloned logo or inline picture must not travel with the text.
            for run in new.findall(qn("w:r")):
                if run.find(qn("w:drawing")) is not None or run.find(qn("w:pict")) is not None:
                    new.remove(run)
            set_para_text(new, block.text)
            return new

        # The template has no sample of this kind, so fall back to its named style.
        p = OxmlElement("w:p")
        style_id = self._style_id(block.kind)
        if style_id:
            props = OxmlElement("w:pPr")
            ref = OxmlElement("w:pStyle")
            ref.set(qn("w:val"), style_id)
            props.append(ref)
            p.append(props)
        set_para_text(p, block.text)
        return p

    def _style_id(self, kind: str) -> str | None:
        names = {
            "heading1": "heading 1",
            "heading2": "heading 2",
            "heading3": "heading 3",
            "bullet": "list bullet",
            "numbered": "list number",
        }
        wanted = names.get(kind)
        if not wanted:
            return None
        for style in self.doc.styles.element.findall(qn("w:style")):
            name = style.find(qn("w:name"))
            if name is not None and name.get(qn("w:val"), "").lower() == wanted:
                return style.get(qn("w:styleId"))
        return None

    def _table(self, block: Block) -> Any:
        if not block.rows:
            raise EditError("A table block needs at least one row")
        columns = max(len(r) for r in block.rows)
        if self.table is None:
            return self._plain_table(block.rows, columns)

        table = copy.deepcopy(self.table)
        rows = table.findall(qn("w:tr"))
        header = rows[0]
        body_samples = [r for r in rows[1:] if not _looks_like_total(r)] or [rows[0]]
        samples = [copy.deepcopy(r) for r in body_samples]
        header_sample = copy.deepcopy(header)
        for row in rows:
            table.remove(row)
        _resize_grid(table, columns)

        for index, values in enumerate(block.rows):
            source = header_sample if index == 0 else samples[(index - 1) % len(samples)]
            row = copy.deepcopy(source)
            _resize_row(row, table, columns)
            _fill_row(row, values)
            table.append(row)
        return table

    def _plain_table(self, rows: list[list[str]], columns: int) -> Any:
        table = self.doc.add_table(rows=len(rows), cols=columns)
        if any(s.name == "Table Grid" for s in self.doc.styles):
            table.style = "Table Grid"
        element = table._tbl
        element.getparent().remove(element)
        for r, values in enumerate(rows):
            for c, value in enumerate(values):
                table.cell(r, c).text = value
        return element


def _looks_like_total(row: Any) -> bool:
    cells = row.findall(qn("w:tc"))
    return bool(cells) and _cell_text(cells[0]).strip().lower().startswith(("total", "grand total", "subtotal"))


def _resize_grid(table: Any, columns: int) -> None:
    grid = table.find(qn("w:tblGrid"))
    if grid is None:
        return
    current = grid.findall(qn("w:gridCol"))
    if len(current) == columns:
        return
    # Total width stays what the template set, so the table still fits the margins.
    total = sum(int(c.get(qn("w:w"), 0)) for c in current) or 9000
    for col in current:
        grid.remove(col)
    for _ in range(columns):
        col = OxmlElement("w:gridCol")
        col.set(qn("w:w"), str(total // columns))
        grid.append(col)


def _resize_row(row: Any, table: Any, columns: int) -> None:
    cells = row.findall(qn("w:tc"))
    for cell in cells:
        props = cell.find(qn("w:tcPr"))
        if props is not None:
            for tag in ("w:gridSpan", "w:vMerge"):
                node = props.find(qn(tag))
                if node is not None:
                    props.remove(node)
    while len(cells) > columns:
        row.remove(cells.pop())
    while len(cells) < columns:
        extra = copy.deepcopy(cells[-1])
        cells[-1].addnext(extra)
        cells.append(extra)
    grid = table.find(qn("w:tblGrid"))
    if grid is None:
        return
    widths = [c.get(qn("w:w")) for c in grid.findall(qn("w:gridCol"))]
    for cell, width in zip(cells, widths):
        props = cell.find(qn("w:tcPr"))
        tc_width = props.find(qn("w:tcW")) if props is not None else None
        if tc_width is not None:
            tc_width.set(qn("w:w"), width)
            tc_width.set(qn("w:type"), "dxa")
