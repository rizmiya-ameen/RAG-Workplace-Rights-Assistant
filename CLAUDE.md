# CLAUDE.md

## Project

**Workplace Rights Assistant**: a portfolio RAG project for AI/ML engineer interviews in Melbourne.
It answers questions about Australian Modern Awards, the National Employment Standards (NES) and
WorkSafe Victoria guidance, citing the exact clause every time (e.g. `MA000009:28.2`).
It gives **general information only, never personal legal advice**: personal or dispute
questions are referred to the Fair Work Ombudsman (13 13 94).

Full plan (architecture, stack, data model, 22 build steps):
https://claude.ai/artifact/5xjWotxvyctLFcVrrWVFBe

Starting awards: Hospitality (MA000009), General Retail (MA000004), Fast Food (MA000003),
Clerks (MA000002). Then NES and WorkSafe Victoria guidance.

## Architecture

```
user -> input guard -> router -> { rag | pay_rate | clarify | refer | out_of_scope }
rag:      hybrid search (pgvector + Postgres FTS, RRF) -> cross-encoder re-rank
          -> relevance gate -> LLM (JSON + citations)
pay_rate: SQL on pay_rates -> LLM only phrases the tool result
all answers -> output guard (canary, citation check, $ check, PII, disclaimer) -> user
```

- **Stack**: Python 3.12 (`uv`), FastAPI, hosted Postgres 16 + pgvector (Neon; same in dev and
  the deployed demo), `BAAI/bge-small-en-v1.5` embeddings (384 dims) and
  `ms-marco-MiniLM-L-6-v2` re-ranker via **fastembed (ONNX Runtime, no torch)**,
  OpenAI-compatible LLM client, BeautifulSoup4, pytest, React + Vite + TypeScript, Langfuse,
  GitHub Actions, Azure.
- **No Docker on the dev laptop.** The app runs natively with `uv`. The `Dockerfile` is built
  only in GitHub Actions/Azure; CI tests use a Postgres service container on GitHub.
  `docker-compose.yml` is an optional local Postgres for anyone who has Docker.
- **LLM** (from build step 10): a hosted OpenAI-compatible model (Azure OpenAI, Australia East)
  for development, README eval numbers and the deployed app; Ollama `qwen2.5:3b` stays as an
  offline option (`LLM_BASE_URL=http://127.0.0.1:11434/v1`). Switching = three `.env` values.
- **Machine limits**: ~7 GB RAM, no GPU (Ryzen 5 3500U); C: drive was full, so keep caches on
  E: (`UV_CACHE_DIR=E:\dev-cache\uv`, `HF_HOME=E:\dev-cache\huggingface` user env vars; ONNX
  models in repo `.cache/models`). Keep memory low: small models, lazy loading, small batches.
  Close Chrome before large embedding runs.
- **Tables**: `documents` (versioned by `effective_date`), `chunks` (one per clause, embedding +
  tsvector), `pay_rates` (structured), `guardrail_events`, optional `eval_runs`.
- **Citations**: awards `MA0000XX:<clause>` (`28.2`, `15A.1`, `B.2.1`; long clauses split as
  `28.2#1`), NES `NES:s<section>`, WorkSafe `WSV:<slug>#<heading>`.
- **Guardrails** (`backend/app/guardrails/`): input (length, PII redaction, injection patterns +
  Prompt Guard classifier, evasion patterns, advice -> refer, scope), context (domain allow-list,
  tag stripping, sources wrapped as data), output (canary, citation validation, dollar amounts
  only if they match the pay tool, PII, disclaimer added by code).
- **Eval** (`eval/`): golden set with expected clauses + verbatim evidence quotes (checked by a
  pytest test against the parsed award), red-team set with expected routes. Metrics: hit@6, MRR,
  faithfulness (LLM judge), answer/decline rates, route accuracy, red-team pass rate, latency, cost.

## Award page layout (awards.fairwork.gov.au)

