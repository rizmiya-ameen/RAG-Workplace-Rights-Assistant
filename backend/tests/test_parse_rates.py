"""Pay-rate extraction tests.

The fixture is a verbatim excerpt of the real MA000009 page (clause 18 and Schedule B up to
B.3). Expected amounts are quoted from Table 3 (clause 18.1) and tables B.2.1-B.2.4 there.
"""

import dataclasses
from decimal import Decimal
from pathlib import Path

import pytest
from bs4 import BeautifulSoup

from app.ingestion.parse_award import latest_raw
from app.ingestion.parse_rates import (
    MIN_RATES_CLAUSE,
    _grid,
    parse_rates_html,
    validate_rates,
)

FIXTURE = Path(__file__).parent / "fixtures" / "MA000009_excerpt.html"


@pytest.fixture(scope="module")
def hosp():
    return parse_rates_html(FIXTURE.read_bytes(), "MA000009")


def find(result, classification, employment_type, rate_type, condition=None):
    rows = [
        r
        for r in result.rates
        if (r.classification, r.employment_type, r.rate_type, r.condition)
        == (classification, employment_type, rate_type, condition)
    ]
    assert len(rows) == 1, rows
    return rows[0]


def test_fixture_passes_validation(hosp):
    assert validate_rates(hosp) == []
    assert hosp.tables_read == ["18.1", "B.2.1", "B.2.2", "B.2.3", "B.2.4"]


def test_weekly_rate_from_minimum_rates_table(hosp):
    row = find(hosp, "Level 2", "full_time", "weekly")
    assert (row.amount, row.unit, row.source_clause) == (Decimal("1029.10"), "per week", "18.1")
    assert hosp.min_hourly["Introductory level"] == Decimal("25.74")


def test_ordinary_hourly_rates_by_employment_type(hosp):
    for employment_type in ("full_time", "part_time"):
        row = find(hosp, "Level 2", employment_type, "hourly")
        assert (row.amount, row.source_clause) == (Decimal("27.08"), "B.2.1")
    casual = find(hosp, "Level 2", "casual", "hourly")
    assert (casual.amount, casual.source_clause) == (Decimal("33.85"), "B.2.3")


def test_penalty_condition_has_column_and_percentage(hosp):
    row = find(hosp, "Level 2", "casual", "penalty", "Sunday (175%)")
    assert row.amount == Decimal("47.39")
    assert find(hosp, "Level 6", "full_time", "penalty", "Public holiday (225%)").amount == Decimal(
        "72.29"
    )


def test_multi_row_overtime_header_keeps_its_column(hosp):
    # B.2.2: "Monday to Friday" spans "First 2 hours" / "After 2 hours".
    first = find(
        hosp, "Level 1", "full_time", "overtime", "Monday to Friday > First 2 hours (150%)"
    )
    after = find(
        hosp, "Level 1", "full_time", "overtime", "Monday to Friday > After 2 hours (200%)"
    )
    weekend = find(
        hosp, "Level 1", "full_time", "overtime", "Weekends and rostered days off (200%)"
    )
    assert (first.amount, after.amount, weekend.amount) == (
        Decimal("39.66"),
        Decimal("52.88"),
        Decimal("52.88"),
    )


def test_junior_and_apprentice_tables_are_not_read_yet(hosp):
    assert not any("years of age" in r.classification for r in hosp.rates)
    assert not any(r.source_clause.startswith(("18.4", "B.3")) for r in hosp.rates)


def test_every_amount_comes_from_the_page(hosp):
    text = " ".join(FIXTURE.read_text("utf-8").split())
    assert all(f"{r.amount:.2f}" in text for r in hosp.rates)


def test_grid_expands_rowspan_and_colspan():
    table = BeautifulSoup(
        "<table><tr><td rowspan=2>a</td><td colspan=2>b</td><td rowspan=2>c</td></tr>"
        "<tr><td>d</td><td>e</td></tr></table>",
        "html.parser",
    ).table
    assert _grid(table) == [["a", "b", "b", "c"], ["a", "d", "e", "c"]]


def test_validation_catches_a_shifted_column(hosp):
    broken = [
        dataclasses.replace(r, amount=Decimal("1.00"))
        if (r.classification, r.rate_type, r.employment_type) == ("Level 3", "hourly", "full_time")
        else r
        for r in hosp.rates
    ]
    problems = validate_rates(dataclasses.replace(hosp, rates=broken))
    assert any("Level 3: minimum hourly 27.97" in p for p in problems)


def test_validation_catches_weekly_hourly_mismatch(hosp):
    broken = dict(hosp.min_hourly, **{"Level 4": Decimal("1.00")})
    problems = validate_rates(dataclasses.replace(hosp, min_hourly=broken))
    assert any("Level 4: weekly 1119.10 / 38" in p for p in problems)


@pytest.mark.parametrize("award_id", sorted(MIN_RATES_CLAUSE))
def test_saved_awards_pass_rate_validation(award_id):
    """Build-step-7 gate: every saved award's adult rate tables parse and cross-check."""
    try:
        path, _ = latest_raw(award_id)
    except FileNotFoundError:
        pytest.skip(f"{award_id} not fetched")
    result = parse_rates_html(path.read_bytes(), award_id)
    assert validate_rates(result) == []
    assert len(result.rates) > 100
