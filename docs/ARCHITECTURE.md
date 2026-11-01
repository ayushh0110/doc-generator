# Architecture

## Overview

```
Browser (React)
    |  REST and server-sent events
FastAPI
    |-- Auth, sessions, files, artifacts
    |-- LangGraph workflow, checkpointed in Postgres
    |       |-- Document engines: docx, pptx, xlsx
    |       |-- Search: Pinecone, or Postgres full-text
    |       |-- Web: Tavily, or DuckDuckGo plus a fetcher
    |       '-- LLM through LangChain: Groq, Anthropic or llama-server
    |-- PostgreSQL: users, messages, files, chunks, sources, versions, citations
    |-- Local file storage: uploads, versions, previews
    '-- LibreOffice: previews and workbook recalculation
```

## The agent graph

Defined in `backend/app/graph/builder.py`, with the nodes in `nodes.py`.

```
START -> supervisor
supervisor -> template_analyst   when a document or a file question is involved
           -> retriever          when uploaded files need searching
           -> web_researcher     when outside facts are needed
           -> responder          for plain chat

template_analyst -> retriever -> web_researcher -> writer (each skipped if not needed)

writer -> builder -> validator -> publisher -> responder -> END
   ^        |            |
   |        |            '-- failed checks go back to the writer, with the reasons
   |        '--------------- edits that can't apply go back too
   '------------------------ after 3 attempts: gave_up -> responder, nothing published
```

| Node | Job | Uses the model? |
|---|---|---|
| supervisor | Decides intent, target file and what research is needed | Yes |
| template_analyst | Reads the structure and content of the target | No |
| retriever | Searches this session's uploads and registers sources | No |
| web_researcher | Searches the web and registers sources | No |
| writer | Produces the edit plan | Yes |
| builder (file generator) | Applies the plan to the original file | No |
| validator | Compares the result with the source | No |
| publisher | Saves the version, the diff and the citations | No |
| responder | Writes the reply, streamed | Yes |

Only three nodes call the model. Everything that touches a file is deterministic, which is what makes the preservation guarantee testable.

State is checkpointed per session with `PostgresSaver`. Everything specific to a turn is reset at the start of that turn (`runner.py`).

## Document engines

`backend/app/documents/`. Each engine has the same four methods:

| Method | Returns |
|---|---|
| `analyse(path)` | The structure profile shown in the Structure tab and given to the writer |
| `outline(path)` | Content with the ids the writer must use |
| `apply(src, ops, dest)` | Writes the edited file, and reports what it was allowed to change |
| `preservation_checks(src, dest, result)` | Before and after comparison |

The operations are Pydantic models in `ops.py`. They double as the structured output schema, so the model can only ask for what the engines can do.

### DOCX

`python-docx` plus direct XML edits. New content is cloned from the template's own examples of each kind. A new heading is a copy of an existing heading with its text swapped, so it carries the real spacing and numbering. Tables are cloned too, resized to fit inside the template's total width.

### PPTX

`python-pptx` plus XML. Text edits reuse each paragraph's formatting. New slides use real layouts, and empty placeholders are removed. `python-pptx` doesn't copy footers or page numbers to new slides, so the engine does, when the deck's other slides have them.

### XLSX

Direct XML patching. `openpyxl` is only used for reading, because saving through it rebuilds the whole package and can lose what it doesn't model. Patching leaves every other part byte-for-byte as uploaded.

Inserting a row updates references across the workbook: formulas on all sheets, defined names, tables, merged cells, conditional formats, data validations, chart ranges and drawing anchors. A range ending directly above the insert is extended, so a `SUM` over a column picks up rows added right above the total.

After an edit, cached formula results are cleared and the calc chain is removed. LibreOffice then recalculates a throwaway copy, and only the values are copied back. That way previews and other readers see correct numbers, and LibreOffice's rewrite of the file never reaches the user.

## Validation

