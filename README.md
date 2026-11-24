# Workplace Rights Assistant

A retrieval-augmented assistant for Australian workplace rights. It answers questions about
Modern Awards (Hospitality MA000009, General Retail MA000004, Fast Food MA000003, Clerks
MA000002), with the National Employment Standards and WorkSafe Victoria guidance to follow,
and cites the exact clause every time (for example `MA000009:28.2`).

It gives general information only, never personal legal advice. Personal or dispute questions
are referred to the Fair Work Ombudsman (13 13 94).

> Work in progress: build steps 1–14 of 22 are done. Retrieval with a cross-encoder
> re-ranker, cited answers (`POST /chat`), exact pay rates from a structured tool, guardrails
> and the full evaluation work; NES and WorkSafe sources and the web UI come next.

## How it works

```
user -> input guard -> router -> { rag | pay_rate | clarify | refer | out_of_scope }
rag:      vector search (pgvector; hybrid with full text is an option) -> top 24
          -> cross-encoder re-rank -> top 6 -> relevance gate -> LLM (JSON + citations)
pay_rate: SQL on pay_rates -> LLM only phrases the tool result
all answers -> output guard (canary, citation check, $ check, PII, disclaimer) -> user
```

- **Ingestion:** award pages from awards.fairwork.gov.au (domain allow-list), parsed into one
  chunk per clause (1,163 chunks) and embedded with `bge-small-en-v1.5` via fastembed (ONNX).
- **Pay rates:** read from the awards' own rate tables into a structured `pay_rates` table
  (1,227 adult rates), cross-checked against each other. The LLM never reads pay amounts from
  text.
- **Router:** plain rules. A question asking for an amount, with the award, employment type
  and classification level known, goes to the pay tool (SQL); if one of those is missing, the
  app asks for it (a clarifying question written by code, no LLM call); everything else is
  answered from the cited clauses.
- **Storage:** Postgres 18 + pgvector on Neon (HNSW for vectors, GIN for full text).
- **Generation:** any OpenAI-compatible model; the demo uses Google Gemini
  (`gemini-3.5-flash-lite`, free tier). Sources are wrapped in `<source id=…>` tags as data,
  dollar amounts are masked before the model sees them, and the reply must be JSON whose
  citations were actually retrieved. A per-request canary detects prompt leaks. The
  disclaimer is added by code, not the model.

Example pay answer (`POST /chat`; here the LLM was over its quota, so the template answer
was used, see below):

> **Q:** How much does a level 2 casual get in a hotel on Sunday?
>
> **A:** Minimum adult rates under the Hospitality Industry (General) Award:
> - Level 2, casual, Sunday (175%): $47.39 per hour (clause B.2.3, from 2026-07-01)
>
> These are the award's minimums; an employer can pay more. *(cites MA000009:B.2.3)*

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

## Guardrails

Mapped to the OWASP Top 10 for LLM applications. Each rule has a test, and
`eval/redteam.jsonl` holds attacks next to legitimate questions that look like them.

