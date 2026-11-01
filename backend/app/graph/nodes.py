import hashlib
import json
import logging
import uuid
from pathlib import Path
from typing import Any, Literal

from langchain_core.exceptions import OutputParserException
from langchain_core.messages import AIMessage, HumanMessage, SystemMessage
from langgraph.config import get_stream_writer
from pydantic import BaseModel, Field, TypeAdapter, ValidationError
from sqlalchemy import func, select

from app.config import get_settings
from app.db import SessionLocal
from app.documents import ENGINES
from app.documents.base import ApplyResult, EditError
from app.documents.defaults import default_template
from app.documents.ops import DocxOp, PptxOp, SetSources, XlsxOp
from app.documents.ops import Source as DocSource
from app.documents.validate import blocking_failures, changes_between, cited_elements, validate_plan
from app.graph import llm, prompts
from app.graph.state import TurnState
from app.models import Artifact, ArtifactVersion, Citation, Source, StoredFile
from app.research.index import search
from app.research.web import search_web
from app.storage import get_storage

logger = logging.getLogger(__name__)

OP_TYPES = {"docx": DocxOp, "pptx": PptxOp, "xlsx": XlsxOp}
OFFICE_KINDS = set(OP_TYPES)
IMAGE_KINDS = ("png", "jpg", "jpeg")
SNIPPET_LIMIT = 1500
HASH_KEYS = {"sha256", "image_sha256"}
MIN_CHARS_PER_SOURCE = 800
MAX_TITLE_CHARS = 120


class TurnPlan(BaseModel):
    intent: Literal["chat", "document"]
    target: str = Field(default="new", description='"new", "artifact:<id>" or "file:<id>"')
    format: Literal["docx", "pptx", "xlsx"] | None = None
    title: str = Field(default="", description="Short title for a new document")
    inspect: str | None = Field(default=None, description='"file:<id>" or "artifact:<id>" for a question about structure')
    search_queries: list[str] = Field(default_factory=list)
    web_queries: list[str] = Field(default_factory=list)
    brief: str


class DocxPlan(BaseModel):
    summary: str
    ops: list[DocxOp]


class PptxPlan(BaseModel):
    summary: str
    ops: list[PptxOp]


class XlsxPlan(BaseModel):
    summary: str
    ops: list[XlsxOp]


PLAN_TYPES = {"docx": DocxPlan, "pptx": PptxPlan, "xlsx": XlsxPlan}


def _step(agent: str, detail: str) -> None:
    get_stream_writer()({"type": "step", "agent": agent, "detail": detail})


def _compact(data: Any) -> str:
    return json.dumps(data, ensure_ascii=False, separators=(",", ":"), default=str)


def _without(data: Any, keys: set[str]) -> Any:
    if isinstance(data, dict):
        return {k: _without(v, keys) for k, v in data.items() if k not in keys}
    if isinstance(data, list):
        return [_without(item, keys) for item in data]
    return data


def _prompt_profile(profile: dict[str, Any], for_writer: bool = False) -> dict[str, Any]:
    """The Structure tab shows everything. A prompt only gets what the model can act on."""
    trimmed = _without(profile, HASH_KEYS)
    if trimmed.get("format") == "pptx":
        trimmed["masters"] = [
            {
                "name": master["name"],
                "layouts": [
                    {
                        "name": layout["name"],
                        "placeholders": [{"idx": p["idx"], "type": p["type"]} for p in layout["placeholders"]],
                    }
                    for layout in master["layouts"]
                ],
            }
            for master in trimmed["masters"]
        ]
        if for_writer:
            # The outline already lists every slide and shape.
            trimmed.pop("slides", None)
    return trimmed


def _conversation(history: list[dict[str, str]]) -> str:
    return "\n".join(f"{m['role']}: {m['content'][:2000]}" for m in history[-12:])


