import hashlib
import re
import zipfile
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Callable, Protocol

from lxml import etree

ImageResolver = Callable[[str], Path]


class EditError(Exception):
    """An op that can't be applied. The message goes back to the writer verbatim."""


@dataclass
class ApplyResult:
    # Original ids of elements the ops were allowed to change.
    touched: set[str] = field(default_factory=set)
    # Package parts the ops were allowed to change, beyond the main document.
    touched_parts: set[str] = field(default_factory=set)
    log: list[str] = field(default_factory=list)
    # Only set by XLSX: sheet name -> (first row, shift) pairs from row edits.
    row_shifts: dict[str, list[tuple[int, int]]] = field(default_factory=dict)
    # Only set by PPTX: anchor slide id -> the last slide placed after it.
    placed_after: dict[str, Any] = field(default_factory=dict)


@dataclass
class Check:
    name: str
    ok: bool
    detail: str = ""
    # Warnings are shown to the user but don't send the plan back to the writer.
    severity: str = "error"

    def to_dict(self) -> dict[str, Any]:
        return {"name": self.name, "ok": self.ok, "detail": self.detail, "severity": self.severity}


class Engine(Protocol):
    extension: str

    def analyse(self, path: Path) -> dict[str, Any]: ...

    def outline(self, path: Path) -> list[dict[str, Any]]: ...

    def apply(self, src: Path, ops: list[Any], dest: Path, resolve_image: ImageResolver | None = None) -> ApplyResult: ...

    def preservation_checks(self, src: Path, dest: Path, result: ApplyResult) -> list[Check]: ...


def strip_list_marker(text: str) -> str:
    """Lists get their markers from the template, so a typed one would show up twice."""
    return re.sub(r"^\s*(?:[•●▪◦‣·*–—-]|\d+[.)])\s+", "", text)


def emu_to_mm(value: int | None) -> float | None:
    return None if value is None else round(value / 36000, 1)


def twips_to_mm(value: int | str | None) -> float | None:
    return None if value is None else round(int(value) / 56.6929, 1)


def sha256(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def canonical(xml: bytes) -> bytes:
    """Same meaning, same bytes. python-docx and python-pptx re-serialise on save."""
    return etree.tostring(etree.fromstring(xml), method="c14n")


def element_hash(element: Any) -> str:
    return sha256(etree.tostring(element, method="c14n"))


def part_fingerprints(path: Path) -> dict[str, str]:
    prints = {}
    with zipfile.ZipFile(path) as package:
        for name in package.namelist():
            data = package.read(name)
            if name.endswith((".xml", ".rels")):
                try:
                    data = canonical(data)
                except etree.XMLSyntaxError:
                    pass
            prints[name] = sha256(data)
    return prints


def theme_summary(xml: bytes) -> dict[str, Any]:
    ns = {"a": "http://schemas.openxmlformats.org/drawingml/2006/main"}
    root = etree.fromstring(xml)
    colors: dict[str, str] = {}
    for slot in root.xpath("//a:clrScheme/*", namespaces=ns):
        name = etree.QName(slot).localname
        value = slot.xpath("a:srgbClr/@val | a:sysClr/@lastClr", namespaces=ns)
        if value:
            colors[name] = "#" + value[0].upper()
    major = root.xpath("//a:majorFont/a:latin/@typeface", namespaces=ns)
    minor = root.xpath("//a:minorFont/a:latin/@typeface", namespaces=ns)
    return {
        "name": root.get("name"),
        "major_font": major[0] if major else None,
        "minor_font": minor[0] if minor else None,
        "colors": colors,
    }
