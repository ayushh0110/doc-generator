import json
import threading
from collections.abc import Iterator

from fastapi import APIRouter, Depends, HTTPException, status
from fastapi.responses import StreamingResponse
from sqlalchemy import select
from sqlalchemy.orm import Session

from app.db import get_db
from app.graph.runner import run_turn
from app.models import ChatSession, Message, Source, StoredFile, User
from app.research.index import forget_session
from app.schemas import ChatIn, MessageOut, SessionIn, SessionOut, SourceOut
from app.security import current_user, owned_session

router = APIRouter(prefix="/sessions", tags=["sessions"])

_busy: set[str] = set()
_busy_lock = threading.Lock()


@router.get("", response_model=list[SessionOut])
def list_sessions(user: User = Depends(current_user), db: Session = Depends(get_db)) -> list[ChatSession]:
    return list(
        db.scalars(
            select(ChatSession).where(ChatSession.user_id == user.id).order_by(ChatSession.updated_at.desc())
        ).all()
    )


@router.post("", response_model=SessionOut, status_code=status.HTTP_201_CREATED)
def create_session(body: SessionIn, user: User = Depends(current_user), db: Session = Depends(get_db)) -> ChatSession:
    chat = ChatSession(user_id=user.id, title=body.title)
    db.add(chat)
    db.commit()
    db.refresh(chat)
    return chat


@router.patch("/{session_id}", response_model=SessionOut)
def rename_session(
    session_id: str, body: SessionIn, user: User = Depends(current_user), db: Session = Depends(get_db)
) -> ChatSession:
    chat = owned_session(session_id, user, db)
    chat.title = body.title
    db.commit()
    db.refresh(chat)
    return chat


@router.delete("/{session_id}", status_code=status.HTTP_204_NO_CONTENT)
def delete_session(session_id: str, user: User = Depends(current_user), db: Session = Depends(get_db)) -> None:
    chat = owned_session(session_id, user, db)
    db.delete(chat)
    db.commit()
    forget_session(session_id)


@router.get("/{session_id}/messages", response_model=list[MessageOut])
def list_messages(session_id: str, user: User = Depends(current_user), db: Session = Depends(get_db)) -> list[Message]:
    owned_session(session_id, user, db)
    return list(
        db.scalars(select(Message).where(Message.session_id == session_id).order_by(Message.created_at)).all()
    )


@router.get("/{session_id}/sources", response_model=list[SourceOut])
def list_sources(session_id: str, user: User = Depends(current_user), db: Session = Depends(get_db)) -> list[Source]:
    owned_session(session_id, user, db)
    return list(db.scalars(select(Source).where(Source.session_id == session_id).order_by(Source.number)).all())


def _sse(events: Iterator[dict], session_id: str) -> Iterator[str]:
    try:
        for event in events:
            yield f"data: {json.dumps(event, ensure_ascii=False)}\n\n"
    finally:
        with _busy_lock:
            _busy.discard(session_id)


@router.post("/{session_id}/chat")
def chat(
    session_id: str, body: ChatIn, user: User = Depends(current_user), db: Session = Depends(get_db)
) -> StreamingResponse:
    session_row = owned_session(session_id, user, db)
    file_ids = list(dict.fromkeys(body.file_ids))
    if file_ids:
        found = db.scalars(
            select(StoredFile.id).where(StoredFile.id.in_(file_ids), StoredFile.session_id == session_id)
        ).all()
        if len(found) != len(file_ids):
            raise HTTPException(status.HTTP_404_NOT_FOUND, "An attached file isn't in this session")

    with _busy_lock:
        if session_id in _busy:
            raise HTTPException(status.HTTP_409_CONFLICT, "This session is still working on the last message")
        _busy.add(session_id)

    try:
        if session_row.title == "Untitled":
            session_row.title = body.message.strip().splitlines()[0][:70] or "Untitled"
        db.add(Message(session_id=session_id, role="user", content=body.message, file_ids=file_ids))
        db.commit()
    except Exception:
        with _busy_lock:
            _busy.discard(session_id)
        raise

    return StreamingResponse(
        _sse(run_turn(session_id, body.message, file_ids), session_id),
        media_type="text/event-stream",
        headers={"Cache-Control": "no-cache", "X-Accel-Buffering": "no"},
    )