def supervisor(state: TurnState) -> dict[str, Any]:
    session_id = state["session_id"]
    with SessionLocal() as db:
        files = db.scalars(select(StoredFile).where(StoredFile.session_id == session_id)).all()
        artifacts = db.scalars(
            select(Artifact).where(Artifact.session_id == session_id).order_by(Artifact.created_at)
        ).all()
        file_lines = [
            f"file:{f.id} {f.filename} ({f.kind})" + (" [attached to this message]" if f.id in state["attached_file_ids"] else "")
            for f in files
        ]
        artifact_lines = [f"artifact:{a.id} {a.title} ({a.kind}, {len(a.versions)} version(s))" for a in artifacts]

    context = (
        f"<conversation>\n{_conversation(state['history'])}\n</conversation>\n\n"
        f"Files in this session:\n{chr(10).join(file_lines) or 'none'}\n\n"
        f"Artifacts in this session, oldest first:\n{chr(10).join(artifact_lines) or 'none'}\n\n"
        f"Current message: {state['user_message']}"
    )
    plan: TurnPlan = llm.structured(TurnPlan).invoke(
        [SystemMessage(prompts.SUPERVISOR), HumanMessage(context)]
    )

    valid_targets = {line.split(" ", 1)[0] for line in file_lines + artifact_lines}
    data = plan.model_dump()
    if plan.intent == "document" and plan.target != "new" and plan.target not in valid_targets:
        logger.warning("Supervisor named an unknown target %s", plan.target)
        data["target"] = "new"
    if plan.inspect and plan.inspect not in valid_targets:
        data["inspect"] = None
    if plan.intent == "document" and data["target"] == "new" and not plan.format:
        data["format"] = "docx"
    if not files:
        data["search_queries"] = []
    # A file attached as reference should always be read, even if routing skipped it.
    target_id = data["target"].split(":", 1)[-1]
    reference_files = [
        f for f in files if f.id in state["attached_file_ids"] and f.id != target_id and f.kind not in IMAGE_KINDS
    ]
    if reference_files and not data["search_queries"]:
        data["search_queries"] = [state["user_message"][:300]]

    what = "Working on a document" if plan.intent == "document" else "Answering a question"
    needs = []
    if data["search_queries"]:
        needs.append("your files")
    if data["web_queries"]:
        needs.append("the web")
    _step("Supervisor", what + (f", using {' and '.join(needs)}" if needs else ""))
    return {"plan": data}


def after_supervisor(state: TurnState) -> str:
    plan = state["plan"]
    if plan["intent"] == "document" or plan.get("inspect"):
        return "template_analyst"
    return _research_or_finish(state, done=set())


def _research_or_finish(state: TurnState, done: set[str]) -> str:
    plan = state["plan"]
    if "retriever" not in done and plan["search_queries"]:
        return "retriever"
    if "web_researcher" not in done and plan["web_queries"]:
        return "web_researcher"
    return "writer" if plan["intent"] == "document" else "responder"


def after_analyst(state: TurnState) -> str:
    if state.get("gave_up"):
        return "responder"
    return _research_or_finish(state, done=set())


def after_retriever(state: TurnState) -> str:
    return _research_or_finish(state, done={"retriever"})


def after_web(state: TurnState) -> str:
    return _research_or_finish(state, done={"retriever", "web_researcher"})


def _resolve_base(reference: str, plan: dict[str, Any], session_id: str) -> dict[str, Any]:
    storage = get_storage()
    with SessionLocal() as db:
        if reference.startswith("artifact:"):
            artifact = db.get(Artifact, reference.split(":", 1)[1])
            if artifact is None or artifact.session_id != session_id or not artifact.versions:
                raise EditError("That document is no longer available")
            latest = artifact.versions[-1]
            return {
                "kind": artifact.kind,
                "path": str(storage.path(latest.storage_key)),
                "file_id": artifact.source_file_id,
                "artifact_id": artifact.id,
                "title": artifact.title,
                "label": f"{artifact.title} v{latest.number}",
            }
        if reference.startswith("file:"):
            stored = db.get(StoredFile, reference.split(":", 1)[1])
            if stored is None or stored.session_id != session_id:
                raise EditError("That file is no longer available")
            if stored.kind not in OFFICE_KINDS:
                raise EditError(f"{stored.filename} can be read for facts, but only DOCX, PPTX and XLSX files can be edited")
            return {
                "kind": stored.kind,
                "path": str(storage.path(stored.storage_key)),
                "file_id": stored.id,
                "artifact_id": None,
                "title": plan.get("title") or Path(stored.filename).stem,
                "label": stored.filename,
            }

    kind = plan.get("format") or "docx"
    return {
        "kind": kind,
        "path": str(default_template(kind, storage.path("defaults"))),
        "file_id": None,
        "artifact_id": None,
        "title": plan.get("title") or "Untitled",
        "label": f"a blank {kind.upper()}",
    }


