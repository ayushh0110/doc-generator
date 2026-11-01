import copy
import math
import re
import zipfile
from pathlib import Path
from typing import Any, Iterable

from lxml import etree
from pptx import Presentation
from pptx.chart.data import CategoryChartData
from pptx.enum.shapes import MSO_SHAPE_TYPE, PP_PLACEHOLDER
from pptx.opc.constants import RELATIONSHIP_TYPE as RT
from pptx.oxml.ns import qn

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
)

R_NS = "http://schemas.openxmlformats.org/officeDocument/2006/relationships"
SOURCES_PREFIX = "Sources:"


def _all_shapes(shapes: Iterable[Any]) -> Iterable[Any]:
    for shape in shapes:
        yield shape
        if shape.shape_type == MSO_SHAPE_TYPE.GROUP:
            yield from _all_shapes(shape.shapes)


def _shape_kind(shape: Any) -> str:
    if shape.is_placeholder:
        return "placeholder"
    if getattr(shape, "has_table", False) and shape.has_table:
        return "table"
    if getattr(shape, "has_chart", False) and shape.has_chart:
        return "chart"
    if shape.shape_type == MSO_SHAPE_TYPE.PICTURE:
        return "picture"
    if shape.shape_type == MSO_SHAPE_TYPE.GROUP:
        return "group"
    return "shape" if not shape.has_text_frame else "text"


def _geometry(shape: Any) -> dict[str, Any]:
    return {
        "left_mm": emu_to_mm(shape.left),
        "top_mm": emu_to_mm(shape.top),
        "width_mm": emu_to_mm(shape.width),
        "height_mm": emu_to_mm(shape.height),
    }


def _placeholder_type(shape: Any) -> str:
    try:
        return str(shape.placeholder_format.type).split(" ")[0].replace("PP_PLACEHOLDER.", "").lower()
    except ValueError:
        return "unknown"


def _set_paragraph(p: Any, text: str, level: int | None = None) -> None:
    runs = p.findall(qn("a:r"))
    template = None
    if runs:
        template = copy.deepcopy(runs[0].find(qn("a:rPr")))
    else:
        end = p.find(qn("a:endParaRPr"))
        if end is not None:
            template = etree.SubElement(etree.Element("wrap"), qn("a:rPr"))
            template.attrib.update(end.attrib)
            for child in end:
                template.append(copy.deepcopy(child))

    for node in list(p):
        if node.tag in (qn("a:r"), qn("a:br"), qn("a:fld")):
            p.remove(node)

    run = etree.Element(qn("a:r"))
    if template is not None:
        run.append(template)
    t = etree.SubElement(run, qn("a:t"))
    t.text = text

    end = p.find(qn("a:endParaRPr"))
    if end is not None:
        end.addprevious(run)
    else:
        p.append(run)

    if level is not None:
        props = p.find(qn("a:pPr"))
        if level == 0 and props is None:
            return
        if props is None:
            props = etree.Element(qn("a:pPr"))
            p.insert(0, props)
        if level:
            props.set("lvl", str(level))
        elif "lvl" in props.attrib:
            del props.attrib["lvl"]


def set_frame_text(tx_body: Any, paragraphs: list[str]) -> None:
    """Fill a text body, reusing each existing paragraph's formatting in turn."""
    existing = tx_body.findall(qn("a:p"))
    wanted = paragraphs or [""]
    for index, raw in enumerate(wanted):
        level = len(raw) - len(raw.lstrip("\t"))
        text = strip_list_marker(raw.lstrip("\t"))
        if index < len(existing):
            p = existing[index]
        else:
            p = copy.deepcopy(existing[-1])
            tx_body.append(p)
            existing.append(p)
        _set_paragraph(p, text, level if level or index >= 1 else None)
    for extra in existing[len(wanted):]:
        tx_body.remove(extra)


def find_replace_in_text_body(root: Any, find: str, replace: str) -> int:
    hits = 0
    for p in root.iter(qn("a:p")):
        nodes = list(p.iter(qn("a:t")))
        joined = "".join(n.text or "" for n in nodes)
        position = joined.rfind(find)
        while position != -1:
            hits += 1
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
                placed = True
            joined = "".join(n.text or "" for n in nodes)
            position = joined.rfind(find, 0, position)
    return hits


