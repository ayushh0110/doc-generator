import hashlib
import logging
from typing import Any

from fastapi import APIRouter, Depends, File, HTTPException, UploadFile, status
from fastapi.responses import FileResponse
from sqlalchemy import select
from sqlalchemy.orm import Session

from app.config import get_settings
from app.db import get_db
from app.documents import ENGINES
from app.documents.safety import UnsafeUpload, vet_upload
from app.models import ChatSession, StoredFile, User
from app.research.extract import passages_from
from app.research.index import index_passages
from app.schemas import FileOut
from app.security import current_user, owned_session
from app.storage import get_storage

logger = logging.getLogger(__name__)
router = APIRouter(tags=["files"])

MEDIA_TYPES = {
    "docx": "application/vnd.openxmlformats-officedocument.wordprocessingml.document",
    "pptx": "application/vnd.openxmlformats-officedocument.presentationml.presentation",
    "xlsx": "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
}


def _out(stored: StoredFile) -> FileOut:
    out = FileOut.model_validate(stored)
    out.has_profile = stored.profile is not None
    return out


def _owned_file(file_id: str, user: User, db: Session) -> StoredFile:
    stored = db.get(StoredFile, file_id)
    if stored is None:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "File not found")
    chat = db.get(ChatSession, stored.session_id)
    if chat is None or chat.user_id != user.id:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "File not found")
    return stored


@router.post("/sessions/{session_id}/files", response_model=FileOut, status_code=status.HTTP_201_CREATED)
def upload_file(
    session_id: str,
    upload: UploadFile = File(...),
    user: User = Depends(current_user),
    db: Session = Depends(get_db),
) -> FileOut:
    owned_session(session_id, user, db)
    limit = get_settings().max_upload_mb * 1024 * 1024
    data = upload.file.read(limit + 1)
    try:
        vetted = vet_upload(upload.filename or "upload", data)
    except UnsafeUpload as exc:
        raise HTTPException(status.HTTP_422_UNPROCESSABLE_CONTENT, str(exc)) from exc

    storage = get_storage()
    key = storage.put_bytes(data, f".{vetted.kind}")
    stored = StoredFile(
        session_id=session_id,
        filename=vetted.filename,
        kind=vetted.kind,
        size_bytes=len(data),
        storage_key=key,
        sha256=hashlib.sha256(data).hexdigest(),
        warnings=vetted.warnings,
    )
    path = storage.path(key)

    if vetted.kind in ENGINES:
        try:
            stored.profile = ENGINES[vetted.kind].analyse(path)
        except Exception as exc:  # Parsers raise all sorts on malformed files.
            logger.warning("Could not analyse upload: %s", exc)
            path.unlink(missing_ok=True)
            raise HTTPException(
                status.HTTP_422_UNPROCESSABLE_CONTENT, f"This {vetted.kind.upper()} could not be read"
            ) from exc

    db.add(stored)
    db.flush()

    # A failed index doesn't lose the upload. The file can still be edited.
    if vetted.kind in {"png", "jpg", "jpeg"}:
        stored.index_status = "skipped"
    else:
        try:
            with db.begin_nested():
                index_passages(db, session_id, stored.id, passages_from(path, vetted.kind))
            stored.index_status = "ready"
        except Exception as exc:
            logger.exception("Indexing failed for file %s", stored.id)
            stored.index_status = "failed"
            stored.index_error = str(exc)[:500]

    db.commit()
    db.refresh(stored)
    return _out(stored)


@router.get("/sessions/{session_id}/files", response_model=list[FileOut])
def list_files(session_id: str, user: User = Depends(current_user), db: Session = Depends(get_db)) -> list[FileOut]:
    owned_session(session_id, user, db)
    rows = db.scalars(select(StoredFile).where(StoredFile.session_id == session_id).order_by(StoredFile.created_at)).all()
    return [_out(r) for r in rows]


@router.get("/files/{file_id}/analysis")
def file_analysis(file_id: str, user: User = Depends(current_user), db: Session = Depends(get_db)) -> dict[str, Any]:
    stored = _owned_file(file_id, user, db)
    if stored.profile is None:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "This file type has no template analysis")
    return stored.profile


@router.get("/files/{file_id}/download")
def download_file(file_id: str, user: User = Depends(current_user), db: Session = Depends(get_db)) -> FileResponse:
    stored = _owned_file(file_id, user, db)
    return FileResponse(
        get_storage().path(stored.storage_key),
        filename=stored.filename,
        media_type=MEDIA_TYPES.get(stored.kind, "application/octet-stream"),
    )
