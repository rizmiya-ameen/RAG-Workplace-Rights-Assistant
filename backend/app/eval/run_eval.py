"""Evaluate the assistant against eval/golden_set.jsonl.

    python -m app.eval.run_eval --mode retrieval          # vector / hybrid, +/- re-ranker
    python -m app.eval.run_eval --mode retrieval --search-modes vector+rerank --no-save
    python -m app.eval.run_eval --mode redteam-input      # input guard routes, no LLM
    python -m app.eval.run_eval --mode routes             # router decisions, no LLM

Retrieval metrics (answerable questions, k = 6):
- hit@1, hit@6: share of questions with an expected clause at rank 1 / in the top 6;
- MRR@6: mean of 1/rank of the first expected clause (0 if not in the top 6);
- hit@24: an expected clause among the 24 candidates (what a re-ranker could recover);
- latency p50/p95 per search.
A retrieved chunk matches an expected clause if its citation without the "#n" split suffix
equals it ("MA000009:18.1#2" matches "MA000009:18.1"). Questions with an award use it as the
search filter (the router or UI picks the award); the others search all awards.

Variants: vector, hybrid, and each "+rerank" (cross-encoder over the 24 candidates, step 13).
Decline questions (not in the sources) report the closest cosine distance and, with the
re-ranker, the top re-rank score next to the answerable ones. `gate` suggests the
relevance-gate threshold (see suggest_gate).

Red-team input (eval/redteam.jsonl): the input guard's route for every row against
expected_route ("rag" = passes the guard). Rows marked known_gap are reported separately
(attacks the regex rules cannot see yet; Prompt Guard classifier in step 16). The answerable
golden questions are also run, to count false positives on ordinary questions. The
must_not_* / must_cite fields are checked on full answers in step 12.

Routes (eval/routes.jsonl, step 14): the router's route for pay, clarify and look-alike
rag questions (pay rows also check the award, employment type, level and days it read), plus
the golden set: answerable questions must stay on rag (or the route in their
"expected_route"), and decline questions must never get a pay answer.

Results go to eval/results/<mode>_<timestamp>.json and the eval_runs table.
Later steps add answer modes (faithfulness, routes, red-team).
"""

import argparse
import json
import statistics
import subprocess
import sys
import time
from datetime import UTC, datetime
from pathlib import Path

from psycopg.types.json import Jsonb

from app.config import REPO_DIR
from app.db.conn import connect
from app.eval.answers import Recorder, answer_metrics, run_one
from app.guardrails.input_guard import check_input
from app.retrieval.search import CANDIDATES, MODES, search
from app.router import route_question

VARIANTS = (*MODES, *(f"{m}+rerank" for m in MODES))

GOLDEN_PATH = REPO_DIR / "eval" / "golden_set.jsonl"
REDTEAM_PATH = REPO_DIR / "eval" / "redteam.jsonl"
ROUTES_PATH = REPO_DIR / "eval" / "routes.jsonl"
RESULTS_DIR = REPO_DIR / "eval" / "results"
K = 6


def load_golden(path: Path = GOLDEN_PATH) -> list[dict]:
    return [json.loads(line) for line in path.read_text("utf-8").splitlines() if line.strip()]


def load_redteam(path: Path = REDTEAM_PATH) -> list[dict]:
    return load_golden(path)


def input_route(question: str, classifier=None) -> str:
    route = check_input(question, classifier=classifier).route
    return "rag" if route == "pass" else route


def full_route(question: str, award: str | None):
    """(route, router decision or None): the input guard, then the router."""
    checked = check_input(question)
    if checked.route != "pass":
        return checked.route, None
    decision = route_question(checked.text, award)
    return decision.route, decision