class PptxEngine:
    extension = "pptx"

    def analyse(self, path: Path) -> dict[str, Any]:
        prs = Presentation(str(path))
        masters = []
        for master_index, master in enumerate(prs.slide_masters):
            masters.append(
                {
                    "name": master.name or f"Master {master_index + 1}",
                    "shapes": [_shape_summary(s) for s in master.shapes],
                    "layouts": [
                        {
                            "name": layout.name,
                            "placeholders": [
                                {
                                    "idx": ph.placeholder_format.idx,
                                    "type": _placeholder_type(ph),
                                    "name": ph.name,
                                    **_geometry(ph),
                                }
                                for ph in layout.placeholders
                            ],
                            "other_shapes": [
                                _shape_summary(s) for s in layout.shapes if not s.is_placeholder
                            ],
                        }
                        for layout in master.slide_layouts
                    ],
                }
            )

        with zipfile.ZipFile(path) as package:
            names = package.namelist()
            theme = next((n for n in sorted(names) if n.startswith("ppt/theme/") and n.endswith(".xml")), None)
            theme_info = theme_summary(package.read(theme)) if theme else None

        slides = []
        fonts: set[str] = set()
        for index, slide in enumerate(prs.slides):
            shapes = []
            for shape in _all_shapes(slide.shapes):
                shapes.append(_shape_summary(shape, f"s{index}."))
                if shape.has_text_frame:
                    for latin in shape._element.iter(qn("a:latin")):
                        if latin.get("typeface"):
                            fonts.add(latin.get("typeface"))
            slides.append(
                {
                    "id": f"s{index}",
                    "layout": slide.slide_layout.name,
                    "shapes": shapes,
                    "has_notes": slide.has_notes_slide,
                }
            )

        return {
            "format": "pptx",
            "slide_size_mm": {"width": emu_to_mm(prs.slide_width), "height": emu_to_mm(prs.slide_height)},
            "theme": theme_info,
            "masters": masters,
            "direct_fonts": sorted(fonts),
            "slides": slides,
            "stats": {
                "slides": len(slides),
                "masters": len(masters),
                "layouts": sum(len(m["layouts"]) for m in masters),
            },
        }

    def outline(self, path: Path) -> list[dict[str, Any]]:
        prs = Presentation(str(path))
        items = []
        for index, slide in enumerate(prs.slides):
            shapes = []
            for shape in _all_shapes(slide.shapes):
                entry = _shape_summary(shape, f"s{index}.")
                if shape.shape_type == MSO_SHAPE_TYPE.GROUP:
                    continue
                shapes.append(entry)
            notes = slide.notes_slide.notes_text_frame.text if slide.has_notes_slide else ""
            items.append(
                {"id": f"s{index}", "kind": "slide", "layout": slide.slide_layout.name, "shapes": shapes, "notes": notes}
            )
        return items

    def apply(self, src: Path, ops: list[Any], dest: Path, resolve_image: ImageResolver | None = None) -> ApplyResult:
        prs = Presentation(str(src))
        slides = {f"s{i}": slide for i, slide in enumerate(prs.slides)}
        result = ApplyResult()
        for op in ops:
            handler = getattr(self, f"_op_{op.op}", None)
            if handler is None:
                raise EditError(f"{op.op} is not a PPTX operation")
            handler(prs, slides, op, result, resolve_image)
        prs.save(str(dest))
        return result

    def _op_set_text(self, prs, slides, op, result, _resolve) -> None:
        slide_id, shape = _find_shape(slides, op.target)
        if not shape.has_text_frame:
            raise EditError(f"{op.target} does not hold text")
        set_frame_text(shape.text_frame._txBody, op.paragraphs)
        result.touched.add(slide_id)
        result.log.append(f"Rewrote {op.target}")

    def _op_add_slide(self, prs, slides, op, result, _resolve) -> None:
        layout = next(
            (l for m in prs.slide_masters for l in m.slide_layouts if l.name == op.layout),
            None,
        )
        if layout is None:
            names = sorted({l.name for m in prs.slide_masters for l in m.slide_layouts})
            raise EditError(f'No layout called "{op.layout}". Available: {", ".join(names)}')

        slide = prs.slides.add_slide(layout)
        filled: set[int] = set()
        for fill in op.content:
            placeholder = _match_placeholder(slide, fill.placeholder, filled)
            if placeholder is None:
                raise EditError(f'Layout "{op.layout}" has no free placeholder for "{fill.placeholder}"')
            set_frame_text(placeholder.text_frame._txBody, fill.paragraphs)
            filled.add(placeholder.placeholder_format.idx)

        # An empty placeholder shows prompt text in edit mode, so it goes.
        for placeholder in list(slide.placeholders):
            if placeholder.placeholder_format.idx not in filled:
                placeholder._element.getparent().remove(placeholder._element)

        _copy_footer_fields(prs, slide, layout)
        if op.notes:
            slide.notes_slide.notes_text_frame.text = op.notes
        # New slides already sit at the end, which is what no "after" means here.
        if op.after:
            _place_slide(prs, slides, slide, op.after, result)
        result.log.append(f'Added a "{op.layout}" slide')

    def _op_duplicate_slide(self, prs, slides, op, result, _resolve) -> None:
        source = _slide(slides, op.target)
        slide = prs.slides.add_slide(source.slide_layout)
        tree = slide.shapes._spTree
        for shape in list(slide.shapes):
            tree.remove(shape._element)

        id_map: dict[str, str] = {}
        for rel in source.part.rels.values():
            if rel.reltype in (RT.SLIDE_LAYOUT, RT.NOTES_SLIDE):
                continue
            if rel.is_external:
                id_map[rel.rId] = slide.part.relate_to(rel.target_ref, rel.reltype, is_external=True)
            else:
                id_map[rel.rId] = slide.part.relate_to(rel.target_part, rel.reltype)

        for child in source.shapes._spTree:
            if child.tag in (qn("p:nvGrpSpPr"), qn("p:grpSpPr")):
                continue
            clone = copy.deepcopy(child)
            for node in clone.iter():
                for name, value in list(node.attrib.items()):
                    if name.startswith(f"{{{R_NS}}}") and value in id_map:
                        node.set(name, id_map[value])
            tree.append(clone)

        for edit in op.edits:
            shape_id = edit.shape.split(".")[-1]
            shape = _shape_by_id(slide, shape_id)
            if shape is None or not shape.has_text_frame:
                raise EditError(f"{op.target} has no text shape {edit.shape}")
            set_frame_text(shape.text_frame._txBody, edit.paragraphs)

        notes = op.notes or (source.notes_slide.notes_text_frame.text if source.has_notes_slide else "")
        if notes:
            slide.notes_slide.notes_text_frame.text = notes
        _place_slide(prs, slides, slide, op.after or op.target, result)
        result.log.append(f"Duplicated {op.target}")

    def _op_delete_slide(self, prs, slides, op, result, _resolve) -> None:
        slide = _slide(slides, op.target)
        id_list = prs.slides._sldIdLst
        for sld_id in list(id_list):
            if sld_id.get("id") == str(slide.slide_id):
                prs.part.drop_rel(sld_id.get(qn("r:id")))
                id_list.remove(sld_id)
                break
        del slides[op.target]
        result.touched.add(op.target)
        result.log.append(f"Deleted {op.target}")

    def _op_move_slide(self, prs, slides, op, result, _resolve) -> None:
        _place_slide(prs, slides, _slide(slides, op.target), op.after, result)
        result.log.append(f"Moved {op.target}")

    def _op_set_cell(self, prs, slides, op, result, _resolve) -> None:
        match = re.fullmatch(r"(s\d+\.sh\d+)\.r(\d+)\.c(\d+)", op.target)
        if not match:
            raise EditError(f"{op.target} is not a cell id. Use the form s2.sh5.r1.c2")
        slide_id, shape = _find_shape(slides, match[1])
        if not getattr(shape, "has_table", False) or not shape.has_table:
            raise EditError(f"{match[1]} is not a table")
        table = shape.table
        row, column = int(match[2]), int(match[3])
        if row >= len(table.rows) or column >= len(table.columns):
            raise EditError(f"{op.target} is outside the table")
        set_frame_text(table.cell(row, column).text_frame._txBody, [op.text])
        result.touched.add(slide_id)
        result.log.append(f"Updated cell {op.target}")

    def _op_set_notes(self, prs, slides, op, result, _resolve) -> None:
        slide = _slide(slides, op.target)
        slide.notes_slide.notes_text_frame.text = op.text
        result.touched_parts.update(_notes_parts(slide))
        result.log.append(f"Updated the notes on {op.target}")

    def _op_set_chart_data(self, prs, slides, op, result, _resolve) -> None:
        slide_id, shape = _find_shape(slides, op.target)
        if not getattr(shape, "has_chart", False) or not shape.has_chart:
            raise EditError(f"{op.target} is not a chart")
        data = CategoryChartData()
        data.categories = op.categories
        for series in op.series:
            if len(series.values) != len(op.categories):
                raise EditError(f'Series "{series.name}" needs one value per category')
            data.add_series(series.name, series.values)
        shape.chart.replace_data(data)
        result.touched.add(slide_id)
        result.touched_parts.add(str(shape.chart.part.partname).lstrip("/"))
        workbook = shape.chart.part.chart_workbook.xlsx_part
        if workbook is not None:
            result.touched_parts.add(str(workbook.partname).lstrip("/"))
        result.log.append(f"Replaced the data in chart {op.target}")

    def _op_replace_image(self, prs, slides, op, result, resolve_image) -> None:
        if resolve_image is None:
            raise EditError("No image file is available for this edit")
        slide_id, shape = _find_shape(slides, op.target)
        if shape.shape_type != MSO_SHAPE_TYPE.PICTURE:
            raise EditError(f"{op.target} is not a picture")
        _, rid = shape.part.get_or_add_image_part(str(resolve_image(op.file_id)))
        shape._element.blipFill.find(qn("a:blip")).set(qn("r:embed"), rid)
        result.touched.add(slide_id)
        result.log.append(f"Swapped picture {op.target}; position and size unchanged")

    def _op_find_replace(self, prs, slides, op, result, _resolve) -> None:
        total = 0
        for slide_id, slide in slides.items():
            hits = find_replace_in_text_body(slide.shapes._spTree, op.find, op.replace)
            if hits:
                result.touched.add(slide_id)
                total += hits
            if slide.has_notes_slide:
                hits = find_replace_in_text_body(slide.notes_slide.element, op.find, op.replace)
                if hits:
                    result.touched_parts.update(_notes_parts(slide))
                    total += hits
        if not total:
            raise EditError(f'"{op.find}" does not appear on any slide')
        result.log.append(f'Replaced "{op.find}" {total} time(s)')

    def _op_set_sources(self, prs, slides, op, result, _resolve) -> None:
        """Per-slide attribution belongs in the notes, where a slide's design isn't affected."""
        if not op.sources:
            return
        lines = []
        for source in op.sources:
            bits = [source.title, source.locator, source.url]
            if source.retrieved_at:
                bits.append(f"Retrieved {source.retrieved_at}")
            lines.append(f"[{source.key}] " + ". ".join(b.rstrip(".") for b in bits if b))

        by_key = {source.key: line for source, line in zip(op.sources, lines)}
        for slide in prs.slides:
            text = " ".join(
                s.text_frame.text for s in _all_shapes(slide.shapes) if s.has_text_frame
            )
            cited = [by_key[k] for k in re.findall(r"\[(S\d+)\]", text) if k in by_key]
            if not cited:
                continue
            frame = slide.notes_slide.notes_text_frame
            existing = frame.text.split(SOURCES_PREFIX)[0].rstrip()
            block = SOURCES_PREFIX + "\n" + "\n".join(dict.fromkeys(cited))
            frame.text = f"{existing}\n\n{block}" if existing else block
            result.touched_parts.update(_notes_parts(slide))
        result.log.append(f"Added {len(op.sources)} source(s) to the speaker notes")

    def preservation_checks(self, src: Path, dest: Path, result: ApplyResult) -> list[Check]:
        before = part_fingerprints(src)
        after = part_fingerprints(dest)

        design = ("ppt/slideMasters/", "ppt/slideLayouts/", "ppt/theme/")
        changed = [
            n
            for n, h in before.items()
            if n.startswith(design) and after.get(n) != h and not n.endswith(".rels")
        ]
        media_before = {h for n, h in before.items() if n.startswith("ppt/media/")}
        media_after = {h for n, h in after.items() if n.startswith("ppt/media/")}
        swapped = any(line.startswith("Swapped picture") for line in result.log)
        removed_slides = any(t for t in result.touched if re.fullmatch(r"s\d+", t))
        layouts_count = sum(1 for n in before if n.startswith("ppt/slideLayouts/slideLayout"))

        checks = [
            Check(
                "Masters, layouts and theme unchanged",
                not changed,
                "Changed: " + ", ".join(changed) if changed else f"{layouts_count} layouts identical",
            ),
            Check(
                "Logos and images kept",
                media_before <= media_after or swapped or removed_slides,
                f"{len(media_before)} image(s) in the original",
            ),
        ]

        src_prs = Presentation(str(src))
        dest_prs = Presentation(str(dest))
        same_size = (src_prs.slide_width, src_prs.slide_height) == (dest_prs.slide_width, dest_prs.slide_height)
        checks.append(Check("Slide size unchanged", same_size))

        final = [element_hash(slide.shapes._spTree) for slide in dest_prs.slides]
        pool = list(final)
        untouched = 0
        problem = None
        for index, slide in enumerate(src_prs.slides):
            if f"s{index}" in result.touched:
                continue
            untouched += 1
            digest = element_hash(slide.shapes._spTree)
            if digest in pool:
                pool.remove(digest)
            elif problem is None:
                problem = f"s{index}"
        checks.append(
            Check(
                "Untouched slides are identical",
                problem is None,
                f"{untouched} slide(s) checked" if problem is None else f"{problem} changed without being targeted",
            )
        )

        # A template that already overflowed somewhere isn't this edit's problem.
        existing = text_overflows(src)
        overflow = [detail for text, detail in text_overflows(dest).items() if text not in existing]
        checks.append(Check("Text fits its boxes", not overflow, "; ".join(overflow)))
        return checks