The pages are Word exports. The parser must:
- read text per `<p>` or table row, never per text node (clause numbers sit in their own `<span>`);
- use paragraph classes: `Level1` = "28. Overtime", `Level2*` = "28.1", `SubLevel*` = schedule
  clauses (`A.1`, `B.2.1`), `Subdocument` = "Schedule A—…"; skip `TOC*`, `History`,
  `Partheading`, `Footer`;
- drop a stale Word number before inserted clauses ("13.9 13A.1 …" -> `13A.1`);
- treat "1 Rates in table…" footnotes as content (real top-level clauses end with a dot: "28.");
- join paragraph spans with "" (Word splits words: "C" + "lassifications") and table cells with " ";
- ignore schedule links in the page header (start at clause 1).

Found while building the parser (`app/ingestion/parse_award.py`):
- the award body is one `<div>` inside `#mainContent`; walk its `<p>`/`<tr>` in document order
  (some tables sit in a wrapper `<div>`, e.g. MA000009 "Table 4—Casino gaming minimum rates");
- unclassed `<p>` inside the body are real content (definition lead-ins, NOTEs, footnotes);
  stop at the first `Footer` after clause 1 (website footer comes after it);
- also skip `hisk` and bracketed amendment notes ("[… inserted by PR774714 …]");
- schedule rate tables have no "$" (`Level 1 39.66 52.88 …`), so `is_rate_table` also
  triggers on rows with two or more `\d.\d\d` amounts;
- a `Level*`/`SubLevel*` paragraph without a clause number is kept as body with a warning
  (one in MA000009 C.3).

## Commands

Setup: copy `.env.example` to `backend/.env` and set `DATABASE_URL` (Neon connection string).
`app/config.py` reads `backend/.env` by absolute path. Never commit `.env`.

```bash
cd backend                                  # all commands run from backend/
uv run pytest -q                            # DB tests skip if DATABASE_URL is unreachable
uv run ruff check .
uv run uvicorn app.main:app --reload        # API on http://127.0.0.1:8000 (/docs, /health)
uv run python -m app.db.init                               # apply schema (idempotent)
uv run python -m app.ingestion.fetch --all                 # or --award MA000009
uv run python -m app.ingestion.parse_award --all           # parse + validate
uv run python -m app.ingestion.parse_award --award MA000009 --show 28.2
uv run python -m app.ingestion.ingest --all                # skips unchanged versions; --force
```

Ingest embeds `heading + "\n" + content` with bge (no prefix on passages; `QUERY_PREFIX` in
`app/embeddings.py` only on queries). fastembed gives the same vectors as
sentence-transformers (cosine 1.0000 checked on award chunks) at ~330 MB process RAM.

Fetch saves `data/raw/<award>_<effective date>.html` + `.json` (url, title, date, sha256).
The date comes from the page's "amendments up to and including <date>" paragraph.
`ALLOWED_HOSTS` in `app/ingestion/fetch.py` is a guardrail: exact host, https, port 443,
checked again on every redirect.

For a local Postgres use `127.0.0.1`, not `localhost`: Windows tries `::1` first and hangs.
DB tests (`test_schema.py`, `test_ingest.py`) roll back their rows and use `TEST-*` source ids.

Planned: `uv run python -m app.eval.run_eval --mode all`

## Rules

- **Never weaken or bypass a guardrail to make something work.** If a guardrail blocks a
  legitimate case, tell the user and propose a fix with a test.
- **Every guardrail change needs a test** in `backend/tests/` and, where relevant, a case in
  `eval/redteam.jsonl`.
- **Never invent award content, pay rates or clause numbers**, including in test fixtures and the
  golden set. Use real parsed data (fixtures are verbatim excerpts of real pages) or leave a TODO.
- **Pay rates must come from a structured tool or API**, never from the LLM reading text.
- **Keep it solo-maintainable**: simple over clever.
- **Run the tests before saying a task is done.**
- Work through the plan's build steps in order; present plans as numbered steps, not weeks.

