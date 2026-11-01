"""Upload checks. Everything here runs before a file reaches a parser."""
import re
import zipfile
from dataclasses import dataclass, field
from io import BytesIO
from pathlib import PurePosixPath

from defusedxml import ElementTree as SafeET
from defusedxml.common import DefusedXmlException

from app.config import get_settings

OOXML_MAIN_PART = {"docx": "word/document.xml", "pptx": "ppt/presentation.xml", "xlsx": "xl/workbook.xml"}
IMAGE_MAGIC = {"png": b"\x89PNG\r\n\x1a\n", "jpg": b"\xff\xd8\xff", "jpeg": b"\xff\xd8\xff"}
TEXT_KINDS = {"txt", "md", "csv"}
ALLOWED = set(OOXML_MAIN_PART) | set(IMAGE_MAGIC) | TEXT_KINDS | {"pdf"}
MAX_ZIP_ENTRIES = 5000
MAX_COMPRESSION_RATIO = 200


class UnsafeUpload(Exception):
    pass


@dataclass
class Vetted:
    kind: str
    filename: str
    warnings: list[str] = field(default_factory=list)


def clean_filename(name: str) -> str:
    leaf = re.split(r"[\\/]", name or "")[-1]
    leaf = re.sub(r"[\x00-\x1f<>:\"|?*]", "_", leaf).strip(" .")
    return leaf[:200] or "upload"


def vet_upload(filename: str, data: bytes) -> Vetted:
    settings = get_settings()
    name = clean_filename(filename)
    kind = name.rsplit(".", 1)[-1].lower() if "." in name else ""
    if kind not in ALLOWED:
        allowed = ", ".join(sorted(ALLOWED))
        raise UnsafeUpload(f".{kind or '?'} files aren't accepted. Allowed: {allowed}")
    if len(data) > settings.max_upload_mb * 1024 * 1024:
        raise UnsafeUpload(f"This file is over the {settings.max_upload_mb} MB limit")
    if not data:
        raise UnsafeUpload("This file is empty")

    warnings: list[str] = []
    if kind in OOXML_MAIN_PART:
        warnings = _vet_ooxml(kind, data)
    elif kind in IMAGE_MAGIC:
        if not data.startswith(IMAGE_MAGIC[kind]):
            raise UnsafeUpload(f"This file isn't really a .{kind}")
    elif kind == "pdf":
        if not data.startswith(b"%PDF-"):
            raise UnsafeUpload("This file isn't really a PDF")
    else:
        if b"\x00" in data[:8192]:
            raise UnsafeUpload(f"This file isn't really .{kind} text")
    return Vetted(kind=kind, filename=name, warnings=warnings)


def _vet_ooxml(kind: str, data: bytes) -> list[str]:
    settings = get_settings()
    if not data.startswith(b"PK\x03\x04"):
        raise UnsafeUpload(f"This file isn't really a .{kind}")
    try:
        archive = zipfile.ZipFile(BytesIO(data))
    except zipfile.BadZipFile as exc:
        raise UnsafeUpload(f"This .{kind} file is damaged") from exc

    warnings: list[str] = []
    with archive:
        entries = archive.infolist()
        if len(entries) > MAX_ZIP_ENTRIES:
            raise UnsafeUpload("This file holds too many internal parts")
        total = sum(e.file_size for e in entries)
        if total > settings.max_unzipped_mb * 1024 * 1024:
            raise UnsafeUpload("This file is too large once unpacked")
        if total > MAX_COMPRESSION_RATIO * max(sum(e.compress_size for e in entries), 1):
            raise UnsafeUpload("This file is compressed suspiciously heavily")

        names = set()
        for entry in entries:
            path = PurePosixPath(entry.filename)
            if path.is_absolute() or ".." in path.parts or "\\" in entry.filename:
                raise UnsafeUpload("This file contains an unsafe internal path")
            names.add(entry.filename)

        if "[Content_Types].xml" not in names or OOXML_MAIN_PART[kind] not in names:
            raise UnsafeUpload(f"This file is not a valid .{kind}")
        if any(n.lower().endswith("vbaproject.bin") for n in names):
            raise UnsafeUpload("Files containing macros aren't accepted")

        for name in names:
            if not name.endswith((".xml", ".rels")):
                continue
            content = archive.read(name)
            try:
                SafeET.fromstring(content, forbid_dtd=True)
            except DefusedXmlException as exc:
                raise UnsafeUpload("This file contains XML entity declarations, which aren't allowed") from exc
            except SafeET.ParseError as exc:
                raise UnsafeUpload(f"Part {name} of this file is not valid XML") from exc
            if name.endswith(".rels") and b'TargetMode="External"' in content:
                if re.search(rb'Target="(file:|\\\\)', content):
                    raise UnsafeUpload("This file links to local files on the machine that opens it")
                warnings.append("Contains external links. They were kept as they are and never followed.")
    return list(dict.fromkeys(warnings))
