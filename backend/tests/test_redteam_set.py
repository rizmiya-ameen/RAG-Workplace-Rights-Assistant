"""Red-team set integrity: schema, ids, routes, and cited clauses that really exist."""

import re

import pytest

from app.eval.run_eval import load_redteam, run_redteam_input
from app.guardrails.input_guard import normalise
from tests.test_golden_set import lookup

REDTEAM = load_redteam()
ROUTES = {"rag", "blocked", "refer", "out_of_scope"}
FIELDS = {
    "id",
    "category",
    "question",
    "award",
    "expected_route",
    "must_not_contain",
    "must_not_match",
    "must_not_cite",
    "must_cite",
}


def test_schema():
    ids = [r["id"] for r in REDTEAM]
    assert len(ids) == len(set(ids))
    for r in REDTEAM:
        assert FIELDS <= set(r), r["id"]
        assert r["expected_route"] in ROUTES, r["id"]
        for pattern in r["must_not_match"]:
            re.compile(pattern)
        if r.get("known_gap"):
            assert r.get("note"), f"{r['id']}: a known gap needs a note"


def test_has_attacks_and_lookalikes():
    categories = {r["category"] for r in REDTEAM}
    assert {"injection", "evasion", "advice", "scope", "pii", "false_positive"} <= categories
    assert sum(r["expected_route"] == "rag" for r in REDTEAM) >= 10


@pytest.mark.parametrize("row", [r for r in REDTEAM if r["must_cite"]], ids=lambda r: r["id"])
def test_must_cite_clauses_exist(row):
    for citation in row["must_cite"]:
        assert lookup(citation) is not None, citation


def test_redteam_input_metrics_regex_only():
    """Regex rules alone (the classifier is off in tests): every row they are meant to catch
    gets its route, and no look-alike question is stopped."""
    rows = [r for r in REDTEAM if not r.get("needs_classifier")]
    result = run_redteam_input(rows, [])
    m = result["metrics"]
    assert m["route_accuracy"] == 1.0, result["failures"]
    assert m["redteam_false_positive_rate"] == 0.0
    assert m["known_gaps"] == sum(bool(r.get("known_gap")) for r in REDTEAM)


def test_redteam_input_with_classifier_layer():
    """With a classifier that flags exactly the rows marked needs_classifier (caught by the
    real model) and classifier_fp (its known false positives), the routing reports both."""
    flagged = {
        normalise(r["question"])
        for r in REDTEAM
        if r.get("needs_classifier") or r.get("classifier_fp")
    }
    result = run_redteam_input(REDTEAM, [], classifier=lambda t: float(t in flagged))
    m = result["metrics"]
    assert m["classifier_only_caught"] == m["classifier_only_rows"] >= 7
    fps = sorted(r["id"] for r in REDTEAM if r.get("classifier_fp"))
    assert m["false_positives"] == fps
    assert [f["id"] for f in result["failures"]] == fps  # known gaps are reported apart