def _shape_summary(shape: Any, prefix: str = "") -> dict[str, Any]:
    entry: dict[str, Any] = {
        "id": f"{prefix}sh{shape.shape_id}",
        "name": shape.name,
        "kind": _shape_kind(shape),
        **_geometry(shape),
    }
    if shape.is_placeholder:
        entry["placeholder_idx"] = shape.placeholder_format.idx
        entry["placeholder_type"] = _placeholder_type(shape)
    if shape.has_text_frame and shape.text_frame.text.strip():
        entry["paragraphs"] = [p.text for p in shape.text_frame.paragraphs]
    if getattr(shape, "has_table", False) and shape.has_table:
        entry["rows"] = [[cell.text for cell in row.cells] for row in shape.table.rows]
    if getattr(shape, "has_chart", False) and shape.has_chart:
        chart = shape.chart
        entry["chart_type"] = str(chart.chart_type).split(" ")[0]
        plot = chart.plots[0]
        entry["categories"] = [str(c) for c in plot.categories]
        entry["series"] = [{"name": s.name, "values": list(s.values)} for s in plot.series]
    if shape.shape_type == MSO_SHAPE_TYPE.PICTURE:
        entry["image_sha256"] = sha256(shape.image.blob)
        entry["image_type"] = shape.image.content_type
    return entry


