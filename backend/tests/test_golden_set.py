"""Golden-set integrity and eval-metric tests.

Every evidence quote must appear verbatim (whitespace-normalised) in the clause it cites, in
the parsed saved source (award, NES or WorkSafe page), and every expected clause must exist.
This keeps invented clauses or quotes out of the eval data.
"""

from functools import cache

import pytest

from app.eval.run_eval import first_hit_rank, load_golden, retrieval_metrics
from app.ingestion.parse_award import latest_raw, parse_award_html
from app.ingestion.parse_nes import parse_nes_html
from app.ingestion.parse_worksafe import parse_worksafe_html

GOLDEN = load_golden()
ANSWERABLE = [g for g in GOLDEN if g["expected"]]


@cache
def clauses(source_id: str) -> dict[str, str]:
    """base clause ref -> whitespace-normalised text of all its chunks."""
    path, _ = latest_raw(source_id)
    if source_id == "NES":
        parse = parse_nes_html
    elif source_id.startswith("WSV:"):
        parse = parse_worksafe_html
    else:
        parse = parse_award_html
    out: dict[str, list[str]] = {}
    for c in parse(path.read_bytes()).chunks:
        out.setdefault(c.clause_ref.split("#")[0], []).append(c.content)
    return {ref: " ".join(" ".join(parts).split()) for ref, parts in out.items()}


def split_citation(citation: str) -> tuple[str, str]:
    """ "MA000009:28.2" -> ("MA000009", "28.2"); "NES:s62" -> ("NES", "s62");
    "WSV:<page>:<section>" -> ("WSV:<page>", "<section>")."""
    if citation.startswith("WSV:"):
        source, ref = citation.rsplit(":", 1)
        return source, ref
    source, ref = citation.split(":", 1)
    return source, ref


def lookup(citation: str) -> str | None:
    award_id, ref = split_citation(citation)
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
            if g["award"]:  # the award filter keeps that award plus the NES and WorkSafe
                ok = (g["award"] + ":", "NES:", "WSV:")
                assert all(e.startswith(ok) for e in g["expected"]), g["id"]
        else:
            assert g["decline_reason"], g["id"]


def test_set_size_matches_plan():
    assert len(ANSWERABLE) >= 50
    assert len(GOLDEN) - len(ANSWERABLE) >= 9  # decline-05 became answerable in step 15
    sources = {split_citation(c)[0].split(":")[0] for g in ANSWERABLE for c in g["expected"]}
    assert {"NES", "WSV"} <= sources


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
