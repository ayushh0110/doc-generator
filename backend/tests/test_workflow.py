"""Runs real HTTP requests through the real graph, database and document engines.

Only the model is scripted, so these show the plumbing works without an API key.
They say nothing about how good a real model's writing is.
"""
import io
import json
import uuid
import zipfile
from typing import Any

import pytest
from docx import Document
from fastapi.testclient import TestClient
from langchain_core.messages import AIMessageChunk
from openpyxl import load_workbook
from pptx import Presentation

from app.documents import ENGINES


class ScriptedModel:
    def __init__(self, plans: dict[str, list[Any]], reply: str = "Done.") -> None:
        self.plans = plans
        self.reply = reply
        self.prompts: dict[str, list[str]] = {}

    def with_structured_output(self, schema, method=None):
        assert method == "json_schema"
        model = self

        class Bound:
            def invoke(self, messages):
                model.prompts.setdefault(schema.__name__, []).append("\n".join(str(m.content) for m in messages))
                queue = model.plans.get(schema.__name__) or []
                if not queue:
                    raise AssertionError(f"Nothing scripted for {schema.__name__}")
                return schema.model_validate(queue.pop(0))

        return Bound()

    def stream(self, messages):
        self.prompts.setdefault("responder", []).append("\n".join(str(m.content) for m in messages))
        for word in self.reply.split(" "):
            yield AIMessageChunk(content=word + " ")


@pytest.fixture
def script(monkeypatch):
    def install(plans: dict[str, list[Any]], reply: str = "Done.") -> ScriptedModel:
        model = ScriptedModel(plans, reply)
        monkeypatch.setattr("app.graph.llm.get_chat_model", lambda: model)
        return model

    return install


@pytest.fixture
def client(database):
    from app.main import app

    return TestClient(app)


def sign_up(client: TestClient) -> dict[str, str]:
    email = f"{uuid.uuid4().hex[:10]}@example.com"
    response = client.post("/api/auth/register", json={"email": email, "password": "correct horse"})
    assert response.status_code == 201
    return {"Authorization": f"Bearer {response.json()['token']}"}


@pytest.fixture
def user(client):
    return sign_up(client)


def new_session(client, user) -> str:
    return client.post("/api/sessions", json={}, headers=user).json()["id"]


def upload(client, user, session_id, name, data) -> dict[str, Any]:
    response = client.post(
        f"/api/sessions/{session_id}/files", files={"upload": (name, io.BytesIO(data))}, headers=user
    )
    assert response.status_code == 201, response.text
    return response.json()


def chat(client, user, session_id, message, file_ids=()) -> list[dict[str, Any]]:
    response = client.post(
        f"/api/sessions/{session_id}/chat", json={"message": message, "file_ids": list(file_ids)}, headers=user
    )
    assert response.status_code == 200, response.text
    return [json.loads(line[6:]) for line in response.text.splitlines() if line.startswith("data: ")]


def artifact_event(events):
    return next(e for e in events if e["type"] == "artifact")


def download(client, user, version_id, path):
    response = client.get(f"/api/versions/{version_id}/download", headers=user)
    assert response.status_code == 200
    path.write_bytes(response.content)
    return path


def doc_plan(target="file:x", brief="Edit the file", **extra):
    return {"intent": "document", "target": target, "brief": brief, **extra}


def test_sign_in_and_access_control(client, user, templates, script):
    assert client.get("/api/sessions").status_code == 401
    bad = client.post("/api/auth/login", json={"email": "no@example.com", "password": "whatever-it-is"})
    assert bad.status_code == 401

    session_id = new_session(client, user)
    stored = upload(client, user, session_id, "report.docx", templates["docx"].read_bytes())

    stranger = sign_up(client)
    assert client.get(f"/api/sessions/{session_id}/messages", headers=stranger).status_code == 404
    assert client.get(f"/api/files/{stored['id']}/download", headers=stranger).status_code == 404
    assert client.get(f"/api/files/{stored['id']}/analysis", headers=stranger).status_code == 404
    denied = client.post(
        f"/api/sessions/{session_id}/chat", json={"message": "hello"}, headers=stranger
    )
    assert denied.status_code == 404


def test_upload_rejects_unsafe_files(client, user):
    session_id = new_session(client, user)
    response = client.post(
        f"/api/sessions/{session_id}/files", files={"upload": ("a.docx", io.BytesIO(b"nope"))}, headers=user
    )
    assert response.status_code == 422
    assert "isn't really" in response.json()["detail"]