def _slide(slides: dict[str, Any], slide_id: str) -> Any:
    slide = slides.get(slide_id)
    if slide is None:
        raise EditError(f"{slide_id} is not a slide in this deck")
    return slide


def _shape_by_id(slide: Any, shape_ref: str) -> Any:
    wanted = int(shape_ref.removeprefix("sh"))
    return next((s for s in _all_shapes(slide.shapes) if s.shape_id == wanted), None)


def _find_shape(slides: dict[str, Any], target: str) -> tuple[str, Any]:
    match = re.fullmatch(r"(s\d+)\.(sh\d+)", target)
    if not match:
        raise EditError(f"{target} is not a shape id. Use the form s2.sh5")
    shape = _shape_by_id(_slide(slides, match[1]), match[2])
    if shape is None:
        raise EditError(f"{match[1]} has no shape {match[2]}")
    return match[1], shape


def _match_placeholder(slide: Any, wanted: str, filled: set[int]) -> Any:
    free = [p for p in slide.placeholders if p.placeholder_format.idx not in filled]
    if wanted.isdigit():
        return next((p for p in free if p.placeholder_format.idx == int(wanted)), None)
    groups = {
        "title": (PP_PLACEHOLDER.TITLE, PP_PLACEHOLDER.CENTER_TITLE),
        "subtitle": (PP_PLACEHOLDER.SUBTITLE,),
        "body": (PP_PLACEHOLDER.BODY, PP_PLACEHOLDER.OBJECT, PP_PLACEHOLDER.SUBTITLE),
    }
    types = groups.get(wanted.lower())
    if types is None:
        return None
    return next((p for p in free if p.placeholder_format.type in types), None)