| Stage | Checks |
|---|---|
| Input (before search or the LLM) | Length limit; Unicode normalisation (zero-width and look-alike characters); PII redaction (email, phone, TFN with checksum, BSB, account number); prompt-injection and evasion patterns -> blocked; personal advice or disputes -> referred to the Fair Work Ombudsman; off-topic -> out of scope |
| Context (retrieved clauses) | Source URL must be on the domain allow-list; tags and hidden characters stripped; clauses containing injection patterns dropped; sources wrapped as escaped data; dollar amounts masked |
| Output (the model's reply) | Canary leak -> blocked; invalid JSON -> fallback; citations not retrieved -> dropped, none left -> fallback; dollar amounts not from the pay tool removed; PII redacted; disclaimer added by code |

Patterns are narrow on purpose: "Can my employer ignore the rules about meal breaks?" and "My
boss pays me cash in hand, is that allowed?" are worker questions and get answers;
"How can I avoid paying penalty rates to my staff?" does not. Guardrail decisions are logged
to `guardrail_events` with rule names and counts, never the question text.

Red-team input routes (`run_eval --mode redteam-input`, no LLM):

| Rows | Route accuracy | Attacks caught | False positives (red-team) | Golden questions blocked | Known gaps caught |
|---|---|---|---|---|---|
| 44 + 2 known gaps | 1.00 | 1.00 | 0.00 | 0 of 59 | 0 of 2 |

The two known gaps (a base64-encoded instruction and letter-spaced text) are invisible to
regex rules; the Prompt Guard classifier is the next layer for those. Even when they get
through, retrieved text is wrapped as data and the output checks still apply.

## Router and pay rates

| Question | Route | What happens |
|---|---|---|
| "How much does a level 2 casual get in a hotel on Sunday?" | `pay_rate` | SQL on `pay_rates`; answer cites the rate table (B.2.3) |
| "How much do casuals get paid at a cafe?" | `clarify` | "…I need to know your classification level (Introductory, Level 1, … Level 6, Level 3A)" |
| "How much overtime pay does a casual get in hospitality?" | `rag` | Overtime, evening, junior and allowance amounts aren't in the tool, so the clauses explain the percentages |
| "Is the pay rate higher on Sundays?" | `rag` | A yes/no question asks about the rule, not an amount |

The LLM only phrases the tool's rows. Its wording is used only if every dollar figure in it
is one the tool returned and it states at least one of them; otherwise (or if the LLM is
down) a template answer built from the same rows is used, so a pay answer never depends on
the model getting a number right.

Router eval (`run_eval --mode routes`, no LLM): `eval/routes.jsonl` has 29 rows (17 pay,
5 clarify, 7 look-alike questions that must stay on the clauses). A test computes each
expected amount from the parsed award page with the tool's own filter, so no amount is typed
by hand.

| Route rows | Route accuracy (pay / clarify / rag) | Golden questions on the right route |
|---|---|---|
| 29 | 1.00 (1.00 / 1.00 / 1.00) | 69 of 69 |

Those rows were written while building the router, so 1.00 flatters it. A check with nine
fresh phrasings found only 2 of the 7 pay questions routed correctly ("what do level 4 retail
casuals earn…", "pay for a part time level 3 cook…" went to the clauses). Three general
rules fixed them ("earn" always means money; a money word plus a level and an employment
type is an amount question; "hourly pay"), and those nine rows are now in the set. A
misrouted pay question fails safe: on the clause route the model cannot state amounts, so
the user gets no number rather than a wrong one.

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
| **Vector + re-ranker** (used by `/chat`) | **0.83** | **0.98** | **0.884** | 0.98 | 2.7 s |
| Hybrid + re-ranker | 0.85 | 0.98 | 0.902 | 0.98 | ~5 s* |

Hybrid search scored lower than vector search alone. Full text finds exact references such
as "clause 28.2", but in plain-language questions its keyword matches push the best semantic
hits down the list. Both modes have the right clause among the top 24 candidates for 58 of 59
questions, so the re-ranker works from the same pool either way.

The re-ranker (`ms-marco-MiniLM-L-6-v2`, a cross-encoder via fastembed/ONNX) reads the
question and each of the 24 candidate clauses together and re-orders them. It put the right
clause first for 83% of questions instead of 64%, and recovered five of the six top-6 misses
(the right-to-disconnect clause moved from rank 23 to 1). Vector + re-ranker is the default:
it matches hybrid on hit@6, is simpler, and hybrid lost one question's clause from the pool.

The cost is latency on CPU. Each batch is padded to its longest clause, so batches of 2 took
~1.5 s for 24 clauses on the dev laptop against ~3.5 s for one batch of 24. (*Hybrid +
re-ranker was measured before that change.)

**Relevance gate.** If the best re-rank score is below −9.7, the app answers "insufficient
context" without calling the LLM. The scores overlap too much for a tighter gate: a found
answer scored −8.7 (till shortfalls) while an out-of-scope visa question scored +0.3, because
the model rates topical similarity, not whether the clause answers the question. So the
threshold is the lowest found answer minus a 1.0 margin; it stops only clearly unrelated
questions, and the model's own "insufficient context" reply handles the rest.

### Full answer evaluation

Every golden and red-team question goes through the real pipeline (`POST /chat` path) on
Gemini `gemini-3.5-flash-lite`. An LLM judge sees only the answer and the clauses it cites
and decides whether every statement is supported.

| Version | hit@6 | Answer rate | Expected clause cited | Faithfulness | Decline accuracy | Red-team pass | p50 / p95 latency | Cost |
|---|---|---|---|---|---|---|---|---|
| Vector + guardrails | 0.90 | 0.90 (53/59) | 0.98 | 1.00 (53/53) | 1.00 (10/10) | 1.00 (44/44) | 1.5 s / 2.5 s | $0 (free tier; 137 LLM calls, 198k tokens) |
| + re-ranker and relevance gate (partial: 69 of 115 questions†) | 0.98 | 1.00 (38/38) | 1.00 | 0.97 (32/33) | 1.00 (2/2) | 1.00 (29/29) | 2.9 s / 5.7 s | $0 |

† The run stopped at the free tier's daily request limit (HTTP 429) after 69 questions;
it is resumable and the row will be completed. The four earlier retrieval misses it reached
(hosp-02, hosp-07, hosp-10, hosp-22) are now answered, cite the expected clause and pass the
judge. The one flagged answer (hosp-20) is a strict call: it says salaried managers don't
get annual leave loading, which clause 25.2(g) does say, but in clumsy wording. Latency is
higher because the re-ranker runs on CPU (~1.5 s).

- **Answer rate:** answerable questions that got a cited answer. Five of the six misses are
  retrieval misses (the right clause was not in the top 6, e.g. the right-to-disconnect
  clause); the re-ranker is the next step. One was a sensible clarifying question.
- **Decline accuracy:** questions the sources cannot answer (a nurse's wage, visa work
  limits, payroll tax) that got no cited answer.
- **Red-team pass:** attacks stopped by the right guard, and legitimate look-alike questions
  not stopped by any guard. Two known gaps (base64 and letter-spaced instructions) are
  reported separately and still get through; the retrieved text is wrapped as data, and
  neither leaked anything.
- **Caveats:** 59 questions is a small set, and the judge is the same model family as the
  answerer, which can be lenient. The judge prompt is in `backend/app/eval/answers.py`.

The first full run scored 0.92 on citations and 0.90 on faithfulness. Reading the flagged
answers found three bugs in the pipeline, not the model: citations to sub-paragraphs
("28.2(a)") were dropped, a citation of a split clause kept only its first part, and times
in rate tables ("7.00 pm") were masked as dollar amounts. Each fix has a regression test.

Run it: `cd backend; uv run python -m app.eval.run_eval --mode retrieval` (or
`--mode redteam-input`, or `--mode answers`, ~15 minutes, resumable with `--resume`).

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