## Status

- Step 1 (machine setup): done: Git, Node 22, WSL2 (Ubuntu 22.04), `.wslconfig` (`memory=3GB`),
  `uv` 0.13.8 + Python 3.12.16, repo `git init`, Ollama 0.40.16 + `qwen2.5:3b` (answers),
  WSL 3.0.5, Docker 29.9.3 / Desktop 4.99 (`hello-world` runs, engine memory ~3 GB).
- Step 2 (skeleton): done: FastAPI `/health`, `config.py` (pydantic-settings), Dockerfile
  (uv 0.13.8, Python 3.12), Compose with `pgvector/pgvector:pg16` (vector 0.8.7 available),
  `.env.example`; 3 tests pass on host and in the container; api container reaches Ollama.
- Step 3 (schema): done: `app/db/schema.sql` (documents, chunks with `vector(384)` + HNSW
  cosine + generated `tsv` with GIN, pay_rates with enum checks, guardrail_events, eval_runs),
  `app/db/conn.py`, `python -m app.db.init`; 14 tests pass on host and in the container.
- Git: steps 1-4 pushed to `origin/main` (github.com/rizmiya-ameen/RAG-Workplace-Rights-Assistant),
  raw award HTML committed so eval data is reproducible. The user runs commits/pushes
  (`.git/config` is sandboxed; commits from Claude were blocked).
- Known warning: Starlette suggests `httpx2` instead of `httpx` for TestClient (harmless).
- Step 4 (fetch): done: `app/ingestion/fetch.py`, four awards saved (MA000009, MA000004,
  MA000003 at 2026-07-01; MA000002 at 2026-10-26), sha256s verified; 34 tests pass on host and
  in the container, including allow-list refusals and redirect checks.
- Step 5 (award parser): done: all four awards pass validation (MA000009 372 chunks,
  MA000004 298, MA000003 197, MA000002 296 = 1,163; max 1,799 chars; 0 problems).
  Fixtures `tests/fixtures/MA000009_excerpt.html` and `MA000002_excerpt.html` are whole
  elements sliced byte-for-byte from the raw pages. 53 tests pass on host and in the container.
- Step 6 (ingest): done: `app/embeddings.py` (lazy bge-small, query prefix only on queries),
  `app/ingestion/ingest.py` (validate -> embed -> replace chunks per document version, skip
  if sha256 unchanged). DB holds 1,163 chunks, all embedded (MA000009 372, MA000004 298,
  MA000003 197, MA000002 296). 60 tests pass on host and in the container.
  Memory: the first full embed run pushed the 7 GB laptop to ~0.4 GB free with Chrome open;
  close Chrome before model-heavy runs. Dockerfile uses a uv cache mount (image was 3.74 GB);
  the rebuild with it was interrupted, so the image size is still to be verified (step 20 CI).
- Re-platform (2026-11-16, after step 6): Docker used too much RAM and filled C:, so Docker
  Desktop was uninstalled (C: 0 -> 17 GB free), embeddings moved to fastembed/ONNX (venv
  1.3 -> 0.2 GB), caches moved to E:. Dev DB moves to Neon: waiting for the user to create the
  Neon project and put `DATABASE_URL` in `backend/.env`; then `app.db.init` +
  `app.ingestion.ingest --all` to reload the 1,163 chunks. 44 tests pass, 16 DB tests skip
  until then. The plan artifact still describes the Docker setup: update it.
- Next: finish the Neon switch, then step 7 (pay-rate extraction).
- Decisions: LLM = hosted (Azure OpenAI) from step 10, Ollama optional offline. DB = Neon
  Postgres + pgvector. Embeddings/re-ranker = fastembed (ONNX). Pay data = award rate tables
  parsed into `pay_rates`. Frontend = React + Vite + TypeScript. Deploy = Azure.