def _place_slide(prs: Any, slides: dict[str, Any], slide: Any, after: str | None, result: ApplyResult) -> None:
    id_list = prs.slides._sldIdLst
    entry = next(e for e in id_list if e.get("id") == str(slide.slide_id))
    id_list.remove(entry)
    if after is None:
        id_list.insert(0, entry)
        return
    # Several slides placed after the same one should keep the order they were asked for.
    anchor_slide = result.placed_after.get(after) or _slide(slides, after)
    anchor = next(e for e in id_list if e.get("id") == str(anchor_slide.slide_id))
    anchor.addnext(entry)
    result.placed_after[after] = slide


def _copy_footer_fields(prs: Any, slide: Any, layout: Any) -> None:
    """python-pptx skips footer and page-number placeholders on new slides.

    If the deck shows them on its other slides, a new slide should too.
    """
    footer_types = (PP_PLACEHOLDER.FOOTER, PP_PLACEHOLDER.SLIDE_NUMBER)
    wanted: set[Any] = set()
    for other in prs.slides:
        if other.slide_id == slide.slide_id:
            continue
        for shape in other.placeholders:
            if shape.placeholder_format.type in footer_types:
                wanted.add(shape.placeholder_format.type)
    for shape in layout.placeholders:
        if shape.placeholder_format.type in wanted:
            slide.shapes._spTree.append(copy.deepcopy(shape._element))


