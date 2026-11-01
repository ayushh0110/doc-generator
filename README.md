# Redline

Chat assistant that creates and edits Word, PowerPoint and Excel files while keeping the original template intact.

Stack: Python, FastAPI, React, LangGraph, LangChain, Pinecone, PostgreSQL.

## How it works

The model doesn't write files. It returns a list of edits against ids in the document, like `replace_text p14`, `add_slide "Title and Content"` or `insert_rows Budget 9`. Python applies those to the uploaded file, then a validator compares the result with the original. If something changed that the plan didn't target, the plan goes back to the model with the reason. After three failed attempts nothing is saved and the user is told why.

Each edit creates a new version that can be compared with the last one, or restored.

Agents in the LangGraph workflow: supervisor, template analyst, retriever, web researcher, writer, file generator, validator, responder. See [docs/ARCHITECTURE.md](docs/ARCHITECTURE.md).

## Setup

Requires Python 3.12, Node 20+ and PostgreSQL 16. LibreOffice is optional, but page previews and workbook recalculation need it.

| Key | Required | Without it |
|---|---|---|
| `GROQ_API_KEY` | Yes, unless you use Anthropic or llama.cpp | The assistant can't reply |
| `PINECONE_API_KEY` | Yes for vector search | Falls back to Postgres full-text search |
| `TAVILY_API_KEY` | No | Web research uses DuckDuckGo |

### Database

On Windows this installs PostgreSQL for the current user without admin rights, starts it, creates the database and writes the connection string to `backend/.env`:

```bash
powershell -ExecutionPolicy Bypass -File scripts/setup_local_postgres.ps1
```

It doesn't run as a service, so run it again after a reboot.

Otherwise, create a database called `redline` and set `DATABASE_URL` in `backend/.env`.

### Backend

```bash
python -m venv .venv
```

Activate it (`.venv\Scripts\activate` on Windows, `source .venv/bin/activate` elsewhere), then:

```bash
pip install -r requirements.txt
```

Copy `backend/.env.example` to `backend/.env` and fill in the keys. Then, from `backend/`:

```bash
alembic upgrade head
```

```bash
uvicorn app.main:app --port 8000
```

### Frontend

From `frontend/`:

```bash
npm install
```

```bash
npm run dev
```

The app runs on http://localhost:5173.

### Other model options

Anthropic: set `LLM_PROVIDER=anthropic` and `ANTHROPIC_API_KEY`.

llama.cpp: start a server, then set `LLM_PROVIDER=llamacpp` and `LLAMACPP_BASE_URL`. Small models struggled with the edit plans when I tried them.

```bash
llama-server -m path/to/model.gguf -ngl 99 -c 16384 --parallel 1 --jinja --port 8080
```

### Docker

Put `POSTGRES_PASSWORD`, `JWT_SECRET`, `GROQ_API_KEY` and `PINECONE_API_KEY` in a `.env` next to `docker-compose.yml`:

```bash
docker compose up --build
```

The app is then on http://localhost:8080. I haven't run this compose file end to end, so it may need small fixes.

## Usage

Attach a file and say what should change. Some things to try with `samples/templates`:

| Attach | Say |
|---|---|
| `meridian_quarterly_report.docx` | Change the title to Q3 Network Review and the subtitle to say October instead of July |
| `meridian_quarterly_report.docx` | Using this template, write a one-page winter readiness plan |
| `meridian_board_deck.pptx` | Add a slide after the depot scorecard on what Hull changes for the network |
| `meridian_depot_budget.xlsx` | Add Hull above the total: region East, budget 400, actual 412, status Watch |
| Any of them | What fonts and margins does this use? |
| Nothing | Make a short deck on the Port of Felixstowe, with sources |

Follow-ups like "shorten the second section" apply to the latest version.

The panel on the right has tabs for the rendered preview, a redline against the previous version, the template structure, sources, and validator checks.

## Tests

```bash
python -m pytest backend/tests
```

These need PostgreSQL but no API keys. The model is scripted, so they cover the graph, database, engines and API, not the quality of a real model's output.

`python samples/generate_outputs.py` rebuilds the fixed sample outputs.

## Known limits

- Rewriting a paragraph keeps the first run's style plus bold, so a sentence with several colours comes back in one.
- Inserting workbook rows updates formulas, names, tables, merges, conditional formats, validations, charts and anchors, but not pivot table sources.
- A duplicated slide shares its chart with the original.
- The PPTX overflow check is an estimate from font and box size.
- Previews come from LibreOffice and can differ slightly from Office.
- Groq's free tier allows 8,000 tokens per minute. Short documents and decks are fine, with some waiting between calls, but a long document or large deck can go over the limit and fail with an error.
- Login lockouts and the one-turn-per-session guard are in memory, so several API workers would need Redis.
- Files are stored on local disk behind a small class in `storage.py`.
