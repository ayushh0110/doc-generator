"""Talks to a stand-in llama-server over real HTTP, to pin down the wire format we rely on."""
import json
import threading
from http.server import BaseHTTPRequestHandler, HTTPServer
from typing import Any

import pytest
from langchain_core.messages import HumanMessage

from app.config import get_settings
from app.graph import llm
from app.graph.nodes import PptxPlan, TurnPlan, XlsxPlan


class Stub(BaseHTTPRequestHandler):
    requests: list[dict[str, Any]] = []
    content = ""

    def log_message(self, *args):
        pass

    def do_POST(self):
        body = json.loads(self.rfile.read(int(self.headers["Content-Length"])))
        Stub.requests.append({"path": self.path, "body": body, "authorization": self.headers.get("Authorization")})
        if body.get("stream"):
            self.send_response(200)
            self.send_header("Content-Type", "text/event-stream")
            self.end_headers()
            for word in ("Hello ", "there"):
                chunk = {
                    "id": "c",
                    "object": "chat.completion.chunk",
                    "created": 1,
                    "model": "local",
                    "choices": [{"index": 0, "delta": {"content": word}, "finish_reason": None}],
                }
                self.wfile.write(f"data: {json.dumps(chunk)}\n\n".encode())
            self.wfile.write(b"data: [DONE]\n\n")
            return
        payload = json.dumps(
            {
                "id": "c",
                "object": "chat.completion",
                "created": 1,
                "model": "local",
                "choices": [
                    {"index": 0, "finish_reason": "stop", "message": {"role": "assistant", "content": Stub.content}}
                ],
                "usage": {"prompt_tokens": 1, "completion_tokens": 1, "total_tokens": 2},
            }
        ).encode()
        self.send_response(200)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(payload)))
        self.end_headers()
        self.wfile.write(payload)


@pytest.fixture
def server(monkeypatch):
    Stub.requests = []
    http = HTTPServer(("127.0.0.1", 0), Stub)
    thread = threading.Thread(target=http.serve_forever, daemon=True)
    thread.start()
    settings = get_settings()
    monkeypatch.setattr(settings, "llm_provider", "llamacpp")
    monkeypatch.setattr(settings, "llamacpp_base_url", f"http://127.0.0.1:{http.server_port}/v1")
    yield http
    http.shutdown()


def test_structured_output_sends_a_schema_and_parses_the_reply(server):
    Stub.content = json.dumps(
        {
            "summary": "Added a row.",
            "ops": [{"op": "insert_rows", "sheet": "Budget", "before_row": 9, "rows": [["Hull", None, 400]]}],
        }
    )
    plan = llm.structured(XlsxPlan).invoke([HumanMessage("Add Hull")])
    assert plan.ops[0].op == "insert_rows"
    assert plan.ops[0].rows == [["Hull", None, 400]]

    request = Stub.requests[0]
    assert request["path"] == "/v1/chat/completions"
    fmt = request["body"]["response_format"]
    assert fmt["type"] == "json_schema"
    assert "ops" in fmt["json_schema"]["schema"]["properties"]
    # llama-server compiles this schema to a grammar, so the operations must survive in it.
    assert "insert_rows" in json.dumps(fmt["json_schema"]["schema"])
    assert "tools" not in request["body"]


@pytest.mark.parametrize("schema", [TurnPlan, XlsxPlan, PptxPlan])
def test_every_plan_schema_can_be_built(server, schema):
    Stub.content = "{}"
    try:
        llm.structured(schema).invoke([HumanMessage("x")])
    except Exception:
        pass  # An empty reply won't validate. Only the request matters here.
    assert Stub.requests[0]["body"]["response_format"]["type"] == "json_schema"


def test_streaming_replies(server):
    chunks = [chunk.text for chunk in llm.get_chat_model().stream([HumanMessage("Hi")])]
    assert "".join(c for c in chunks if isinstance(c, str)) == "Hello there"


def test_no_key_needed_for_a_local_model(server):
    assert llm.get_chat_model() is not None


def test_groq_sends_its_key_and_a_schema(server, monkeypatch):
    settings = get_settings()
    monkeypatch.setattr(settings, "llm_provider", "groq")
    monkeypatch.setattr(settings, "groq_api_key", "test-key")
    monkeypatch.setattr(settings, "groq_base_url", settings.llamacpp_base_url)
    Stub.content = json.dumps({"summary": "x", "ops": [{"op": "add_sheet", "name": "Notes"}]})
    plan = llm.structured(XlsxPlan).invoke([HumanMessage("Add a sheet")])
    assert plan.ops[0].name == "Notes"
    assert Stub.requests[0]["authorization"] == "Bearer test-key"
    assert Stub.requests[0]["body"]["model"] == settings.groq_model
    assert settings.llm_configured


def test_groq_without_a_key_says_so(monkeypatch):
    settings = get_settings()
    monkeypatch.setattr(settings, "llm_provider", "groq")
    monkeypatch.setattr(settings, "groq_api_key", None)
    with pytest.raises(llm.LLMNotConfigured, match="GROQ_API_KEY"):
        llm.get_chat_model()