def _notes_parts(slide: Any) -> set[str]:
    part = slide.notes_slide.part
    return {str(part.partname).lstrip("/")}


def _inherited_size(shape: Any, level: int) -> float:
    """Walk run -> shape -> layout -> master for the font size, in points."""
    level_tag = f"a:lvl{level + 1}pPr"
    sources = [shape._element]
    if shape.is_placeholder:
        idx = shape.placeholder_format.idx
        layout = shape.part.slide_layout
        for candidate in layout.placeholders:
            if candidate.placeholder_format.idx == idx:
                sources.append(candidate._element)
                break
    for source in sources:
        for node in source.iter(qn(level_tag)):
            size = node.find(qn("a:defRPr"))
            if size is not None and size.get("sz"):
                return int(size.get("sz")) / 100

    if shape.is_placeholder:
        master = shape.part.slide_layout.slide_master
        kind = shape.placeholder_format.type
        # Footers and page numbers take their size from the master's own placeholder.
        if kind in (PP_PLACEHOLDER.FOOTER, PP_PLACEHOLDER.SLIDE_NUMBER, PP_PLACEHOLDER.DATE):
            for candidate in master.placeholders:
                if candidate.placeholder_format.type == kind:
                    for node in candidate._element.iter(qn(level_tag)):
                        size = node.find(qn("a:defRPr"))
                        if size is not None and size.get("sz"):
                            return int(size.get("sz")) / 100
            return 12.0
        style_name = "p:titleStyle" if kind in (PP_PLACEHOLDER.TITLE, PP_PLACEHOLDER.CENTER_TITLE) else "p:bodyStyle"
        styles = master._element.find(qn("p:txStyles"))
        if styles is not None:
            style = styles.find(qn(style_name))
            if style is not None:
                node = style.find(qn(level_tag))
                if node is not None and node.find(qn("a:defRPr")) is not None:
                    size = node.find(qn("a:defRPr")).get("sz")
                    if size:
                        return int(size) / 100
    return 18.0


def text_overflows(path: Path) -> dict[str, str]:
    """Rough fit estimate, keyed by the overflowing text.

    It can't replace looking at a render, but it catches the obvious.
    """
    problems: dict[str, str] = {}
    prs = Presentation(str(path))
    for index, slide in enumerate(prs.slides):
        for shape in _all_shapes(slide.shapes):
            if not shape.has_text_frame or not shape.text_frame.text.strip():
                continue
            if not shape.width or not shape.height:
                continue
            body_pr = shape.text_frame._txBody.find(qn("a:bodyPr"))
            if body_pr is not None and (
                body_pr.find(qn("a:normAutofit")) is not None or body_pr.find(qn("a:spAutoFit")) is not None
            ):
                continue

            frame = shape.text_frame
            inner_width_pt = (shape.width - (frame.margin_left or 0) - (frame.margin_right or 0)) / 12700
            inner_height_pt = (shape.height - (frame.margin_top or 0) - (frame.margin_bottom or 0)) / 12700
            if inner_width_pt <= 0:
                continue

            needed = 0.0
            for paragraph in frame.paragraphs:
                size = _inherited_size(shape, paragraph.level)
                explicit = next((r.font.size.pt for r in paragraph.runs if r.font.size), None)
                size = explicit or size
                chars_per_line = max(inner_width_pt / (size * 0.5), 1)
                lines = max(math.ceil(len(paragraph.text) / chars_per_line), 1)
                needed += lines * size * 1.2
            if needed > inner_height_pt * 1.1:
                problems[frame.text] = (
                    f"s{index}.sh{shape.shape_id} needs about {needed:.0f}pt of height but has {inner_height_pt:.0f}pt"
                )
    return problems
