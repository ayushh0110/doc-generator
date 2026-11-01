"""Runs one chat turn and turns the graph's stream into events for the browser."""
import logging
from collections.abc import Iterator
from typing import Any

import openai
from sqlalchemy import select

from app.db import SessionLocal
from app.graph.builder import get_graph
from app.graph.llm import LLMNotConfigured
from app.models import Message

logger = logging.getLogger(__name__)


def fresh_turn(session_id: str, message: str, file_ids: list[str], history: list[dict[str, str]]) -> dict[str, Any]:
    # The checkpointer keeps state per session, so everything per-turn is reset here.
    return {
        "session_id": session_id,
        "user_message": message,
        "attached_file_ids": file_ids,
        "history": history,
        "plan": {},
        "base": None,
        "profile": None,
        "outline": None,
        "evidence": [],
        "ops": [],
        "summary": "",
        "feedback": [],
        "attempts": 0,
        "candidate": None,
        "applied": None,
        "checks": [],
        "gave_up": False,
        "version_id": None,
        "published": None,
        "reply": "",
    }


def run_turn(session_id: str, message: str, file_ids: list[str], graph: Any = None) -> Iterator[dict[str, Any]]:
    """Yield events as the turn runs, and save the assistant's message at the end."""
    with SessionLocal() as db:
        rows = db.scalars(
            select(Message)
            .where(Message.session_id == session_id)
            .order_by(Message.created_at.desc())
            .limit(14)
        ).all()
        # The newest row is the message being answered, which the state carries separately.
        history = [{"role": m.role, "content": m.content} for m in reversed(rows[1:])]

    reply = ""
    trace: list[dict[str, str]] = []
    version_id = None
    failed = False
    config = {"configurable": {"thread_id": session_id}, "recursion_limit": 40}
    try:
        graph = graph if graph is not None else get_graph()
        for event in graph.stream(fresh_turn(session_id, message, file_ids, history), config, stream_mode="custom"):
            if event["type"] == "step":
                trace.append({"agent": event["agent"], "detail": event["detail"]})
            elif event["type"] == "token":
                reply += event["text"]
            elif event["type"] == "artifact":
                version_id = event["version_id"]
            yield event
    except LLMNotConfigured as exc:
        failed = True
        reply = str(exc)
        yield {"type": "error", "message": reply}
    except openai.APIConnectionError:
        failed = True
        reply = "Can't reach the model service. If you're using llama-server, check that it's running."
        yield {"type": "error", "message": reply}
    except openai.APIStatusError as exc:
        # Usually a rate limit, or a prompt too long for the model's context.
        failed = True
        logger.warning("The model service refused a request: %s", exc)
        reply = f"The model service refused the request: {exc.message}"
        yield {"type": "error", "message": reply}
    except Exception:
        failed = True
        logger.exception("Turn failed in session %s", session_id)
        reply = reply or "Something went wrong while working on that, and nothing was saved. Please try again."
        yield {"type": "error", "message": reply}

    with SessionLocal() as db:
        saved = Message(
            session_id=session_id,
            role="assistant",
            content=reply,
            artifact_version_id=version_id,
            trace=trace,
        )
        db.add(saved)
        db.commit()
        yield {"type": "done", "message_id": saved.id, "failed": failed}