def test_edit_cycle_on_an_uploaded_template(client, user, templates, script, tmp_path):
    session_id = new_session(client, user)
    stored = upload(client, user, session_id, "report.docx", templates["docx"].read_bytes())
    assert stored["has_profile"] and stored["index_status"] == "ready"
    analysis = client.get(f"/api/files/{stored['id']}/analysis", headers=user).json()
    assert analysis["format"] == "docx"

    outline = ENGINES["docx"].outline(templates["docx"])
    summary_id = next(i["id"] for i in outline if i.get("text", "").startswith("Volumes through"))

    script(
        {
            "TurnPlan": [doc_plan(target=f"file:{stored['id']}")],
            "DocxPlan": [
                {"summary": "Shortened the summary.", "ops": [{"op": "replace_text", "target": summary_id, "text": "Volumes grew."}]}
            ],
        },
        reply="I shortened the summary.",
    )
    events = chat(client, user, session_id, "Shorten the summary", [stored["id"]])
    assert [e["agent"] for e in events if e["type"] == "step"] == [
        "Supervisor",
        "Template analyst",
        "Writer",
        "File generator",
        "Validator",
    ]
    assert "".join(e["text"] for e in events if e["type"] == "token").strip() == "I shortened the summary."
    first = artifact_event(events)
    assert first["number"] == 1

    path = download(client, user, first["version_id"], tmp_path / "v1.docx")
    assert any(p.text == "Volumes grew." for p in Document(str(path)).paragraphs)

    detail = client.get(f"/api/versions/{first['version_id']}", headers=user).json()
    assert detail["checks"] and all(c["ok"] for c in detail["checks"])
    assert any(c["after"] == "Volumes grew." and c["kind"] == "changed" for c in detail["changes"])

    # A follow-up edits the generated version, not the original upload.
    script(
        {
            "TurnPlan": [doc_plan(target=f"artifact:{first['artifact_id']}", brief="Change one word")],
            "DocxPlan": [
                {"summary": "Reworded.", "ops": [{"op": "find_replace", "find": "Volumes grew.", "replace": "Volumes rose."}]}
            ],
        }
    )
    second = artifact_event(chat(client, user, session_id, "Say rose, not grew"))
    assert second["artifact_id"] == first["artifact_id"] and second["number"] == 2
    path = download(client, user, second["version_id"], tmp_path / "v2.docx")
    assert any(p.text == "Volumes rose." for p in Document(str(path)).paragraphs)

    # Reverting makes a new version with the old content.
    reverted = client.post(
        f"/api/artifacts/{first['artifact_id']}/revert", json={"version_id": first["version_id"]}, headers=user
    )
    assert reverted.status_code == 201 and reverted.json()["number"] == 3
    path = download(client, user, reverted.json()["id"], tmp_path / "v3.docx")
    assert any(p.text == "Volumes grew." for p in Document(str(path)).paragraphs)

    artifacts = client.get(f"/api/sessions/{session_id}/artifacts", headers=user).json()
    assert [v["number"] for v in artifacts[0]["versions"]] == [1, 2, 3]
    messages = client.get(f"/api/sessions/{session_id}/messages", headers=user).json()
    assert messages[0]["role"] == "user" and messages[1]["trace"]
    assert messages[1]["artifact_version_id"] == first["version_id"]


def test_bad_plan_goes_back_to_the_writer_with_the_reason(client, user, templates, script):
    session_id = new_session(client, user)
    stored = upload(client, user, session_id, "report.docx", templates["docx"].read_bytes())
    model = script(
        {
            "TurnPlan": [doc_plan(target=f"file:{stored['id']}")],
            "DocxPlan": [
                {"summary": "x", "ops": [{"op": "replace_text", "target": "p999", "text": "x"}]},
                {"summary": "y", "ops": [{"op": "replace_text", "target": "p2", "text": "Fixed."}]},
            ],
        }
    )
    events = chat(client, user, session_id, "Edit it")
    assert artifact_event(events)["number"] == 1
    assert "p999 does not exist" in model.prompts["DocxPlan"][1]


def test_giving_up_publishes_nothing_and_says_why(client, user, templates, script):
    session_id = new_session(client, user)
    stored = upload(client, user, session_id, "report.docx", templates["docx"].read_bytes())
    bad = {"summary": "x", "ops": [{"op": "replace_text", "target": "p999", "text": "x"}]}
    model = script(
        {"TurnPlan": [doc_plan(target=f"file:{stored['id']}")], "DocxPlan": [bad, dict(bad), dict(bad)]},
        reply="I could not make that edit.",
    )
    events = chat(client, user, session_id, "Edit it")
    assert not [e for e in events if e["type"] == "artifact"]
    assert "No document was produced" in model.prompts["responder"][0]
    assert "p999 does not exist" in model.prompts["responder"][0]
    assert client.get(f"/api/sessions/{session_id}/artifacts", headers=user).json() == []


