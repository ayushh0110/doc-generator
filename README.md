# Redline

You upload a Word, PowerPoint or Excel file, tell it what to change in chat, and get the same file back with the change made. The logo, styles, slide master and formulas stay where they were. It can also make new documents and pull facts from your files or the web, with citations.

Built with FastAPI, React, LangGraph, LangChain, Pinecone and PostgreSQL.

The main idea: the model never writes the file. It sends back a list of small edits like `replace_text p14` or `insert_rows Budget 9`, Python applies them to the original, and a validator compares the result with what you uploaded. If something changed that shouldn't have, the model gets the error and tries again. After 3 tries it gives up and tells you why.

More on how it fits together in [docs/ARCHITECTURE.md](docs/ARCHITECTURE.md).

## Running it

You need Python 3.12, Node 20+ and PostgreSQL 16. Install LibreOffice too if you want page previews and recalculated Excel values.

**Database.** On Windows this script installs Postgres for your user (no admin needed), starts it, creates the database and writes `DATABASE_URL` to `backend/.env`. It doesn't run as a service, so run it again after a reboot.

```bash
powershell -ExecutionPolicy Bypass -File scripts/setup_local_postgres.ps1
```

On anything else, create a database called `redline` and set `DATABASE_URL` yourself.

**Backend.** Copy `backend/.env.example` to `backend/.env` and add your keys. You need `GROQ_API_KEY` for the model. `PINECONE_API_KEY` is for vector search, and without it search falls back to Postgres full-text. `TAVILY_API_KEY` is optional, web research uses DuckDuckGo otherwise.

```bash
python -m venv .venv
.venv\Scripts\activate
pip install -r requirements.txt
cd backend
alembic upgrade head
uvicorn app.main:app --port 8000
```

(`source .venv/bin/activate` on Mac or Linux.)

**Frontend.**

```bash
cd frontend
npm install
npm run dev
```

Then open http://localhost:5173 and make an account.

### Using a different model

For Claude, set `LLM_PROVIDER=anthropic` and `ANTHROPIC_API_KEY`.

For a local model, start llama.cpp and set `LLM_PROVIDER=llamacpp` and `LLAMACPP_BASE_URL`. I tried a small Gemma model this way and it made too many mistakes on the edit plans, so use something bigger.

```bash
llama-server -m path/to/model.gguf -ngl 99 -c 16384 --parallel 1 --jinja --port 8080
```

### Docker

Put `POSTGRES_PASSWORD`, `JWT_SECRET`, `GROQ_API_KEY` and `PINECONE_API_KEY` in a `.env` next to `docker-compose.yml`, then run `docker compose up --build`. It serves on http://localhost:8080. I haven't run this compose file end to end, so it may need small fixes.

## Using it

Attach a file and say what you want. The files in `samples/templates` are good for trying it out:

- `meridian_quarterly_report.docx`: "Change the title to Q3 Network Review and the subtitle to say October instead of July"
- `meridian_board_deck.pptx`: "Add a slide after the depot scorecard on what Hull changes for the network"
- `meridian_depot_budget.xlsx`: "Add Hull above the total: region East, budget 400, actual 412, status Watch"
- Any of them: "What fonts and margins does this use?"
- No file: "Make a short deck on the Port of Felixstowe, with sources"

Follow-ups like "shorten the second section" apply to the latest version. The right panel shows the rendered pages, what changed, the template structure, the sources and the validator's checks. You can go back to any earlier version from there.

`samples/README.md` explains the sample files, including real output from the model.

## Tests

```bash
python -m pytest backend/tests
```

These need Postgres but no API keys. The model is scripted, so they test the graph, database, engines and API, not how good a real model's writing is.

## Known limits

- Groq's free tier allows 8,000 tokens per minute. Short documents and decks are fine, with some waiting between calls, but a long document or large deck can go over the limit and fail with an error.
