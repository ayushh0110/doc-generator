# Design notes

Why some things are built the way they are.

## The model returns edits, not documents

**Choice:** a fixed list of operations, applied by code.

**Why:** a template's value is in details a generator can't reproduce. If the model only chooses words and structure, it can't damage the design. It also makes preservation testable, because the code knows what it was asked to change and can verify nothing else moved.

**Cost:** the model can only do what an operation exists for. It can't, say, restyle a heading. For this brief, that's the right way round.

## Ids are positions, resolved before any edit runs

The outline labels things `p14` or `s2.sh5`. All ids in a plan are looked up against the original file before the first edit, so inserting a paragraph doesn't shift what a later id means.

## XLSX is patched as XML

`openpyxl` is the obvious tool, and I started by planning to use it, but saving through it rebuilds the workbook from its own object model. Patching the XML directly means reference shifting has to be written by hand, which is the most intricate code here. In exchange, styles, charts and images come through byte-for-byte, and a test asserts exactly that.

## Sync, not async

FastAPI endpoints and the graph are synchronous, using threads. `psycopg`'s async mode doesn't work on Windows' default event loop, and the heavy work (parsing, LibreOffice, model calls) gains little from async at this scale. The server-sent events stream comes from a generator running on a worker thread.

## Citations are read from the file

The plan is one source of truth, the file is another, and they drift when a later edit removes a sentence. Reading citations from the finished file means the Sources section can't list something the document no longer cites. It takes two passes: apply once to see what's cited, then apply again with the Sources list included.

## Validation sends plans back instead of patching them

When a check fails, the reason goes to the writer as feedback, up to three attempts. Quietly repairing a plan would hide problems, and publishing a doubtful file would be worse than saying it couldn't be done.

## Fallbacks without keys

Search falls back to Postgres full-text, and web search to DuckDuckGo. The fallback is shown in the sidebar, so a demo can't be mistaken for running on Pinecone when it isn't. Pinecone is what the brief asks for and is the default whenever a key is set.

## Model choice

Groq with `openai/gpt-oss-120b` is the default, because it has a free tier and enforces a JSON schema on its output. I tried a small local Gemma model on llama.cpp first. It ran fine but made too many judgement mistakes, like replacing a whole subtitle with one word. The provider is a setting, so Anthropic and llama.cpp still work.

Structured output uses `method="json_schema"` for every provider. LangChain's default relies on forced tool choice, which current Claude models reject.

Groq's free limit is 8,000 tokens per minute, and a request counts its prompt plus the reply it reserves. Deck prompts went over at first, so they now leave out what the model doesn't need: image hashes, layout positions, and slides already listed in the outline. The Structure tab still shows everything.

## What I'd do next

- Keep inline formatting when rewriting a paragraph, by matching runs to the new text.
- Move turns to a job queue so they survive an API restart.
- Add an eval set with a real model, scoring edits against expected results. The tests here prove the plumbing, not the writing.
- Show changes on the rendered pages, not just as text.
