import io
import zipfile

import pytest

from app.documents.safety import UnsafeUpload, clean_filename, vet_upload


def _zip(parts: dict[str, bytes]) -> bytes:
    buffer = io.BytesIO()
    with zipfile.ZipFile(buffer, "w", zipfile.ZIP_DEFLATED) as archive:
        for name, data in parts.items():
            archive.writestr(name, data)
    return buffer.getvalue()


MINIMAL = {"[Content_Types].xml": b"<Types/>", "word/document.xml": b"<document/>"}


def test_real_templates_pass(templates):
    for kind, path in templates.items():
        assert vet_upload(path.name, path.read_bytes()).kind == kind


def test_extension_and_content_must_agree():
    with pytest.raises(UnsafeUpload, match="isn't really"):
        vet_upload("report.docx", b"not a zip at all")
    with pytest.raises(UnsafeUpload, match="isn't really"):
        vet_upload("logo.png", b"GIF89a....")
    with pytest.raises(UnsafeUpload, match="not a valid"):
        vet_upload("deck.pptx", _zip(MINIMAL))


def test_blocked_types():
    with pytest.raises(UnsafeUpload, match="aren't accepted"):
        vet_upload("run.exe", b"MZ")
    with pytest.raises(UnsafeUpload, match="aren't accepted"):
        vet_upload("macro.docm", _zip(MINIMAL))


def test_macros_are_rejected():
    with pytest.raises(UnsafeUpload, match="macros"):
        vet_upload("a.docx", _zip({**MINIMAL, "word/vbaProject.bin": b"x"}))


def test_xml_entities_are_rejected():
    bomb = b'<!DOCTYPE lolz [<!ENTITY lol "lol">]><document>&lol;</document>'
    with pytest.raises(UnsafeUpload, match="entity"):
        vet_upload("a.docx", _zip({**MINIMAL, "word/document.xml": bomb}))


def test_zip_bombs_are_rejected():
    with pytest.raises(UnsafeUpload, match="compressed suspiciously|too large"):
        vet_upload("a.docx", _zip({**MINIMAL, "word/media/fill.xml": b"\0" * 50_000_000}))


def test_path_traversal_is_rejected():
    with pytest.raises(UnsafeUpload, match="unsafe internal path"):
        vet_upload("a.docx", _zip({**MINIMAL, "../../evil.xml": b"<x/>"}))


def test_links_to_local_files_are_rejected():
    rels = b'<Relationships><Relationship Id="r" Type="t" Target="file:///c:/secret" TargetMode="External"/></Relationships>'
    with pytest.raises(UnsafeUpload, match="local files"):
        vet_upload("a.docx", _zip({**MINIMAL, "word/_rels/document.xml.rels": rels}))


def test_filenames_are_cleaned():
    assert clean_filename("..\\..\\evil.docx") == "evil.docx"
    assert clean_filename("/etc/passwd") == "passwd"
    assert clean_filename("") == "upload"