`validate.py` and each engine's `preservation_checks`:

- The file opens and parses after the edit.
- Package parts the plan didn't target are unchanged, by canonical XML hash or by exact bytes for XLSX.
- No images were lost.
- Page setup and slide size are unchanged.
- Body elements, slides or cells the plan didn't target are identical and in order.
- New PPTX text fits its box. Overflow that was already in the template doesn't count.
- Workbooks contain no formula errors.
- Every `[S#]` in new text refers to a real source.
- No placeholder text.

## RAG and citations

On upload, text is split into passages with a locator such as `Slide 4`, `Page 3`, `Section "Summary"` or `Sheet "Budget"`. Each passage's text is stored in Postgres, and in Pinecone when a key is set. Pinecone embeds it with its hosted model, in a namespace per session. Results are always re-checked against the session in SQL.

Each passage or page that feeds a turn gets a stable key within the session: S1, S2 and so on. The writer cites by key.

Citations are read back from the finished file, not from the plan, so deleting a cited sentence also drops its source. The result is written in three places:

| Where | How |
|---|---|
| Database | `citations` table: element, excerpt and source |
| In the file | DOCX Sources section, PPTX speaker notes, XLSX Sources sheet |
| UI | Click `S1` in a reply to jump to the source and see where this version uses it |

## Security

| Risk | Handling |
|---|---|
| Disguised files | Extension and content must agree |
| Zip bombs | Limits on entry count, unpacked size and compression ratio |
| XXE and entity bombs | Any XML part with a DTD is rejected, using `defusedxml` |
| Path traversal | Zip entry paths are checked, and stored names are random |
| Macros | Macro-enabled formats and `vbaProject.bin` are rejected |
| Links to local files | Rejected. Other external links are kept and flagged, and never followed |
| Access between users | Every lookup goes through ownership. Other users' items return 404 |
| Search leaks | Search is namespaced per session, and results are re-filtered in SQL |
| SSRF from web research | Fetches only reach public addresses, and each redirect is re-checked |
| Prompt injection | Documents and web text are wrapped as data, with an instruction not to obey them. The model also has no tools. It can only return edits to the current file, which the validator checks |
| Passwords and tokens | Argon2 hashes, signed JWTs that expire, and a lockout after repeated failed logins |

Prompt injection can't be fully prevented: a hostile document could still influence the wording of a reply. The design limits what such text could do, because no model output runs as code or reaches anything outside the file being edited.

## Data model

```
users 1-* chat_sessions 1-* messages
                        1-* files 1-* chunks
                        1-* sources
                        1-* artifacts 1-* artifact_versions 1-* citations *-1 sources
```

`alembic/versions` holds the schema. Reverting creates a new version, so history is never rewritten.

## API

Interactive docs are at `/docs` when the API is running.

| Method and path | Purpose |
|---|---|
| `POST /api/auth/register`, `POST /api/auth/login` | Accounts |
| `GET, POST /api/sessions`, `PATCH, DELETE /api/sessions/{id}` | Sessions |
| `GET /api/sessions/{id}/messages`, `/sources`, `/artifacts`, `/files` | Session contents |
| `POST /api/sessions/{id}/files` | Upload |
| `POST /api/sessions/{id}/chat` | Run a turn, streamed as SSE |
| `GET /api/files/{id}/analysis`, `/download` | Uploaded files |
| `GET /api/versions/{id}`, `/download`, `/preview`, `/preview/{page}`, `/structure`, `/citations` | Versions |
| `POST /api/artifacts/{id}/revert` | Restore a version |
| `GET /api/health` | What's configured |

## Scaling

The API holds no state a second instance would need, apart from the two in-memory guards noted in the README. Long turns run on the request thread and stream, which is fine for a proof of concept. Under real load, turns would move to a job queue with the same event stream, files would move to S3, and LibreOffice would become its own worker because it's limited to one conversion at a time per process.
