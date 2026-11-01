import os
import shutil
import subprocess
import tempfile
import threading
from pathlib import Path

import pypdfium2

COMMON_PATHS = [
    r"C:\Program Files\LibreOffice\program\soffice.exe",
    r"C:\Program Files (x86)\LibreOffice\program\soffice.exe",
    "/usr/bin/soffice",
    "/usr/lib/libreoffice/program/soffice",
    "/Applications/LibreOffice.app/Contents/MacOS/soffice",
]
MAX_PAGES = 40

_convert_lock = threading.Lock()


class PreviewUnavailable(Exception):
    pass


def find_soffice() -> str | None:
    configured = os.environ.get("SOFFICE_PATH")
    if configured and Path(configured).exists():
        return configured
    found = shutil.which("soffice")
    if found:
        return found
    return next((p for p in COMMON_PATHS if Path(p).exists()), None)


def render_pages(document: Path, out_dir: Path, scale: float = 1.4) -> int:
    """Render a document to page-N.png files. Returns the page count."""
    soffice = find_soffice()
    if soffice is None:
        raise PreviewUnavailable("LibreOffice is not installed on this server")

    out_dir.mkdir(parents=True, exist_ok=True)
    with tempfile.TemporaryDirectory() as tmp:
        tmp_path = Path(tmp)
        # LibreOffice falls over if two conversions share a profile, hence the lock
        # and the throwaway profile directory.
        with _convert_lock:
            try:
                subprocess.run(
                    [
                        soffice,
                        f"-env:UserInstallation=file:///{(tmp_path / 'profile').as_posix()}",
                        "--headless",
                        "--convert-to",
                        "pdf",
                        "--outdir",
                        str(tmp_path),
                        str(document),
                    ],
                    check=True,
                    capture_output=True,
                    timeout=180,
                )
            except subprocess.SubprocessError as exc:
                raise PreviewUnavailable(f"LibreOffice could not convert this file: {exc}") from exc

        pdf_path = tmp_path / f"{document.stem}.pdf"
        if not pdf_path.exists():
            raise PreviewUnavailable("LibreOffice did not produce a PDF")

        pdf = pypdfium2.PdfDocument(str(pdf_path))
        try:
            count = min(len(pdf), MAX_PAGES)
            for index in range(count):
                image = pdf[index].render(scale=scale).to_pil()
                image.save(out_dir / f"page-{index + 1}.png", optimize=True)
        finally:
            pdf.close()
    return count
