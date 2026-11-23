"""Evaluate the assistant against eval/golden_set.jsonl.

    python -m app.eval.run_eval --mode retrieval          # vector vs hybrid
    python -m app.eval.run_eval --mode retrieval --search-modes hybrid --no-save

Retrieval metrics (answerable questions, k = 6):
- hit@1, hit@6: share of questions with an expected clause at rank 1 / in the top 6;
- MRR@6: mean of 1/rank of the first expected clause (0 if not in the top 6);
- hit@24: an expected clause among the 24 candidates (what a re-ranker could recover);
- latency p50/p95 per search.
A retrieved chunk matches an expected clause if its citation without the "#n" split suffix
equals it ("MA000009:18.1#2" matches "MA000009:18.1"). Questions with an award use it as the
search filter (the router or UI picks the award); the others search all awards.

Decline questions (not in the sources) report the closest cosine distance next to the one
for answerable questions, to tune the relevance gate later (step 13).

Results go to eval/results/<mode>_<timestamp>.json and the eval_runs table.
Later steps add answer modes (faithfulness, routes, red-team).
"""

import argparse
import json
import statistics
import subprocess
import time
from datetime import UTC, datetime
from pathlib import Path

from psycopg.types.json import Jsonb

from app.config import REPO_DIR
from app.db.conn import connect
from app.retrieval.search import CANDIDATES, MODES, search

GOLDEN_PATH = REPO_DIR / "eval" / "golden_set.jsonl"
RESULTS_DIR = REPO_DIR / "eval" / "results"
K = 6


def load_golden(path: Path = GOLDEN_PATH) -> list[dict]:
    return [json.loads(line) for line in path.read_text("utf-8").splitlines() if line.strip()]


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


def run_retrieval(conn, golden: list[dict], search_mode: str, k: int = K) -> dict:
    """Retrieves CANDIDATES chunks per question: metrics at k, plus hit@CANDIDATES, the share
    a re-ranker over the candidate pool could still put on top."""
    rows, ranks, latencies = [], [], []
    top_dist = {"answerable": [], "decline": []}
    for item in golden:
        started = time.perf_counter()
        hits = search(
            conn, item["question"], mode=search_mode, award=item.get("award"), k=CANDIDATES
        )
        latencies.append(1000 * (time.perf_counter() - started))
        retrieved = [h.citation for h in hits]
        distances = [h.distance for h in hits[:k] if h.distance is not None]
        kind = "answerable" if item["expected"] else "decline"
        if distances:
            top_dist[kind].append(min(distances))
        rank = first_hit_rank(retrieved, item["expected"]) if item["expected"] else None
        if item["expected"]:
            ranks.append(rank)
        rows.append({"id": item["id"], "rank": rank, "retrieved": retrieved})

    metrics = retrieval_metrics(ranks, k)
    metrics[f"hit@{CANDIDATES}"] = sum(r is not None for r in ranks) / len(ranks)
    metrics["latency_ms_p50"] = round(statistics.median(latencies), 1)
    metrics["latency_ms_p95"] = round(_percentile(latencies, 0.95), 1)
    for kind, values in top_dist.items():
        if values:
            metrics[f"top1_distance_{kind}_mean"] = round(statistics.mean(values), 3)
    return {"search_mode": search_mode, "metrics": metrics, "questions": rows}


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


def markdown_table(results: list[dict], k: int = K) -> str:
    header = (
        f"| Retrieval | hit@1 | hit@{k} | MRR@{k} | hit@{CANDIDATES} | p50 ms "
        "| top-1 dist. answerable / decline |"
    )
    lines = [header, "|---|---|---|---|---|---|---|"]
    for r in results:
        m = r["metrics"]
        dist = (
            f"{m.get('top1_distance_answerable_mean', '-')} / "
            f"{m.get('top1_distance_decline_mean', '-')}"
        )
        lines.append(
            f"| {r['search_mode']} | {m['hit@1']:.2f} | {m[f'hit@{k}']:.2f} | "
            f"{m[f'mrr@{k}']:.3f} | {m[f'hit@{CANDIDATES}']:.2f} | {m['latency_ms_p50']:.0f} "
            f"| {dist} |"
        )
    return "\n".join(lines)


def main() -> None:
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    ap.add_argument("--mode", choices=["retrieval"], default="retrieval")
    ap.add_argument("--search-modes", nargs="+", choices=MODES, default=list(MODES))
    ap.add_argument("--no-save", action="store_true", help="don't write results or eval_runs")
    args = ap.parse_args()

    golden = load_golden()
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

        if args.no_save:
            return
        stamp = datetime.now(UTC).strftime("%Y%m%dT%H%M%SZ")
        payload = {"mode": args.mode, "git_sha": _git_sha(), "created": stamp, "runs": results}
        RESULTS_DIR.mkdir(parents=True, exist_ok=True)
        out = RESULTS_DIR / f"{args.mode}_{stamp}.json"
        out.write_text(json.dumps(payload, indent=1), encoding="utf-8")
        conn.execute(
            "INSERT INTO eval_runs (git_sha, mode, metrics) VALUES (%s, %s, %s)",
            (
                payload["git_sha"],
                args.mode,
                Jsonb({r["search_mode"]: r["metrics"] for r in results}),
            ),
        )
        conn.commit()
        print(f"\nsaved {out.relative_to(REPO_DIR)} and an eval_runs row")


if __name__ == "__main__":
    main()