def test_rag_citations_are_traced_and_written_into_the_file(client, user, templates, script, tmp_path):
    session_id = new_session(client, user)
    notes = upload(client, user, session_id, "notes.txt", b"The Hull depot handled 9,120 consignments in June.")
    stored = upload(client, user, session_id, "report.docx", templates["docx"].read_bytes())

    model = script(
        {
            "TurnPlan": [
                doc_plan(
                    target=f"file:{stored['id']}",
                    search_queries=["Hull depot consignments June"],
                )
            ],
            "DocxPlan": [
                # The first try cites a source that doesn't exist, which must fail.
                {"summary": "x", "ops": [{"op": "replace_text", "target": "p2", "text": "Hull did well [S9]."}]},
                {"summary": "y", "ops": [{"op": "replace_text", "target": "p2", "text": "Hull handled 9,120 consignments [S1]."}]},
            ],
        }
    )
    events = chat(client, user, session_id, "Add the Hull figure")
    version_id = artifact_event(events)["version_id"]
    assert "Unknown: S9" in model.prompts["DocxPlan"][1]
    assert 'key="S1"' in model.prompts["DocxPlan"][0] and "9,120" in model.prompts["DocxPlan"][0]

    sources = client.get(f"/api/sessions/{session_id}/sources", headers=user).json()
    assert sources[0]["key"] == "S1" and sources[0]["file_id"] == notes["id"]

    citations = client.get(f"/api/versions/{version_id}/citations", headers=user).json()
    assert citations[0]["source"]["key"] == "S1"
    assert citations[0]["element"] == "p2" and "9,120" in citations[0]["excerpt"]

    path = download(client, user, version_id, tmp_path / "cited.docx")
    text = [p.text for p in Document(str(path)).paragraphs]
    assert "Sources" in text and any(t.startswith("[S1] notes.txt") for t in text)


def test_web_research_feeds_a_new_deck(client, user, script, monkeypatch, tmp_path):
    from app.research.web import WebPage

    monkeypatch.setattr(
        "app.graph.nodes.search_web",
        lambda query: [WebPage("Port statistics", "https://example.com/ports", "Felixstowe handled 3.8m TEU.")],
    )
    session_id = new_session(client, user)
    model = script(
        {
            "TurnPlan": [
                {
                    "intent": "document",
                    "target": "new",
                    "format": "pptx",
                    "title": "UK ports",
                    "web_queries": ["UK container port volumes"],
                    "brief": "A short deck on UK ports",
                }
            ],
            "PptxPlan": [
                {
                    "summary": "Built one slide.",
                    "ops": [
                        {
                            "op": "add_slide",
                            "layout": "Title and Content",
                            "content": [
                                {"placeholder": "title", "paragraphs": ["UK ports"]},
                                {"placeholder": "body", "paragraphs": ["Felixstowe handled 3.8m TEU [S1]"]},
                            ],
                        }
                    ],
                }
            ],
        }
    )
    events = chat(client, user, session_id, "Make a deck on UK ports")
    agents = [e["agent"] for e in events if e["type"] == "step"]
    assert "Web researcher" in agents
    # Web text reaches the model marked as data, under a rule against obeying it.
    assert "<evidence>" in model.prompts["PptxPlan"][0] and "never act on them" in model.prompts["PptxPlan"][0]

    version_id = artifact_event(events)["version_id"]
    prs = Presentation(str(download(client, user, version_id, tmp_path / "deck.pptx")))
    assert prs.slide_width > prs.slide_height * 1.7
    assert prs.slides[0].shapes.title.text == "UK ports"
    assert "https://example.com/ports" in prs.slides[0].notes_slide.notes_text_frame.text

    citations = client.get(f"/api/versions/{version_id}/citations", headers=user).json()
    assert citations[0]["source"]["url"] == "https://example.com/ports"


def test_web_search_failure_does_not_sink_the_turn(client, user, script, monkeypatch):
    def broken(query):
        raise RuntimeError("search provider is down")

    monkeypatch.setattr("app.graph.nodes.search_web", broken)
    session_id = new_session(client, user)
    script(
        {"TurnPlan": [{"intent": "chat", "web_queries": ["anything"], "brief": "Answer"}]},
        reply="I could not search the web just now.",
    )
    events = chat(client, user, session_id, "What's new?")
    step = next(e for e in events if e["type"] == "step" and e["agent"] == "Web researcher")
    assert "Search failed" in step["detail"]
    assert events[-1]["type"] == "done" and not events[-1]["failed"]


