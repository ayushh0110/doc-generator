from pathlib import Path

from app.documents.base import Engine
from app.documents.docx_engine import DocxEngine
from app.documents.pptx_engine import PptxEngine
from app.documents.xlsx_engine import XlsxEngine

ENGINES: dict[str, Engine] = {"docx": DocxEngine(), "pptx": PptxEngine(), "xlsx": XlsxEngine()}


def engine_for(path: Path | str) -> Engine:
    suffix = Path(path).suffix.lower().lstrip(".")
    if suffix not in ENGINES:
        raise ValueError(f"No document engine for .{suffix} files")
    return ENGINES[suffix]