def _describe_profile(profile: dict[str, Any]) -> str:
    stats = profile.get("stats", {})
    theme = (profile.get("theme") or {}).get("name")
    bits = [
        f"{value} {name.replace('_', ' ').removesuffix('s') if value == 1 else name.replace('_', ' ')}"
        for name, value in stats.items()
        if value
    ]
    return ", ".join(bits) + (f"; theme {theme}" if theme else "")


def template_analyst(state: TurnState) -> dict[str, Any]:
    plan = state["plan"]
    reference = plan["target"] if plan["intent"] == "document" else plan["inspect"]
    try:
        base = _resolve_base(reference, plan, state["session_id"])
    except EditError as exc:
        return {"base": None, "profile": None, "outline": None, "gave_up": True, "feedback": [str(exc)]}

    engine = ENGINES[base["kind"]]
    path = Path(base["path"])
    profile = engine.analyse(path)
    outline = engine.outline(path)
    _step("Template analyst", f"Read {base['label']}: {_describe_profile(profile)}")
    return {"base": base, "profile": profile, "outline": outline}


def _register_source(db: Any, session_id: str, **fields: Any) -> Source:
    raw = "|".join(str(fields.get(k, "")) for k in ("kind", "url", "file_id", "locator", "title"))
    fingerprint = hashlib.sha256(raw.encode()).hexdigest()
    existing = db.scalar(select(Source).where(Source.session_id == session_id, Source.fingerprint == fingerprint))
    if existing is not None:
        return existing
    number = (db.scalar(select(func.max(Source.number)).where(Source.session_id == session_id)) or 0) + 1
    source = Source(session_id=session_id, number=number, fingerprint=fingerprint, **fields)
    db.add(source)
    db.flush()
    return source


def _evidence_entry(source: Source, text: str) -> dict[str, Any]:
    return {
        "key": source.key,
        "kind": source.kind,
        "title": source.title,
        "locator": source.locator,
        "url": source.url,
        "text": text,
    }


def retriever(state: TurnState) -> dict[str, Any]:
    session_id = state["session_id"]
    evidence = list(state.get("evidence") or [])
    seen = {e["key"] for e in evidence}
    with SessionLocal() as db:
        for query in state["plan"]["search_queries"]:
            for hit in search(db, session_id, query):
                source = _register_source(
                    db,
                    session_id,
                    kind="file",
                    title=hit.filename,
                    file_id=hit.file_id,
                    locator=hit.locator,
                    snippet=hit.text[:SNIPPET_LIMIT],
                )
                if source.key not in seen:
                    seen.add(source.key)
                    evidence.append(_evidence_entry(source, hit.text))
        db.commit()
    _step("Retriever", f"Found {len(evidence)} relevant passage(s) in your files")
    return {"evidence": evidence}


def web_researcher(state: TurnState) -> dict[str, Any]:
    session_id = state["session_id"]
    evidence = list(state.get("evidence") or [])
    seen = {e["key"] for e in evidence}
    found = 0
    problems = []
    with SessionLocal() as db:
        for query in state["plan"]["web_queries"]:
            try:
                pages = search_web(query)
            except Exception as exc:  # Search providers fail in many ways. The turn can go on without them.
                logger.warning("Web search failed for %r: %s", query, exc)
                problems.append(query)
                continue
            for page in pages:
                source = _register_source(
                    db,
                    session_id,
                    kind="web",
                    title=page.title[:MAX_TITLE_CHARS],
                    url=page.url,
                    snippet=page.text[:SNIPPET_LIMIT],
                )
                if source.key not in seen:
                    seen.add(source.key)
                    evidence.append(_evidence_entry(source, page.text))
                    found += 1
        db.commit()
    detail = f"Read {found} web page(s)"
    if problems:
        detail += f". Search failed for: {'; '.join(problems)}"
    _step("Web researcher", detail)
    return {"evidence": evidence}


