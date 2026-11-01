from typing import Any, TypedDict


class TurnState(TypedDict, total=False):
    session_id: str
    user_message: str
    attached_file_ids: list[str]
    history: list[dict[str, str]]

    plan: dict[str, Any]
    # What this turn edits: {kind, path, file_id, artifact_id, title}.
    base: dict[str, Any] | None
    profile: dict[str, Any] | None
    outline: list[dict[str, Any]] | None

    # One entry per passage: {key, kind, title, locator, url, text}.
    evidence: list[dict[str, Any]]

    ops: list[dict[str, Any]]
    summary: str
    feedback: list[str]
    attempts: int

    candidate: str | None
    applied: dict[str, Any] | None
    checks: list[dict[str, Any]]
    gave_up: bool

    version_id: str | None
    published: dict[str, Any] | None
    reply: str