def run_routes(rows: list[dict], golden: list[dict]) -> dict:
    results = []
    for r in rows:
        route, d = full_route(r["question"], r["award"])
        ok = route == r["expected_route"]
        if ok and route == "pay_rate":
            s = r["slots"]
            ok = (d.award, d.employment_type, d.level, list(d.days), d.casino) == (
                s["award"],
                s["employment_type"],
                s["level"],
                s["days"],
                s.get("casino", False),
            )
        if ok and route == "clarify":
            ok = d.missing == r["missing"]
        results.append({"id": r["id"], "expected": r["expected_route"], "actual": route, "ok": ok})

    golden_wrong = []
    for g in golden:
        route, _ = full_route(g["question"], g["award"])
        if g["expected"]:
            want = g.get("expected_route", "rag")
            if route != want:
                golden_wrong.append(f"{g['id']}({route})")
        elif route == "pay_rate":
            golden_wrong.append(f"{g['id']}({route})")
    metrics = {
        "n": len(results),
        "route_accuracy": sum(r["ok"] for r in results) / len(results),
        "golden_n": len(golden),
        "golden_route_accuracy": 1 - len(golden_wrong) / len(golden),
    }
    for kind in ("pay_rate", "clarify", "rag"):
        part = [r for r in results if r["expected"] == kind]
        metrics[f"{kind}_accuracy"] = sum(r["ok"] for r in part) / len(part)
    return {
        "metrics": metrics,
        "failures": [r for r in results if not r["ok"]],
        "golden_wrong": golden_wrong,
        "rows": results,
    }


def run_redteam_input(rows: list[dict], golden: list[dict], classifier=None) -> dict:
    """`classifier`: injection scorer for the input guard (default: per settings, i.e. the
    ONNX classifier in a normal run)."""
    results = [
        {
            "id": r["id"],
            "category": r["category"],
            "expected": r["expected_route"],
            "actual": input_route(r["question"], classifier),
            "known_gap": bool(r.get("known_gap")),
            "needs_classifier": bool(r.get("needs_classifier")),
        }
        for r in rows
    ]
    scored = [r for r in results if not r["known_gap"]]
    legit = [r for r in scored if r["expected"] == "rag"]
    attacks = [r for r in scored if r["expected"] != "rag"]
    golden_blocked = [
        g["id"] for g in golden if g["expected"] and input_route(g["question"], classifier) != "rag"
    ]
    metrics = {
        "n": len(scored),
        "route_accuracy": sum(r["expected"] == r["actual"] for r in scored) / len(scored),
        "attacks_caught": sum(r["actual"] == r["expected"] for r in attacks) / len(attacks),
        "redteam_false_positive_rate": sum(r["actual"] != "rag" for r in legit) / len(legit),
        "golden_false_positives": len(golden_blocked),
        "known_gaps_caught": sum(r["actual"] == r["expected"] for r in results if r["known_gap"]),
        "known_gaps": sum(r["known_gap"] for r in results),
        "classifier_only_caught": sum(
            r["actual"] == r["expected"] for r in scored if r["needs_classifier"]
        ),
        "classifier_only_rows": sum(r["needs_classifier"] for r in scored),
        "false_positives": [r["id"] for r in legit if r["actual"] != "rag"],
    }
    return {
        "metrics": metrics,
        "failures": [r for r in scored if r["expected"] != r["actual"]],
        "golden_blocked": golden_blocked,
        "rows": results,
    }


def base_citation(citation: str) -> str:
    return citation.split("#", 1)[0]


def first_hit_rank(retrieved: list[str], expected: list[str]) -> int | None:
    """1-based rank of the first retrieved citation that matches an expected clause."""
    wanted = set(expected)
    for rank, citation in enumerate(retrieved, 1):
        if base_citation(citation) in wanted:
            return rank
    return None


def retrieval_metrics(ranks: list[int | None], k: int = K) -> dict:
    n = len(ranks)
    return {
        "n": n,
        "hit@1": sum(r == 1 for r in ranks) / n,
        f"hit@{k}": sum(r is not None and r <= k for r in ranks) / n,
        f"mrr@{k}": sum(1 / r for r in ranks if r is not None and r <= k) / n,
    }


def _percentile(values: list[float], p: float) -> float:
    ordered = sorted(values)
    return ordered[min(len(ordered) - 1, round(p * (len(ordered) - 1)))]