def _evidence_block(evidence: list[dict[str, Any]]) -> str:
    if not evidence:
        return "<evidence>none</evidence>"
    # Sources are already best-first, so when space is tight the weakest ones go.
    budget = get_settings().evidence_chars
    kept = evidence[: max(budget // MIN_CHARS_PER_SOURCE, 1)]
    share = budget // len(kept)
    parts = []
    for item in kept:
        where = item["url"] or item["locator"] or ""
        text = item["text"]
        if len(text) > share:
            text = text[:share].rsplit(" ", 1)[0] + " ..."
        parts.append(f'<source key="{item["key"]}" title="{item["title"]}" where="{where}">\n{text}\n</source>')
    return "<evidence>\n" + "\n".join(parts) + "\n</evidence>"


def writer(state: TurnState) -> dict[str, Any]:
    base = state.get("base")
    if base is None:
        return {"gave_up": True}
    attempts = state.get("attempts", 0) + 1
    kind = base["kind"]
    system = f"{prompts.WRITER_COMMON}\n\n{prompts.WRITER_BY_FORMAT[kind]}"
    body = (
        f"Job: {state['plan']['brief']}\n\n"
        f"The user's words: {state['user_message']}\n\n"
        f"<conversation>\n{_conversation(state['history'])}\n</conversation>\n\n"
        f"Template analysis:\n<document>{_compact(_prompt_profile(state['profile'], for_writer=True))}</document>\n\n"
        f"Current content, with the ids to use:\n<document>{_compact(_without(state['outline'], HASH_KEYS))}</document>\n\n"
        f"{_evidence_block(state.get('evidence') or [])}"
    )
    attached_images = _attached_images(state)
    if attached_images:
        body += f"\n\nImages the user attached, usable as file_id: {_compact(attached_images)}"
    if state.get("feedback"):
        body += (
            "\n\nYour previous plan failed. Fix these problems and return a complete new plan:\n- "
            + "\n- ".join(state["feedback"])
            + f"\n\nThe previous plan was: {_compact(state.get('ops', []))}"
            + "\n\nThe summary should describe the new plan only, with no mention of this retry."
        )

    _step("Writer", f"Drafting the edits (attempt {attempts})" if attempts > 1 else "Drafting the edits")
    try:
        plan = llm.structured(PLAN_TYPES[kind]).invoke([SystemMessage(system), HumanMessage(body)])
    except (OutputParserException, ValidationError, ValueError) as exc:
        return {"attempts": attempts, "ops": [], "feedback": [f"The plan was not valid: {exc}"], "candidate": None}
    if not plan.ops:
        return {
            "attempts": attempts,
            "ops": [],
            "summary": plan.summary,
            "feedback": ["The plan had no operations, so nothing would change."],
            "candidate": None,
        }
    return {
        "attempts": attempts,
        "ops": [op.model_dump() for op in plan.ops],
        "summary": plan.summary,
        "feedback": [],
        "candidate": None,
    }


def after_writer(state: TurnState) -> str:
    if state.get("base") is None:
        return "responder"
    if state.get("ops"):
        return "builder"
    return _retry_or_stop(state)


def _attached_images(state: TurnState) -> list[dict[str, str]]:
    if not state["attached_file_ids"]:
        return []
    with SessionLocal() as db:
        files = db.scalars(
            select(StoredFile).where(
                StoredFile.id.in_(state["attached_file_ids"]), StoredFile.kind.in_(IMAGE_KINDS)
            )
        ).all()
        return [{"file_id": f.id, "name": f.filename} for f in files]


def _image_resolver(session_id: str) -> Any:
    def resolve(file_id: str) -> Path:
        with SessionLocal() as db:
            stored = db.get(StoredFile, file_id)
            if stored is None or stored.session_id != session_id or stored.kind not in IMAGE_KINDS:
                raise EditError(f"{file_id} is not an image attached in this session")
            return get_storage().path(stored.storage_key)

    return resolve


def _parse_ops(kind: str, raw: list[dict[str, Any]]) -> list[Any]:
    return TypeAdapter(list[OP_TYPES[kind]]).validate_python(raw)


def _sources_for(session_id: str, keys: list[str]) -> list[DocSource]:
    numbers = [int(k[1:]) for k in keys]
    with SessionLocal() as db:
        rows = db.scalars(
            select(Source)
            .where(Source.session_id == session_id, Source.number.in_(numbers))
            .order_by(Source.number)
        ).all()
        return [
            DocSource(
                key=s.key,
                title=s.title,
                locator=s.locator,
                url=s.url or "",
                retrieved_at=s.retrieved_at.date().isoformat() if s.retrieved_at else "",
            )
            for s in rows
        ]


def builder(state: TurnState) -> dict[str, Any]:
    base = state["base"]
    kind = base["kind"]
    if not state.get("ops"):
        return {"candidate": None, "applied": None}

    engine = ENGINES[kind]
    src = Path(base["path"])
    work = get_storage().path(f"work/{uuid.uuid4().hex}")
    work.mkdir(parents=True, exist_ok=True)
    draft = work / f"draft.{kind}"
    final = work / f"result.{kind}"
    try:
        ops = _parse_ops(kind, state["ops"])
        resolver = _image_resolver(state["session_id"])
        engine.apply(src, ops, draft, resolver)

        # What's cited is read from the result, so sources stay true across edits.
        keys = list(dict.fromkeys(key for key, _, _ in cited_elements(engine.outline(draft))))
        sources = _sources_for(state["session_id"], keys)
        result = engine.apply(src, [*ops, SetSources(sources=sources)], final, resolver)
    except (EditError, ValidationError) as exc:
        _cleanup(work)
        return {"candidate": None, "applied": None, "feedback": [str(exc)]}
    except Exception as exc:
        # A plan the engine chokes on is still the writer's to fix, not the end of the turn.
        logger.exception("Could not apply a plan")
        _cleanup(work)
        return {"candidate": None, "applied": None, "feedback": [f"The plan could not be applied: {exc}"]}

    _step("File generator", f"Applied {len(ops)} edit(s) to the original file")
    return {
        "candidate": str(final),
        "applied": {
            "touched": sorted(result.touched),
            "touched_parts": sorted(result.touched_parts),
            "log": result.log,
            "row_shifts": {k: [list(v) for v in shifts] for k, shifts in result.row_shifts.items()},
        },
    }


def after_builder(state: TurnState) -> str:
    if state.get("candidate"):
        return "validator"
    return _retry_or_stop(state)


def _retry_or_stop(state: TurnState) -> str:
    if state.get("attempts", 0) >= get_settings().max_writer_attempts:
        return "gave_up"
    return "writer"


def gave_up(state: TurnState) -> dict[str, Any]:
    _step("Validator", f"Stopped after {state.get('attempts', 0)} attempts")
    return {"gave_up": True}


def validator(state: TurnState) -> dict[str, Any]:
    base = state["base"]
    src = Path(base["path"])
    candidate = Path(state["candidate"])
    applied = state["applied"]
    result = ApplyResult(
        touched=set(applied["touched"]),
        touched_parts=set(applied["touched_parts"]),
        log=applied["log"],
        row_shifts={k: [tuple(v) for v in shifts] for k, shifts in applied["row_shifts"].items()},
    )
    with SessionLocal() as db:
        numbers = db.scalars(select(Source.number).where(Source.session_id == state["session_id"])).all()
    checks = validate_plan(src, candidate, result, _parse_ops(base["kind"], state["ops"]), {f"S{n}" for n in numbers})
    failures = blocking_failures(checks)
    passed = len(checks) - len(failures)
    if failures:
        _cleanup(candidate.parent)
        _step("Validator", f"{len(failures)} check(s) failed: {failures[0].name}")
    else:
        _step("Validator", f"All {passed} checks passed")
    return {
        "checks": [c.to_dict() for c in checks],
        "feedback": [f"{c.name}: {c.detail}" for c in failures],
    }


def after_validator(state: TurnState) -> str:
    if not state.get("feedback"):
        return "publisher"
    return _retry_or_stop(state)


def publisher(state: TurnState) -> dict[str, Any]:
    base = state["base"]
    kind = base["kind"]
    storage = get_storage()
    candidate = Path(state["candidate"])
    key = storage.put_file(candidate, f".{kind}")
    stored_path = storage.path(key)

    engine = ENGINES[kind]
    after_outline = engine.outline(stored_path)
    changes = changes_between(state.get("outline") or [], after_outline)

    with SessionLocal() as db:
        if base["artifact_id"]:
            artifact = db.get(Artifact, base["artifact_id"])
        else:
            artifact = Artifact(
                session_id=state["session_id"],
                title=base["title"][:255],
                kind=kind,
                source_file_id=base["file_id"],
            )
            db.add(artifact)
            db.flush()
        number = (artifact.versions[-1].number if artifact.versions else 0) + 1
        version = ArtifactVersion(
            artifact_id=artifact.id,
            number=number,
            storage_key=key,
            summary=state.get("summary", ""),
            instruction=state["user_message"],
            operations=state["ops"],
            checks=state.get("checks", []),
            changes=changes,
        )
        db.add(version)
        db.flush()

        sources = {
            s.key: s for s in db.scalars(select(Source).where(Source.session_id == state["session_id"])).all()
        }
        for source_key, element, excerpt in cited_elements(after_outline):
            if source_key in sources:
                db.add(Citation(version_id=version.id, source_id=sources[source_key].id, element=element[:120], excerpt=excerpt))
        db.commit()
        published = {
            "artifact_id": artifact.id,
            "version_id": version.id,
            "number": number,
            "title": artifact.title,
            "kind": kind,
        }

    _cleanup(candidate.parent)
    get_stream_writer()({"type": "artifact", **published})
    return {"version_id": published["version_id"], "published": published}


def _cleanup(folder: Path) -> None:
    for path in folder.glob("*"):
        path.unlink(missing_ok=True)
    folder.rmdir()


def responder(state: TurnState) -> dict[str, Any]:
    plan = state.get("plan") or {}
    lines = [f"Job: {plan.get('brief', state['user_message'])}"]
    if state.get("published"):
        published = state["published"]
        lines.append(f"Produced {published['title']} version {published['number']}. Summary: {state.get('summary', '')}")
        warnings = [c for c in state.get("checks", []) if not c["ok"]]
        if warnings:
            lines.append(f"Checks needing attention: {_compact(warnings)}")
    elif plan.get("intent") == "document":
        lines.append(f"No document was produced. Failures: {_compact(state.get('feedback', []))}")
    if plan.get("intent") == "chat" and state.get("profile"):
        lines.append(f"Analysis of the file:\n<document>{_compact(_prompt_profile(state['profile']))}</document>")
    if state.get("evidence"):
        lines.append(_evidence_block(state["evidence"]))

    messages: list[Any] = [SystemMessage(prompts.RESPONDER)]
    for item in state["history"][-8:]:
        cls = HumanMessage if item["role"] == "user" else AIMessage
        messages.append(cls(item["content"][:4000]))
    messages.append(HumanMessage(f"{state['user_message']}\n\nWhat happened this turn:\n" + "\n\n".join(lines)))

    writer_fn = get_stream_writer()
    reply = ""
    for chunk in llm.get_chat_model().stream(messages):
        text = chunk.text if isinstance(chunk.text, str) else chunk.text()
        if text:
            reply += text
            writer_fn({"type": "token", "text": text})
    return {"reply": reply}
