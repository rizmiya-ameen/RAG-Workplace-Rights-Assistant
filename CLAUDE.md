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
rag:      vector search (hybrid FTS+RRF optional) -> top 24 -> cross-encoder re-rank
          -> top 6 -> relevance gate -> LLM (JSON + citations)
pay_rate: SQL on pay_rates -> LLM only phrases the tool result
all answers -> output guard (canary, citation check, $ check, PII, disclaimer) -> user
```

- **Stack**: Python 3.12 (`uv`), FastAPI, hosted Postgres 18 + pgvector (Neon; same in dev and
  the deployed demo), `BAAI/bge-small-en-v1.5` embeddings (384 dims) and
  `ms-marco-MiniLM-L-6-v2` re-ranker via **fastembed (ONNX Runtime, no torch)**,
  OpenAI-compatible LLM client (plain httpx, `app/llm.py`), BeautifulSoup4, pytest, React + Vite + TypeScript, Langfuse,
  GitHub Actions, Azure.
- **No Docker on the dev laptop.** The app runs natively with `uv`. The `Dockerfile` is built
  only in GitHub Actions/Azure; CI tests use a Postgres service container on GitHub.
  `docker-compose.yml` is an optional local Postgres for anyone who has Docker.
- **LLM** (from build step 10): **Google Gemini free tier** (AI Studio key, no card, no
  expiry) via its OpenAI-compatible endpoint, model `gemini-3.5-flash-lite`, for development,
  eval numbers and the demo. `gemini-2.5-*` is closed to new users; `gemini-3.8-flash` was
  often 503 (busy) on the free tier. Flash-Lite rejects `reasoning_effort=none`; `low` works
  (default thinking ate the token budget and cut answers off). GitHub Models was retired
  2026-07-30, so it is not an option. Azure OpenAI is not used (trial credit lasts 30 days).
  Ollama was uninstalled (2026-11-23); it can be reinstalled as an offline option.
  Switching = three `.env` values (see `.env.example`).
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
uv run python -m app.ingestion.parse_rates --all           # pay rates: parse + cross-check
uv run python -m app.ingestion.parse_rates --award MA000009 --show "Level 2"
uv run python -m app.ingestion.ingest --all                # skips unchanged chunks; --force
                                                           # (pay rates reload every run)
uv run python -m app.retrieval.search "Do casuals get more on Sundays?" --mode hybrid --award MA000009
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

`uv run python -m app.eval.run_eval --mode retrieval` (vector vs hybrid; `--no-save` to skip
writing results). Later: `--mode all` (answers, faithfulness, routes, red-team).

`uv run python -m app.eval.run_eval --mode redteam-input` (input guard routes, no LLM).
`uv run python -m app.eval.run_eval --mode answers` (real answers + judge on Gemini, ~15 min,
~140 LLM calls; `--limit 5` for a quick check, `--resume <file>` after a quota stop).

`uv run python -m app.pipeline "question" --award MA000009` asks one question end to end
(same path as `POST /chat`). Needs `LLM_API_KEY` in `backend/.env`; never print it.

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
  until then. Plan artifact updated 2026-11-23 (Neon, fastembed, no local Docker, progress).
- C: cleanup (2026-11-23): pip + npm caches purged, Ollama and `qwen2.5:3b` uninstalled
  (C: 9.5 -> 23.2 GB free). WSL Ubuntu (2.6 GB) kept.
- Neon switch (2026-11-23): done: project in AWS Sydney (ap-southeast-2), Postgres 18.7,
  pgvector 0.8.7, pooled endpoint. Schema applied, 1,163 chunks re-ingested (MA000009 372,
  MA000004 298, MA000003 197, MA000002 296; ~6.5 min). 60 tests pass, 0 skipped.
  docker-compose moved to `pgvector/pgvector:pg18`; CI (step 20) must also use pg18.
- Step 7 (pay rates): done: `app/ingestion/parse_rates.py` reads tables into a grid
  (rowspan/colspan expanded). Adult only: weekly (full-time) from the minimum-rates clause
  (`MIN_RATES_CLAUSE`: MA000009 18.1, MA000004 17.1, MA000003 15.1, MA000002 16.1); hourly,
  penalty and overtime from the "Summary of Hourly Rates" schedule (B, or A in MA000003).
  `condition` = title qualifier ("casino gaming", "other than shiftworkers") + column header
  + percentage, e.g. "Sunday (175%)". Ordinary column = header exactly "Ordinary hours" or
  "Monday to Friday" (Clerks); Clerks casual ordinary is "Monday to Friday" (125%).
  Validation: weekly / 38 = hourly; every minimum hourly rate appears as a schedule
  full-time hourly rate; no duplicate rows. Ingest reloads pay rates every run.
  Neon: 1,227 pay rates (MA000009 367, MA000004 336, MA000003 124, MA000002 400).
  Also fixed `_AMOUNT` (missed "1056.80"), so 17.1/15.1/16.1 are now rate-table chunks.
  76 tests pass. TODO: junior, apprentice, managerial rates and allowances (Schedule C).
- Step 8 (retrieval): done: `app/retrieval/search.py` `search(conn, query, mode, award, k)`;
  latest effective_date per source only; vector = cosine on HNSW with
  `hnsw.iterative_scan = strict_order` (award filter still returns k); hybrid = vector top 24
  + full-text top 24 fused with RRF (k=60). Full text ORs the query's stemmed words and drops
  words in > 20% of chunks (`MAX_DF`; "employe" is in 76%) because ts_rank has no IDF.
  Manual checks: hybrid finds "clause 28.2" and 13A (disconnect) that vector misses; vector
  kept some hits hybrid lost (MA000004 11.2). Compare properly in step 9. ~50-100 ms/query.
  82 tests pass.
- Step 9 (golden set + retrieval eval): done: `eval/golden_set.jsonl` = 59 answerable
  (hosp 26, retail 11, ff 10, clerks 10, cross-award 2; each with expected clauses + verbatim
  quote) + 10 decline. `tests/test_golden_set.py` checks every quote and expected clause
  against the parsed award. `app/eval/run_eval.py --mode retrieval` writes
  eval/results/*.json (git-ignored) + an eval_runs row. First results (k=6):
  vector hit@1 0.64, hit@6 0.90, MRR 0.735, hit@24 0.98; hybrid 0.54 / 0.80 / 0.641 / 0.98.
  Hybrid loses on ordering, not recall: keep both modes, decide after the re-ranker.
  Top-1 cosine distance: answerable mean 0.255 vs decline 0.327 (overlapping; gate needs the
  re-ranker score too). README created with the eval table. 204 tests pass.
- Step 10 (generation): done, on Gemini `gemini-3.5-flash-lite`. `app/llm.py` (httpx POST,
  temperature 0, JSON mode, `reasoning_effort`, retries on 429/5xx/timeout with backoff and
  capped Retry-After; errors never contain the key). `app/rag/prompts.py`: sources in
  `<source id=... award=... heading=... [type="rate-table"]>`, HTML-escaped (a clause or
  question cannot close/open tags); `$` amounts masked as `[amount]` everywhere and bare
  amounts in rate tables (clause numbers like 16.10 kept); per-request canary in the system
  prompt. `app/rag/generate.py`: canary leak -> blocked, invalid JSON -> fallback, citations
  not retrieved dropped (base clause maps to its `#n` part), no valid citation -> fallback.
  `app/pipeline.py` `answer_question()` -> routes rag / clarify / insufficient_context /
  fallback; insufficient and fallback use fixed text; disclaimer added by code; checks that
  fire go to `guardrail_events` (no user text; caller commits). `POST /chat` (question 1-1000
  chars, award enum; 503 if the LLM is down). Retrieval for chat: vector, k=6.
  Found: rate-table chunks lose their columns ("Saturday Sunday" over "150% 175%"), and the
  model said casual Sunday = 150% (B.2.3 says 175%). Rate tables are tagged and the prompt
  forbids reading figures off them; exact rates come from the pay tool (step 14). Possible
  later improvement: join table cells with " | " in parse_award (needs re-ingest + golden
  check). Five real questions (sunday, hosp-01, retail-01, ff-01, clerks-01, plus a retail
  roster question over HTTP) gave cited answers verified against the clauses; the visa
  question declined. ~1.5-4 s per answer (11 s on a cold start). 242 tests pass.