def suggest_gate(rows: list[dict], k: int = K) -> dict | None:
    """Relevance-gate threshold on the top re-rank score: the highest value that still lets
    through every answerable question whose expected clause is in the top k (gating those
    would turn a good answer into "insufficient context"), minus a margin of 1.0 logit
    because 59 questions is a small sample. Reports how many decline questions it stops."""
    scored = [r for r in rows if r.get("top_rerank") is not None]
    if not scored:
        return None
    found = [r["top_rerank"] for r in scored if r["rank"] is not None and r["rank"] <= k]
    threshold = round(min(found) - 1.0, 2)
    answerable = [r for r in scored if r["answerable"]]
    decline = [r for r in scored if not r["answerable"]]
    return {
        "threshold": threshold,
        "answerable_gated": sum(r["top_rerank"] < threshold for r in answerable),
        "decline_gated": sum(r["top_rerank"] < threshold for r in decline),
        "answerable": len(answerable),
        "decline": len(decline),
    }


def run_retrieval(conn, golden: list[dict], variant: str, k: int = K) -> dict:
    """Retrieves CANDIDATES chunks per question: metrics at k, plus hit@CANDIDATES, the share
    a re-ranker over the candidate pool could still put on top."""
    mode, _, extra = variant.partition("+")
    rows, ranks, latencies = [], [], []
    top_dist = {"answerable": [], "decline": []}
    top_rerank = {"answerable": [], "decline": []}
    for item in golden:
        started = time.perf_counter()
        hits = search(
            conn,
            item["question"],
            mode=mode,
            award=item.get("award"),
            k=CANDIDATES,
            rerank=extra == "rerank",
        )
        latencies.append(1000 * (time.perf_counter() - started))
        retrieved = [h.citation for h in hits]
        distances = [h.distance for h in hits[:k] if h.distance is not None]
        kind = "answerable" if item["expected"] else "decline"
        if distances:
            top_dist[kind].append(min(distances))
        best = hits[0].rerank_score if hits else None
        if best is not None:
            top_rerank[kind].append(best)
        rank = first_hit_rank(retrieved, item["expected"]) if item["expected"] else None
        if item["expected"]:
            ranks.append(rank)
        rows.append(
            {
                "id": item["id"],
                "answerable": bool(item["expected"]),
                "rank": rank,
                "top_rerank": best,
                "retrieved": retrieved,
            }
        )

    metrics = retrieval_metrics(ranks, k)
    metrics[f"hit@{CANDIDATES}"] = sum(r is not None for r in ranks) / len(ranks)
    metrics["latency_ms_p50"] = round(statistics.median(latencies), 1)
    metrics["latency_ms_p95"] = round(_percentile(latencies, 0.95), 1)
    for kind, values in top_dist.items():
        if values:
            metrics[f"top1_distance_{kind}_mean"] = round(statistics.mean(values), 3)
    for kind, values in top_rerank.items():
        if values:
            metrics[f"top1_rerank_{kind}_mean"] = round(statistics.mean(values), 2)
    gate = suggest_gate(rows, k)
    if gate:
        metrics["gate"] = gate
    return {"search_mode": variant, "metrics": metrics, "questions": rows}


def _git_sha() -> str | None:
    try:
        out = subprocess.run(
            ["git", "rev-parse", "--short", "HEAD"],
            check=False,
            cwd=REPO_DIR,
            capture_output=True,
            text=True,
        )
        return out.stdout.strip() or None
    except OSError:
        return None


def gate_failures(results: list[dict], min_hit: float, k: int = K) -> list[str]:
    """CI gate (step 20): every variant run must keep hit@k at or above min_hit."""
    return [
        f"{r['search_mode']}: hit@{k} {r['metrics'][f'hit@{k}']:.3f} < {min_hit}"
        for r in results
        if r["metrics"][f"hit@{k}"] < min_hit
    ]


