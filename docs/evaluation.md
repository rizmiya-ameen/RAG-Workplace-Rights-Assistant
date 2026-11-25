# Evaluation

How retrieval, routing and answers are measured, with the full numbers behind the summary in
the [README](../README.md#evaluation). All runs use the same data as the live demo.

## Golden set and retrieval

The golden set (`eval/golden_set.jsonl`) has 77 answerable questions (59 on the awards, 10 on
the NES, 8 on WorkSafe guidance), each with expected clauses and an evidence quote, plus 9
questions the assistant should decline. A test re-parses the saved sources and checks that
every quote appears word for word in the clause it cites, so the eval data cannot contain
invented clauses. (Step 15 turned one decline question, reporting an injury to WorkSafe,
into an answerable one.)

Retrieval on the first 59 award questions, top 6 results, award filter applied when the
question names an industry:

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

With the NES and WorkSafe added (77 questions; not comparable row for row with the table
above):

| Retrieval (step 15) | hit@1 | hit@6 | MRR@6 | p50 latency |
|---|---|---|---|---|
| Vector only | 0.62 | 0.88 | 0.722 | 81 ms |
| **Vector + re-ranker** (used by `/chat`) | **0.77** | **0.96** | **0.832** | 3.5 s |

A question naming an award also searches the NES and WorkSafe pages, since they apply to
every award's employees. The first version put all of them in one top-24 pool, and the 233
general chunks pushed two award clauses out of it (right to disconnect, pay deductions):
hit@6 0.94. Now the award gets its own top 24 and the general documents their own top 8,
and the re-ranker picks across both (0.96, with a regression test). The cost is 32 clauses
to re-rank instead of 24 (p50 2.4 s → 3.5 s on the laptop CPU). Still missed: retail-10
(not in the candidates), and two NES questions where the award's own 38-hour and
casual-conversion clauses outrank the NES section.

**Relevance gate.** If the best re-rank score is below −9.7, the app answers "insufficient
context" without calling the LLM. The scores overlap too much for a tighter gate: a found
answer scored −8.7 (till shortfalls) while an out-of-scope visa question scored +0.3, because
the model rates topical similarity, not whether the clause answers the question. So the
threshold is the lowest found answer minus a 1.0 margin; it stops only clearly unrelated
questions, and the model's own "insufficient context" reply handles the rest.

### Full answer evaluation

Every golden, red-team and route question goes through the real pipeline (`POST /chat`
path) on Gemini `gemini-3.5-flash-lite`. An LLM judge sees only the answer and the clauses it
cites and decides whether every statement is supported. Pay answers are checked by amount
instead: every expected amount (recomputed from the award page by a test) must be stated.

| Version | hit@6 | Answer rate | Expected clause cited | Faithfulness | Decline accuracy | Red-team pass | p50 / p95 latency | Cost |
|---|---|---|---|---|---|---|---|---|
| Vector + guardrails | 0.90 | 0.90 (53/59) | 0.98 | 1.00 (53/53) | 1.00 (10/10) | 1.00 (44/44) | 1.5 s / 2.5 s | $0 (free tier; 137 LLM calls, 198k tokens) |
| + re-ranker and relevance gate (partial: 69 of 115 questions†) | 0.98 | 1.00 (38/38) | 1.00 | 0.97 (32/33) | 1.00 (2/2) | 1.00 (29/29) | 2.9 s / 5.7 s | $0 |
| **+ router and pay tool, NES and WorkSafe, injection classifier** (steps 14–16; 77 + 9 golden, 62 red-team, 29 route questions) | 0.96 | 0.90 (69/77) | 1.00 | 0.99 (67/68) | 1.00 (9/9) | 0.97 (60/62)‡ | 4.2 s / 9.9 s | $0 (197 LLM calls, 301k tokens) |

‡ The two red-team failures are the classifier's documented false positives (fp-13, fp-15,
see [security.md](security.md#guardrails)). Route questions: 29 of 29 right, and all 18 pay answers state the expected
amounts. Latency by route: answered from clauses p50 5.3 s (the CPU re-ranker over 32
clauses is most of it on the dev laptop), pay answers 1.3 s, blocked 35 ms.

What the steps 14–16 runs showed:
- The first run after step 15 scored 0.94 faithfulness. Two of its four flags came from my
  own prompt: a line added in step 15 ("the NES is a minimum; an award can add to it") was
  repeated without a source, and the rule "say the amount comes from the pay-rate table"
  made the model write "the amount is not stated" for an allowance whose amount sits in the
  clause. Both fixed; 0.99 on the re-run. That run's route score (0.82) was an eval bug: the
  LLM asking a clarifying question was counted as a router misroute (fixed, with a test).
- The one remaining flag is fair: asked about the right to disconnect, the model listed the
  factors that decide whether refusing contact is unreasonable. The clause only says
  section 333M(3) "lists matters"; the factors came from the model's own knowledge.
- More clarifying questions: 6 answerable questions got "which award / are you casual?"
  instead of an answer (1 in step 12), mostly general questions where NES and award
  clauses are retrieved together (nes-01, nes-02, all-01, all-02). Two are retrieval misses
  (retail-10, nes-10).

† The run stopped at the free tier's daily request limit (HTTP 429) after 69 questions,
and the full run above replaces it. The four earlier retrieval misses it reached
(hosp-02, hosp-07, hosp-10, hosp-22) are now answered, cite the expected clause and pass the
judge. The one flagged answer (hosp-20) is a strict call: it says salaried managers don't
get annual leave loading, which clause 25.2(g) does say, but in clumsy wording. Latency is
higher because the re-ranker runs on CPU (~1.5 s).

- **Answer rate:** answerable questions that got a cited answer. In the first row, five of
  the six misses were retrieval misses (the right clause was not in the top 6, e.g. the
  right-to-disconnect clause), which is why the re-ranker came next. One was a sensible
  clarifying question.
- **Decline accuracy:** questions the sources cannot answer (a nurse's wage, visa work
  limits, payroll tax) that got no cited answer.
- **Red-team pass:** attacks stopped by the right guard, and legitimate look-alike questions
  not stopped by any guard. Two known gaps (base64 and letter-spaced instructions) are
  reported separately and still get through; the retrieved text is wrapped as data, and
  neither leaked anything.
- **Caveats:** 77 questions is a small set, and the judge is the same model family as the
  answerer, which can be lenient. The judge prompt is in `backend/app/eval/answers.py`.

The first full run scored 0.92 on citations and 0.90 on faithfulness. Reading the flagged
answers found three bugs in the pipeline, not the model: citations to sub-paragraphs
("28.2(a)") were dropped, a citation of a split clause kept only its first part, and times
in rate tables ("7.00 pm") were masked as dollar amounts. Each fix has a regression test.

Run it: `cd backend; uv run python -m app.eval.run_eval --mode retrieval` (or
`--mode redteam-input`, or `--mode answers`, ~15 minutes, resumable with `--resume`).


## Router

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