- Step 11 (guardrails): done. `app/guardrails/`:
  `input_guard.py` `check_input()` -> pass / blocked / refer / out_of_scope, in order: length,
  normalise (NFKC, zero-width + control chars removed), PII redaction (email, AU phone, TFN
  with checksum, BSB, account no.; placeholders like `[EMAIL]`, counts logged, never values),
  injection patterns, evasion patterns, advice/dispute -> refer, positive scope check (no
  workplace word at all -> out_of_scope). Patterns are narrow on purpose: "Can my employer
  ignore the rules about breaks?", "My boss pays me cash in hand, is that legal?", "Can my
  employer avoid paying penalty rates?" must pass (tests). `find_injection()` is shared with
  `context_guard.py` `check_context()`: source URL must pass fetch's `check_allowed`, text
  cleaned (tags, zero-width, control chars), sources with injection patterns dropped (0 of
  1,163 real chunks flagged; cleaning changes none: tests). `output_guard.py`
  `check_output()`: `$`/AUD/"dollars" amounts not from the pay tool replaced (allowed set
  empty until step 14), PII redacted. Canary/JSON/citation checks stay in `rag/generate.py`.
  Pipeline: input guard first (blocked/refer/out_of_scope skip search and LLM, fixed texts),
  search on the redacted question, context guard, LLM, output guard. `eval/redteam.jsonl`:
  46 rows (13 injection, 6 evasion, 8 advice, 4 scope, 3 PII, 12 look-alike legitimate),
  2 marked `known_gap` (base64, letter-spaced text: for Prompt Guard, step 16).
  `run_eval --mode redteam-input`: route accuracy 1.00 on 44 rows, attacks caught 1.00,
  false positives 0.00, 0 of 59 golden questions blocked. Decline-02 (TFN) and decline-06
  (payroll tax) now stop at the scope check. Bug found by tests: a TFN followed by "."
  was not redacted (lookahead); fixed with a regression test. Live: attacks/refer/scope
  answered in <40 ms without LLM; PII question answered with citation. 443 tests pass.