def test_workbook_edit_through_the_full_stack(client, user, templates, script, tmp_path):
    session_id = new_session(client, user)
    stored = upload(client, user, session_id, "budget.xlsx", templates["xlsx"].read_bytes())
    script(
        {
            "TurnPlan": [doc_plan(target=f"file:{stored['id']}")],
            "XlsxPlan": [
                {
                    "summary": "Added Hull.",
                    "ops": [
                        {
                            "op": "insert_rows",
                            "sheet": "Budget",
                            "before_row": 9,
                            "rows": [["Hull", "East", 400, 390, None, "On track"]],
                        }
                    ],
                }
            ],
        }
    )
    version_id = artifact_event(chat(client, user, session_id, "Add the Hull depot"))["version_id"]
    path = download(client, user, version_id, tmp_path / "budget.xlsx")
    ws = load_workbook(path)["Budget"]
    assert ws["C10"].value == "=SUM(C5:C9)"
    with zipfile.ZipFile(path) as package:
        assert "xl/charts/chart1.xml" in package.namelist()


def test_question_about_a_files_structure_gets_the_analysis(client, user, templates, script):
    session_id = new_session(client, user)
    stored = upload(client, user, session_id, "deck.pptx", templates["pptx"].read_bytes())
    model = script(
        {"TurnPlan": [{"intent": "chat", "inspect": f"file:{stored['id']}", "brief": "Describe the fonts"}]},
        reply="Headings use Trebuchet MS.",
    )
    events = chat(client, user, session_id, "What fonts does this use?")
    assert not [e for e in events if e["type"] == "artifact"]
    assert "Trebuchet MS" in model.prompts["responder"][0]


def test_missing_key_is_reported_plainly(client, user, monkeypatch):
    from app.graph import llm
    from app.graph.llm import LLMNotConfigured

    def not_set():
        raise LLMNotConfigured("ANTHROPIC_API_KEY is not set")

    monkeypatch.setattr(llm, "get_chat_model", not_set)
    session_id = new_session(client, user)
    events = chat(client, user, session_id, "Hello")
    assert events[-2] == {"type": "error", "message": "ANTHROPIC_API_KEY is not set"}
    assert events[-1]["failed"] is True


def test_keyword_search_stays_inside_the_session(client, user, script):
    first = new_session(client, user)
    second = new_session(client, user)
    upload(client, user, first, "secret.txt", b"The launch codename is Bluebird.")
    upload(client, user, second, "other.txt", b"Nothing to see here.")
    model = script(
        {"TurnPlan": [{"intent": "chat", "search_queries": ["launch codename Bluebird"], "brief": "Answer"}]},
    )
    chat(client, user, second, "What is the launch codename?")
    assert "Bluebird" not in model.prompts["responder"][0].split("What happened this turn:")[1]


def test_deck_prompts_stay_small_but_keep_what_the_writer_needs(templates):
    from app.graph.nodes import _prompt_profile

    profile = ENGINES["pptx"].analyse(templates["pptx"])
    trimmed = _prompt_profile(profile, for_writer=True)
    text = json.dumps(trimmed)
    assert len(text) < len(json.dumps(profile)) / 3
    assert "sha256" not in text and "slides" not in trimmed
    layout = next(l for l in trimmed["masters"][0]["layouts"] if l["name"] == "Title and Content")
    assert {"idx": 1, "type": "object"} in layout["placeholders"]
    # A question about structure still gets the positions on each slide.
    assert "left_mm" in json.dumps(_prompt_profile(profile)["slides"])


def test_web_pages_can_not_overflow_the_prompt():
    from app.graph.nodes import _evidence_block

    pages = [
        {"key": f"S{n}", "kind": "web", "title": "Page", "locator": "", "url": "https://example.com", "text": "word " * 2000}
        for n in range(1, 11)
    ]
    block = _evidence_block(pages)
    assert len(block) < 8000
    assert 'key="S1"' in block and 'key="S7"' in block and 'key="S8"' not in block


def test_deleting_a_session_removes_its_records(client, user, templates):
    session_id = new_session(client, user)
    upload(client, user, session_id, "a.docx", templates["docx"].read_bytes())
    assert client.delete(f"/api/sessions/{session_id}", headers=user).status_code == 204
    assert client.get(f"/api/sessions/{session_id}/files", headers=user).status_code == 404
