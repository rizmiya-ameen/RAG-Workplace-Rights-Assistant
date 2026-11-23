"""Golden-set integrity and eval-metric tests.

Every evidence quote must appear verbatim (whitespace-normalised) in the clause it cites, in
the parsed saved award, and every expected clause must exist. This keeps invented clauses or
quotes out of the eval data.
"""

from functools import cache

import pytest

from app.eval.run_eval import first_hit_rank, load_golden, retrieval_metrics
from app.ingestion.parse_award import latest_raw, parse_award_html

GOLDEN = load_golden()
ANSWERABLE = [g for g in GOLDEN if g["expected"]]


@cache
def clauses(award_id: str) -> dict[str, str]:
    """base clause ref -> whitespace-normalised text of all its chunks."""
    path, _ = latest_raw(award_id)
    out: dict[str, list[str]] = {}
    for c in parse_award_html(path.read_bytes()).chunks:
        out.setdefault(c.clause_ref.split("#")[0], []).append(c.content)
    return {ref: " ".join(" ".join(parts).split()) for ref, parts in out.items()}


def lookup(citation: str) -> str | None:
    award_id, ref = citation.split(":", 1)
    try:
        return clauses(award_id).get(ref)
    except FileNotFoundError:
        pytest.skip(f"{award_id} not fetched")


def test_ids_are_unique_and_fields_present():
    ids = [g["id"] for g in GOLDEN]
    assert len(ids) == len(set(ids))
    for g in GOLDEN:
        assert {"id", "question", "award", "expected", "evidence"} <= set(g), g["id"]
        if g["expected"]:
            assert g["evidence"], f"{g['id']}: answerable item needs evidence"
            if g["award"]:
                assert all(e.startswith(g["award"] + ":") for e in g["expected"]), g["id"]
        else:
            assert g["decline_reason"], g["id"]


def test_set_size_matches_plan():
    assert len(ANSWERABLE) >= 50
    assert len(GOLDEN) - len(ANSWERABLE) >= 10


@pytest.mark.parametrize("item", ANSWERABLE, ids=lambda g: g["id"])
def test_expected_clauses_exist(item):
    for citation in item["expected"]:
        assert lookup(citation) is not None, f"{citation} is not a clause in the parsed award"


@pytest.mark.parametrize("item", ANSWERABLE, ids=lambda g: g["id"])
def test_evidence_quotes_are_verbatim(item):
    for ev in item["evidence"]:
        assert ev["clause"] in item["expected"], f"{ev['clause']} cited but not expected"
        text = lookup(ev["clause"])
        assert text is not None, f"{ev['clause']} not found"
        quote = " ".join(ev["quote"].split())
        assert quote in text, f"quote not in {ev['clause']}: {quote!r}"


def test_first_hit_rank_ignores_split_suffix():
    retrieved = ["MA000009:11.5", "MA000009:18.1#2", "MA000009:11.1"]
    assert first_hit_rank(retrieved, ["MA000009:18.1"]) == 2
    assert first_hit_rank(retrieved, ["MA000009:11.1", "MA000009:11.5"]) == 1
    assert first_hit_rank(retrieved, ["MA000009:28.2"]) is None
    assert first_hit_rank(retrieved, ["MA000009:18"]) is None  # no prefix matching


def test_retrieval_metrics():
    m = retrieval_metrics([1, 2, None, 6, 7], k=6)
    assert m["n"] == 5
    assert m["hit@1"] == pytest.approx(1 / 5)
    assert m["hit@6"] == pytest.approx(3 / 5)
    assert m["mrr@6"] == pytest.approx((1 + 1 / 2 + 1 / 6) / 5)
