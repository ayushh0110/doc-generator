import re
from pathlib import Path
from typing import Any

from fastapi import APIRouter, Depends, HTTPException, status
from fastapi.responses import FileResponse
from pydantic import BaseModel
from sqlalchemy import select
from sqlalchemy.orm import Session

from app.api.files import MEDIA_TYPES
from app.db import get_db
from app.documents import ENGINES
from app.documents.preview import PreviewUnavailable, render_pages
from app.documents.validate import changes_between
from app.models import Artifact, ArtifactVersion, ChatSession, Citation, Message, User
from app.schemas import ArtifactOut, CitationOut, PreviewOut, VersionDetail, VersionOut
from app.security import current_user, owned_session
from app.storage import get_storage

router = APIRouter(tags=["artifacts"])


class RevertIn(BaseModel):
    version_id: str


def _owned_artifact(artifact_id: str, user: User, db: Session) -> Artifact:
    artifact = db.get(Artifact, artifact_id)
    if artifact is not None:
        chat = db.get(ChatSession, artifact.session_id)
        if chat is not None and chat.user_id == user.id:
            return artifact
    raise HTTPException(status.HTTP_404_NOT_FOUND, "Document not found")


def _owned_version(version_id: str, user: User, db: Session) -> ArtifactVersion:
    version = db.get(ArtifactVersion, version_id)
    if version is None:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "Version not found")
    _owned_artifact(version.artifact_id, user, db)
    return version


@router.get("/sessions/{session_id}/artifacts", response_model=list[ArtifactOut])
def list_artifacts(session_id: str, user: User = Depends(current_user), db: Session = Depends(get_db)) -> list[Artifact]:
    owned_session(session_id, user, db)
    return list(db.scalars(select(Artifact).where(Artifact.session_id == session_id).order_by(Artifact.created_at)).all())


@router.get("/versions/{version_id}", response_model=VersionDetail)
def version_detail(version_id: str, user: User = Depends(current_user), db: Session = Depends(get_db)) -> ArtifactVersion:
    return _owned_version(version_id, user, db)


@router.get("/versions/{version_id}/citations", response_model=list[CitationOut])
def version_citations(version_id: str, user: User = Depends(current_user), db: Session = Depends(get_db)) -> list[Citation]:
    _owned_version(version_id, user, db)
    return list(db.scalars(select(Citation).where(Citation.version_id == version_id)).all())


@router.get("/versions/{version_id}/download")
def download_version(version_id: str, user: User = Depends(current_user), db: Session = Depends(get_db)) -> FileResponse:
    version = _owned_version(version_id, user, db)
    artifact = version.artifact
    safe_title = re.sub(r"[^\w\- ]+", "", artifact.title).strip() or "document"
    return FileResponse(
        get_storage().path(version.storage_key),
        filename=f"{safe_title} v{version.number}.{artifact.kind}",
        media_type=MEDIA_TYPES[artifact.kind],
    )


def _pages(folder: Path) -> int:
    return len(list(folder.glob("page-*.png")))


@router.get("/versions/{version_id}/preview", response_model=PreviewOut)
def preview_version(version_id: str, user: User = Depends(current_user), db: Session = Depends(get_db)) -> PreviewOut:
    version = _owned_version(version_id, user, db)
    storage = get_storage()
    folder = storage.preview_dir(version.storage_key)
    if _pages(folder):
        return PreviewOut(pages=_pages(folder), available=True)
    try:
        return PreviewOut(pages=render_pages(storage.path(version.storage_key), folder), available=True)
    except PreviewUnavailable as exc:
        return PreviewOut(pages=0, available=False, reason=str(exc))


@router.get("/versions/{version_id}/preview/{page}")
def preview_page(page: int, version_id: str, user: User = Depends(current_user), db: Session = Depends(get_db)) -> FileResponse:
    version = _owned_version(version_id, user, db)
    path = get_storage().preview_dir(version.storage_key) / f"page-{page}.png"
    if page < 1 or not path.exists():
        raise HTTPException(status.HTTP_404_NOT_FOUND, "No such page")
    return FileResponse(path, media_type="image/png")


@router.get("/versions/{version_id}/structure")
def version_structure(version_id: str, user: User = Depends(current_user), db: Session = Depends(get_db)) -> dict[str, Any]:
    version = _owned_version(version_id, user, db)
    return ENGINES[version.artifact.kind].analyse(get_storage().path(version.storage_key))


@router.post("/artifacts/{artifact_id}/revert", response_model=VersionOut, status_code=status.HTTP_201_CREATED)
def revert_artifact(
    artifact_id: str, body: RevertIn, user: User = Depends(current_user), db: Session = Depends(get_db)
) -> ArtifactVersion:
    """Going back is itself a new version, so history is never rewritten."""
    artifact = _owned_artifact(artifact_id, user, db)
    source = next((v for v in artifact.versions if v.id == body.version_id), None)
    if source is None:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "Version not found")

    latest = artifact.versions[-1]
    if latest.id == source.id:
        raise HTTPException(status.HTTP_409_CONFLICT, "That is already the current version")

    storage = get_storage()
    engine = ENGINES[artifact.kind]
    key = storage.put_file(storage.path(source.storage_key), f".{artifact.kind}")
    version = ArtifactVersion(
        artifact_id=artifact.id,
        number=latest.number + 1,
        storage_key=key,
        summary=f"Restored version {source.number}.",
        instruction=f"Revert to version {source.number}",
        operations=[],
        checks=source.checks,
        changes=changes_between(engine.outline(storage.path(latest.storage_key)), engine.outline(storage.path(key))),
    )
    db.add(version)
    db.flush()
    for citation in source.citations:
        db.add(
            Citation(version_id=version.id, source_id=citation.source_id, element=citation.element, excerpt=citation.excerpt)
        )
    db.add(
        Message(
            session_id=artifact.session_id,
            role="assistant",
            content=f"Restored {artifact.title} to version {source.number}. This is saved as version {version.number}.",
            artifact_version_id=version.id,
        )
    )
    db.commit()
    db.refresh(version)
    return version
