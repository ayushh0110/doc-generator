import datetime as dt
from typing import Any

from pydantic import BaseModel, ConfigDict, EmailStr, Field


class ORM(BaseModel):
    model_config = ConfigDict(from_attributes=True)


class Credentials(BaseModel):
    email: EmailStr
    password: str = Field(min_length=8, max_length=200)


class TokenOut(BaseModel):
    token: str
    email: str


class SessionIn(BaseModel):
    title: str = Field(default="Untitled", max_length=200)


class SessionOut(ORM):
    id: str
    title: str
    created_at: dt.datetime
    updated_at: dt.datetime


class MessageOut(ORM):
    id: str
    role: str
    content: str
    file_ids: list[str]
    artifact_version_id: str | None
    trace: list[dict[str, str]]
    created_at: dt.datetime


class ChatIn(BaseModel):
    message: str = Field(min_length=1, max_length=20000)
    file_ids: list[str] = Field(default_factory=list)


class FileOut(ORM):
    id: str
    filename: str
    kind: str
    size_bytes: int
    warnings: list[str]
    index_status: str
    index_error: str | None
    has_profile: bool = False
    created_at: dt.datetime


class VersionOut(ORM):
    id: str
    number: int
    summary: str
    instruction: str
    created_at: dt.datetime


class ArtifactOut(ORM):
    id: str
    title: str
    kind: str
    source_file_id: str | None
    created_at: dt.datetime
    versions: list[VersionOut]


class VersionDetail(VersionOut):
    checks: list[dict[str, Any]]
    changes: list[dict[str, Any]]
    operations: list[dict[str, Any]]


class SourceOut(ORM):
    id: str
    key: str
    kind: str
    title: str
    url: str | None
    file_id: str | None
    locator: str
    snippet: str
    retrieved_at: dt.datetime


class CitationOut(BaseModel):
    element: str
    excerpt: str
    source: SourceOut


class PreviewOut(BaseModel):
    pages: int
    available: bool
    reason: str | None = None
