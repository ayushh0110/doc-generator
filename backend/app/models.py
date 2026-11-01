import datetime as dt
import uuid

from sqlalchemy import JSON, BigInteger, Computed, DateTime, ForeignKey, Index, Integer, String, Text, UniqueConstraint, func
from sqlalchemy.dialects.postgresql import TSVECTOR
from sqlalchemy.orm import Mapped, mapped_column, relationship

from app.db import Base


def new_id() -> str:
    return uuid.uuid4().hex


def _pk() -> Mapped[str]:
    return mapped_column(String(32), primary_key=True, default=new_id)


def _created() -> Mapped[dt.datetime]:
    return mapped_column(DateTime(timezone=True), server_default=func.now())


class User(Base):
    __tablename__ = "users"

    id: Mapped[str] = _pk()
    email: Mapped[str] = mapped_column(String(320), unique=True)
    password_hash: Mapped[str] = mapped_column(String(255))
    created_at: Mapped[dt.datetime] = _created()


class ChatSession(Base):
    __tablename__ = "chat_sessions"

    id: Mapped[str] = _pk()
    user_id: Mapped[str] = mapped_column(ForeignKey("users.id", ondelete="CASCADE"), index=True)
    title: Mapped[str] = mapped_column(String(200), default="Untitled")
    created_at: Mapped[dt.datetime] = _created()
    updated_at: Mapped[dt.datetime] = mapped_column(DateTime(timezone=True), server_default=func.now(), onupdate=func.now())


class Message(Base):
    __tablename__ = "messages"

    id: Mapped[str] = _pk()
    session_id: Mapped[str] = mapped_column(ForeignKey("chat_sessions.id", ondelete="CASCADE"), index=True)
    role: Mapped[str] = mapped_column(String(16))
    content: Mapped[str] = mapped_column(Text)
    file_ids: Mapped[list] = mapped_column(JSON, default=list)
    artifact_version_id: Mapped[str | None] = mapped_column(String(32), nullable=True)
    # The steps this turn went through, so the UI can show them again after a reload.
    trace: Mapped[list] = mapped_column(JSON, default=list)
    created_at: Mapped[dt.datetime] = _created()


class StoredFile(Base):
    __tablename__ = "files"

    id: Mapped[str] = _pk()
    session_id: Mapped[str] = mapped_column(ForeignKey("chat_sessions.id", ondelete="CASCADE"), index=True)
    filename: Mapped[str] = mapped_column(String(255))
    kind: Mapped[str] = mapped_column(String(16))
    size_bytes: Mapped[int] = mapped_column(BigInteger)
    storage_key: Mapped[str] = mapped_column(String(255))
    sha256: Mapped[str] = mapped_column(String(64))
    # Template analysis, for docx, pptx and xlsx.
    profile: Mapped[dict | None] = mapped_column(JSON, nullable=True)
    warnings: Mapped[list] = mapped_column(JSON, default=list)
    index_status: Mapped[str] = mapped_column(String(16), default="pending")
    index_error: Mapped[str | None] = mapped_column(Text, nullable=True)
    created_at: Mapped[dt.datetime] = _created()


class Chunk(Base):
    """Every chunk's text lives here. Pinecone holds the vectors and points back."""

    __tablename__ = "chunks"
    __table_args__ = (Index("ix_chunks_search", "search", postgresql_using="gin"),)

    id: Mapped[str] = _pk()
    session_id: Mapped[str] = mapped_column(ForeignKey("chat_sessions.id", ondelete="CASCADE"), index=True)
    file_id: Mapped[str] = mapped_column(ForeignKey("files.id", ondelete="CASCADE"), index=True)
    position: Mapped[int] = mapped_column(Integer)
    locator: Mapped[str] = mapped_column(String(255))
    text: Mapped[str] = mapped_column(Text)
    search = mapped_column(TSVECTOR, Computed("to_tsvector('english', text)", persisted=True))


class Source(Base):
    """A file passage or web page that fed a turn. S1, S2... stay stable within a session."""

    __tablename__ = "sources"
    __table_args__ = (UniqueConstraint("session_id", "number"), UniqueConstraint("session_id", "fingerprint"))

    id: Mapped[str] = _pk()
    session_id: Mapped[str] = mapped_column(ForeignKey("chat_sessions.id", ondelete="CASCADE"), index=True)
    number: Mapped[int] = mapped_column(Integer)
    kind: Mapped[str] = mapped_column(String(8))
    title: Mapped[str] = mapped_column(String(500))
    url: Mapped[str | None] = mapped_column(Text, nullable=True)
    file_id: Mapped[str | None] = mapped_column(String(32), nullable=True)
    locator: Mapped[str] = mapped_column(String(255), default="")
    snippet: Mapped[str] = mapped_column(Text, default="")
    fingerprint: Mapped[str] = mapped_column(String(64))
    retrieved_at: Mapped[dt.datetime] = _created()

    @property
    def key(self) -> str:
        return f"S{self.number}"


class Artifact(Base):
    __tablename__ = "artifacts"

    id: Mapped[str] = _pk()
    session_id: Mapped[str] = mapped_column(ForeignKey("chat_sessions.id", ondelete="CASCADE"), index=True)
    title: Mapped[str] = mapped_column(String(255))
    kind: Mapped[str] = mapped_column(String(8))
    source_file_id: Mapped[str | None] = mapped_column(String(32), nullable=True)
    created_at: Mapped[dt.datetime] = _created()

    versions: Mapped[list["ArtifactVersion"]] = relationship(
        back_populates="artifact", order_by="ArtifactVersion.number", cascade="all, delete-orphan"
    )


class ArtifactVersion(Base):
    __tablename__ = "artifact_versions"
    __table_args__ = (UniqueConstraint("artifact_id", "number"),)

    id: Mapped[str] = _pk()
    artifact_id: Mapped[str] = mapped_column(ForeignKey("artifacts.id", ondelete="CASCADE"), index=True)
    number: Mapped[int] = mapped_column(Integer)
    storage_key: Mapped[str] = mapped_column(String(255))
    summary: Mapped[str] = mapped_column(Text, default="")
    instruction: Mapped[str] = mapped_column(Text, default="")
    operations: Mapped[list] = mapped_column(JSON, default=list)
    checks: Mapped[list] = mapped_column(JSON, default=list)
    changes: Mapped[list] = mapped_column(JSON, default=list)
    created_at: Mapped[dt.datetime] = _created()

    artifact: Mapped[Artifact] = relationship(back_populates="versions")
    citations: Mapped[list["Citation"]] = relationship(cascade="all, delete-orphan")


class Citation(Base):
    """Which part of a generated version came from which source."""

    __tablename__ = "citations"

    id: Mapped[str] = _pk()
    version_id: Mapped[str] = mapped_column(ForeignKey("artifact_versions.id", ondelete="CASCADE"), index=True)
    source_id: Mapped[str] = mapped_column(ForeignKey("sources.id", ondelete="CASCADE"))
    element: Mapped[str] = mapped_column(String(120))
    excerpt: Mapped[str] = mapped_column(Text)

    source: Mapped[Source] = relationship()