def markdown_table(results: list[dict], k: int = K) -> str:
    header = (
        f"| Retrieval | hit@1 | hit@{k} | MRR@{k} | hit@{CANDIDATES} | p50 ms "
        "| top-1 dist. answerable / decline | top-1 re-rank answerable / decline |"
    )
    lines = [header, "|---|---|---|---|---|---|---|---|"]
    for r in results:
        m = r["metrics"]
        dist = (
            f"{m.get('top1_distance_answerable_mean', '-')} / "
            f"{m.get('top1_distance_decline_mean', '-')}"
        )
        lines.append(
            f"| {r['search_mode']} | {m['hit@1']:.2f} | {m[f'hit@{k}']:.2f} | "
            f"{m[f'mrr@{k}']:.3f} | {m[f'hit@{CANDIDATES}']:.2f} | {m['latency_ms_p50']:.0f} "
            f"| {dist} | {m.get('top1_rerank_answerable_mean', '-')} / "
            f"{m.get('top1_rerank_decline_mean', '-')} |"
        )
    return "\n".join(lines)


def main() -> None:
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    ap.add_argument(
        "--mode",
        choices=["retrieval", "redteam-input", "routes", "answers"],
        default="retrieval",
    )
    ap.add_argument("--search-modes", nargs="+", choices=VARIANTS, default=list(VARIANTS))
    ap.add_argument("--no-save", action="store_true", help="don't write results or eval_runs")
    ap.add_argument("--limit", type=int, help="answers: first N questions per set")
    ap.add_argument(
        "--sets",
        nargs="+",
        choices=["golden", "redteam", "routes"],
        default=["golden", "redteam", "routes"],
    )
    ap.add_argument("--pace", type=float, default=4.0, help="answers: seconds between LLM calls")
    ap.add_argument("--no-judge", action="store_true", help="answers: skip the faithfulness judge")
    ap.add_argument("--resume", type=Path, help="answers: results .jsonl to continue")
    ap.add_argument(
        "--min-hit",
        type=float,
        help=f"retrieval: exit 1 if any variant's hit@{K} is below this (CI gate)",
    )
    args = ap.parse_args()

    golden = load_golden()
    if args.mode == "answers":
        run_answers(args, golden)
        return
    if args.mode == "routes":
        result = run_routes(load_golden(ROUTES_PATH), golden)
        m = result["metrics"]
        print(
            f"router: {m['route_accuracy']:.2f} correct over {m['n']} route rows (pay "
            f"{m['pay_rate_accuracy']:.2f}, clarify {m['clarify_accuracy']:.2f}, rag "
            f"{m['rag_accuracy']:.2f}); golden questions on the right route "
            f"{m['golden_route_accuracy']:.2f} of {m['golden_n']}"
        )
        for f in result["failures"]:
            print(f"  FAIL {f['id']}: expected {f['expected']}, got {f['actual']}")
        for gid in result["golden_wrong"]:
            print(f"  golden question misrouted: {gid}")
        if not args.no_save:
            with connect() as conn:
                _save(conn, args.mode, {"routes": result}, {"routes": m})
        return
    if args.mode == "redteam-input":
        result = run_redteam_input(load_redteam(), golden)
        m = result["metrics"]
        print(
            f"red-team input routes: {m['route_accuracy']:.2f} correct over {m['n']} rows "
            f"(attacks caught {m['attacks_caught']:.2f}, false positives "
            f"{m['redteam_false_positive_rate']:.2f}); golden questions blocked: "
            f"{m['golden_false_positives']}; known gaps caught {m['known_gaps_caught']}/"
            f"{m['known_gaps']}"
        )
        for f in result["failures"]:
            print(f"  FAIL {f['id']}: expected {f['expected']}, got {f['actual']}")
        for gid in result["golden_blocked"]:
            print(f"  golden question blocked: {gid}")
        if not args.no_save:
            with connect() as conn:
                _save(conn, args.mode, {"redteam-input": result}, {"redteam-input": m})
        return

    with connect() as conn:
        results = [run_retrieval(conn, golden, m) for m in args.search_modes]
        answerable = sum(bool(g["expected"]) for g in golden)
        print(f"{answerable} answerable + {len(golden) - answerable} decline questions\n")
        print(markdown_table(results))
        answerable_ids = {g["id"] for g in golden if g["expected"]}
        for r in results:
            misses = [
                f"{q['id']}({q['rank'] or '-'})"
                for q in r["questions"]
                if q["id"] in answerable_ids and (q["rank"] is None or q["rank"] > K)
            ]
            print(
                f"\n{r['search_mode']} not in top {K} ({len(misses)}, rank in the "
                f"{CANDIDATES} candidates): {', '.join(misses) or 'none'}"
            )
            gate = r["metrics"].get("gate")
            if gate:
                print(
                    f"  gate: top re-rank score < {gate['threshold']} -> insufficient context; "
                    f"stops {gate['decline_gated']}/{gate['decline']} decline and "
                    f"{gate['answerable_gated']}/{gate['answerable']} answerable questions"
                )

        if not args.no_save:
            _save(conn, args.mode, results, {r["search_mode"]: r["metrics"] for r in results})

    if args.min_hit is not None:
        failures = gate_failures(results, args.min_hit)
        for f in failures:
            print(f"\nGATE FAILED {f}")
        if failures:
            sys.exit(1)
        print(f"\ngate passed: hit@{K} >= {args.min_hit} for {', '.join(args.search_modes)}")


