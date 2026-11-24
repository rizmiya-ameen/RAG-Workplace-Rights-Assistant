# Workplace Rights Assistant

A retrieval-augmented assistant for Australian workplace rights. It answers questions about
Modern Awards (Hospitality MA000009, General Retail MA000004, Fast Food MA000003, Clerks
MA000002), with the National Employment Standards and WorkSafe Victoria guidance to follow,
and cites the exact clause every time (for example `MA000009:28.2`).

It gives general information only, never personal legal advice. Personal or dispute questions
are referred to the Fair Work Ombudsman (13 13 94).

> Work in progress: build steps 1–10 of 22 are done. Retrieval, evaluation and cited answers
> (`POST /chat`) work; guardrails, the re-ranker, the pay-rate tool and the web UI come next.

## How it works

```
user -> input guard -> router -> { rag | pay_rate | clarify | refer | out_of_scope }
rag:      hybrid search (pgvector + Postgres full text, RRF) -> cross-encoder re-rank
          -> relevance gate -> LLM (JSON + citations)
pay_rate: SQL on pay_rates -> LLM only phrases the tool result
all answers -> output guard (canary, citation check, $ check, PII, disclaimer) -> user
```

- **Ingestion:** award pages from awards.fairwork.gov.au (domain allow-list), parsed into one
  chunk per clause (1,163 chunks) and embedded with `bge-small-en-v1.5` via fastembed (ONNX).
- **Pay rates:** read from the awards' own rate tables into a structured `pay_rates` table
  (1,227 adult rates), cross-checked against each other. The LLM never reads pay amounts from
  text.
- **Storage:** Postgres 18 + pgvector on Neon (HNSW for vectors, GIN for full text).
- **Generation:** any OpenAI-compatible model; the demo uses Google Gemini
  (`gemini-3.5-flash-lite`, free tier). Sources are wrapped in `<source id=…>` tags as data,
  dollar amounts are masked before the model sees them, and the reply must be JSON whose
  citations were actually retrieved. A per-request canary detects prompt leaks. The
  disclaimer is added by code, not the model.

Example (`POST /chat`, General Retail):

> **Q:** How much notice does an employer have to give for a roster change?
>
> **A:** For permanent roster changes, employers must give employees other than part-time
> employees at least 7 days' written notice, which extends to 14 days if the employee
> disagrees (MA000004:15.9#1). For part-time employees, the employer can change the regular
> pattern of work by giving 7 days' written notice, or 48 hours in an emergency
> (MA000004:10.10).

### A bug the citations caught

The first real answer said casual hospitality workers get 150% on Sundays. The cited clause
(MA000009 B.2.3) says 175%. Rate tables lose their column layout when stored as text, so the
model matched the "Saturday Sunday" headers to the wrong percentages. Rate tables are now
labelled in the prompt and the model may not read figures off them; exact rates come from the
structured pay-rate tool, which reads the real table grid.

## Evaluation

The golden set (`eval/golden_set.jsonl`) has 59 answerable questions, each with expected
clauses and an evidence quote, plus 10 questions the assistant should decline. A test
re-parses the saved awards and checks that every quote appears word for word in the clause it
cites, so the eval data cannot contain invented clauses.

Retrieval, top 6 results, award filter applied when the question names an industry:

| Retrieval | hit@1 | hit@6 | MRR@6 | hit@24 | p50 latency |
|---|---|---|---|---|---|
| Vector only | 0.64 | 0.90 | 0.735 | 0.98 | 50 ms |
| Hybrid (vector + full text, RRF) | 0.54 | 0.80 | 0.641 | 0.98 | 73 ms |

Hybrid search scored lower than vector search alone. Full text finds exact references such
as "clause 28.2", but in plain-language questions its keyword matches push the best semantic
hits down the list. Both modes have the right clause among the top 24 candidates for 58 of 59
questions, so the cross-encoder re-ranker (next on the plan) works from the same pool either
way.

Run it: `cd backend; uv run python -m app.eval.run_eval --mode retrieval`

## Running locally

Requires Python 3.12 with [uv](https://docs.astral.sh/uv/) and a Postgres 18 database with
pgvector (the project uses Neon).

```bash
cp .env.example backend/.env          # then set DATABASE_URL and LLM_API_KEY
cd backend
uv run python -m app.db.init          # create tables
uv run python -m app.ingestion.ingest --all
uv run python -m app.retrieval.search "Do casuals get more on Sundays?" --award MA000009
uv run python -m app.pipeline "Can an office worker ask to work from home?"   # needs LLM_API_KEY
uv run uvicorn app.main:app                                                 # /docs, POST /chat
uv run pytest -q
```
