# Workplace Rights Assistant: project guide

A plain-English explanation of what we are building, how it works, what is done, and what
comes next. Read it top to bottom once; later, use the table of contents to jump around.

Last updated: 16 November 2026 (after build step 6).

## Contents

1. [What it is, in one paragraph](#1-what-it-is-in-one-paragraph)
2. [Why this makes a strong portfolio project](#2-why-this-makes-a-strong-portfolio-project)
3. [The key ideas, explained simply](#3-the-key-ideas-explained-simply)
4. [How the whole system works](#4-how-the-whole-system-works)
5. [The data: award pages and why they are tricky](#5-the-data-award-pages-and-why-they-are-tricky)
6. [The database](#6-the-database)
7. [Where the code is](#7-where-the-code-is)
8. [Technology choices and why](#8-technology-choices-and-why)
9. [The 22 build steps and where we are](#9-the-22-build-steps-and-where-we-are)
10. [How to run it](#10-how-to-run-it)
11. [Your to-do list](#11-your-to-do-list)
12. [Interview talking points](#12-interview-talking-points)
13. [Glossary](#13-glossary)

---

## 1. What it is, in one paragraph

The **Workplace Rights Assistant** is a question-answering app for Australian workplace rules.
You ask something like *"Do casual retail workers get paid more on public holidays?"* and it
answers using the official documents: **Modern Awards** (the legal minimum pay and conditions
for an industry, e.g. the Hospitality award `MA000009`), the **National Employment Standards
(NES)**, and **WorkSafe Victoria** safety guidance. Every answer **cites the exact clause** it
came from (e.g. `MA000009:28.2`), so the user can check it. It gives **general information
only, never personal legal advice**: questions about someone's own dispute are referred to the
Fair Work Ombudsman (13 13 94).

## 2. Why this makes a strong portfolio project

Most RAG demos are "chat with a PDF". This one shows the things employers actually worry about:

| What interviewers look for | Where this project shows it |
|---|---|
| Messy real-world data | Award pages are Word exports with split words, stale numbering and odd tables; a custom parser handles them and is validated |
| Retrieval quality, measured | A golden set of real questions with expected clauses; metrics (hit@6, MRR) tracked as features are added |
| Not making things up | Every answer must cite retrieved clauses; uncited answers are blocked; pay amounts only come from a database, never from the model's memory |
| Security | Guardrails mapped to the OWASP Top 10 for LLM apps (prompt injection, data leakage, etc.) |
| Engineering discipline | Tests for every rule, CI on GitHub, typed API, reproducible data (sha256 of every source page) |
| Production thinking | Deployed on Azure, observability (Langfuse), rate limiting, cost per question |
| Working within constraints | Built on a 7 GB laptop: memory and disk limits drove real design decisions (see section 8) |

## 3. The key ideas, explained simply

**LLM (large language model).** A model like GPT that writes fluent text. It is good at
wording, bad at remembering exact legal details. So we never let it answer from memory.

**RAG (retrieval-augmented generation).** Instead of asking the LLM directly, we first
**retrieve** the most relevant clauses from the official documents, then give those clauses to
the LLM and say "answer using only these, and cite them". Retrieval supplies the facts; the LLM
supplies the wording.

**Chunk.** A piece of a document small enough to retrieve and show. Here, one chunk = one
award clause (e.g. clause 28.2). Long clauses are split into parts `28.2#1`, `28.2#2`.

**Embedding (vector).** A list of numbers (here 384 of them) that represents the *meaning* of a
piece of text. Texts with similar meaning get similar vectors, even with different words:
"weekend loading" and "Saturday penalty rate" end up close together. A small model
(`bge-small-en-v1.5`) turns text into vectors.

**Vector search.** Turn the question into a vector, then find the chunks whose vectors are
closest (cosine distance). Good at meaning, weaker at exact terms like "clause 28.2".

**Keyword (full-text) search.** Classic search on the actual words, like a search engine.
Good at exact terms, weak at synonyms. Postgres has this built in.

**Hybrid search and RRF.** Run both searches and merge the two ranked lists with **Reciprocal
Rank Fusion**: a chunk ranked high in either list rises to the top. This beats either search
alone, and we will prove it with numbers.

**Re-ranker (cross-encoder).** A second, more careful model that reads the question and each
candidate clause *together* and scores how well they match. We retrieve ~24 candidates quickly,
then the re-ranker picks the best 6.

**Relevance gate.** If even the best chunk is not close enough to the question, we say
"I don't have enough information" instead of letting the LLM guess.

**Citations.** Every answer lists the clauses it used. The code checks that each cited clause
was actually retrieved; anything else is dropped.

**Guardrails.** Code checks before and after the LLM: block prompt-injection attempts
("ignore your instructions…"), remove personal information (phone numbers, tax file numbers),
refer personal legal questions to the Ombudsman, block answers that leak the system prompt,
and remove any dollar amount that did not come from the pay-rate database.

**Router.** Decides what kind of question it is: a general rules question (use RAG), a pay
question (look it up in the pay-rate table), a question missing details (ask which award /
casual or full-time / which level), a personal dispute (refer), or out of scope.

**Evaluation (eval).** A fixed set of test questions with known correct clauses, run after
every change, so we can show *measured* improvements instead of "it seems better".

## 4. How the whole system works

There are two separate flows.

### Ingestion (offline, run occasionally: awards change every 1 July)

```
awards.fairwork.gov.au ──► fetch ──► data/raw/MA000009_2026-07-01.html (+ .json with sha256)
                                          │
                                          ▼
                                   parse into clauses ──► validate (nothing missing,
                                          │                no duplicates, size limits)
                                          ▼
                     embed each clause (384 numbers) ──► store in Postgres (chunks table)
                     rate tables ──► pay_rates table (structured numbers, step 7)
```

### Answering a question (online, every request)

```
question
  │
  ▼
input guardrails ── injection? PII? personal advice? too long?
  │
  ▼
router ──┬── rag ──► hybrid search ─► re-rank ─► relevance gate ─► LLM writes JSON answer + citations
         ├── pay_rate ──► SQL lookup in pay_rates ─► LLM only phrases the result
         ├── clarify ──► "Which award / employment type / level?"
         ├── refer ──► Fair Work Ombudsman message
         └── out_of_scope
  │
  ▼
output guardrails ── citations real? dollar amounts from the tool? prompt leaked? PII? add disclaimer
  │
  ▼
answer + clickable citations (React web page)
```

### A worked example (how a question travels)

1. The user asks: *"Do casual hospitality employees get paid extra on Sundays?"*
2. **Input guard**: no injection, no personal details, it is a general question: pass.
3. **Router**: it asks about a rule, not a specific dollar amount: route `rag`.
4. **Hybrid search**: vector search finds clauses about penalty rates and weekend work;
   keyword search finds clauses containing "Sunday" and "casual". RRF merges them.
5. **Re-ranker** keeps the best 6 clauses from the Hospitality award.
6. **Relevance gate**: the top clause is close enough: continue.
7. **LLM** receives the 6 clauses (marked as data, not instructions) and must return
   `{answer, citations, insufficient_context, needs_clarification}`.
8. **Output guard**: drops any citation that was not in the 6 clauses, removes any dollar
   amount that did not come from the pay tool, adds the "general information only" disclaimer.
9. The user sees the answer with citation chips like `MA000009:<clause>` that open the clause
   text and the official page.

If the user had asked *"What is the Sunday rate for a level 2 casual?"*, the router would send
it to the **pay tool** instead: the amount comes from the `pay_rates` table, and the LLM may
only rephrase it.

## 5. The data: award pages and why they are tricky

We use four awards to start (later also the NES and WorkSafe Victoria):

| Award | Code | Version (amendments up to) | Chunks |
|---|---|---|---|
| Hospitality Industry (General) Award 2020 | MA000009 | 1 July 2026 | 372 |
| General Retail Industry Award 2020 | MA000004 | 1 July 2026 | 298 |
| Fast Food Industry Award 2020 | MA000003 | 1 July 2026 | 197 |
| Clerks—Private Sector Award 2020 | MA000002 | 26 October 2026 | 296 |
| **Total** | | | **1,163** |

The pages on `awards.fairwork.gov.au` are exported from Microsoft Word, which causes problems a
naive parser gets wrong:

- **Words are split across HTML tags.** "maximum" is stored as "ma" + "ximum". We join the
  pieces of each paragraph without spaces.
- **Structure is only in CSS class names.** `Level1` = "28. Overtime", `Level2` = "28.1 …",
  `SubLevel1/2` = schedule clauses like "B.2.1", `Subdocument` = "Schedule B—…".
- **Inserted clauses keep an old number.** Clause 13A.1 appears as "13.9 13A.1 …"; we keep 13A.1.
- **Footnotes look like clause numbers.** "1 Rates in table are calculated…" is a footnote, not
  clause 1. Real top-level clauses end with a dot ("28.").
- **Rate tables in schedules have no "$" sign** ("Level 1 39.66 52.88 …"), so we detect them by
  their number pattern.
- **The page has website menus and footers** around the award; we read only the award body,
  from clause 1 to the award's footer.

The parser checks itself: every clause number must produce a chunk, no clause reference may
repeat, and no chunk may exceed 1,800 characters. All four awards pass with zero problems.

Every downloaded page is saved with its **sha256 fingerprint**, so we can prove which exact
version the answers and evaluation numbers came from.

## 6. The database

Postgres 18 with the **pgvector** extension (vectors) and built-in full-text search. One
database holds everything:

| Table | What it holds |
|---|---|
| `documents` | One row per source version (award + effective date), with URL and sha256 |
| `chunks` | One row per clause: reference (`28.2`), heading, text, rate-table flag, **embedding** (384 numbers) and a **search index** of its words |
| `pay_rates` | Structured pay data: classification, employment type, rate type, amount, source clause, date (step 7) |
| `guardrail_events` | Every guardrail decision (never the user's raw text or personal data) |
| `eval_runs` | Evaluation results over time (optional) |

Two special indexes make search fast: **HNSW** for vectors (finds nearest neighbours quickly)
and **GIN** for words (classic search index).

## 7. Where the code is

Everything is under `backend/app/`. Built so far:

| File | What it does | Step |
|---|---|---|
| `config.py` | Settings (database URL, model names, LLM URL/key/timeout), read from `backend/.env` | 2 |
| `main.py` | The web API (FastAPI). Only `/health` so far; `/chat` comes in step 10 | 2 |
| `db/schema.sql` | Creates all tables and indexes | 3 |
| `db/conn.py`, `db/init.py` | Connect to the database; apply the schema | 3 |
| `ingestion/fetch.py` | Downloads award pages, only from allow-listed sites, saves them with sha256 | 4 |
| `ingestion/parse_award.py` | Turns an award page into clause chunks and validates them | 5 |
| `embeddings.py` | Turns text into vectors with `bge-small-en-v1.5` | 6 |
| `ingestion/ingest.py` | Parse → validate → embed → save to the database | 6 |
| `backend/tests/` | 60 automated tests; `fixtures/` holds exact excerpts of the real pages | 2-6 |

Still to come (in build order):

| File | What it will do | Step |
|---|---|---|
| `ingestion/parse_rates.py` | Read pay-rate tables into `pay_rates` | 7 |
| `retrieval/hybrid.py` | Vector + keyword search merged with RRF | 8 |
| `eval/golden_set.jsonl`, `app/eval/run_eval.py` | Test questions and the evaluation script | 9 |
| `rag/prompts.py`, `rag/generate.py` | Prompt, LLM call, JSON answer, `POST /chat` | 10 |
| `guardrails/` | Input, context and output guardrails | 11 |
| `retrieval/rerank.py` | Cross-encoder re-ranker | 13 |
| `router.py`, `tools/pay_rates.py` | Route questions; pay lookups | 14 |
| `frontend/` | React chat page with citation chips | 18 |

**Where is the LLM loaded?** Nowhere, by design. The LLM runs as a separate service (a hosted
API, or Ollama on your laptop for offline work). Our code just sends it an HTTP request at
`LLM_BASE_URL`. Nothing before step 10 needs it.

## 8. Technology choices and why

| Layer | Choice | Why |
|---|---|---|
| Language | Python 3.12, managed by `uv` | Standard for AI work; `uv` is fast and keeps one lockfile |
| Web API | FastAPI | Typed requests/responses and free interactive docs at `/docs` |
| Database | Postgres 18 + pgvector, hosted on **Neon** | Vectors, keyword search, pay rates and logs in one database; matches many job ads |
| Embeddings | `bge-small-en-v1.5` via **fastembed (ONNX Runtime)** | Free, runs on CPU, strong quality; ONNX uses ~330 MB RAM instead of ~875 MB with torch |
| Re-ranker | `ms-marco-MiniLM-L-6-v2` via fastembed | Small (~90 MB) but a big accuracy gain |
| LLM | Hosted OpenAI-compatible model (Azure OpenAI); Ollama `qwen2.5:3b` offline | Fast answers (~2 s vs 20-60 s on CPU); switching is three `.env` values |
| Parsing | BeautifulSoup4 | The award pages are HTML |
| Tests | pytest | Standard |
| Frontend | React + Vite + TypeScript | Standard modern web stack |
| CI/CD | GitHub Actions | Tests and retrieval eval on every push |
| Deploy | Azure (Container Apps, Static Web Apps) | Common in Melbourne job ads; scales to zero |
| Monitoring | Langfuse + `guardrail_events` table | Traces, latency, cost, guardrail statistics |

### The re-platform (a good interview story)

The first version ran Postgres and the app in **Docker** on the laptop, with **torch** for the
embedding model. On a 7 GB laptop this caused real problems: the first full embedding run left
0.4 GB of RAM free, and Docker's virtual disk grew to 12 GB and **filled the C: drive**. We
measured, then changed the platform without throwing away any of the code:

- Docker removed from the laptop (Docker is still used for deployment, built in the cloud).
- Postgres moved to Neon (hosted, free tier, same Postgres + pgvector).
- torch replaced with ONNX Runtime: **identical vectors** (checked: cosine similarity 1.0000)
  at a third of the memory; the Python environment went from 1.3 GB to 0.2 GB.
- Caches moved off the full C: drive. Result: C: from 0 to 17 GB free.

## 9. The 22 build steps and where we are

| # | Step | Status |
|---|---|---|
| 1 | Machine setup | Done |
| 2 | Skeleton (API, config, tests) | Done |
| 3 | Database schema | Done |
| 4 | Fetch award pages (with allow-list) | Done |
| 5 | Award parser + validation | Done |
| 6 | Ingest: embed and store | Done (1,163 chunks in Neon) |
| 7 | Pay-rate extraction | Done (1,227 adult rates; juniors/apprentices later) |
| 8 | Retrieval: vector, then hybrid | Done (`app/retrieval/search.py`) |
| 9 | Golden set + first evaluation | Done (vector hit@6 0.90, hybrid 0.80) |
| 10 | Generation: prompt, JSON, `/chat` | |
| 11 | Guardrails | |
| 12 | Full evaluation | |
| 13 | Re-ranker | |
| 14 | Router, pay tool, clarify route | |
| 15 | NES and WorkSafe ingestion | |
| 16 | Prompt Guard classifier | |
| 17 | Observability (Langfuse) | |
| 18 | Frontend | |
| 19 | Hardening (rate limits, timeouts) | |
| 20 | CI on GitHub Actions | |
| 21 | Deploy to Azure | |
| 22 | Portfolio polish (README, demo GIF) | |

Each step ends with a concrete "done when" check and passing tests. The full plan:
https://claude.ai/artifact/5xjWotxvyctLFcVrrWVFBe

## 10. How to run it

From PowerShell:

```powershell
cd E:\RAG-workplace-rights-assistant\backend
uv run pytest -q                                   # run all tests
uv run uvicorn app.main:app --reload               # start the API, then open http://127.0.0.1:8000/docs
uv run python -m app.ingestion.fetch --all         # download the four awards
uv run python -m app.ingestion.parse_award --all   # parse and validate them
uv run python -m app.ingestion.parse_award --award MA000009 --show 28.2   # look at one clause
uv run python -m app.db.init                       # create the database tables
uv run python -m app.ingestion.ingest --all        # embed and store everything
```

The database commands need `DATABASE_URL` in `backend/.env` (see the to-do list).

## 11. Your to-do list

**Now:**

1. **Create the Neon database** (about 5 minutes):
   - sign up at https://neon.tech (free plan);
   - create a project; choose an Australian or Asia-Pacific region if offered;
   - copy the connection string (starts with `postgresql://`, ends with `?sslmode=require`);
   - create the file `backend\.env` with one line: `DATABASE_URL=<the string>`;
   - tell Claude it is done. Do not paste the password into the chat.
2. **Push to GitHub**: `git push` (the last two commits are only on your laptop).

**Later (not needed yet):**

3. Before step 10: create an Azure account and an Azure OpenAI resource (Australia East) for
   the hosted LLM. New accounts get free credit; an evaluation run costs cents.
4. Step 17: a free Langfuse account for monitoring.
5. Step 21: Azure resources for deployment.
6. Optional: `ollama rm qwen2.5:3b` frees 1.8 GB on C: if you won't use the offline LLM.

## 12. Interview talking points

- **"Why RAG and not fine-tuning?"** Awards change every 1 July; RAG updates by re-ingesting,
  and every answer can cite its source. Fine-tuning cannot cite and goes stale.
- **"How do you stop hallucination?"** Retrieval first; a relevance gate that refuses when
  nothing relevant is found; citation checking in code; pay amounts only from a database tool;
  faithfulness measured by an LLM judge in the evaluation.
- **"How do you know retrieval is good?"** A golden set with expected clauses and verbatim
  evidence quotes (a test checks every quote exists in the real award), measured with hit@6 and
  MRR, with one README row per improvement (vector → hybrid → re-ranker → router).
- **"Hardest data problem?"** Word-exported HTML: split words, stale clause numbers, footnotes
  that look like clauses, rate tables without "$". Solved with class-based parsing plus a
  validator, tested against byte-exact excerpts of the real pages.
- **"Security?"** OWASP LLM Top 10: injection patterns + a classifier, PII redaction, a
  domain allow-list for sources, sources wrapped as data, a canary token to detect prompt
  leaks, output validation, rate limiting.
- **"Constraints?"** A 7 GB laptop forced measured trade-offs: ONNX instead of torch
  (same vectors, a third of the memory), hosted Postgres instead of local Docker.

## 13. Glossary

| Term | Meaning |
|---|---|
| Modern Award | Legal document setting minimum pay and conditions for an industry or occupation |
| NES | National Employment Standards: minimum entitlements for all national-system employees, set by the Fair Work Act 2009 |
| Clause | A numbered section of an award, e.g. 28.2; schedules use letters, e.g. B.2.1 |
| Chunk | One stored piece of text (here: one clause, or part of a long clause) |
| Embedding / vector | 384 numbers representing a text's meaning |
| pgvector | Postgres extension for storing and searching vectors |
| HNSW | Fast index for finding nearest vectors |
| Full-text search / tsvector | Postgres keyword search and its word index |
| RRF | Reciprocal Rank Fusion: merges two ranked lists |
| Cross-encoder / re-ranker | Model that scores a question and a passage together, more accurately |
| hit@6 | Share of questions where a correct clause is in the top 6 results |
| MRR | Mean Reciprocal Rank: rewards putting the correct clause near the top |
| Faithfulness | Whether the answer is supported by the cited clauses |
| Guardrail | A code check that blocks or fixes unsafe inputs or outputs |
| Canary token | A random marker in the system prompt; if it appears in an answer, the prompt leaked |
| PII | Personally identifiable information (names, phone numbers, tax file numbers…) |
| ONNX Runtime | A light engine for running AI models without torch |
| sha256 | A fingerprint of a file; any change to the file changes it |
| Neon | A hosted Postgres service with a free tier |
| uv | A fast Python package and environment manager |
