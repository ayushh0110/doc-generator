"""Search over a session's uploads.

Pinecone when a key is configured. Without one, Postgres full-text search, so the
app still works on a laptop with no accounts set up. Vectors are namespaced by
session, which is what stops one user's documents surfacing in another's chat.
"""
import logging
import re
import time
from dataclasses import dataclass
from functools import lru_cache
from typing import Any

from pinecone import Pinecone
from sqlalchemy import func, select
from sqlalchemy.orm import Session

from app.config import get_settings
from app.models import Chunk, StoredFile

logger = logging.getLogger(__name__)
EMBED_BATCH = 90


@dataclass
class Hit:
    chunk_id: str
    file_id: str
    filename: str
    locator: str
    text: str
    score: float


@lru_cache
def _pinecone_index() -> tuple[Pinecone, Any] | None:
    settings = get_settings()
    if not settings.pinecone_api_key:
        return None
    client = Pinecone(api_key=settings.pinecone_api_key)
    if not client.has_index(settings.pinecone_index):
        client.create_index_for_model(
            name=settings.pinecone_index,
            cloud=settings.pinecone_cloud,
            region=settings.pinecone_region,
            embed={"model": settings.pinecone_embed_model, "field_map": {"text": "text"}},
        )
        while not client.describe_index(settings.pinecone_index).status["ready"]:
            time.sleep(1)
    return client, client.Index(settings.pinecone_index)


def search_backend() -> str:
    return "pinecone" if get_settings().pinecone_api_key else "postgres"


def index_passages(db: Session, session_id: str, file_id: str, passages: list[tuple[str, str]]) -> None:
    chunks = [
        Chunk(session_id=session_id, file_id=file_id, position=i, locator=locator[:255], text=text)
        for i, (locator, text) in enumerate(passages)
    ]
    db.add_all(chunks)
    db.flush()

    pinecone = _pinecone_index()
    if pinecone is None:
        return
    _, index = pinecone
    records = [
        {"_id": chunk.id, "text": chunk.text, "file_id": chunk.file_id, "locator": chunk.locator}
        for chunk in chunks
    ]
    for start in range(0, len(records), EMBED_BATCH):
        index.upsert_records(namespace=session_id, records=records[start : start + EMBED_BATCH])


def search(db: Session, session_id: str, query: str, limit: int = 6, file_ids: list[str] | None = None) -> list[Hit]:
    pinecone = _pinecone_index()
    if pinecone is None:
        return _keyword_search(db, session_id, query, limit, file_ids)
    try:
        return _vector_search(db, pinecone[1], session_id, query, limit, file_ids)
    except Exception:
        # A vector store outage shouldn't sink the whole turn.
        logger.exception("Pinecone search failed, falling back to Postgres")
        return _keyword_search(db, session_id, query, limit, file_ids)


def _vector_search(db: Session, index: Any, session_id: str, query: str, limit: int, file_ids: list[str] | None) -> list[Hit]:
    response = index.search(
        namespace=session_id,
        inputs={"text": query},
        top_k=limit,
        filter={"file_id": {"$in": file_ids}} if file_ids else None,
        fields=["file_id"],
    )
    scores = {hit.id: hit.score for hit in response.result.hits}
    if not scores:
        return []
    rows = db.execute(
        select(Chunk, StoredFile.filename)
        .join(StoredFile, Chunk.file_id == StoredFile.id)
        # The session filter stays on, whatever the vector store returned.
        .where(Chunk.id.in_(scores), Chunk.session_id == session_id)
    ).all()
    hits = [_hit(chunk, filename, scores[chunk.id]) for chunk, filename in rows]
    return sorted(hits, key=lambda h: h.score, reverse=True)


def _keyword_search(db: Session, session_id: str, query: str, limit: int, file_ids: list[str] | None) -> list[Hit]:
    # websearch_to_tsquery requires every word. Any of them will do here.
    terms = re.findall(r"[^\W_]+", query)
    if not terms:
        return []
    ts_query = func.to_tsquery("english", " | ".join(terms))
    rank = func.ts_rank(Chunk.search, ts_query)
    statement = (
        select(Chunk, StoredFile.filename, rank.label("rank"))
        .join(StoredFile, Chunk.file_id == StoredFile.id)
        .where(Chunk.session_id == session_id, Chunk.search.op("@@")(ts_query))
        .order_by(rank.desc())
        .limit(limit)
    )
    if file_ids:
        statement = statement.where(Chunk.file_id.in_(file_ids))
    return [_hit(chunk, filename, float(score)) for chunk, filename, score in db.execute(statement).all()]


def _hit(chunk: Chunk, filename: str, score: float) -> Hit:
    return Hit(chunk.id, chunk.file_id, filename, chunk.locator, chunk.text, score)


def forget_session(session_id: str) -> None:
    pinecone = _pinecone_index()
    if pinecone is None:
        return
    try:
        pinecone[1].delete(delete_all=True, namespace=session_id)
    except Exception:
        logger.exception("Could not delete Pinecone namespace %s", session_id)
