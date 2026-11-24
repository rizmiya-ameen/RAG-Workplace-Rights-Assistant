"""eval/routes.jsonl integrity: the router gives each row its expected route and slots, and
every expected amount is what the pay tool's filter returns from the parsed award page (not
the database), so no amount in the eval data is typed by hand."""

import json
from datetime import date
from functools import cache

import pytest

from app.config import REPO_DIR
from app.ingestion.parse_award import latest_raw
from app.ingestion.parse_rates import parse_rates_html
from app.router import route_question
from app.tools.pay_rates import Rate, select

ROWS = [
    json.loads(line)
    for line in (REPO_DIR / "eval" / "routes.jsonl").read_text(encoding="utf-8").splitlines()
    if line.strip()
]


@cache
def parsed_rates(award: str) -> list[Rate]:
    try:
        path, _ = latest_raw(award)
    except FileNotFoundError:
        pytest.skip(f"{award} not fetched")
    return [
        Rate(
            award,
            r.classification,
            r.employment_type,
            r.rate_type,
            r.condition,
            r.amount,
            r.unit,
            r.source_clause,
            date.min,
        )
        for r in parse_rates_html(path.read_bytes(), award).rates
    ]


def test_ids_unique_and_routes_known():
    assert len({r["id"] for r in ROWS}) == len(ROWS)
    assert {r["expected_route"] for r in ROWS} == {"pay_rate", "clarify", "rag"}


@pytest.mark.parametrize("row", ROWS, ids=[r["id"] for r in ROWS])
def test_router_matches_expected_route(row):
    d = route_question(row["question"], row["award"])
    assert d.route == row["expected_route"], d.reason
    if row["expected_route"] == "pay_rate":
        slots = row["slots"]
        assert (d.award, d.employment_type, d.level) == (
            slots["award"],
            slots["employment_type"],
            slots["level"],
        )
        assert list(d.days) == slots["days"] and d.casino == slots.get("casino", False)
    if row["expected_route"] == "clarify":
        assert d.missing == row["missing"]


PAY_ROWS = [r for r in ROWS if r["expected_route"] == "pay_rate"]


@pytest.mark.parametrize("row", PAY_ROWS, ids=[r["id"] for r in PAY_ROWS])
def test_expected_amounts_come_from_the_award_page(row):
    s = row["slots"]
    rates = [r for r in parsed_rates(s["award"]) if r.employment_type == s["employment_type"]]
    found = select(rates, level=s["level"], days=tuple(s["days"]), casino=s.get("casino", False))
    amounts = {f"{r.amount:.2f}" for r in found}
    assert found and set(row["expected_amounts"]) <= amounts
