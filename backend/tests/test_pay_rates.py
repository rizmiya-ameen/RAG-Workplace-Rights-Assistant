"""Pay tool: level matching, day/variant filtering, descriptions (no database)."""

from datetime import date
from decimal import Decimal

import pytest

from app.tools.pay_rates import Rate, describe, level_matches, select, short_levels


def _rate(cls, rate_type, condition, amount, clause="B.2.3", type_="casual"):
    return Rate(
        "MA000009",
        cls,
        type_,
        rate_type,
        condition,
        Decimal(amount),
        "per week" if rate_type == "weekly" else "per hour",
        clause,
        date(2026, 7, 1),
    )


# Rows as stored (values from MA000009 B.2.3 / B.6.3, MA000002 B.3.1).
RATES = [
    _rate("Level 2", "hourly", None, "33.85"),
    _rate("Level 2", "penalty", "Saturday (150%)", "40.62"),
    _rate("Level 2", "penalty", "Sunday (175%)", "47.39"),
    _rate("Level 2", "penalty", "Public holiday (250%)", "67.70"),
    _rate("Level 2", "overtime", "Weekends (200%)", "54.16"),
    _rate("Level 3A", "hourly", "casino gaming", "38.54", "B.6.3"),
    _rate("Level 3A", "penalty", "casino gaming; Sunday (175%)", "53.95", "B.6.3"),
    _rate("Level 20", "hourly", None, "99.00"),  # not a real level: must not match "2"
]


@pytest.mark.parametrize(
    ("classification", "level", "ok"),
    [
        ("Level 2", "2", True),
        ("Retail Employee Level 2", "2", True),
        ("Level 2—Year 1", "2", True),
        ("Fast food employee level 3—in charge of 2 or more people", "3", True),
        ("Level 20", "2", False),
        ("Level 3A", "3", False),
        ("Level 3A", "3a", True),
        ("Introductory level", "introductory", True),
        ("Introductory level", "1", False),
    ],
)
def test_level_matches(classification, level, ok):
    assert level_matches(classification, level) is ok


def test_select_days_and_order():
    got = select(RATES, level="2", days=("ordinary", "sunday"))
    assert [str(r.amount) for r in got] == ["33.85", "47.39"]
    got = select(RATES, level="2", days=("saturday", "sunday", "public_holiday"))
    assert [str(r.amount) for r in got] == ["40.62", "47.39", "67.70"]  # never overtime


def test_casino_rows_only_when_asked():
    assert select(RATES, level="3a") == []
    got = select(RATES, level="3a", days=("ordinary", "sunday"), casino=True)
    assert [str(r.amount) for r in got] == ["38.54", "53.95"]
    assert select(RATES, level="2", casino=True) == []


def test_shiftworker_rows_are_left_out():
    shift = _rate("Level 1—Year 1", "penalty", "shiftworkers; Permanent night (155%)", "1")
    other = _rate("Level 1—Year 1", "penalty", "other than shiftworkers; Sunday (225%)", "60.68")
    assert select([shift, other], level="1", days=("sunday",)) == [other]


def test_describe():
    assert describe(RATES[2]) == (
        "Level 2, casual, Sunday (175%): $47.39 per hour (clause B.2.3, from 2026-07-01)"
    )
    weekly = _rate("Level 2", "weekly", None, "1029.10", "18.1", "full_time")
    assert describe(weekly) == (
        "Level 2, full-time, weekly: $1,029.10 per week (clause 18.1, from 2026-07-01)"
    )
    assert "casino gaming, ordinary hours: $38.54" in describe(RATES[5])


def test_short_levels():
    assert short_levels(["Introductory level", "Level 1", "Level 3A"]) == [
        "Introductory",
        "Level 1",
        "Level 3A",
    ]
    assert short_levels(["Level 1—Year 1", "Level 1—Year 2", "Retail Employee Level 2"]) == [
        "Level 1",
        "Level 2",
    ]