- Step 12 (full eval): done. `app/eval/answers.py` + `run_eval --mode answers` (also
  `--limit`, `--sets golden|redteam`, `--pace` s between LLM calls (default 4), `--no-judge`,
  `--resume <jsonl>`; errors are retried on resume). Rows append to
  eval/results/answers_<stamp>.jsonl as they finish (free-tier quota safe). Judge: same
  Gemini model unless `JUDGE_MODEL` is set; sees only the answer + cited clause text.
  Red-team on real answers: attacks need their guard route; a look-alike (expected rag)
  fails only if a guard stopped it; must_not_contain/match/cite and must_cite (when answered)
  checked; known gaps reported apart. Latency excludes the eval's pacing wait.
  Final run (vector + guardrails, gemini-3.5-flash-lite): answer rate 0.90 (53/59), expected
  clause cited 0.98, faithfulness 1.00 (53/53), decline accuracy 1.00, red-team pass 1.00
  (44/44; known gaps 0/2), p50 1.5 s / p95 2.5 s, 137 LLM calls, 198k tokens, $0.
  First run was 0.92 citation / 0.90 faithfulness; reading flagged answers found 3 pipeline
  bugs, all fixed with regression tests: sub-paragraph citations ("28.2(a)") dropped
  (`match_citations` now strips "(a)(i)"); a whole-clause citation of a split clause kept
  only "#1" (now every retrieved part); times "7.00 pm" masked as amounts (bare-amount mask
  skips am/pm). Prompt also says to cite without sub-paragraphs. Remaining misses: hosp-02,
  hosp-07, hosp-10, hosp-22, retail-10 (retrieval: right clause not in top 6), all-02
  (clarify), ff-07 cites Schedule A instead of the expected clause. 460 tests pass.
- Step 13 (re-ranker): done. `app/retrieval/rerank.py` (`Xenova/ms-marco-MiniLM-L-6-v2`
  via fastembed TextCrossEncoder, lazy, scores = logits on heading + content);
  `search(..., rerank=True)` re-orders the top 24 candidates and returns k with
  `rerank_score`. `/chat` = vector + rerank (`retrieval_rerank=True`). Retrieval eval
  variants vector / hybrid / vector+rerank / hybrid+rerank: vector+rerank hit@1 0.83, hit@6
  0.98, MRR 0.884 (vector alone 0.64 / 0.90 / 0.735); hybrid+rerank 0.85 / 0.98 / 0.902 but
  lost hosp-22 from its pool, so vector+rerank stays (simpler). Only miss: retail-10 (not in
  24 candidates). CPU latency: batches pad to the longest clause, so `reranker_batch_size=2`
  (~1.5 s for 24 clauses vs ~3.5 s at 24). Relevance gate in pipeline: top rerank score <
  `relevance_threshold` (-9.7) -> insufficient_context, no LLM call, event
  `context/relevance_gate` with the score. Threshold = lowest found answer (hosp-22 -8.71)
  minus 1.0 (`suggest_gate` in run_eval). Scores overlap badly (visa decline +0.31), so the
  gate only stops clearly unrelated questions; the LLM's insufficient_context does the rest.
  Answer eval with rerank stopped at Gemini's daily quota (429) after 69/115 questions
  (eval/results/answers_20261124T053930Z.jsonl): answer rate 38/38, citation 1.00,
  faithfulness 32/33 (hosp-20: strict judge, claim is in 25.2(g)), red-team 29/29, p50 2.9
  s. TODO: `--resume ../eval/results/answers_20261124T053930Z.jsonl` after the quota resets,
  then complete the README row. 467 tests pass.
- Next: step 14 (router, pay tool, clarify route).
- Decisions: retrieval for chat = vector + cross-encoder re-rank (step 13). LLM = Gemini
  free tier (`gemini-3.5-flash-lite`) from step 10; Ollama optional offline. The free tier
  stopped us with 429 after ~480 requests in a day; one full answer eval is ~140 calls. DB = Neon
  Postgres + pgvector. Embeddings/re-ranker = fastembed (ONNX). Pay data = award rate tables
  parsed into `pay_rates`. Frontend = React + Vite + TypeScript. Deploy = Azure.