def run_answers(args, golden: list[dict]) -> None:
    items = []
    if "golden" in args.sets:
        items += [("golden", g) for g in golden[: args.limit]]
    if "redteam" in args.sets:
        items += [("redteam", r) for r in load_redteam()[: args.limit]]
    if "routes" in args.sets:
        items += [("routes", r) for r in load_golden(ROUTES_PATH)[: args.limit]]

    stamp = datetime.now(UTC).strftime("%Y%m%dT%H%M%SZ")
    out = args.resume or RESULTS_DIR / f"answers_{stamp}.jsonl"
    done: dict[str, dict] = {}
    if args.resume:
        for line in out.read_text(encoding="utf-8").splitlines():
            row = json.loads(line)
            if row["route"] != "error":  # retry errors (quota, busy) on resume
                done[row["id"]] = row
    out.parent.mkdir(parents=True, exist_ok=True)
    rows = list(done.values())
    out.write_text("".join(json.dumps(r) + "\n" for r in rows), encoding="utf-8")

    recorder = Recorder(pace_s=args.pace)
    todo = [(k, it) for k, it in items if it["id"] not in done]
    print(f"{len(items)} questions, {len(done)} already done; results -> {out}")
    with connect() as conn:
        for n, (kind, item) in enumerate(todo, 1):
            row = run_one(conn, item, kind, recorder, do_judge=not args.no_judge)
            conn.commit()  # keep guardrail_events from the run
            rows.append(row)
            with out.open("a", encoding="utf-8") as f:
                f.write(json.dumps(row) + "\n")
            flag = row.get("failures") or (row.get("judge") or {}).get("unsupported_claims")
            print(f"[{n}/{len(todo)}] {row['id']:10} {row['route']:20} {flag or ''}", flush=True)

        metrics = answer_metrics(rows, golden)
        print("\n" + json.dumps(metrics, indent=1))
        if not args.no_save:
            _save(conn, "answers", {"results_file": out.name}, {"answers": metrics})


def _save(conn, mode: str, runs, metrics: dict) -> None:
    stamp = datetime.now(UTC).strftime("%Y%m%dT%H%M%SZ")
    payload = {"mode": mode, "git_sha": _git_sha(), "created": stamp, "runs": runs}
    RESULTS_DIR.mkdir(parents=True, exist_ok=True)
    out = RESULTS_DIR / f"{mode}_{stamp}.json"
    out.write_text(json.dumps(payload, indent=1), encoding="utf-8")
    conn.execute(
        "INSERT INTO eval_runs (git_sha, mode, metrics) VALUES (%s, %s, %s)",
        (payload["git_sha"], mode, Jsonb(metrics)),
    )
    conn.commit()
    print(f"\nsaved {out.relative_to(REPO_DIR)} and an eval_runs row")


if __name__ == "__main__":
    main()
