"""Turns uploaded files into located passages for the search index."""
from pathlib import Path

from langchain_text_splitters import RecursiveCharacterTextSplitter
from pypdf import PdfReader

from app.documents import ENGINES

_splitter = RecursiveCharacterTextSplitter(chunk_size=1100, chunk_overlap=150)


def passages_from(path: Path, kind: str) -> list[tuple[str, str]]:
    """Return (locator, text) pairs. The locator is what a citation will point at."""
    if kind == "docx":
        return _docx(path)
    if kind == "pptx":
        return _pptx(path)
    if kind == "xlsx":
        return _xlsx(path)
    if kind == "pdf":
        return _pdf(path)
    if kind in {"txt", "md", "csv"}:
        return _split("Full text", path.read_text(encoding="utf-8", errors="replace"))
    return []


def _split(locator: str, text: str) -> list[tuple[str, str]]:
    return [(locator, piece) for piece in _splitter.split_text(text) if piece.strip()]


def _docx(path: Path) -> list[tuple[str, str]]:
    sections: list[tuple[str, list[str]]] = [("Start of document", [])]
    for item in ENGINES["docx"].outline(path):
        if item["kind"] == "paragraph":
            if item.get("style", "").lower().startswith(("heading", "title")) and item["text"].strip():
                sections.append((f'Section "{item["text"].strip()[:120]}"', []))
            elif item["text"].strip():
                sections[-1][1].append(item["text"])
        else:
            for row in item["rows"]:
                sections[-1][1].append(" | ".join(cell["text"] for cell in row))
    out = []
    for locator, lines in sections:
        if lines:
            out.extend(_split(locator, "\n".join(lines)))
    return out


def _pptx(path: Path) -> list[tuple[str, str]]:
    out = []
    for index, slide in enumerate(ENGINES["pptx"].outline(path), start=1):
        lines = []
        for shape in slide["shapes"]:
            lines.extend(shape.get("paragraphs", []))
            for row in shape.get("rows", []):
                lines.append(" | ".join(row))
            for series in shape.get("series", []):
                lines.append(f"{series['name']}: " + ", ".join(
                    f"{c}={v}" for c, v in zip(shape.get("categories", []), series["values"])
                ))
        if slide.get("notes"):
            lines.append(f"Notes: {slide['notes']}")
        if lines:
            out.extend(_split(f"Slide {index}", "\n".join(lines)))
    return out


def _xlsx(path: Path) -> list[tuple[str, str]]:
    out = []
    for sheet in ENGINES["xlsx"].outline(path):
        lines = []
        for row in sheet["rows"]:
            lines.append(f"Row {row['row']}: " + " | ".join(str(c["value"]) for c in row["cells"]))
        if lines:
            out.extend(_split(f'Sheet "{sheet["id"]}"', "\n".join(lines)))
    return out


def _pdf(path: Path) -> list[tuple[str, str]]:
    out = []
    for number, page in enumerate(PdfReader(str(path)).pages, start=1):
        text = page.extract_text() or ""
        if text.strip():
            out.extend(_split(f"Page {number}", text))
    return out
