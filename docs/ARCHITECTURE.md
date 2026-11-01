# Architecture

React talks to FastAPI over REST, and chat replies stream back as server-sent events. Each chat message runs through a LangGraph workflow. Postgres holds users, messages, sources and versions, and uploaded files live on local disk. Pinecone holds the search vectors, and LibreOffice renders previews.

## The agent workflow

The graph is in `backend/app/graph/builder.py` and the nodes are in `nodes.py`.

```
supervisor -> template_analyst -> retriever -> web_researcher -> writer
                                                                   |
                 responder <- publisher <- validator <- builder <--'
```

Steps get skipped when they aren't needed. A plain question goes from the supervisor straight to the responder, and research only runs if the supervisor asks for it.

- **supervisor** works out what the message wants: which file to work on, and whether it needs a search.
- **template_analyst** reads the file's structure and content.
- **retriever** searches this session's uploads, and **web_researcher** searches the web. Both register what they find as sources.
- **writer** produces the list of edits.
- **builder** applies the edits to the original file.
- **validator** compares the result with the original.
- **publisher** saves the new version with its changes and citations.
- **responder** writes the reply.

Only the supervisor, writer and responder call the model. Everything that touches a file is plain code, which is why the "nothing else changed" check can be tested at all.

If the edits can't be applied or a check fails, the reason goes back to the writer. After 3 attempts nothing is saved and the responder explains what went wrong.

LangGraph's Postgres checkpointer keeps state per session. `runner.py` resets the per-turn fields at the start of each message.

## Document engines

These are in `backend/app/documents/`. Each format has an engine with the same four methods: `analyse` (the structure shown in the UI), `outline` (the content, with the ids the writer has to use), `apply` and `preservation_checks`.

The edits themselves are Pydantic models in `ops.py`. They're also the schema the model has to follow, so it can't ask for anything the engines don't support.

**Word** uses python-docx plus some direct XML. New content is copied from the template's own examples, so a new heading is an existing heading with different text, and it keeps the real spacing and numbering. Tables work the same way and get resized to fit the template's width.

**PowerPoint** uses python-pptx. New slides come from the deck's real layouts and empty placeholders get removed. python-pptx doesn't put footers or page numbers on new slides, so the engine copies them over if the other slides have them.

**Excel** is the odd one out. openpyxl only reads here, because saving through it rebuilds the whole file and can lose anything it doesn't model. So edits patch the sheet XML directly and every other part stays byte-for-byte the same. The downside is that inserting a row means updating references by hand: formulas, names, tables, merged cells, conditional formats, validations, chart ranges and anchors. A range ending right above the new row gets extended, so a `SUM` picks up a row added above the total.

After an Excel edit the cached formula results are stale. They get cleared, LibreOffice recalculates a throwaway copy, and only the values are copied back. LibreOffice's rewritten file never reaches the user.

## What the validator checks

- The file still opens.
- Parts the plan didn't touch are unchanged, and so are the paragraphs, slides or cells it didn't target.
- No images were lost, and page setup and slide size are the same.
- New slide text fits its box. This is an estimate from font and box size.
- No formula errors in workbooks.
- Every `[S1]` style citation points at a real source.
- No placeholder text.

The checks protect the design, not the meaning. If the model writes a bad sentence in the right place, they'll still pass.

## Search and citations

On upload, text is split into passages that remember where they came from, like `Slide 4` or `Sheet "Budget"`. The text goes in Postgres and the vectors go in Pinecone, with one namespace per session. Results are checked against the session again in SQL, so one session can't see another's files. With no Pinecone key, search uses Postgres full-text instead.

Anything that feeds a turn gets a key like S1 that stays the same for the whole session, and the writer cites by key. Citations are read back from the finished file rather than from the plan, so deleting a cited sentence drops its source too. They end up in a `citations` table, in the file (a Sources section in Word, speaker notes in PowerPoint, a Sources sheet in Excel) and in the UI.

## Security

Uploads are checked before any parser sees them (`documents/safety.py`). The extension has to match the content, zip bombs and unsafe paths are rejected, and so is any XML with a DTD. Macro files and links to local files are refused too. Stored files get random names.

Users log in with JWTs, and passwords are hashed with Argon2. Every lookup checks ownership, and someone else's item returns 404. Web research only fetches public addresses and rechecks each redirect, so a search result can't point it at something internal.

Prompt injection can't be fully prevented. Documents and web pages are marked as data in the prompts, and the model has no tools, so the most it can do is return edits to the current file, and those still go through the validator. A hostile document could still affect the wording of a reply.

## Scaling

The API is nearly stateless, apart from login lockouts and the one-turn-per-session guard, which are in memory and would need Redis with several workers. Turns run on the request thread, which is fine for a proof of concept. Under real load I'd move turns to a job queue, files to S3, and LibreOffice to its own worker, since it only does one conversion at a time.

The API docs are at `/docs` when the server is running.
